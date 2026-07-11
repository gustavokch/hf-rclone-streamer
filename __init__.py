"""
HF to GDrive Streamer

Stream Hugging Face models to Google Drive via rclone.
"""

__version__ = "1.0.0"
__author__ = "Your Name"

from .config import Config, get_config
from .hf_api import (
    ModelInfo,
    FileInfo,
    search_models,
    get_model_info,
    download_file,
    format_size,
)
from .rclone_client import (
    detect_mode,
    copy_file,
    get_free_space,
)
from .transfer_manager import (
    TransferManager,
    TransferState,
    TransferProgress,
)

__all__ = [
    "Config",
    "get_config",
    "ModelInfo",
    "FileInfo",
    "search_models",
    "get_model_info",
    "download_file",
    "format_size",
    "detect_mode",
    "copy_file",
    "get_free_space",
    "TransferManager",
    "TransferState",
    "TransferProgress",
]
