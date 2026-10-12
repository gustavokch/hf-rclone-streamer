"""Unit tests for config.Config.

Covers the YAML config file (replaces the old JSON file), the new
``default_model_id`` / ``quant`` keys, and the precedence chain:
DEFAULTS < YAML file < environment variables.

Run: .venv/bin/python test_config.py
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config


def test_new_gguf_defaults():
    cfg = Config(config_file=Path("/nonexistent/nope.yaml"))
    assert cfg.default_model_id == (
        "huihui-ai/Huihui-Qwen3.8-Flash-Next-abliterated-v2-GGUF"
    ), f"default_model_id: {cfg.default_model_id!r}"
    assert cfg.quant is None, f"quant: {cfg.quant!r}"


def _write_yaml(directory: Path, text: str) -> Path:
    path = directory / "config.yaml"
    path.write_text(text)
    return path


def test_yaml_overrides_defaults():
    with tempfile.TemporaryDirectory() as td:
        path = _write_yaml(
            Path(td), "dest_dir: /Models/Qwen\nquant: Q4_K_XL\n"
        )
        cfg = Config(config_file=path)
    assert cfg.dest_dir == "/Models/Qwen", f"dest_dir: {cfg.dest_dir!r}"
    assert cfg.quant == "Q4_K_XL", f"quant: {cfg.quant!r}"
    # Untouched keys keep their defaults
    assert cfg.cache_dir == "/tmp/hf_cache", f"cache_dir: {cfg.cache_dir!r}"


def test_env_beats_yaml():
    with tempfile.TemporaryDirectory() as td:
        path = _write_yaml(Path(td), "dest_dir: /Models/Qwen\n")
        with mock.patch.dict(
            os.environ, {"HF_RCLONE_DEST_DIR": "/EnvWins"}
        ):
            cfg = Config(config_file=path)
    assert cfg.dest_dir == "/EnvWins", f"dest_dir: {cfg.dest_dir!r}"


def test_xdg_default_location():
    with tempfile.TemporaryDirectory() as td:
        xdg = Path(td)
        (xdg / "hf-rclone-streamer").mkdir()
        (xdg / "hf-rclone-streamer" / "config.yaml").write_text(
            "cache_dir: /tmp/from_xdg\n"
        )
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}):
            cfg = Config()
    assert cfg.cache_dir == "/tmp/from_xdg", f"cache_dir: {cfg.cache_dir!r}"


def test_missing_file_falls_back_to_defaults():
    with tempfile.TemporaryDirectory() as td:
        xdg = Path(td)  # empty XDG dir: no config.yaml anywhere
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}):
            cfg = Config()
    assert cfg.dest_dir == "/Models", f"dest_dir: {cfg.dest_dir!r}"


def test_legacy_json_config_warns():
    """config.json beside a missing config.yaml ⇒ warn (settings silently lost otherwise)."""
    import warnings
    with tempfile.TemporaryDirectory() as td:
        xdg = Path(td) / "xdg"
        (xdg / "hf-rclone-streamer").mkdir(parents=True)
        (xdg / "hf-rclone-streamer" / "config.json").write_text('{"dest_dir": "/Legacy"}')
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                cfg = Config()
    assert any("config.json" in str(w.message) for w in caught), [
        str(w.message) for w in caught
    ]
    assert cfg.dest_dir == "/Models", f"dest_dir: {cfg.dest_dir!r}"


def test_missing_explicit_config_warns():
    """An explicit --config path that does not exist must not fail silently."""
    import warnings
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        Config(config_file=Path("/nonexistent/nope.yaml"))
    assert any("nope.yaml" in str(w.message) for w in caught), [
        str(w.message) for w in caught
    ]


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    run()
