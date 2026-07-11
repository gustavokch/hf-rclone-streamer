"""
Transfer manager for orchestrating HF downloads to GDrive.

Handles the download → cache → upload pipeline with progress tracking
and state checkpointing for resume capability.
"""

import os
import json
import hashlib
from pathlib import Path
from typing import Optional, List, Dict, Any, Callable
from dataclasses import dataclass, asdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

try:
    from .config import Config, get_config
    from .hf_api import (
        ModelInfo, FileInfo,
        get_model_info,
        download_file,
        format_size as hf_format_size,
    )
    from .rclone_client import (
        copy_file,
        get_free_space,
        format_size as rclone_format_size,
    )
except ImportError:
    # Absolute imports for running directly
    from config import Config, get_config
    from hf_api import (
        ModelInfo, FileInfo,
        get_model_info,
        download_file,
        format_size as hf_format_size,
    )
    from rclone_client import (
        copy_file,
        get_free_space,
        format_size as rclone_format_size,
    )


@dataclass
class TransferState:
    """State of a file transfer."""
    model_id: str
    filename: str
    status: str  # 'pending', 'downloading', 'cached', 'uploading', 'complete', 'failed'
    downloaded_bytes: int = 0
    total_bytes: int = 0
    uploaded_bytes: int = 0
    cache_path: Optional[str] = None
    error: Optional[str] = None
    retries: int = 0
    last_update: float = 0


@dataclass
class TransferProgress:
    """Overall transfer progress."""
    total_files: int
    completed_files: int
    total_bytes: int
    downloaded_bytes: int
    uploaded_bytes: int
    failed_files: int


