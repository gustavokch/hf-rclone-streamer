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
    """Check if rclone is available in PATH.

    Raises:
        RcloneNotFoundError: If rclone is not found.
    """
    if not shutil.which("rclone"):
        raise RcloneNotFoundError(
            "rclone not found in PATH. Install it from https://rclone.org/"
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

    cmd = ["rclone", "listremotes"]
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

    cmd = ["rclone", "about", remote]
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


def get_free_space(
    rclone_path: Optional[str] = None,
    remote: Optional[str] = None,
) -> int:
    """Get free space on Google Drive.

    Args:
        rclone_path: Path to mount or config file.
        remote: Remote name for config mode.

    Returns:
        Free space in bytes.
    """
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
    free_str = info.get("Free", "0")

    # Parse size string (e.g., "12.5GBytes")
    match = re.match(r"([\d.]+)(\w+)", free_str)
    if not match:
        return 0

    value, unit = match.groups()
    value = float(value)

    unit_map = {
        "Bytes": 1,
        "KBytes": 1024,
        "MBytes": 1024**2,
        "GBytes": 1024**3,
        "TBytes": 1024**4,
    }

    return int(value * unit_map.get(unit, 1))


def copy_to_mount(
    src: Path,
    mount_path: Path,
    dest_subdir: str,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> Path:
    """Copy a file to an rclone mount point.

    Args:
        src: Source file path.
        mount_path: Mount point directory.
        dest_subdir: Destination subdirectory (e.g., "/Models/Llama").
        progress_callback: Optional callback(bytes_copied, total_bytes).

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

    Raises:
        CopyError: If copy fails after all retries.
    """
    check_rclone_available()

    last_error = None

    for attempt in range(max_retries):
        try:
            cmd = [
                "rclone",
                "copyto",
                str(src),
                f"{remote}{dest_path}",
            ]

            if config_path:
                cmd.extend(["--config", str(config_path)])

            cmd.extend([
                "--progress",
                "--stats-one-line",
                "--stats", "1s",
            ])

            # Run rclone and capture progress
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            # Parse progress from output
            size_pattern = re.compile(r"([\d.]+)([KMGT]?iB)")

            for line in process.stdout:
                # Look for progress info
                if "Transferred:" in line or "%" in line:
                    # Extract progress if possible
                    pass  # Progress parsing would go here

            return_code = process.wait()

            if return_code != 0:
                raise subprocess.CalledProcessError(return_code, cmd)

            return  # Success

        except subprocess.CalledProcessError as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                continue

    raise CopyError(
        f"Failed to copy {src} to {remote}{dest_path} after {max_retries} attempts: {last_error}"
    )


def copy_file(
    src: Path,
    rclone_path: Optional[str] = None,
    dest_dir: str = "/Models",
    remote: Optional[str] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    max_retries: int = 3,
    retry_delay: int = 5,
) -> None:
    """Copy a file to Google Drive using the appropriate mode.

    Args:
        src: Source file path.
        rclone_path: Path to mount or config file.
        dest_dir: Destination directory on GDrive.
        remote: Remote name for config mode.
        progress_callback: Optional progress callback.
        max_retries: Maximum retry attempts.
        retry_delay: Seconds between retries.

    Raises:
        RcloneError: If operation fails.
    """
    mode = detect_mode(rclone_path)

    if mode.mode == "mount":
        copy_to_mount(src, mode.path, dest_dir, progress_callback)
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
        cmd = ["rclone", "check", str(local), remote_path]
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

    cmd = ["rclone", "ls", remote_path]
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

    cmd = ["rclone", "mkdir", remote_path]
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
