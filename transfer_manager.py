"""
Transfer manager for orchestrating HF downloads to GDrive.

Handles the download → cache → upload pipeline with progress tracking
and state checkpointing for resume capability.
"""

import os
import sys
import json
import hashlib
import threading
from pathlib import Path
from typing import Optional, List, Dict, Any, Callable
from dataclasses import dataclass, asdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

try:
    from .config import Config, get_config
    from .cancel import CancelToken, NO_CANCEL
    from .rate_estimator import RateEstimator, format_rate, format_eta
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
    from cancel import CancelToken, NO_CANCEL
    from rate_estimator import RateEstimator, format_rate, format_eta
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
    # Derived rates/ETA (None until enough samples arrive). Optional with
    # defaults so existing positional construction still works.
    download_rate: Optional[float] = None
    upload_rate: Optional[float] = None
    eta_seconds: Optional[float] = None


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

        # Guards self.states and the on-disk state file: the pipelined driver
        # updates one shard's state from the upload worker thread while the
        # main thread updates another's, and both write the shared JSON file.
        self._state_lock = threading.RLock()

        # Serializes stdout between the in-place progress line (drawn from both
        # the download/main and upload/worker threads via _notify_progress) and
        # status messages, so a status can't glue onto a parked progress line and
        # the two threads can't interleave a partial line. Lock order is always
        # _state_lock -> _output_lock (only _notify_progress nests both), so no
        # deadlock.
        self._output_lock = threading.Lock()
        self._progress_line_open = False

        # Cancellation token for the current transfer. A fresh one is created
        # at the start of each transfer_model() call; setting it terminates
        # any in-flight aria2c/rclone subprocess so Ctrl-C exits promptly.
        self._cancel = CancelToken()

        # Per-stream rate estimators, fed from _notify_progress with the
        # cumulative downloaded/uploaded bytes. Read by _populate_rates for the
        # live speed and ETA display.
        self._download_estimator = RateEstimator()
        self._upload_estimator = RateEstimator()

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
        with self._state_lock:
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
        with self._state_lock:
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
        """Sample the rate estimators and notify the progress callback.

        Sampling is unconditional (even with no callback) so ``get_progress`` /
        ``print_status`` can report live rates during a callback-less transfer —
        the estimators must see the bytes. The rate/ETA populate and the
        callback itself are conditional on a callback being set, since the
        populated ``progress`` is otherwise a discarded local (and
        ``get_progress`` re-runs ``_populate_rates`` itself). Runs under
        ``_state_lock`` (held by ``_update_state``); the estimators add their own
        locks in a one-way order, so no deadlock.
        """
        progress = self._calculate_progress()
        now = time.time()
        self._download_estimator.sample(now, progress.downloaded_bytes)
        self._upload_estimator.sample(now, progress.uploaded_bytes)
        if self.progress_callback:
            self._populate_rates(progress)
            with self._output_lock:
                self.progress_callback(progress)
                self._progress_line_open = True

    def _print_status(self, msg: str) -> None:
        """Print a status line on its own row, closing any open progress line.

        The progress callback leaves its line without a trailing newline, so a
        status message would otherwise glue onto the parked ETA. Serialized with
        the progress draw under ``_output_lock`` so the download (main) and upload
        (worker) threads can't interleave a partial line.
        """
        with self._output_lock:
            if self._progress_line_open:
                # Close the parked progress line: on a TTY clear the row and
                # rewrite on it; when redirected, break to a fresh log line.
                sys.stdout.write("\r\033[K" if sys.stdout.isatty() else "\n")
                self._progress_line_open = False
            sys.stdout.write(msg + "\n")
            sys.stdout.flush()

    def finish_progress_line(self) -> None:
        """Close the in-place progress line if one is open (call at transfer end)."""
        with self._output_lock:
            if self._progress_line_open:
                sys.stdout.write("\n")
                sys.stdout.flush()
                self._progress_line_open = False

    def _populate_rates(self, progress: TransferProgress) -> None:
        """Set download/upload rates and ETA on ``progress``.

        Reads last-known rates from the estimators (no sampling), so this is
        safe to call from ``get_progress`` without ``_state_lock``.
        """
        progress.download_rate = self._download_estimator.current_rate()
        progress.upload_rate = self._upload_estimator.current_rate()
        progress.eta_seconds = self._compute_eta(progress)

    def _compute_eta(self, progress: TransferProgress) -> Optional[float]:
        """Completion ETA, upload-based with a download warmup fallback.

        The transfer is "complete" when all bytes are on GDrive, so the upload
        rate is the honest basis. With the depth-1 pipeline, when download is
        the bottleneck ``uploaded_bytes`` still advances at ~the download rate
        (one shard lagged), so this stays sane in both bottleneck cases. The
        download-based fallback fires only while no upload rate exists yet
        (``blended_rate()`` is ``None`` — i.e. the first shard still
        downloading), avoiding minutes of "calculating…" on a large first
        shard; an upload *stall* (samples present but rate ``0``) yields
        ``None`` (displayed as `calculating…`), not a download-based ETA that
        would tick down while the upload is stuck.
        """
        remaining_upload = progress.total_bytes - progress.uploaded_bytes
        if remaining_upload <= 0:
            return 0.0
        blended = self._upload_estimator.blended_rate()
        if blended is not None and blended > 0:
            return remaining_upload / blended
        # Warmup only: no upload rate yet (first shard still downloading). A
        # genuine *stall* (blended == 0.0, not None) must NOT fall through to a
        # download-based ETA — that would tick down while the upload (the
        # completion gate) is stuck. Let it read "calculating…".
        if blended is None:
            remaining_total = progress.total_bytes - progress.downloaded_bytes
            if remaining_total > 0:
                return self._download_estimator.eta(remaining_total)
        return None

    def _calculate_progress(self) -> TransferProgress:
        """Calculate overall transfer progress.

        Takes ``_state_lock`` so the five passes over ``self.states.values()``
        see a consistent snapshot — a concurrent size-changing write (a new key
        in ``transfer_model`` or ``clear_state``) would otherwise raise
        ``RuntimeError`` mid-iteration. Re-entrant: ``_update_state`` /
        ``_notify_progress`` already hold the lock, ``get_progress`` takes it
        fresh.

        Returns:
            TransferProgress object.
        """
        with self._state_lock:
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
            free_space = get_free_space(
                self.config.rclone_path,
                remote=self.config.remote,
                no_mount=self.config.no_mount,
            )
            if free_space < model_info.total_size:
                print(f"Warning: Not enough space on GDrive")
                print(f"Available: {rclone_format_size(free_space)}")
                print(f"Required: {hf_format_size(model_info.total_size)}")
                if not input("Continue anyway? (y/N): ").lower() == "y":
                    return False
        except Exception as e:
            print(f"Could not check available space: {e}")

        # Initialize states for all files. Takes _state_lock: adding keys changes
        # dict size, which would race _calculate_progress's iteration otherwise.
        with self._state_lock:
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

        # Fresh cancel token per transfer: setting it (on Ctrl-C, or in the
        # finally below) terminates the in-flight aria2c/rclone subprocess.
        # Both paths start their subprocess with start_new_session=True, so a
        # terminal Ctrl-C does NOT reach it directly — only the token does.
        self._cancel = CancelToken()

        try:
            # Transfer each file
            if self.config.pipeline:
                # Depth-1 pipeline: upload shard x overlaps download of shard x+1.
                success = self._transfer_model_pipelined(
                    model_id, model_info.files, dest_dir
                )
            else:
                success = True
                for file in model_info.files:
                    key = self._get_state_key(model_id, file.path)
                    state = self.states[key]

                    # Skip if already complete
                    if state.status == "complete" and self.config.resume:
                        # Reclaim stale cache left by a run that crashed before
                        # per-file cleanup (only safe for files already uploaded).
                        if state.cache_path and Path(state.cache_path).exists():
                            try:
                                Path(state.cache_path).unlink()
                            except FileNotFoundError:
                                pass
                        self._print_status(f"Skipping {file.path} (already transferred)")
                        continue

                    if not self._transfer_file(
                        model_id=model_id,
                        file_info=file,
                        dest_dir=dest_dir,
                    ):
                        success = False
                        if not self._retry_or_fail(key):
                            self._print_status(f"Failed to transfer {file.path} after retries")
                            continue
        except KeyboardInterrupt:
            print("\nCancellation requested — terminating in-flight transfer...")
            self._cancel.set()
            with self._state_lock:
                self._save_state()
            raise
        finally:
            # Belt-and-suspenders: ensure no subprocess outlives the transfer
            # even if the except path above is missed. No-op once already set.
            self._cancel.set()

        # Cleanup if requested
        if self.config.cleanup:
            self._cleanup_cache(model_id)

        return success

    def _cache_path_for(self, model_id: str, file_info: FileInfo) -> Path:
        """Resolve the on-disk cache path for a file."""
        safe_filename = file_info.path.replace("/", "_")
        return self.cache_dir / f"{model_id.replace('/', '_')}_{safe_filename}"

    def _download_file(
        self,
        model_id: str,
        file_info: FileInfo,
        cancel=NO_CANCEL,
    ) -> Optional[Path]:
        """Download a single file to cache.

        Args:
            model_id: Model ID.
            file_info: File information.
            cancel: CancelToken to abort the download on Ctrl-C.

        Returns:
            Cache path on success, None on failure (state already recorded).
        """
        key = self._get_state_key(model_id, file_info.path)
        state = self.states[key]
        cache_path = self._cache_path_for(model_id, file_info)

        try:
            if state.status != "cached" or not Path(cache_path).exists():
                self._print_status(f"Downloading {file_info.path}...")
                state.status = "downloading"
                self._update_state(key)

                download_file(
                    model_id=model_id,
                    filename=file_info.path,
                    local_path=Path(cache_path),
                    token=self.config.hf_token,
                    max_retries=self.config.max_retries,
                    retry_delay=self.config.retry_delay,
                    use_aria2c=self.config.use_aria2c,
                    connections=self.config.aria2c_connections,
                    progress_callback=lambda downloaded, total: self._update_state(
                        key, downloaded_bytes=downloaded
                    ),
                    cancel=cancel,
                )

                state.status = "cached"
                state.cache_path = str(cache_path)
                state.downloaded_bytes = file_info.size
                self._update_state(key)

            return cache_path

        except Exception as e:
            self._print_status(f"Error downloading {file_info.path}: {e}")
            state.status = "failed"
            state.error = str(e)
            state.retries += 1
            self._update_state(key)
            return None

    def _upload_file(
        self,
        model_id: str,
        file_info: FileInfo,
        cache_path: Path,
        dest_dir: str,
        cancel=NO_CANCEL,
    ) -> bool:
        """Upload a single cached file to GDrive.

        Args:
            model_id: Model ID.
            file_info: File information.
            cache_path: Local cache path to upload.
            dest_dir: Destination directory.
            cancel: CancelToken to abort the upload on Ctrl-C.

        Returns:
            True if successful, False otherwise (state already recorded).
        """
        key = self._get_state_key(model_id, file_info.path)
        state = self.states[key]

        try:
            self._print_status(f"Uploading {file_info.path}...")
            state.status = "uploading"
            self._update_state(key)

            copy_file(
                src=Path(cache_path),
                rclone_path=self.config.rclone_path,
                dest_dir=dest_dir,
                remote=self.config.remote,
                no_mount=self.config.no_mount,
                drive_chunk_size=self.config.drive_chunk_size,
                progress_callback=lambda copied, total: self._update_state(
                    key, uploaded_bytes=copied
                ),
                max_retries=self.config.max_retries,
                retry_delay=self.config.retry_delay,
                cancel=cancel,
            )

            state.status = "complete"
            state.uploaded_bytes = file_info.size
            self._update_state(key)

            # Free disk now that the upload succeeded: delete this file's cache
            # immediately so peak disk stays near one shard (not the whole model).
            # The end-of-model _cleanup_cache still sweeps anything left on failure.
            if self.config.cleanup:
                try:
                    Path(cache_path).unlink()
                except FileNotFoundError:
                    pass

            self._print_status(f"Complete: {file_info.path}")
            return True

        except Exception as e:
            self._print_status(f"Error uploading {file_info.path}: {e}")
            state.status = "failed"
            state.error = str(e)
            state.retries += 1
            self._update_state(key)
            return False

    def _transfer_file(
        self,
        model_id: str,
        file_info: FileInfo,
        dest_dir: str,
    ) -> bool:
        """Transfer a single file sequentially (download then upload).

        Used by the non-pipelined path. The pipelined driver calls
        ``_download_file`` and ``_upload_file`` directly so they can overlap
        across shards.

        Args:
            model_id: Model ID.
            file_info: File information.
            dest_dir: Destination directory.

        Returns:
            True if successful, False otherwise.
        """
        cache_path = self._download_file(model_id, file_info, cancel=self._cancel)
        if cache_path is None:
            return False
        return self._upload_file(
            model_id, file_info, cache_path, dest_dir, cancel=self._cancel
        )

    def _transfer_model_pipelined(
        self,
        model_id: str,
        files: List[FileInfo],
        dest_dir: str,
    ) -> bool:
        """Transfer files with a depth-1 pipeline.

        Download of shard x+1 runs on the main thread while upload of shard x
        runs on a single worker thread (``ThreadPoolExecutor(max_workers=1)``),
        so the two never block each other beyond one shard of lag. The
        single-worker pool also guarantees uploads never overlap each other.

        Returns:
            True if all files transferred successfully, False otherwise.
        """
        success = True
        # Set on Ctrl-C (inner handler below) to SIGTERM the in-flight
        # aria2c/rclone subprocess BEFORE the executor's shutdown(wait=True)
        # runs — otherwise shutdown blocks on the worker until the transfer
        # finishes and the process appears to hang.
        cancel = self._cancel
        # pending_upload is None or (state_key, Future). Tracked as a tuple so
        # _drain can apply _retry_or_fail to the shard that actually failed.
        pending_upload: Optional[tuple] = None

        def _drain(pending: Optional[tuple]) -> tuple:
            """Wait for the previous upload future, applying retry/fail policy.

            Returns (None, ok) so callers rebind pending_upload to None.
            """
            if pending is None:
                return None, True
            key, future = pending
            ok = future.result()  # _upload_file returns bool, never raises
            if not ok:
                # _upload_file already set status=failed, error, retries+=1.
                if not self._retry_or_fail(key):
                    self._print_status(f"Failed to transfer {self.states[key].filename} after retries")
            return None, ok

        # The `with` block guarantees the worker is fully shut down before this
        # method returns, so _cleanup_cache (back in transfer_model) can't
        # unlink a file the worker is still reading.
        with ThreadPoolExecutor(max_workers=1) as executor:
            # Inner handler: a Ctrl-C during the loop must set the cancel token
            # BEFORE the `with` exits (which calls shutdown(wait=True) on the
            # worker). Setting it SIGTERMs the in-flight subprocess so the
            # worker finishes promptly instead of blocking shutdown for the rest
            # of the transfer. State checkpointing on cancel is owned by
            # transfer_model, which wraps both the pipelined and sequential paths.
            try:
                for file in files:
                    key = self._get_state_key(model_id, file.path)
                    state = self.states[key]

                    # Skip files already complete under resume. The pending
                    # upload still must be drained first so its result counts.
                    if state.status == "complete" and self.config.resume:
                        pending_upload, ok = _drain(pending_upload)
                        if not ok:
                            success = False
                        if state.cache_path and Path(state.cache_path).exists():
                            try:
                                Path(state.cache_path).unlink()
                            except FileNotFoundError:
                                pass
                        self._print_status(f"Skipping {file.path} (already transferred)")
                        continue

                    # Download on the main thread (blocks). The previous shard's
                    # upload (if any) runs concurrently on the worker — this is
                    # the overlap: upload(x) ∥ download(x+1).
                    cache_path = self._download_file(
                        model_id, file, cancel=cancel
                    )
                    if cache_path is None:
                        # Download failed; state already recorded. Drain the
                        # pending upload before moving on so its result isn't
                        # lost (un-resulted futures swallow their exceptions).
                        success = False
                        pending_upload, _ = _drain(pending_upload)
                        if not self._retry_or_fail(key):
                            self._print_status(f"Failed to transfer {file.path} after retries")
                        continue

                    # Submit this shard's upload to the worker, THEN join the
                    # previous upload. Order matters: submit before drain keeps
                    # the next iteration's download from stalling on the join.
                    this_upload = executor.submit(
                        self._upload_file, model_id, file, cache_path,
                        dest_dir, cancel,
                    )
                    pending_upload, ok = _drain(pending_upload)
                    if not ok:
                        success = False
                    pending_upload = (key, this_upload)

                # Drain the final shard's upload.
                pending_upload, ok = _drain(pending_upload)
                if not ok:
                    success = False
            except KeyboardInterrupt:
                # Fires before the `with` exits → before shutdown(wait=True).
                cancel.set()
                raise

        return success

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

        self._print_status(f"Retrying {state.filename} (attempt {state.retries + 1})...")
        state.status = "pending"
        state.error = None
        self._update_state(key)

        return True

    def _cleanup_cache(self, model_id: str) -> None:
        """Clean up cached files for a model.

        Args:
            model_id: Model ID.
        """
        with self._state_lock:
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
        # _calculate_progress took _state_lock above; _populate_rates reads
        # estimator locks only (no _state_lock, no sampling).
        progress = self._calculate_progress()
        self._populate_rates(progress)
        return progress

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
        with self._state_lock:
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
        print(
            f"Rate: Download {format_rate(progress.download_rate)} | "
            f"Upload {format_rate(progress.upload_rate)} | "
            f"ETA {format_eta(progress.eta_seconds)}"
        )

        if progress.failed_files > 0:
            print("\nFailed Files:")
            for state in self.get_failed_transfers():
                print(f"  - {state.filename}: {state.error}")

        print("="*60 + "\n")
