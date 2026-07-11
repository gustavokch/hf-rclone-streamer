#!/usr/bin/env python3
"""
HF to GDrive Streamer - Main CLI

Stream Hugging Face model downloads to Google Drive via rclone.
Uses localhost as a VFS cache with aria2c for high-speed downloads.
"""

import argparse
import sys
from pathlib import Path
from typing import List, Optional

# Add script directory to path for imports when running directly
script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

try:
    from .config import Config, get_config
    from .hf_api import search_models, get_model_info, format_size, print_model_summary
    from .transfer_manager import TransferManager, TransferProgress
    from .rclone_client import detect_mode, get_free_space, format_size as rclone_format_size
    from .setup import run_setup, quick_check
except ImportError:
    # Running directly (not as a module)
    from config import Config, get_config
    from hf_api import search_models, get_model_info, format_size, print_model_summary
    from transfer_manager import TransferManager, TransferProgress
    from rclone_client import detect_mode, get_free_space, format_size as rclone_format_size
    from setup import run_setup, quick_check


def create_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser.

    Returns:
        Configured ArgumentParser.
    """
    parser = argparse.ArgumentParser(
        description="Stream Hugging Face models to Google Drive",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Download a model to mount point
  %(prog)s download meta-llama/Llama-3.1-8B --rclone-path ~/gdrive

  # Search and download
  %(prog)s search "llama 3" --author meta-llama

  # Resume interrupted transfer
  %(prog)s download meta-llama/Llama-3.1-8B --resume

  # Use aria2c with more connections
  %(prog)s download gpt2 --aria2c-connections 32
        """,
    )

    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 1.0.0",
    )

    # Global options
    parser.add_argument(
        "--rclone-path",
        type=str,
        help="Path to rclone mount or config file",
    )
    parser.add_argument(
        "--cache-dir",
        type=str,
        default="/tmp/hf_cache",
        help="Temporary cache directory (default: /tmp/hf_cache)",
    )
    parser.add_argument(
        "--dest-dir",
        type=str,
        default="/Models",
        help="Destination directory on GDrive (default: /Models)",
    )
    parser.add_argument(
        "--include",
        type=str,
        default="*",
        help="Glob pattern for files to include (default: *)",
    )
    parser.add_argument(
        "--exclude",
        type=str,
        action="append",
        help="Glob patterns to exclude (can be used multiple times)",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Don't delete cache after successful upload",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from existing cache",
    )
    parser.add_argument(
        "--hf-token",
        type=str,
        help="Hugging Face authentication token",
    )
    parser.add_argument(
        "--no-aria2c",
        action="store_true",
        help="Disable aria2c and use huggingface_hub for downloads",
    )
    parser.add_argument(
        "--aria2c-connections",
        type=int,
        default=16,
        help="Number of connections per file for aria2c (default: 16)",
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Download command
    download_parser = subparsers.add_parser(
        "download",
        help="Download a model from Hugging Face",
    )
    download_parser.add_argument(
        "model_id",
        type=str,
        help="Model ID (e.g., meta-llama/Llama-3.1-8B)",
    )

    # Search command
    search_parser = subparsers.add_parser(
        "search",
        help="Search for models on Hugging Face",
    )
    search_parser.add_argument(
        "query",
        type=str,
        help="Search query",
    )
    search_parser.add_argument(
        "--author",
        type=str,
        help="Filter by author/organization",
    )
    search_parser.add_argument(
        "--tags",
        type=str,
        nargs="*",
        help="Filter by tags (e.g., text-generation pytorch)",
    )
    search_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of results (default: 20)",
    )
    search_parser.add_argument(
        "--download",
        action="store_true",
        help="Download selected model after search",
    )

    # Batch command
    batch_parser = subparsers.add_parser(
        "batch",
        help="Download multiple models from a file",
    )
    batch_parser.add_argument(
        "file",
        type=str,
        help="File containing model IDs (one per line)",
    )

    # Info command
    info_parser = subparsers.add_parser(
        "info",
        help="Show information about a model",
    )
    info_parser.add_argument(
        "model_id",
        type=str,
        help="Model ID",
    )

    # Status command
    status_parser = subparsers.add_parser(
        "status",
        help="Show transfer status",
    )

    # Setup command
    setup_parser = subparsers.add_parser(
        "setup",
        help="Set up rclone and Google Drive for first-time users",
    )
    setup_parser.add_argument(
        "--check",
        action="store_true",
        help="Quick check of setup status without interaction",
    )

    return parser


