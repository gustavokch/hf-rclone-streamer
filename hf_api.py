"""
Hugging Face Hub API client wrapper.

Provides functions for searching models, getting model info, and downloading files.
"""

import os
import fnmatch
import shutil
import subprocess
import re
from pathlib import Path
from typing import List, Dict, Any, Optional, Callable, Iterator
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
import time
import urllib.parse


try:
    from .cancel import CancelToken, NO_CANCEL, terminate
except ImportError:
    from cancel import CancelToken, NO_CANCEL, terminate


try:
    from huggingface_hub import (
        hf_hub_download,
        list_repo_files,
        list_repo_tree,
        get_hf_file_metadata,
        list_models,
        HfApi,
    )
    # Check if RepoFile is available (newer API)
    try:
        from huggingface_hub.hf_api import RepoFile
        HAS_REPOFILE_CLASS = True
    except ImportError:
        HAS_REPOFILE_CLASS = False
    HF_AVAILABLE = True
except ImportError:
    HF_AVAILABLE = False
    HAS_REPOFILE_CLASS = False


@dataclass
class FileInfo:
    """Information about a file in a model repository."""
    path: str
    size: int
    commit_hash: Optional[str] = None
    blob_id: Optional[str] = None


@dataclass
class ModelInfo:
    """Information about a model."""
    model_id: str
    author: Optional[str] = None
    tags: List[str] = None
    files: List[FileInfo] = None
    total_size: int = 0
    card_data: Dict[str, Any] = None

    def __post_init__(self):
        if self.tags is None:
            self.tags = []
        if self.files is None:
            self.files = []
        if self.card_data is None:
            self.card_data = {}


class HFApiError(Exception):
    """Base exception for HF API errors."""
    pass


class ModelNotFoundError(HFApiError):
    """Exception raised when a model is not found."""
    pass


class FileDownloadError(HFApiError):
    """Exception raised when a file download fails."""
    pass


def check_aria2c_available() -> bool:
    """Check if aria2c is available in PATH.

    Returns:
        True if aria2c is available, False otherwise.
    """
    return shutil.which("aria2c") is not None


def check_hf_available() -> None:
    """Check if huggingface_hub library is available.

    Raises:
        ImportError: If huggingface_hub is not installed.
    """
    if not HF_AVAILABLE:
        raise ImportError(
            "huggingface_hub library is required. "
            "Install it with: pip install huggingface-hub"
        )


def get_hf_download_url(
    model_id: str,
    filename: str,
    revision: Optional[str] = None,
    token: Optional[str] = None,
) -> str:
    """Get the direct download URL for a Hugging Face file.

    Args:
        model_id: Model ID.
        filename: Path to the file.
        revision: Optional git revision.
        token: Optional Hugging Face API token.

    Returns:
        Direct download URL.
    """
    check_hf_available()

    # Use huggingface_hub to get the URL
    from huggingface_hub import hf_hub_url

    url = hf_hub_url(
        repo_id=model_id,
        filename=filename,
        revision=revision,
    )

    # Add token if provided (for private models)
    if token:
        # URL encode the token
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        query["token"] = [token]
        new_query = urllib.parse.urlencode(query, doseq=True)
        url = parsed._replace(query=new_query).geturl()

    return url


