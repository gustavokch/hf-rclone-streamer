"""
Setup module for helping naive users configure rclone and mount Google Drive.

Guides users through:
1. Checking/installing prerequisites (rclone, aria2c)
2. Configuring rclone with Google Drive
3. Creating a mount point
4. Mounting Google Drive
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional, List

try:
    from .rclone_client import get_rclone_binary
except ImportError:
    from rclone_client import get_rclone_binary


class SetupError(Exception):
    """Exception raised during setup."""
    pass


class RcloneSetup:
    """Helper for setting up rclone with Google Drive."""

    # Mount flags tuned for this streamer's workload: upload-only streaming to
    # Google Drive with minimal local disk and maximum upload throughput.
    #   - vfs-cache-mode writes: upload-only, so reads aren't cached (less disk
    #     than 'full'); files opened for write are still staged then uploaded.
    #   - vfs-cache-max-size/max-age/poll-interval: evict the staging copy ASAP
    #     after each upload, so peak disk stays near 1x the active file.
    #   - drive-chunk-size 64M: fewer HTTP round-trips to GDrive -> throughput.
    #   - noappledouble/noapplexattr: skip macOS metadata churn on the mount.
    OPTIMIZED_MOUNT_FLAGS = [
        "--vfs-cache-mode", "writes",
        "--vfs-cache-max-size", "10G",
        "--vfs-cache-max-age", "1h",
        "--vfs-cache-poll-interval", "1m",
        "--drive-chunk-size", "64M",
        "--buffer-size", "32M",
        "--low-level-retries", "10",
        "--dir-cache-time", "1h",
        "--attr-timeout", "1h",
        "--noappledouble",
        "--noapplexattr",
    ]

    def __init__(self):
        """Initialize the setup helper."""
        self.check_commands()

    def check_commands(self) -> None:
        """Check if required commands are available."""
        self.rclone_available = shutil.which("rclone") is not None
        self.aria2c_available = shutil.which("aria2c") is not None

    def print_status(self) -> None:
        """Print the current status of prerequisites."""
        print("\n" + "="*60)
        print("Prerequisite Check")
        print("="*60)

        print(f"rclone: {'✓ Installed' if self.rclone_available else '✗ Not found'}")
        print(f"aria2c: {'✓ Installed' if self.aria2c_available else '✗ Not found'}")
        print("="*60 + "\n")

    def install_instructions(self) -> str:
        """Get installation instructions for missing tools.

        Returns:
            Installation instructions string.
        """
        instructions = []

        if not self.rclone_available:
            instructions.append("""
# Install rclone
# macOS:
brew install rclone

# Ubuntu/Debian:
sudo apt install rclone

# Or download from: https://rclone.org/downloads/
""")

        if not self.aria2c_available:
            instructions.append("""
# Install aria2c
# macOS:
brew install aria2

# Ubuntu/Debian:
sudo apt install aria2

