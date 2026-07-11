"""
Main entry point for running the package with `python -m hf_rclone_streamer`.
"""

from .hf_rclone_streamer import main
import sys

if __name__ == "__main__":
    sys.exit(main())