class Aria2cDownloader:
    """Downloader using aria2c for multi-connection downloads."""

    def __init__(
        self,
        connections: int = 16,
        max_retries: int = 3,
        retry_delay: int = 5,
    ):
        """Initialize the aria2c downloader.

        Args:
            connections: Number of connections per file (default: 16).
            max_retries: Maximum retry attempts.
            retry_delay: Seconds between retries.
        """
        if not check_aria2c_available():
            raise RuntimeError(
                "aria2c not found. Install it with: "
                "brew install aria2 (macOS) or apt install aria2 (Linux)"
            )

        self.connections = connections
        self.max_retries = max_retries
        self.retry_delay = retry_delay

    def download(
        self,
        url: str,
        output_path: Path,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        headers: Optional[Dict[str, str]] = None,
        cancel=NO_CANCEL,
    ) -> Path:
        """Download a file using aria2c.

        Args:
            url: URL to download.
            output_path: Path to save the file.
            progress_callback: Optional callback(downloaded_bytes, total_bytes).
            headers: Optional HTTP headers.
            cancel: Optional CancelToken to terminate aria2c promptly on Ctrl-C.

        Returns:
            Path to downloaded file.

        Raises:
            FileDownloadError: If download fails.
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        last_error = None

        for attempt in range(self.max_retries):
            try:
                # Build aria2c command
                cmd = [
                    "aria2c",
                    "--continue=true",  # Resume capability
                    "--max-tries=5",
                    "--retry-wait=3",
                    f"--split={self.connections}",
                    f"--min-split-size=1M",
                    "--max-file-not-found=5",
                    "--timeout=60",
                    "--connect-timeout=30",
                    f"--max-connection-per-server={self.connections}",
                    "--allow-overwrite=true",
                    "--auto-file-renaming=false",
                    "-d", str(output_path.parent),
                    "-o", output_path.name,
                    url,
                ]

                # Add headers if provided
                if headers:
                    for key, value in headers.items():
                        cmd.extend(["--header", f"{key}: {value}"])

                # If progress callback is provided, we'll monitor progress
                if progress_callback:
                    return self._download_with_progress(
                        cmd, url, output_path, progress_callback, cancel
                    )
                else:
                    # Simple download without progress monitoring
                    result = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    return output_path

            except subprocess.CalledProcessError as e:
                last_error = e
                if cancel.is_set():
                    raise FileDownloadError(f"download cancelled: {url}")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
                    continue

                raise FileDownloadError(
                    f"aria2c download failed: {e.stderr if e.stderr else str(e)}"
                )
            except Exception as e:
                last_error = e
                if cancel.is_set():
                    raise FileDownloadError(f"download cancelled: {url}")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
                    continue

                raise FileDownloadError(f"Download failed: {e}")

        raise FileDownloadError(
            f"Failed to download after {self.max_retries} attempts: {last_error}"
        )

    def _download_with_progress(
        self,
        cmd: List[str],
        url: str,
        output_path: Path,
        progress_callback: Callable[[int, int], None],
        cancel=NO_CANCEL,
    ) -> Path:
        """Download with progress monitoring.

        Args:
            cmd: aria2c command list.
            url: Download URL.
            output_path: Output file path.
            progress_callback: Progress callback.
            cancel: Optional CancelToken to terminate aria2c on Ctrl-C.

        Returns:
            Path to downloaded file.
        """
        # Add progress parameters
        cmd.extend(["--summary-interval=1", "--show-console-readout=true"])

        # Start aria2c in its own session so cancellation is deterministic:
        # terminal Ctrl-C won't reach it (we terminate it via the CancelToken).
        process = cancel.register(subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        ))

        # Parse progress from output
        size_pattern = re.compile(r"([\d.]+)([KMGT]?iB)")

        total_size = 0
        last_downloaded = 0

        try:
            for line in process.stdout:
                if cancel.is_set():
                    break
                # aria2c outputs progress like:
                # [# SIZE  MiB/s  ETA]
                # Download: 1.0GiB/10.0GiB (10%)
                # Or uses a progress bar

                # Try to extract progress information
                if "/" in line:
                    # aria2c emits lines like:
                    #   [#e859b4 1.2GiB/5.0GiB(24%) CN:16 DL:52MiB ETA:1m15s]
                    # The unit may be "B" (at 0%) or "KiB"/"MiB"/"GiB"/"TiB".
                    match = re.search(r"([\d.]+[KMGT]?i?B)/([\d.]+[KMGT]?i?B)", line)
                    if match:
                        downloaded_str, total_str = match.groups()
                        downloaded = self._parse_size(downloaded_str)
                        total = self._parse_size(total_str)

                        if total > 0:
                            total_size = total
                            progress_callback(downloaded, total_size)

            return_code = process.wait()
        finally:
            cancel.unregister(process)
            # On unwind with aria2c still alive (e.g. Ctrl-C interrupted the
            # read mid-transfer), kill it so it can't outlive this call.
            if process.poll() is None:
                terminate(process)

        if return_code != 0:
            raise subprocess.CalledProcessError(return_code, cmd)

        return output_path

    def _parse_size(self, size_str: str) -> int:
        """Parse aria2c size string to bytes.

        Args:
            size_str: Size string like "1.5GiB".

        Returns:
            Size in bytes.
        """
        size_str = size_str.strip().upper()

        # Handle the "iB" suffix (binary)
        match = re.match(r"([\d.]+)([KMGT])?I?B?", size_str)
        if not match:
            return 0

        value = float(match.group(1))
        unit = match.group(2) or ""

        unit_map = {
            "K": 1024,
            "M": 1024**2,
            "G": 1024**3,
            "T": 1024**4,
        }

        return int(value * unit_map.get(unit, 1))


def search_models(
    query: str,
    author: Optional[str] = None,
    tags: Optional[List[str]] = None,
    limit: int = 50,
    token: Optional[str] = None,
) -> List[str]:
    """Search for models on Hugging Face Hub.

    Args:
        query: Search query string.
        author: Filter by model author/organization.
        tags: Filter by tags (e.g., ['text-generation', 'pytorch']).
        limit: Maximum number of results to return.
        token: Optional Hugging Face API token.

    Returns:
        List of matching model IDs.
    """
    check_hf_available()

    api = HfApi(token=token)

    try:
        # New API uses keyword arguments directly
        models = api.list_models(
            search=query,
            author=author,
            tags=tags,
            limit=limit,
        )
        return [model.id for model in models]
    except Exception as e:
        raise HFApiError(f"Failed to search models: {e}")


def get_model_info(
    model_id: str,
    revision: Optional[str] = None,
    token: Optional[str] = None,
    include_patterns: Optional[List[str]] = None,
    exclude_patterns: Optional[List[str]] = None,
) -> ModelInfo:
    """Get detailed information about a model.

    Args:
        model_id: Model ID (e.g., 'meta-llama/Llama-3.1-8B').
        revision: Optional git revision (branch, tag, or commit hash).
        token: Optional Hugging Face API token.
        include_patterns: Optional glob patterns for files to include.
        exclude_patterns: Optional glob patterns for files to exclude.

    Returns:
        ModelInfo object with model metadata and file list.
    """
    check_hf_available()

    api = HfApi(token=token)

    try:
        # Get model metadata
        model_data = api.model_info(model_id, revision=revision)

        # Get file list
        repo_tree = list_repo_tree(
            repo_id=model_id,
            revision=revision,
            token=token,
            repo_type="model",
        )

        files = []
        total_size = 0

        for item in repo_tree:
            # Check if item is a file (new API uses RepoFile class, old API uses type attribute)
            is_file = HAS_REPOFILE_CLASS and isinstance(item, RepoFile)
            if not is_file and hasattr(item, 'type'):
                is_file = item.type == "file"

            if not is_file:
                continue

            file_path = item.path

            # Apply inclusion filters
            if include_patterns and not any(
                fnmatch.fnmatch(file_path, pattern)
                for pattern in include_patterns
            ):
                continue

            # Apply exclusion filters
            if exclude_patterns and any(
                fnmatch.fnmatch(file_path, pattern)
                for pattern in exclude_patterns
            ):
                continue

            # Get file size (RepoFile has size attribute)
            if hasattr(item, 'size'):
                file_size = item.size
            else:
                # Fallback to getting metadata
                try:
                    from huggingface_hub import hf_hub_url
                    url = hf_hub_url(repo_id=model_id, filename=file_path, revision=revision)
                    metadata = get_hf_file_metadata(url, token=token)
                    file_size = metadata.size
                except Exception:
                    file_size = 0

            file_info = FileInfo(
                path=file_path,
                size=file_size,
                blob_id=item.blob_id,
            )
            files.append(file_info)
            total_size += file_size

        # Extract author from model ID or model data
        author = model_data.author
        if not author and "/" in model_id:
            author = model_id.split("/")[0]

        # Get tags
        tags = list(model_data.tags or [])

        return ModelInfo(
            model_id=model_id,
            author=author,
            tags=tags,
            files=files,
            total_size=total_size,
            card_data=model_data.card_data if hasattr(model_data, 'card_data') else {},
        )

    except Exception as e:
        # Check if it's a "not found" error
        error_str = str(e).lower()
        if 'not found' in error_str or 'does not exist' in error_str or '404' in error_str:
            raise ModelNotFoundError(f"Model not found: {model_id}") from e
        else:
            raise HFApiError(f"Failed to get model info: {e}") from e


def get_file_size(
    model_id: str,
    filename: str,
    revision: Optional[str] = None,
    token: Optional[str] = None,
) -> int:
    """Get the size of a file in a model repository.

    Args:
        model_id: Model ID.
        filename: Path to the file in the repository.
        revision: Optional git revision.
        token: Optional Hugging Face API token.

    Returns:
        File size in bytes.
    """
    check_hf_available()

    try:
        metadata = get_hf_file_metadata(
            repo_id=model_id,
            filename=filename,
            revision=revision,
            token=token,
        )
        return metadata.size
    except Exception as e:
        raise HFApiError(f"Failed to get file size: {e}")


def download_file(
    model_id: str,
    filename: str,
    local_path: Path,
    revision: Optional[str] = None,
    token: Optional[str] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    max_retries: int = 3,
    retry_delay: int = 5,
    use_aria2c: bool = True,
    connections: int = 16,
    cancel=NO_CANCEL,
) -> Path:
    """Download a file from Hugging Face Hub.

    Args:
        model_id: Model ID.
        filename: Path to the file in the repository.
        local_path: Local path to save the file.
        revision: Optional git revision.
        token: Optional Hugging Face API token.
        progress_callback: Optional callback(downloaded_bytes, total_bytes).
        max_retries: Maximum number of retry attempts.
        retry_delay: Seconds between retries.
        use_aria2c: Use aria2c for download (default: True).
        connections: Number of connections for aria2c (default: 16).
        cancel: Optional CancelToken to terminate the download on Ctrl-C.

    Returns:
        Path to the downloaded file.

    Raises:
        FileDownloadError: If download fails after all retries.
    """
    check_hf_available()

    # Try aria2c first (default) for better performance on large files
    if use_aria2c and check_aria2c_available():
        try:
            url = get_hf_download_url(model_id, filename, revision, token)
            downloader = Aria2cDownloader(
                connections=connections,
                max_retries=max_retries,
                retry_delay=retry_delay,
            )
            return downloader.download(url, local_path, progress_callback, cancel=cancel)
        except Exception as e:
            print(f"aria2c download failed, falling back to huggingface_hub: {e}")
            # Fall through to huggingface_hub download

    # Fallback to huggingface_hub
    last_error = None

    for attempt in range(max_retries):
        try:
            # Ensure parent directory exists
            local_path.parent.mkdir(parents=True, exist_ok=True)

            # Download with progress
            hf_hub_download(
                repo_id=model_id,
                filename=filename,
                local_dir=local_path.parent,
                local_dir_use_symlinks=False,
                revision=revision,
                token=token,
                resume_download=True,
            )

            # The downloaded file might have a different name if using local_dir
            # Find the actual downloaded file
            downloaded_file = local_path
            if not downloaded_file.exists():
                # hf_hub_download might have created it with the original filename
                downloaded_file = local_path.parent / filename.split("/")[-1]

            if not downloaded_file.exists():
                raise FileDownloadError(
                    f"Download completed but file not found at {downloaded_file}"
                )

            return downloaded_file

        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                continue

    raise FileDownloadError(
        f"Failed to download {filename} after {max_retries} attempts: {last_error}"
    )


def download_file_stream(
    model_id: str,
    filename: str,
    local_path: Path,
    revision: Optional[str] = None,
    token: Optional[str] = None,
    chunk_size: int = 8192,
    max_retries: int = 3,
    retry_delay: int = 5,
) -> Iterator[bytes]:
    """Stream a file download from Hugging Face Hub.

    Args:
        model_id: Model ID.
        filename: Path to the file in the repository.
        local_path: Local path to save the file.
        revision: Optional git revision.
        token: Optional Hugging Face API token.
        chunk_size: Size of chunks to yield.
        max_retries: Maximum number of retry attempts.
        retry_delay: Seconds between retries.

    Yields:
        Chunks of file data.

    Raises:
        FileDownloadError: If download fails.
    """
    check_hf_available()

    for attempt in range(max_retries):
        try:
            # Use hf_hub_download with a custom progress handler
            # For true streaming, we'd need to use the URL directly
            # For now, we'll download and yield chunks
            downloaded_path = download_file(
                model_id=model_id,
                filename=filename,
                local_path=local_path,
                revision=revision,
                token=token,
                max_retries=1,
            )

            with open(downloaded_path, "rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk

            return  # Success

        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                continue
            raise FileDownloadError(f"Stream download failed: {e}")


def list_model_files(
    model_id: str,
    revision: Optional[str] = None,
    token: Optional[str] = None,
) -> List[str]:
    """List all files in a model repository.

    Args:
        model_id: Model ID.
        revision: Optional git revision.
        token: Optional Hugging Face API token.

    Returns:
        List of file paths.
    """
    check_hf_available()

    try:
        return list_repo_files(
            repo_id=model_id,
            revision=revision,
            token=token,
            repo_type="model",
        )
    except Exception as e:
        raise ModelNotFoundError(f"Model not found: {model_id}") from e
    except Exception as e:
        raise HFApiError(f"Failed to list files: {e}")


def get_file_info(
    model_id: str,
    filename: str,
    revision: Optional[str] = None,
    token: Optional[str] = None,
) -> FileInfo:
    """Get information about a specific file.

    Args:
        model_id: Model ID.
        filename: Path to the file.
        revision: Optional git revision.
        token: Optional Hugging Face API token.

    Returns:
        FileInfo object.
    """
    check_hf_available()

    try:
        metadata = get_hf_file_metadata(
            repo_id=model_id,
            filename=filename,
            revision=revision,
            token=token,
        )

        return FileInfo(
            path=filename,
            size=metadata.size,
            commit_hash=metadata.commit_hash,
            blob_id=metadata.blob_id,
        )
    except Exception as e:
        raise ModelNotFoundError(f"Model not found: {model_id}") from e
    except Exception as e:
        raise HFApiError(f"Failed to get file info: {e}")


def format_size(size_bytes: int) -> str:
    """Format byte size as human-readable string.

    Args:
        size_bytes: Size in bytes.

    Returns:
        Formatted string (e.g., "1.5 GB").
    """
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"


def print_model_summary(model_info: ModelInfo) -> None:
    """Print a summary of model information.

    Args:
        model_info: ModelInfo object.
    """
    print(f"\n{'='*60}")
    print(f"Model: {model_info.model_id}")
    print(f"Author: {model_info.author or 'Unknown'}")
    print(f"Total Size: {format_size(model_info.total_size)}")
    print(f"Files: {len(model_info.files)}")
    print(f"\nFiles:")
    print(f"{'Size':>12}  {'Path'}")
    print("-" * 60)
    for file in sorted(model_info.files, key=lambda f: f.size, reverse=True):
        print(f"{format_size(file.size):>12}  {file.path}")
    print(f"{'='*60}\n")
