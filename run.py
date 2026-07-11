#!/usr/bin/env python3
"""
Entry point script for running HF to GDrive Streamer.
Run this directly: python3 run.py <command>
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# Now import and run
import hf_rclone_streamer

if __name__ == "__main__":
    sys.exit(hf_rclone_streamer.main())
