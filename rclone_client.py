"""
Rclone client for interacting with Google Drive.

Supports both mount mode (direct file I/O) and config mode (CLI commands).
"""

import os
import subprocess
import shutil
from pathlib import Path
from typing import Optional, List, Dict, Any, Callable
from dataclasses import dataclass
import time
import re


try:
    from .cancel import NO_CANCEL, terminate
except ImportError:
    from cancel import NO_CANCEL, terminate


# The rclone binary to invoke. Defaults to the FUSE-enabled build so `mount`
# works on macOS; override via HF_RCLONE_RCLONE_BINARY env var or Config.
RCLONE_BINARY = os.environ.get("HF_RCLONE_RCLONE_BINARY", "rclone-fuse")


def set_rclone_binary(binary: Optional[str]) -> None:
    """Override the rclone binary used by all functions in this module.

    Args:
        binary: Path or name of the rclone binary (e.g. "rclone-fuse").
    """
    global RCLONE_BINARY
    if binary:
        RCLONE_BINARY = binary


def get_rclone_binary() -> str:
    """Return the rclone binary currently in use."""
    return RCLONE_BINARY


def _rclone_cmd(*args: str) -> List[str]:
    """Build an rclone command list using the configured binary."""
    return [RCLONE_BINARY, *args]


@dataclass
class RcloneMode:
    """Detected rclone operation mode."""
    mode: str  # 'mount' or 'config'
    path: Optional[Path] = None  # Mount path or config file path
    remote_name: Optional[str] = None  # For config mode


class RcloneError(Exception):
    """Base exception for rclone errors."""
    pass


class RcloneNotFoundError(RcloneError):
    """Exception raised when rclone is not found."""
    pass


class MountNotFoundError(RcloneError):
    """Exception raised when mount point is not accessible."""
    pass


class ConfigNotFoundError(RcloneError):
    """Exception raised when rclone config file is not found."""
    pass


class CopyError(RcloneError):
    """Exception raised when copy operation fails."""
    pass


def check_rclone_available() -> None:
    """Check if the configured rclone binary is available in PATH.

    Raises:
        RcloneNotFoundError: If the rclone binary is not found.
    """
    if not shutil.which(RCLONE_BINARY):
        raise RcloneNotFoundError(
            f"rclone binary '{RCLONE_BINARY}' not found in PATH. "
            "Build it with: python run.py setup-fuse, or point at a different "
            "binary via --rclone-binary / HF_RCLONE_RCLONE_BINARY."
        )


def detect_mode(rclone_path: Optional[str] = None) -> RcloneMode:
    """Detect whether the given path is a mount point or config file.

    Args:
        rclone_path: Path to check. If None, attempts to auto-detect.

    Returns:
        RcloneMode object with detected mode and path.

    Raises:
        RcloneError: If detection fails.
    """
    check_rclone_available()

    if rclone_path is None:
        # Try to auto-detect common mount points
        common_mounts = [
            Path.home() / "gdrive",
            Path.home() / "Google Drive",
            Path("/mnt/gdrive"),
            Path("/Volumes/gdrive"),
        ]
        for mount in common_mounts:
            if mount.exists() and mount.is_dir():
                return RcloneMode(mode="mount", path=mount)
        raise RcloneError(
            "No rclone mount found at common locations. "
            "Specify --rclone-path."
        )

    path = Path(rclone_path).expanduser().resolve()

    if not path.exists():
        raise MountNotFoundError(
            f"Path does not exist: {rclone_path}"
        )

    # Check if it's a config file
    if path.is_file():
        if path.suffix == ".conf" or path.name == "rclone.conf":
            return RcloneMode(mode="config", path=path)
        else:
            raise ConfigNotFoundError(
                f"File doesn't appear to be an rclone config: {rclone_path}"
            )

    # Check if it's a directory (mount point)
    if path.is_dir():
        # Verify it's actually a mount
        try:
            # Check for .rclone directory or rclone marker
            if (path / ".rclone").exists():
                return RcloneMode(mode="mount", path=path)

            # Try to list directory - if it's a mount, it should work
            list(path.iterdir())
            return RcloneMode(mode="mount", path=path)
        except PermissionError:
            raise MountNotFoundError(
                f"Cannot access mount point (permissions): {rclone_path}"
            )

    raise RcloneError(
        f"Cannot determine rclone mode for path: {rclone_path}"
    )


