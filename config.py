"""
Configuration management for HF to GDrive Streamer.

Supports environment variable overrides and XDG config directory.
"""

import os
import warnings
from pathlib import Path
from typing import Optional, Dict, Any

import yaml


class Config:
    """Configuration manager for HF to GDrive Streamer."""

    # Default configuration values
    DEFAULTS: Dict[str, Any] = {
        # Model downloaded when `download` is run without a model ID
        "default_model_id": "huihui-ai/Huihui-Qwen3.8-Flash-Next-abliterated-v2-GGUF",
        "quant": None,  # GGUF quant filter (e.g. Q4_K_XL); None = all files
        "rclone_path": None,  # Path to rclone mount or config file
        "dest_dir": "/Models",  # Default destination on GDrive
        "cache_dir": "/tmp/hf_cache",  # Temporary cache location
        "include": "*",  # Default file inclusion pattern
        "exclude": None,  # Default exclusion patterns
        "max_shard_size": 5 * 1024 * 1024 * 1024,  # 5GB
        "cleanup": True,  # Delete cache after successful upload
        "resume": False,  # Resume from existing cache
        "max_retries": 3,  # Max retries for failed transfers
        "retry_delay": 5,  # Seconds between retries
        "hf_token": None,  # Hugging Face auth token
        "timeout": 300,  # Timeout for operations in seconds
        "use_aria2c": True,  # Use aria2c for downloads (default: True)
        "aria2c_connections": 16,  # Number of connections per file for aria2c
        "pipeline": True,  # Overlap upload of shard x with download of x+1 (depth-1)
        # --- rclone binary / upload strategy ---
        "rclone_binary": "rclone-fuse",  # FUSE-enabled rclone binary to invoke
        "no_mount": False,  # Bypass FUSE mount; upload via `rclone copyto` directly
        "remote": None,  # Remote name for no-mount/config mode (auto-detected if None)
        "drive_chunk_size": "64M",  # GDrive upload chunk size (throughput lever)
        # --- mount VFS cache (used by `setup` mount command) ---
        "vfs_cache_mode": "writes",  # writes = upload-only, less disk than 'full'
        "vfs_cache_max_size": "10G",  # Cap idle VFS cache (active file is exempt)
        "vfs_cache_max_age": "1h",  # Evict staging copy soon after upload
    }

    # Environment variable mappings
    ENV_PREFIX = "HF_RCLONE_"
    ENV_MAPPING: Dict[str, str] = {
        "default_model_id": "DEFAULT_MODEL_ID",
        "quant": "QUANT",
        "rclone_path": "RCLONE_PATH",
        "dest_dir": "DEST_DIR",
        "cache_dir": "CACHE_DIR",
        "max_shard_size": "MAX_SHARD_SIZE",
        "cleanup": "CLEANUP",
        "resume": "RESUME",
        "max_retries": "MAX_RETRIES",
        "retry_delay": "RETRY_DELAY",
        "hf_token": "HF_TOKEN",
        "timeout": "TIMEOUT",
        "rclone_binary": "RCLONE_BINARY",
        "no_mount": "NO_MOUNT",
        "remote": "REMOTE",
        "drive_chunk_size": "DRIVE_CHUNK_SIZE",
        "pipeline": "PIPELINE",
    }

    def __init__(self, config_file: Optional[Path] = None):
        """Initialize configuration.

        Args:
            config_file: Optional path to a YAML configuration file.
        """
        self._config = self.DEFAULTS.copy()
        self._load_from_file(config_file)
        self._load_from_env()

    @property
    def xdg_config_home(self) -> Path:
        """Get XDG config home directory."""
        xdg = os.environ.get("XDG_CONFIG_HOME")
        if xdg:
            return Path(xdg)
        return Path.home() / ".config"

    @property
    def config_dir(self) -> Path:
        """Get application config directory."""
        return self.xdg_config_home / "hf-rclone-streamer"

    def _load_from_file(self, config_file: Optional[Path] = None) -> None:
        """Load configuration from a YAML file.

        Args:
            config_file: Path to config file. If None, tries default location.
        """
        explicit = config_file is not None
        if config_file is None:
            config_file = self.config_dir / "config.yaml"

        if not config_file.exists():
            if explicit:
                warnings.warn(f"Config file not found: {config_file} — using defaults")
            else:
                legacy = self.config_dir / "config.json"
                if legacy.exists():
                    warnings.warn(
                        f"Legacy config file {legacy} is no longer read; rename "
                        f"it to {legacy.with_suffix('.yaml')} to keep your settings"
                    )
            return

        try:
            with open(config_file, "r") as f:
                user_config = yaml.safe_load(f)
                if isinstance(user_config, dict):
                    self._config.update(user_config)
        except (yaml.YAMLError, IOError) as e:
            # Don't fail on config file errors, just warn
            warnings.warn(f"Failed to load config file: {e}")

    def _load_from_env(self) -> None:
        """Load configuration from environment variables."""
        for key, env_name in self.ENV_MAPPING.items():
            env_value = os.environ.get(f"{self.ENV_PREFIX}{env_name}")
            if env_value is not None:
                # Convert string values to appropriate types
                self._config[key] = self._parse_env_value(key, env_value)

    def _parse_env_value(self, key: str, value: str) -> Any:
        """Parse environment variable value to appropriate type.

        Args:
            key: Configuration key.
            value: String value from environment.

        Returns:
            Parsed value.
        """
        # Boolean values
        if key in ("cleanup", "resume", "no_mount", "pipeline"):
            return value.lower() in ("1", "true", "yes", "on")

        # Integer values
        if key in ("max_shard_size", "max_retries", "retry_delay", "timeout"):
            try:
                return int(value)
            except ValueError:
                return self.DEFAULTS[key]

        # String values
        return value

    def get(self, key: str, default: Any = None) -> Any:
        """Get a configuration value.

        Args:
            key: Configuration key.
            default: Default value if key not found.

        Returns:
            Configuration value.
        """
        return self._config.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Set a configuration value.

        Args:
            key: Configuration key.
            value: Value to set.
        """
        self._config[key] = value

    def update(self, updates: Dict[str, Any]) -> None:
        """Update multiple configuration values.

        Args:
            updates: Dictionary of key-value pairs to update.
        """
        self._config.update(updates)

    @property
    def default_model_id(self) -> str:
        """Get the model downloaded when no model ID is given."""
        return self.get(
            "default_model_id", "huihui-ai/Huihui-Qwen3.8-Flash-Next-abliterated-v2-GGUF"
        )

    @property
    def quant(self) -> Optional[str]:
        """Get the GGUF quant filter (e.g. 'Q4_K_XL'), or None for all files."""
        return self.get("quant")

    @property
    def rclone_path(self) -> Optional[str]:
        """Get rclone path (mount point or config file)."""
        return self.get("rclone_path")

    @property
    def dest_dir(self) -> str:
        """Get destination directory on GDrive."""
        return self.get("dest_dir", "/Models")

    @property
    def cache_dir(self) -> str:
        """Get cache directory path."""
        return self.get("cache_dir", "/tmp/hf_cache")

    @property
    def include_pattern(self) -> str:
        """Get file inclusion pattern."""
        return self.get("include", "*")

    @property
    def exclude_patterns(self) -> Optional[list]:
        """Get file exclusion patterns."""
        exclude = self.get("exclude")
        if exclude is None:
            return None
        if isinstance(exclude, str):
            return [exclude]
        return exclude

    @property
    def max_shard_size(self) -> int:
        """Get maximum shard size in bytes."""
        return self.get("max_shard_size", 5 * 1024 * 1024 * 1024)

    @property
    def cleanup(self) -> bool:
        """Get cleanup flag."""
        return self.get("cleanup", True)

    @property
    def resume(self) -> bool:
        """Get resume flag."""
        return self.get("resume", False)

    @property
    def max_retries(self) -> int:
        """Get maximum retry attempts."""
        return self.get("max_retries", 3)

    @property
    def retry_delay(self) -> int:
        """Get retry delay in seconds."""
        return self.get("retry_delay", 5)

    @property
    def hf_token(self) -> Optional[str]:
        """Get Hugging Face authentication token."""
        return self.get("hf_token")

    @property
    def timeout(self) -> int:
        """Get operation timeout in seconds."""
        return self.get("timeout", 300)

    @property
    def use_aria2c(self) -> bool:
        """Get whether to use aria2c for downloads."""
        return self.get("use_aria2c", True)

    @property
    def aria2c_connections(self) -> int:
        """Get number of aria2c connections per file."""
        return self.get("aria2c_connections", 16)

    @property
    def pipeline(self) -> bool:
        """Whether to overlap upload of shard x with download of shard x+1."""
        return self.get("pipeline", True)

    @property
    def rclone_binary(self) -> str:
        """Get the rclone binary to invoke (default: rclone-fuse)."""
        return self.get("rclone_binary", "rclone-fuse")

    @property
    def no_mount(self) -> bool:
        """Whether to bypass the FUSE mount and upload via `rclone copyto`."""
        return self.get("no_mount", False)

    @property
    def remote(self) -> Optional[str]:
        """Get the remote name for no-mount/config mode."""
        return self.get("remote")

    @property
    def drive_chunk_size(self) -> str:
        """Get the GDrive upload chunk size (e.g. '64M')."""
        return self.get("drive_chunk_size", "64M")

    @property
    def vfs_cache_mode(self) -> str:
        """Get the VFS cache mode for the mount (default: writes)."""
        return self.get("vfs_cache_mode", "writes")

    @property
    def vfs_cache_max_size(self) -> str:
        """Get the VFS cache size cap (default: 10G)."""
        return self.get("vfs_cache_max_size", "10G")

    @property
    def vfs_cache_max_age(self) -> str:
        """Get the VFS cache max age (default: 1h)."""
        return self.get("vfs_cache_max_age", "1h")

    def ensure_cache_dir(self) -> Path:
        """Ensure cache directory exists and return its path.

        Returns:
            Path to cache directory.
        """
        cache_path = Path(self.cache_dir)
        cache_path.mkdir(parents=True, exist_ok=True)
        return cache_path


# Global configuration instance
_config: Optional[Config] = None


def get_config(config_file: Optional[Path] = None) -> Config:
    """Get the global configuration instance.

    Args:
        config_file: Optional path to configuration file.

    Returns:
        Configuration instance.
    """
    global _config
    if _config is None:
        _config = Config(config_file)
    return _config


def reset_config() -> None:
    """Reset the global configuration instance."""
    global _config
    _config = None