class TransferManager:
    """Manager for orchestrating transfers from HF to GDrive."""

    def __init__(
        self,
        config: Optional[Config] = None,
        progress_callback: Optional[Callable[[TransferProgress], None]] = None,
    ):
        """Initialize the transfer manager.

        Args:
            config: Optional configuration. Uses global config if None.
            progress_callback: Optional callback for progress updates.
        """
        self.config = config or get_config()
        self.progress_callback = progress_callback

        # State tracking
        self.states: Dict[str, TransferState] = {}
        self.state_file = self.config.config_dir / "transfer_state.json"

        # Ensure cache directory exists
        self.cache_dir = self.config.ensure_cache_dir()

        # Load previous state if resume is enabled
        if self.config.resume:
            self._load_state()

    def _get_state_key(self, model_id: str, filename: str) -> str:
        """Get a unique key for a transfer state.

        Args:
            model_id: Model identifier.
            filename: File name.

        Returns:
            Unique state key.
        """
        return f"{model_id}:{filename}"

    def _load_state(self) -> None:
        """Load transfer state from disk."""
        if not self.state_file.exists():
            return

        try:
            with open(self.state_file, "r") as f:
                data = json.load(f)

            for key, state_data in data.items():
                self.states[key] = TransferState(**state_data)

        except (json.JSONDecodeError, IOError):
            pass  # Start fresh on error

    def _save_state(self) -> None:
        """Save transfer state to disk."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        data = {
            key: asdict(state)
            for key, state in self.states.items()
        }

        with open(self.state_file, "w") as f:
            json.dump(data, f, indent=2)

    def _update_state(
        self,
        key: str,
        status: Optional[str] = None,
        downloaded_bytes: Optional[int] = None,
        uploaded_bytes: Optional[int] = None,
        cache_path: Optional[str] = None,
        error: Optional[str] = None,
        retries: Optional[int] = None,
    ) -> None:
        """Update a transfer state.

        Args:
            key: State key.
            status: New status.
            downloaded_bytes: Downloaded bytes.
            uploaded_bytes: Uploaded bytes.
            cache_path: Cache file path.
            error: Error message.
            retries: Retry count.
        """
        if key not in self.states:
            return

        state = self.states[key]

        if status is not None:
            state.status = status
        if downloaded_bytes is not None:
            state.downloaded_bytes = downloaded_bytes
        if uploaded_bytes is not None:
            state.uploaded_bytes = uploaded_bytes
        if cache_path is not None:
            state.cache_path = cache_path
        if error is not None:
            state.error = error
        if retries is not None:
            state.retries = retries

        state.last_update = time.time()

        self._save_state()
        self._notify_progress()

    def _notify_progress(self) -> None:
        """Notify progress callback if set."""
        if not self.progress_callback:
            return

        progress = self._calculate_progress()
        self.progress_callback(progress)

    def _calculate_progress(self) -> TransferProgress:
        """Calculate overall transfer progress.

        Returns:
            TransferProgress object.
        """
        total_files = len(self.states)
        completed_files = sum(
            1 for s in self.states.values()
            if s.status == "complete"
        )
        failed_files = sum(
            1 for s in self.states.values()
            if s.status == "failed"
        )
        total_bytes = sum(s.total_bytes for s in self.states.values())
        downloaded_bytes = sum(s.downloaded_bytes for s in self.states.values())
        uploaded_bytes = sum(s.uploaded_bytes for s in self.states.values())

        return TransferProgress(
            total_files=total_files,
            completed_files=completed_files,
            total_bytes=total_bytes,
            downloaded_bytes=downloaded_bytes,
            uploaded_bytes=uploaded_bytes,
            failed_files=failed_files,
        )

    def transfer_model(
        self,
        model_id: str,
        dest_dir: Optional[str] = None,
        include_patterns: Optional[List[str]] = None,
        exclude_patterns: Optional[List[str]] = None,
    ) -> bool:
        """Transfer an entire model from HF to GDrive.

        Args:
            model_id: Model ID (e.g., 'meta-llama/Llama-3.1-8B').
            dest_dir: Destination directory on GDrive.
            include_patterns: File patterns to include.
            exclude_patterns: File patterns to exclude.

        Returns:
            True if all files transferred successfully, False otherwise.
        """
        dest_dir = dest_dir or self.config.dest_dir
        include_patterns = include_patterns or [self.config.include_pattern]
        exclude_patterns = exclude_patterns or self.config.exclude_patterns

        print(f"Fetching model info for {model_id}...")

        try:
            model_info = get_model_info(
                model_id=model_id,
                token=self.config.hf_token,
                include_patterns=include_patterns,
                exclude_patterns=exclude_patterns,
            )
        except Exception as e:
            print(f"Failed to get model info: {e}")
            return False

        if not model_info.files:
            print("No files to transfer.")
            return True

        print(f"Found {len(model_info.files)} files ({hf_format_size(model_info.total_size)})")

        # Check available space
        try:
            free_space = get_free_space(self.config.rclone_path)
            if free_space < model_info.total_size:
                print(f"Warning: Not enough space on GDrive")
                print(f"Available: {rclone_format_size(free_space)}")
                print(f"Required: {hf_format_size(model_info.total_size)}")
                if not input("Continue anyway? (y/N): ").lower() == "y":
                    return False
        except Exception as e:
            print(f"Could not check available space: {e}")

        # Initialize states for all files
        for file in model_info.files:
            key = self._get_state_key(model_id, file.path)
            if key not in self.states:
                self.states[key] = TransferState(
                    model_id=model_id,
                    filename=file.path,
                    status="pending",
                    total_bytes=file.size,
                )
            else:
                # Update total bytes in case it changed
                self.states[key].total_bytes = file.size

        self._save_state()

        # Transfer each file
        success = True
        for file in model_info.files:
            key = self._get_state_key(model_id, file.path)
            state = self.states[key]

            # Skip if already complete
            if state.status == "complete" and self.config.resume:
                print(f"Skipping {file.path} (already transferred)")
                continue

            if not self._transfer_file(
                model_id=model_id,
                file_info=file,
                dest_dir=dest_dir,
            ):
                success = False
                if not self._retry_or_fail(key):
                    print(f"Failed to transfer {file.path} after retries")
                    continue

        # Cleanup if requested
        if self.config.cleanup:
            self._cleanup_cache(model_id)

        return success

    def _transfer_file(
        self,
        model_id: str,
        file_info: FileInfo,
        dest_dir: str,
    ) -> bool:
        """Transfer a single file.

        Args:
            model_id: Model ID.
            file_info: File information.
            dest_dir: Destination directory.

        Returns:
            True if successful, False otherwise.
        """
        key = self._get_state_key(model_id, file_info.path)
        state = self.states[key]

        # Generate cache path
        safe_filename = file_info.path.replace("/", "_")
        cache_path = self.cache_dir / f"{model_id.replace('/', '_')}_{safe_filename}"

        try:
            # Download to cache
            if state.status != "cached" or not Path(cache_path).exists():
                print(f"Downloading {file_info.path}...")
                state.status = "downloading"
                self._update_state(key)

                downloaded = download_file(
                    model_id=model_id,
                    filename=file_info.path,
                    local_path=Path(cache_path),
                    token=self.config.hf_token,
                    max_retries=self.config.max_retries,
                    retry_delay=self.config.retry_delay,
                    use_aria2c=self.config.use_aria2c,
                    connections=self.config.aria2c_connections,
                )

                state.status = "cached"
                state.cache_path = str(cache_path)
                state.downloaded_bytes = file_info.size
                self._update_state(key)

            # Upload to GDrive
            print(f"Uploading {file_info.path}...")
            state.status = "uploading"
            self._update_state(key)

            copy_file(
                src=Path(cache_path),
                rclone_path=self.config.rclone_path,
                dest_dir=dest_dir,
                progress_callback=lambda copied, total: self._update_state(
                    key, uploaded_bytes=copied
                ),
                max_retries=self.config.max_retries,
                retry_delay=self.config.retry_delay,
            )

            state.status = "complete"
            state.uploaded_bytes = file_info.size
            self._update_state(key)

            print(f"Complete: {file_info.path}")
            return True

        except Exception as e:
            print(f"Error transferring {file_info.path}: {e}")
            state.status = "failed"
            state.error = str(e)
            state.retries += 1
            self._update_state(key)
            return False

    def _retry_or_fail(self, key: str) -> bool:
        """Retry a failed transfer or mark as permanently failed.

        Args:
            key: State key.

        Returns:
            True if retrying, False if failed permanently.
        """
        state = self.states[key]

        if state.retries >= self.config.max_retries:
            state.status = "failed"
            self._update_state(key)
            return False

        print(f"Retrying {state.filename} (attempt {state.retries + 1})...")
        state.status = "pending"
        state.error = None
        self._update_state(key)

        return True

    def _cleanup_cache(self, model_id: str) -> None:
        """Clean up cached files for a model.

        Args:
            model_id: Model ID.
        """
        prefix = model_id.replace("/", "_")

        for file in self.cache_dir.glob(f"{prefix}_*"):
            try:
                file.unlink()
                print(f"Cleaned up: {file.name}")
            except IOError as e:
                print(f"Failed to cleanup {file.name}: {e}")

        # Clean up state entries for this model
        to_remove = [
            key for key, state in self.states.items()
            if state.model_id == model_id and state.status == "complete"
        ]
        for key in to_remove:
            del self.states[key]

        self._save_state()

    def get_progress(self) -> TransferProgress:
        """Get current transfer progress.

        Returns:
            TransferProgress object.
        """
        return self._calculate_progress()

    def get_failed_transfers(self) -> List[TransferState]:
        """Get list of failed transfers.

        Returns:
            List of failed TransferState objects.
        """
        return [
            state for state in self.states.values()
            if state.status == "failed"
        ]

    def clear_state(self) -> None:
        """Clear all transfer state."""
        self.states.clear()
        if self.state_file.exists():
            self.state_file.unlink()

    def print_status(self) -> None:
        """Print current transfer status."""
        progress = self.get_progress()

        print("\n" + "="*60)
        print("Transfer Status")
        print("="*60)
        print(f"Total Files: {progress.total_files}")
        print(f"Completed: {progress.completed_files}")
        print(f"Failed: {progress.failed_files}")
        print(f"\nProgress:")
        print(f"Downloaded: {hf_format_size(progress.downloaded_bytes)} / {hf_format_size(progress.total_bytes)}")
        print(f"Uploaded: {rclone_format_size(progress.uploaded_bytes)} / {hf_format_size(progress.total_bytes)}")

        if progress.failed_files > 0:
            print("\nFailed Files:")
            for state in self.get_failed_transfers():
                print(f"  - {state.filename}: {state.error}")

        print("="*60 + "\n")