def get_remotes(config_path: Optional[Path] = None) -> List[str]:
    """Get list of remotes from rclone config.

    Args:
        config_path: Optional path to rclone config file.

    Returns:
        List of remote names.
    """
    check_rclone_available()

    cmd = _rclone_cmd("listremotes")
    if config_path:
        cmd.extend(["--config", str(config_path)])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
        remotes = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return remotes
    except subprocess.CalledProcessError as e:
        raise RcloneError(f"Failed to list remotes: {e.stderr}")


def get_remote_info(remote: str, config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Get information about a remote.

    Args:
        remote: Remote name (e.g., "gdrive:").
        config_path: Optional path to rclone config file.

    Returns:
        Dictionary with remote information.
    """
    check_rclone_available()

    cmd = _rclone_cmd("about", remote)
    if config_path:
        cmd.extend(["--config", str(config_path)])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )

        # Parse output
        info = {}
        for line in result.stdout.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                info[key.strip()] = value.strip()

        return info
    except subprocess.CalledProcessError as e:
        raise RcloneError(f"Failed to get remote info: {e.stderr}")


def resolve_remote(
    rclone_path: Optional[str] = None,
    remote: Optional[str] = None,
    config_path: Optional[Path] = None,
) -> str:
    """Resolve the remote name to use for direct/config operations.

    Precedence: explicit `remote` arg > `rclone_path` if it names a remote >
    first configured remote (auto-detect).

    Args:
        rclone_path: Optional path that may be a remote spec (e.g. "drive-gkch:").
        remote: Explicit remote name.
        config_path: Optional rclone config file path.

    Returns:
        Remote name without trailing colon (e.g. "drive-gkch").
    """
    check_rclone_available()

    if remote:
        return remote.rstrip(":")

    # If rclone_path looks like a remote spec (e.g. "drive-gkch:" or "drive-gkch")
    # rather than a filesystem path, treat it as the remote name.
    if rclone_path and "/" not in rclone_path and "\\" not in rclone_path:
        candidate = rclone_path.strip()
        if not Path(candidate).exists():
            return candidate.rstrip(":")

    remotes = get_remotes(config_path)
    if not remotes:
        raise RcloneError(
            "No rclone remotes configured. Run 'rclone config' to create one."
        )
    return remotes[0].rstrip(":")


def get_free_space(
    rclone_path: Optional[str] = None,
    remote: Optional[str] = None,
    no_mount: bool = False,
) -> int:
    """Get free space on Google Drive.

    Args:
        rclone_path: Path to mount or config file (ignored in no_mount mode).
        remote: Remote name for config/no-mount mode.
        no_mount: If True, query the remote directly via `rclone about`.

    Returns:
        Free space in bytes.
    """
    check_rclone_available()

    # Direct/no-mount mode (or no path given): query the remote via `rclone about`.
    if no_mount or rclone_path is None:
        resolved = resolve_remote(None, remote)
        info = get_remote_info(f"{resolved}:")
        return _parse_free_space(info)

    mode = detect_mode(rclone_path)

    if mode.mode == "mount":
        # Use shutil to get disk usage
        stat = shutil.disk_usage(mode.path)
        return stat.free

    # Config mode
    if remote is None:
        remotes = get_remotes(mode.path)
        if not remotes:
            raise RcloneError("No remotes found in config")
        remote = remotes[0]

    info = get_remote_info(remote, mode.path)
    return _parse_free_space(info)


# Size suffix → byte multiplier. Shared by the `rclone about` parser and the
# rclone progress-line parser so both read "GiB", "GB", "GBytes", … the same
# way. Unknown unit → 0 (treated as "unknown", not a handful of bytes).
_SIZE_UNITS = {
    "B": 1, "Byte": 1, "Bytes": 1,
    "KiB": 1024, "KBytes": 1024, "KB": 1024,
    "MiB": 1024**2, "MBytes": 1024**2, "MB": 1024**2,
    "GiB": 1024**3, "GBytes": 1024**3, "GB": 1024**3,
    "TiB": 1024**4, "TBytes": 1024**4, "TB": 1024**4,
    "PiB": 1024**5, "PBytes": 1024**5, "PB": 1024**5,
}


def _parse_size_value(value: str, unit: str) -> int:
    """Bytes represented by ``value`` (e.g. "2.930") with ``unit`` ("GiB").

    Unknown unit or non-numeric value → 0 (treated as "unknown", not a handful
    of bytes, and never raises — this runs on every rclone stdout line).
    """
    try:
        return int(float(value) * _SIZE_UNITS.get(unit, 0))
    except (ValueError, TypeError):
        return 0


def _parse_free_space(info: Dict[str, Any]) -> int:
    """Parse the 'Free' field from `rclone about` output into bytes.

    Handles both `rclone about` suffix styles: "4.999TiB" (default) and
    "12.5GBytes" (--full).
    """
    free_str = info.get("Free", "0")

    # Number, optional whitespace, then a unit of letters.
    match = re.search(r"([\d.]+)\s*([A-Za-z]+)", free_str)
    if not match:
        return 0

    return _parse_size_value(match.group(1), match.group(2))


# rclone `--stats-one-line` transfer field, e.g. "2.930 GiB / 5.859 GiB".
# Surfaced per second via ``--stats-log-level NOTICE`` (NOT ``--progress``: to a
# non-TTY pipe rclone ``-P`` updates in place with ``\r`` and block-buffers the
# whole run, flushing only at exit, so mid-run ticks never arrive — uploads then
# report no progress). With NOTICE the line is prefixed like
# ``NOTICE:   2.930 GiB / 5.859 GiB, 50%, ...`` and newline-flushed, so each
# ``for line in stdout`` iteration is one tick. There is no "Transferred:" label,
# so match the ``X unit / Y unit`` pair directly (tolerant of spaces around the
# slash) and read transferred bytes from the first operand.
_RCLONE_TRANSFER_RE = re.compile(
    r"([\d.]+)\s*([KMGT]?i?B)\s*/\s*([\d.]+)\s*([KMGT]?i?B)"
)


def _rclone_transferred_bytes(line: str) -> Optional[int]:
    """Bytes transferred so far, parsed from one rclone stats line.

    Returns None for lines without a ``X / Y`` byte-transfer field (the
    file-count "Transferred: N / N" line, "Checks:" lines, the rate field,
    etc.) so the caller can skip them. Also None for a matched-but-garbage
    number (the ``[\\d.]+`` group admits multi-dot strings like "1.2.3"): such
    a line is skipped rather than feeding a spurious 0-byte sample to the
    estimator.
    """
    match = _RCLONE_TRANSFER_RE.search(line)
    if not match:
        return None
    value, unit = match.group(1), match.group(2)
    try:
        float(value)
    except ValueError:
        return None
    return _parse_size_value(value, unit)


def copy_to_mount(
    src: Path,
    mount_path: Path,
    dest_subdir: str,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    cancel=NO_CANCEL,
) -> Path:
    """Copy a file to an rclone mount point.

    Args:
        src: Source file path.
        mount_path: Mount point directory.
        dest_subdir: Destination subdirectory (e.g., "/Models/Llama").
        progress_callback: Optional callback(bytes_copied, total_bytes).
        cancel: Optional CancelToken to abort the copy on Ctrl-C.

    Returns:
        Destination file path.
    """
    dest_path = mount_path / dest_subdir.lstrip("/") / src.name

    # Ensure destination directory exists
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    total_size = src.stat().st_size

    try:
        with open(src, "rb") as fsrc:
            with open(dest_path, "wb") as fdest:
                copied = 0
                while True:
                    if cancel.is_set():
                        raise CopyError("copy to mount cancelled")
                    chunk = fsrc.read(1024 * 1024)  # 1MB chunks
                    if not chunk:
                        break
                    fdest.write(chunk)
                    copied += len(chunk)

                    if progress_callback:
                        progress_callback(copied, total_size)

        return dest_path

    except IOError as e:
        raise CopyError(f"Failed to copy file to mount: {e}")


def copy_with_rclone(
    src: Path,
    remote: str,
    dest_path: str,
    config_path: Optional[Path] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    max_retries: int = 3,
    retry_delay: int = 5,
    drive_chunk_size: str = "64M",
    cancel=NO_CANCEL,
) -> None:
    """Copy a file using rclone CLI.

    Args:
        src: Source file path.
        remote: Remote name (e.g., "gdrive:").
        dest_path: Destination path on remote.
        config_path: Optional path to rclone config.
        progress_callback: Optional callback(bytes_copied, total_bytes).
        max_retries: Maximum retry attempts.
        retry_delay: Seconds between retries.
        drive_chunk_size: GDrive upload chunk size (default 64M).
        cancel: Optional CancelToken to terminate rclone on Ctrl-C.

    Raises:
        CopyError: If copy fails after all retries.
    """
    check_rclone_available()

    total_size = src.stat().st_size
    last_error = None

    for attempt in range(max_retries):
        try:
            cmd = _rclone_cmd(
                "copyto",
                str(src),
                f"{remote}{dest_path}",
            )

            if config_path:
                cmd.extend(["--config", str(config_path)])

            cmd.extend([
                "--drive-chunk-size", drive_chunk_size,
                "--stats-one-line",
                "--stats", "1s",
                "--stats-log-level", "NOTICE",
            ])

            # Run rclone in its own session so cancellation is deterministic.
            process = cancel.register(subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            ))

            if progress_callback:
                progress_callback(0, total_size)

            try:
                for line in process.stdout:
                    if cancel.is_set():
                        break
                    if progress_callback:
                        transferred = _rclone_transferred_bytes(line)
                        if transferred is not None:
                            progress_callback(transferred, total_size)

                return_code = process.wait()
            finally:
                cancel.unregister(process)
                if process.poll() is None:
                    terminate(process)

            if return_code != 0:
                raise subprocess.CalledProcessError(return_code, cmd)

            if progress_callback:
                progress_callback(total_size, total_size)
            return  # Success

        except subprocess.CalledProcessError as e:
            last_error = e
            if cancel.is_set():
                raise CopyError(f"copy cancelled: {src}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                continue

    raise CopyError(
        f"Failed to copy {src} to {remote}{dest_path} after {max_retries} attempts: {last_error}"
    )


def copy_direct(
    src: Path,
    remote: str,
    dest_path: str,
    config_path: Optional[Path] = None,
    drive_chunk_size: str = "64M",
    progress_callback: Optional[Callable[[int, int], None]] = None,
    max_retries: int = 3,
    retry_delay: int = 5,
    cancel=NO_CANCEL,
) -> None:
    """Upload a single file directly to the remote via `rclone copyto`.

    Bypasses the FUSE mount entirely, so the file lives on local disk only
    once (the aria2c temp copy) — no VFS staging duplicate. This is the
    minimal-disk upload path. ``--drive-chunk-size`` controls throughput:
    larger chunks mean fewer API round-trips to Google Drive. It is a Google
    Drive backend flag, so this path assumes a GDrive remote (``--remote``);
    rclone errors out if pointed at a different backend.

    Args:
        src: Source file path.
        remote: Remote name (e.g. "drive-gkch").
        dest_path: Destination path on remote (e.g. "/Models/file.bin").
        config_path: Optional rclone config file.
        drive_chunk_size: GDrive upload chunk size (default 64M).
        progress_callback: Optional callback(bytes_copied, total_bytes).
        max_retries: Maximum retry attempts.
        retry_delay: Seconds between retries.
        cancel: Optional CancelToken to terminate rclone on Ctrl-C.

    Raises:
        CopyError: If copy fails after all retries.
    """
    check_rclone_available()

    remote_spec = f"{remote.rstrip(':')}:"
    total_size = src.stat().st_size
    last_error = None

    # Progress is parsed from each rclone stats tick by the shared
    # _rclone_transferred_bytes() helper (same wiring as copy_with_rclone).

    for attempt in range(max_retries):
        try:
            cmd = _rclone_cmd(
                "copyto",
                str(src),
                f"{remote_spec}{dest_path}",
                "--drive-chunk-size", drive_chunk_size,
                "--stats-one-line",
                "--stats", "1s",
                "--stats-log-level", "NOTICE",
            )
            if config_path:
                cmd.extend(["--config", str(config_path)])

            # Run rclone in its own session so cancellation is deterministic.
            process = cancel.register(subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            ))

            if progress_callback:
                progress_callback(0, total_size)

            try:
                for line in process.stdout:
                    if cancel.is_set():
                        break
                    if progress_callback:
                        transferred = _rclone_transferred_bytes(line)
                        if transferred is not None:
                            progress_callback(transferred, total_size)

                return_code = process.wait()
            finally:
                cancel.unregister(process)
                if process.poll() is None:
                    terminate(process)

            if return_code != 0:
                raise subprocess.CalledProcessError(return_code, cmd)

            if progress_callback:
                progress_callback(total_size, total_size)
            return  # Success

        except subprocess.CalledProcessError as e:
            last_error = e
            if cancel.is_set():
                raise CopyError(f"copy cancelled: {src}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                continue

    raise CopyError(
        f"Failed to copy {src} to {remote_spec}{dest_path} "
        f"after {max_retries} attempts: {last_error}"
    )


def copy_file(
    src: Path,
    rclone_path: Optional[str] = None,
    dest_dir: str = "/Models",
    remote: Optional[str] = None,
    no_mount: bool = False,
    drive_chunk_size: str = "64M",
    progress_callback: Optional[Callable[[int, int], None]] = None,
    max_retries: int = 3,
    retry_delay: int = 5,
    cancel=NO_CANCEL,
) -> None:
    """Copy a file to Google Drive using the appropriate mode.

    Three modes:
      - no_mount=True: direct `rclone copyto` (1x disk, no FUSE mount needed).
      - rclone_path is a directory: write through the FUSE mount (2x disk peak).
      - rclone_path is a config file: `rclone copyto` with that config.

    Args:
        src: Source file path.
        rclone_path: Path to mount or config file (or a remote spec in no_mount).
        dest_dir: Destination directory on GDrive.
        remote: Remote name for config/no-mount mode (auto-detected if None).
        no_mount: Bypass the FUSE mount and upload directly.
        drive_chunk_size: GDrive upload chunk size (default 64M).
        progress_callback: Optional progress callback.
        max_retries: Maximum retry attempts.
        retry_delay: Seconds between retries.
        cancel: Optional CancelToken to terminate the upload on Ctrl-C.

    Raises:
        RcloneError: If operation fails.
    """
    if no_mount:
        resolved = resolve_remote(rclone_path, remote)
        dest_path = f"{dest_dir}/{src.name}"
        copy_direct(
            src=src,
            remote=resolved,
            dest_path=dest_path,
            drive_chunk_size=drive_chunk_size,
            progress_callback=progress_callback,
            max_retries=max_retries,
            retry_delay=retry_delay,
            cancel=cancel,
        )
        return

    mode = detect_mode(rclone_path)

    if mode.mode == "mount":
        copy_to_mount(src, mode.path, dest_dir, progress_callback, cancel=cancel)
    else:
        if remote is None:
            remotes = get_remotes(mode.path)
            if not remotes:
                raise RcloneError("No remotes found in config")
            remote = remotes[0]

        dest_path = f"{dest_dir}/{src.name}"
        copy_with_rclone(
            src=src,
            remote=remote,
            dest_path=dest_path,
            config_path=mode.path,
            progress_callback=progress_callback,
            max_retries=max_retries,
            retry_delay=retry_delay,
            drive_chunk_size=drive_chunk_size,
            cancel=cancel,
        )


def verify_upload(
    local: Path,
    remote_path: str,
    config_path: Optional[Path] = None,
) -> bool:
    """Verify that a file was uploaded correctly.

    Args:
        local: Local file path.
        remote_path: Remote file path (e.g., "gdrive:/Models/file.bin").
        config_path: Optional rclone config path.

    Returns:
        True if verification passes, False otherwise.
    """
    check_rclone_available()

    try:
        # Use rclone check to verify
        cmd = _rclone_cmd("check", str(local), remote_path)
        if config_path:
            cmd.extend(["--config", str(config_path)])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minutes
        )

        # Check output for errors
        if "errors: 0" in result.stdout:
            return True

        return False

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def list_directory(
    remote_path: str,
    config_path: Optional[Path] = None,
) -> List[str]:
    """List files in a remote directory.

    Args:
        remote_path: Remote directory path.
        config_path: Optional rclone config path.

    Returns:
        List of file/directory names.
    """
    check_rclone_available()

    cmd = _rclone_cmd("ls", remote_path)
    if config_path:
        cmd.extend(["--config", str(config_path)])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )

        files = []
        for line in result.stdout.splitlines():
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                files.append(parts[1])

        return files

    except subprocess.CalledProcessError as e:
        raise RcloneError(f"Failed to list directory: {e.stderr}")


def ensure_directory(
    remote_path: str,
    config_path: Optional[Path] = None,
) -> None:
    """Ensure a directory exists on the remote.

    Args:
        remote_path: Remote directory path.
        config_path: Optional rclone config path.
    """
    check_rclone_available()

    cmd = _rclone_cmd("mkdir", remote_path)
    if config_path:
        cmd.extend(["--config", str(config_path)])

    try:
        subprocess.run(cmd, capture_output=True, check=True)
    except subprocess.CalledProcessError as e:
        # Directory might already exist, which is fine
        if "directory not created" not in e.stderr.lower():
            raise RcloneError(f"Failed to create directory: {e}")


def format_size(size_bytes: int) -> str:
    """Format byte size as human-readable string.

    Args:
        size_bytes: Size in bytes.

    Returns:
        Formatted string.
    """
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"