# Fedora:
sudo dnf install aria2
""")

        return "\n".join(instructions)

    def check_rclone_config(self) -> List[str]:
        """Get list of configured rclone remotes.

        Returns:
            List of remote names.
        """
        if not self.rclone_available:
            raise SetupError("rclone is not installed")

        try:
            result = subprocess.run(
                ["rclone", "listremotes"],
                capture_output=True,
                text=True,
                check=True,
            )
            remotes = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            return remotes
        except subprocess.CalledProcessError as e:
            raise SetupError(f"Failed to list rclone remotes: {e.stderr}")

    def has_gdrive_remote(self) -> bool:
        """Check if there's a Google Drive remote configured.

        Returns:
            True if a gdrive remote exists.
        """
        remotes = self.check_rclone_config()
        return any("gdrive" in remote.lower() for remote in remotes)

    def guide_rclone_config(self) -> str:
        """Guide user through rclone config.

        Returns:
            Name of the configured remote.
        """
        print("\n" + "="*60)
        print("Setting up rclone with Google Drive")
        print("="*60)
        print("\nThis will open the rclone configuration wizard.")
        print("You'll need to:")
        print("  1. Select 'New Remote'")
        print("  2. Give it a name (e.g., 'gdrive')")
        print("  3. Select storage type 'Google Drive'")
        print("  4. Follow the OAuth2 flow in your browser")
        print("  5. Select desired access levels (usually 'All')")
        print("\n" + "="*60)

        input("\nPress Enter to start rclone config...")

        try:
            subprocess.run(["rclone", "config"], check=True)
        except subprocess.CalledProcessError as e:
            raise SetupError(f"rclone config failed: {e}")
        except KeyboardInterrupt:
            raise SetupError("Setup cancelled by user")

        # Check if a remote was created
        remotes = self.check_rclone_config()
        if not remotes:
            raise SetupError("No remotes configured. Please run 'rclone config' manually.")

        # Find a gdrive remote
        for remote in remotes:
            if "gdrive" in remote.lower():
                return remote

        # If no gdrive remote, return the first one
        return remotes[0].rstrip(":")

    def suggest_mount_point(self) -> Path:
        """Suggest a default mount point.

        Returns:
            Suggested mount point path.
        """
        suggestions = [
            Path.home() / "gdrive",
            Path.home() / "Google Drive",
            Path("/mnt/gdrive"),
        ]

        for path in suggestions:
            if not path.exists():
                return path

        # Default to ~/gdrive
        return Path.home() / "gdrive"

    def create_mount_point(self, mount_path: Path) -> None:
        """Create mount point directory.

        Args:
            mount_path: Path to create.
        """
        mount_path = Path(mount_path).expanduser().resolve()

        if mount_path.exists():
            if not mount_path.is_dir():
                raise SetupError(f"Mount point exists but is not a directory: {mount_path}")
            print(f"Mount point already exists: {mount_path}")
        else:
            mount_path.mkdir(parents=True, exist_ok=True)
            print(f"Created mount point: {mount_path}")

    def _mount_base_cmd(
        self,
        remote: str,
        mount_path: Path,
        vfs_cache: bool = True,
    ) -> List[str]:
        """Build the foreground rclone mount command (no --daemon)."""
        cmd = [
            get_rclone_binary(),
            "mount",
            f"{remote}:",
            str(mount_path),
        ]
        if vfs_cache:
            cmd.extend(self.OPTIMIZED_MOUNT_FLAGS)
        return cmd

    def get_mount_command(
        self,
        remote: str,
        mount_path: Path,
        vfs_cache: bool = True,
    ) -> List[str]:
        """Get the rclone mount command (daemonized, with logging).

        Args:
            remote: Remote name.
            mount_path: Mount point path.
            vfs_cache: Whether to use the optimized VFS cache flags.

        Returns:
            Command as list of strings.
        """
        cmd = self._mount_base_cmd(remote, mount_path, vfs_cache=vfs_cache)
        cmd.extend([
            "--daemon",  # Run in background
            "--log-file", str(mount_path.parent / "rclone.log"),
            "--log-level", "INFO",
        ])
        return cmd

    def print_mount_command(
        self,
        remote: str,
        mount_path: Path,
        vfs_cache: bool = True,
    ) -> None:
        """Print the mount command for user to run.

        Args:
            remote: Remote name.
            mount_path: Mount point path.
            vfs_cache: Whether to use the optimized VFS cache flags.
        """
        fg_cmd = self._mount_base_cmd(remote, mount_path, vfs_cache=vfs_cache)
        daemon_cmd = fg_cmd + [
            "--daemon",
            "--log-file", str(mount_path.parent / "rclone.log"),
            "--log-level", "INFO",
        ]

        print("\n" + "="*60)
        print("Mount Command (optimized for min disk / max throughput)")
        print("="*60)
        print("\nTo mount your Google Drive in the foreground, run:")
        print()
        print(f"  {' '.join(fg_cmd)}")
        print()
        print("\nOr run in the background with logging:")
        print()
        print(f"  {' '.join(daemon_cmd)}")
        print()
        print("="*60)

    def mount_now(
        self,
        remote: str,
        mount_path: Path,
        vfs_cache: bool = True,
    ) -> bool:
        """Attempt to mount Google Drive now (foreground).

        Args:
            remote: Remote name.
            mount_path: Mount point path.
            vfs_cache: Whether to use the optimized VFS cache flags.

        Returns:
            True if mount successful, False otherwise.
        """
        mount_path = Path(mount_path).expanduser().resolve()

        # Foreground (no --daemon) so errors are visible.
        cmd = self._mount_base_cmd(remote, mount_path, vfs_cache=vfs_cache)

        print(f"\nAttempting to mount {remote}: to {mount_path}...")
        print(f"Binary: {get_rclone_binary()}")
        print("Note: This command may require sudo privileges on some systems.")
        print("Press Ctrl+C to stop the mount (or close this terminal).\n")

        try:
            # Run mount command
            subprocess.run(cmd, check=True)
            return True
        except subprocess.CalledProcessError as e:
            print(f"Mount failed: {e}")
            print("\nYou may need to:")
            print("  1. Run with sudo: sudo " + " ".join(cmd))
            print("  2. Add your user to the fuse group: sudo usermod -a -G fuse $USER")
            print("  3. Or run the mount command manually in a separate terminal")
            return False
        except KeyboardInterrupt:
            print("\n\nMount interrupted.")
            return False

    def interactive_setup(self) -> dict:
        """Run interactive setup for naive users.

        Returns:
            Dictionary with setup results (remote, mount_path, etc.).
        """
        print("\n" + "="*60)
        print("HF to GDrive Streamer - Setup Wizard")
        print("="*60)
        print("\nThis wizard will help you:")
        print("  1. Check prerequisites")
        print("  2. Configure rclone with Google Drive")
        print("  3. Set up a mount point")
        print("  4. Mount your Google Drive")
        print()

        # Check prerequisites
        self.print_status()

        if not self.rclone_available or not self.aria2c_available:
            print("⚠️  Missing prerequisites:")
            print(self.install_instructions())
            if not input("\nContinue anyway? (y/N): ").lower() == "y":
                return {"success": False, "reason": "Prerequisites not installed"}

        # Check existing rclone config
        print("\nChecking rclone configuration...")

        try:
            remotes = self.check_rclone_config()
            print(f"Found {len(remotes)} configured remote(s):")
            for remote in remotes:
                print(f"  - {remote}")
        except SetupError as e:
            print(f"Note: {e}")

        # Check for gdrive remote
        if self.has_gdrive_remote():
            print("\n✓ Google Drive remote already configured!")
            remote = "gdrive"  # Assume it's called gdrive
        elif not remotes:
            # No remotes at all - need to configure
            print("\nNo rclone remotes configured.")
            if input("\nConfigure Google Drive now? (Y/n): ").lower() == "n":
                return {"success": False, "reason": "No remotes configured"}
            try:
                remote = self.guide_rclone_config()
                print(f"\n✓ Configured remote: {remote}")
            except SetupError as e:
                print(f"\n✗ Setup failed: {e}")
                return {"success": False, "reason": str(e)}
        else:
            # Use existing remote
            print("\nUsing existing remote (first in list):")
            remote = remotes[0].rstrip(":")
            print(f"  {remote}")

        # Suggest mount point
        mount_path = self.suggest_mount_point()
        print(f"\nSuggested mount point: {mount_path}")

        custom_mount = input(f"Use this path? (Y/n): ").strip()
        if custom_mount and custom_mount.lower() == "n":
            mount_path = input("Enter custom mount path: ").strip()
            mount_path = Path(mount_path).expanduser().resolve()

        # Create mount point
        try:
            self.create_mount_point(mount_path)
        except SetupError as e:
            print(f"✗ {e}")
            return {"success": False, "reason": str(e)}

        # Print mount command
        self.print_mount_command(remote, mount_path)

        # Offer to mount now
        if input("\nMount Google Drive now? (Y/n): ").strip().lower() != "n":
            print("\nNote: If mounting fails, you can run the command manually.")
            if input("Continue? (Y/n): ").strip().lower() != "n":
                try:
                    self.mount_now(remote, mount_path)
                    print("\n✓ Google Drive mounted successfully!")
                    print(f"Files will be available at: {mount_path}")
                except (SetupError, KeyboardInterrupt):
                    print("\nMount cancelled. You can mount manually using the command above.")

        # Save configuration
        print("\n" + "="*60)
        print("Setup Summary")
        print("="*60)
        print(f"\nRemote: {remote}")
        print(f"Mount point: {mount_path}")
        print("\nTo use this streamer, run:")
        print()
        print(f"  python -m hf_rclone_streamer download <model_id> --rclone-path {mount_path}")
        print()
        print("Or save as default configuration:")
        print()
        print(f"  export HF_RCLONE_RCLONE_PATH={mount_path}")
        print()
        print("="*60)

        return {
            "success": True,
            "remote": remote,
            "mount_path": str(mount_path),
        }


def run_setup() -> int:
    """Run the interactive setup wizard.

    Returns:
        Exit code.
    """
    try:
        setup = RcloneSetup()
        result = setup.interactive_setup()

        if result.get("success"):
            return 0
        else:
            print(f"\nSetup failed: {result.get('reason', 'Unknown error')}")
            return 1

    except KeyboardInterrupt:
        print("\n\nSetup cancelled by user.")
        return 130  # Standard exit code for SIGINT
    except Exception as e:
        print(f"\nUnexpected error: {e}")
        return 1


def quick_check() -> dict:
    """Quick check of setup status without interaction.

    Returns:
        Dictionary with status information.
    """
    setup = RcloneSetup()

    return {
        "rclone_available": setup.rclone_available,
        "aria2c_available": setup.aria2c_available,
        "remotes": setup.check_rclone_config() if setup.rclone_available else [],
        "has_gdrive": setup.has_gdrive_remote() if setup.rclone_available else False,
    }