def cmd_download(args: argparse.Namespace, config: Config) -> int:
    """Handle the download command.

    Args:
        args: Parsed arguments.
        config: Configuration object.

    Returns:
        Exit code.
    """
    model_id = args.model_id

    print(f"Preparing to download: {model_id}")
    print(f"Cache directory: {config.cache_dir}")
    print(f"Destination: {config.dest_dir}")

    # Verify rclone setup
    try:
        mode = detect_mode(config.rclone_path)
        print(f"Rclone mode: {mode.mode}")
        if mode.mode == "mount":
            print(f"Mount path: {mode.path}")
    except Exception as e:
        print(f"Error: {e}")
        return 1

    # Check aria2c availability
    import shutil
    if config.use_aria2c:
        if shutil.which("aria2c"):
            print(f"Using aria2c with {config.aria2c_connections} connections")
        else:
            print("Warning: aria2c not found, falling back to huggingface_hub")
            config.set("use_aria2c", False)

    # Create transfer manager with progress callback
    def progress_callback(progress: TransferProgress) -> None:
        """Display progress during transfer."""
        if progress.total_bytes > 0:
            download_pct = progress.downloaded_bytes / progress.total_bytes * 100
            upload_pct = progress.uploaded_bytes / progress.total_bytes * 100
        else:
            download_pct = 0
            upload_pct = 0

        print(
            f"\rProgress: {progress.completed_files}/{progress.total_files} files | "
            f"Download: {format_size(progress.downloaded_bytes)}/{format_size(progress.total_bytes)} "
            f"({download_pct:.1f}%) | "
            f"Upload: {rclone_format_size(progress.uploaded_bytes)}/{format_size(progress.total_bytes)} "
            f"({upload_pct:.1f}%)",
            end="",
            flush=True,
        )

    manager = TransferManager(config=config, progress_callback=progress_callback)

    # Start transfer
    include_patterns = [config.include_pattern]
    exclude_patterns = config.exclude_patterns

    success = manager.transfer_model(
        model_id=model_id,
        dest_dir=config.dest_dir,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
    )

    print()  # New line after progress

    if success:
        print("Transfer completed successfully!")
        return 0
    else:
        print("Transfer completed with errors.")
        manager.print_status()
        return 1


def cmd_search(args: argparse.Namespace, config: Config) -> int:
    """Handle the search command.

    Args:
        args: Parsed arguments.
        config: Configuration object.

    Returns:
        Exit code.
    """
    print(f"Searching for: {args.query}")

    if args.author:
        print(f"Author: {args.author}")

    try:
        results = search_models(
            query=args.query,
            author=args.author,
            tags=args.tags if args.tags else None,
            limit=args.limit,
            token=config.hf_token,
        )

        if not results:
            print("No results found.")
            return 0

        print(f"\nFound {len(results)} models:\n")

        for i, model_id in enumerate(results, 1):
            print(f"{i}. {model_id}")

        # Interactive selection if --download flag is set
        if args.download:
            print()
            try:
                choice = input("Enter number to download (or 'q' to quit): ").strip()

                if choice.lower() == "q":
                    return 0

                idx = int(choice) - 1
                if 0 <= idx < len(results):
                    selected_model = results[idx]

                    # Create args for download
                    import argparse
                    download_args = argparse.Namespace(
                        model_id=selected_model,
                        rclone_path=args.rclone_path,
                        cache_dir=args.cache_dir,
                        dest_dir=args.dest_dir,
                        include=args.include,
                        exclude=args.exclude,
                        no_cleanup=args.no_cleanup,
                        resume=args.resume,
                        hf_token=args.hf_token,
                        no_aria2c=args.no_aria2c,
                        aria2c_connections=args.aria2c_connections,
                    )

                    return cmd_download(download_args, config)
                else:
                    print("Invalid selection.")
                    return 1

            except (ValueError, KeyboardInterrupt):
                print("\nCancelled.")
                return 0

        return 0

    except Exception as e:
        print(f"Error during search: {e}")
        return 1


