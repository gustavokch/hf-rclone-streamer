#!/usr/bin/env python3
"""
Rclone FUSE Builder for macOS

Builds rclone from source with FUSE mount support on macOS.
The Homebrew version of rclone does not support FUSE mounting on macOS.
"""

import os
import subprocess
import shutil
import sys
import platform
import urllib.request
from pathlib import Path
from typing import Optional, Tuple


class RcloneFUSEBuilder:
    """Build rclone from source with FUSE support on macOS."""

    RCLONE_VERSION = "v1.68.0"
    BUILD_DIR = Path.home() / ".hf-rclone-streamer" / "rclone-build"
    RCLONE_BINARY = BUILD_DIR / "rclone"

    def __init__(self):
        """Initialize the builder."""
        self.os_name = platform.system()
        self.arch = self._get_arch()

    def _get_arch(self) -> str:
        """Get system architecture."""
        arch = platform.machine()
        if arch == "x86_64":
            return "amd64"
        elif arch == "arm64":
            return "arm64"
        return arch

    def is_macos(self) -> bool:
        """Check if running on macOS."""
        return self.os_name == "Darwin"

    def check_fuse_installed(self) -> bool:
        """Check if macFUSE is installed."""
        try:
            result = subprocess.run(
                ["system_profiler", "SPExtensionsDataType"],
                capture_output=True,
                text=True,
                timeout=30
            )
            return "osxfuse" in result.stdout.lower() or "macfuse" in result.stdout.lower()
        except Exception:
            # Try checking kernel extensions
            try:
                result = subprocess.run(
                    ["kextstat"],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                return "fuse" in result.stdout.lower()
            except Exception:
                return False

    def check_rclone_mount_support(self) -> Tuple[bool, str]:
        """Check if current rclone supports mount.

        Returns:
            Tuple of (supports_mount, message)
        """
        try:
            result = subprocess.run(
                ["rclone", "mount", "--help"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                return True, "rclone mount support available"
            else:
                return False, "rclone mount not supported"
        except FileNotFoundError:
            return False, "rclone not found"
        except Exception as e:
            return False, f"Error checking rclone: {e}"

    def check_homebrew_rclone(self) -> bool:
        """Check if rclone is from Homebrew (no FUSE support)."""
        try:
            rclone_path = shutil.which("rclone")
            if rclone_path:
                brew_prefix = subprocess.run(
                    ["brew", "--prefix"],
                    capture_output=True,
                    text=True,
                    timeout=10
                ).stdout.strip()
                return str(brew_prefix) in rclone_path
        except Exception:
            pass
        return False

    def get_fuse_install_instructions(self) -> str:
        """Get instructions for installing macFUSE."""
        return """
To enable rclone mount on macOS, you need to install macFUSE:

1. Download macFUSE from: https://macfuse.github.io/
2. Install the package (requires system extension)
3. Restart your computer if prompted
4. Run this script again

Alternatively, you can use Homebrew Cask:
    brew install --cask macfuse

Note: Installing macFUSE requires system privileges and may require
allowing the extension in System Preferences > Security & Privacy.
"""

    def install_fuse_prompt(self) -> bool:
        """Prompt user to install macFUSE.

        Returns:
            True if user wants to proceed, False otherwise.
        """
        print("\n" + "=" * 70)
        print("macFUSE is required for rclone mount on macOS")
        print("=" * 70)
        print(self.get_fuse_install_instructions())

        response = input("\nHave you installed macFUSE? (y/n): ").strip().lower()
        return response == 'y' or response == 'yes'

    def download_rclone_source(self) -> bool:
        """Download rclone source code.

        Returns:
            True if successful.
        """
        url = f"https://github.com/rclone/rclone/releases/download/{self.RCLONE_VERSION}/rclone-{self.RCLONE_VERSION}-{self.os_name.lower()}-{self.arch}.zip"
        extracted_dir = self.BUILD_DIR / f"rclone-{self.RCLONE_VERSION}-{self.os_name.lower()}-{self.arch}"

        # Check if already downloaded
        if self.RCLONE_BINARY.exists() and self.RCLONE_BINARY.is_file():
            print(f"✓ rclone binary already exists at {self.RCLONE_BINARY}")
            return True

        self.BUILD_DIR.mkdir(parents=True, exist_ok=True)

        print(f"Downloading rclone {self.RCLONE_VERSION}...")
        try:
            zip_path = self.BUILD_DIR / "rclone.zip"
            urllib.request.urlretrieve(url, zip_path)

            # Unzip
            print("Extracting...")
            subprocess.run(
                ["unzip", "-o", str(zip_path), "-d", str(self.BUILD_DIR)],
                check=True,
                timeout=120
            )

            # Move binary to expected location
            source_binary = extracted_dir / "rclone"
            if source_binary.exists():
                shutil.copy(str(source_binary), str(self.RCLONE_BINARY))
                os.chmod(str(self.RCLONE_BINARY), 0o755)
                zip_path.unlink()
                print(f"✓ Downloaded rclone to {self.RCLONE_BINARY}")
                return True
            else:
                print("✗ rclone binary not found in extracted archive")
                return False

        except Exception as e:
            print(f"✗ Failed to download rclone: {e}")
            return False

    def build_rclone_from_source(self) -> bool:
        """Build rclone from source with FUSE support.

        Returns:
            True if successful.
        """
        print("\n" + "=" * 70)
        print("Building rclone from source with FUSE support")
        print("=" * 70)

        # Check for Go
        if not shutil.which("go"):
            print("\n✗ Go is required to build rclone from source")
            print("\nInstall Go with: brew install go")
            return False

        build_dir = self.BUILD_DIR / "src"
        build_dir.mkdir(parents=True, exist_ok=True)

        print(f"\nCloning rclone repository...")
        try:
            if (build_dir / "rclone").exists():
                print("Repository already exists, updating...")
                subprocess.run(
                    ["git", "-C", str(build_dir / "rclone"), "pull"],
                    check=True,
                    timeout=300
                )
            else:
                subprocess.run(
                    ["git", "clone", "https://github.com/rclone/rclone.git",
                     str(build_dir / "rclone")],
                    check=True,
                    timeout=600
                )

            print("Building rclone (this may take a few minutes)...")
            subprocess.run(
                ["go", "build", "-tags", "fuse", "-o", str(self.RCLONE_BINARY)],
                cwd=str(build_dir / "rclone"),
                check=True,
                timeout=600
            )

            os.chmod(str(self.RCLONE_BINARY), 0o755)
            print(f"✓ Built rclone with FUSE support at {self.RCLONE_BINARY}")
            return True

        except subprocess.TimeoutExpired:
            print("✗ Build timed out")
            return False
        except subprocess.CalledProcessError as e:
            print(f"✗ Build failed: {e}")
            return False
        except Exception as e:
            print(f"✗ Error building rclone: {e}")
            return False

    def install_rclone_alias(self) -> bool:
        """Create an alias script to use the custom rclone.

        Returns:
            True if successful.
        """
        bin_dir = Path.home() / ".local" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)

        alias_script = bin_dir / "rclone-fuse"
        with open(alias_script, "w") as f:
            f.write(f"""#!/bin/bash
export PATH="{bin_dir}:$PATH"
exec "{self.RCLONE_BINARY}" "$@"
""")
        os.chmod(alias_script, 0o755)

        print(f"\n✓ Created rclone-fuse alias at {alias_script}")
        print(f"\nTo use the FUSE-enabled rclone:")
        print(f"  1. Add to your shell profile: export PATH=\"{bin_dir}:$PATH\"")
        print(f"  2. Or use directly: {alias_script} mount ...")

        # Also try to update shell profile
        zshrc = Path.home() / ".zshrc"
        if zshrc.exists():
            path_line = f'export PATH="{bin_dir}:$PATH"'
            content = zshrc.read_text()
            if path_line not in content:
                with open(zshrc, "a") as f:
                    f.write(f"\n# HF rclone streamer FUSE support\n{path_line}\n")
                print(f"✓ Added to {zshrc}")
                print(f"  Run: source ~/.zshrc")

        return True

    def setup_fuse_rclone(self) -> bool:
        """Main setup flow for FUSE-enabled rclone on macOS.

        Returns:
            True if setup successful.
        """
        print("\n🔧 rclone FUSE Builder for macOS")
        print("=" * 70)

        # Check platform
        if not self.is_macos():
            print("This module is for macOS only")
            return False

        # Check current rclone
        supports_mount, msg = self.check_rclone_mount_support()
        if supports_mount and not self.check_homebrew_rclone():
            print(f"✓ {msg}")
            print("Your rclone already supports FUSE mounting!")
            return True

        if self.check_homebrew_rclone():
            print("⚠️  Detected Homebrew rclone (no FUSE support)")
            print("We'll build a FUSE-enabled version alongside it.")

        # Check macFUSE
        if not self.check_fuse_installed():
            print("\n⚠️  macFUSE is not installed")
            if not self.install_fuse_prompt():
                print("\nSetup cancelled. Please install macFUSE and try again.")
                return False

        # Offer download or build
        print("\nChoose installation method:")
        print("  1. Download pre-built binary (recommended, faster)")
        print("  2. Build from source (requires Go)")

        choice = input("\nChoose (1/2): ").strip()

        success = False
        if choice == "1":
            success = self.download_rclone_source()
        elif choice == "2":
            success = self.build_rclone_from_source()
        else:
            print("Invalid choice")
            return False

        if success:
            self.install_rclone_alias()
            print("\n✓ Setup complete!")
            return True

        return False


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Build rclone with FUSE support for macOS"
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only check FUSE support, don't install"
    )

    args = parser.parse_args()

    builder = RcloneFUSEBuilder()

    if args.check_only:
        # Just check status
        print("Checking rclone FUSE support...")
        if builder.is_macos():
            supports_mount, msg = builder.check_rclone_mount_support()
            print(f"Mount support: {supports_mount} ({msg})")
            print(f"macFUSE installed: {builder.check_fuse_installed()}")
            print(f"Homebrew rclone: {builder.check_homebrew_rclone()}")
        else:
            print("Not running on macOS")
        return

    # Run setup
    success = builder.setup_fuse_rclone()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