def cmd_batch(args: argparse.Namespace, config: Config) -> int:
    """Handle the batch download command.

    Args:
        args: Parsed arguments.
        config: Configuration object.

    Returns:
        Exit code.
    """
    file_path = Path(args.file)

    if not file_path.exists():
        print(f"Error: File not found: {file_path}")
        return 1

    # Read model IDs from file
    with open(file_path, "r") as f:
        model_ids = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    if not model_ids:
        print("No model IDs found in file.")
        return 0

    print(f"Found {len(model_ids)} models to download:\n")

    for i, model_id in enumerate(model_ids, 1):
        print(f"{i}. {model_id}")

    if not input("\nProceed with download? (y/N): ").lower() == "y":
        print("Cancelled.")
        return 0

    # Download each model
    success_count = 0
    for i, model_id in enumerate(model_ids, 1):
        print(f"\n[{i}/{len(model_ids)}] Downloading: {model_id}")
        print("-" * 60)

        # Create args for download
        download_args = argparse.Namespace(
            model_id=model_id,
            rclone_path=args.rclone_path,
            cache_dir=args.cache_dir,
            dest_dir=args.dest_dir,
            include=args.include,
            exclude=args.exclude,
            no_cleanup=args.no_cleanup,
            resume=args.resume,
            hf_token=args.hf_token,
            no_aria2c=args.no_aria2c,
            aria2c_connections=args.aria2c_connections,
        )

        result = cmd_download(download_args, config)

        if result == 0:
            success_count += 1

    print(f"\n{'='*60}")
    print(f"Batch download complete: {success_count}/{len(model_ids)} succeeded")
    print(f"{'='*60}")

    return 0 if success_count == len(model_ids) else 1


def cmd_info(args: argparse.Namespace, config: Config) -> int:
    """Handle the info command.

    Args:
        args: Parsed arguments.
        config: Configuration object.

    Returns:
        Exit code.
    """
    try:
        model_info = get_model_info(
            model_id=args.model_id,
            token=config.hf_token,
            include_patterns=[config.include_pattern],
            exclude_patterns=config.exclude_patterns,
        )

        print_model_summary(model_info)
        return 0

    except Exception as e:
        print(f"Error: {e}")
        return 1


def cmd_status(args: argparse.Namespace, config: Config) -> int:
    """Handle the status command.

    Args:
        args: Parsed arguments.
        config: Configuration object.

    Returns:
        Exit code.
    """
    manager = TransferManager(config=config)
    manager.print_status()
    return 0


def cmd_setup(args: argparse.Namespace, config: Config) -> int:
    """Handle the setup command.

    Args:
        args: Parsed arguments.
        config: Configuration object.

    Returns:
        Exit code.
    """
    if args.check:
        # Quick check mode
        status = quick_check()

        print("\n" + "="*60)
        print("Setup Status Check")
        print("="*60)
        print(f"\nrclone: {'✓ Available' if status['rclone_available'] else '✗ Not installed'}")
        print(f"aria2c: {'✓ Available' if status['aria2c_available'] else '✗ Not installed'}")

        if status["rclone_available"]:
            if status["remotes"]:
                print(f"\nConfigured remotes ({len(status['remotes'])}):")
                for remote in status["remotes"]:
                    print(f"  - {remote}")
                print(f"\nGoogle Drive: {'✓ Configured' if status['has_gdrive'] else '✗ Not found'}")
            else:
                print("\nNo rclone remotes configured.")
                print("Run 'python -m hf_rclone_streamer setup' to configure.")

        print("\n" + "="*60)
        return 0

    # Interactive setup mode
    return run_setup()


def main() -> int:
    """Main entry point.

    Returns:
        Exit code.
    """
    parser = create_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 0

    # Build config from args
    config = Config()
    if args.rclone_path:
        config.set("rclone_path", args.rclone_path)
    if args.cache_dir:
        config.set("cache_dir", args.cache_dir)
    if args.dest_dir:
        config.set("dest_dir", args.dest_dir)
    if args.include:
        config.set("include", args.include)
    if args.exclude:
        config.set("exclude", args.exclude)
    if args.no_cleanup:
        config.set("cleanup", False)
    if args.resume:
        config.set("resume", True)
    if args.hf_token:
        config.set("hf_token", args.hf_token)
    if args.no_aria2c:
        config.set("use_aria2c", False)
    if args.aria2c_connections:
        config.set("aria2c_connections", args.aria2c_connections)

    # Dispatch to command handler
    command_handlers = {
        "download": cmd_download,
        "search": cmd_search,
        "batch": cmd_batch,
        "info": cmd_info,
        "status": cmd_status,
        "setup": cmd_setup,
    }

    handler = command_handlers.get(args.command)
    if handler:
        return handler(args, config)

    print(f"Unknown command: {args.command}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
