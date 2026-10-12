"""Unit tests for the CLI parser and the quant -> include-pattern helper.

Seams under test:
- ``create_parser()``: `download` accepts an optional model ID, `--quant`
  parses, global ``--config`` parses.
- ``quant_include_pattern()``: pure translation of a quant tag into the
  GGUF include pattern used by the download filter.

Run: .venv/bin/python test_cli.py
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hf_rclone_streamer import create_parser, quant_include_pattern


def test_download_model_id_optional():
    parser = create_parser()
    args = parser.parse_args(["download"])
    assert args.model_id is None, f"model_id: {args.model_id!r}"
    assert args.quant is None, f"quant: {args.quant!r}"


def test_download_model_id_positional():
    parser = create_parser()
    args = parser.parse_args(["download", "meta-llama/Llama-3.1-8B"])
    assert args.model_id == "meta-llama/Llama-3.1-8B", f"model_id: {args.model_id!r}"


def test_download_quant_flag():
    parser = create_parser()
    args = parser.parse_args(["download", "--quant", "Q4_K_XL"])
    assert args.quant == "Q4_K_XL", f"quant: {args.quant!r}"


def test_global_config_flag():
    parser = create_parser()
    args = parser.parse_args(["--config", "/tmp/c.yaml", "download"])
    assert args.config == "/tmp/c.yaml", f"config: {args.config!r}"


def test_quant_pattern_basic():
    assert quant_include_pattern("Q4_K_XL") == ["*Q4_K_XL*.gguf"], (
        quant_include_pattern("Q4_K_XL")
    )


def test_quant_pattern_uppercases():
    assert quant_include_pattern("q4_k_xl") == ["*Q4_K_XL*.gguf"], (
        quant_include_pattern("q4_k_xl")
    )


def test_quant_pattern_none():
    assert quant_include_pattern(None) is None


def test_help_shows_configured_default_model():
    """`download --help` must reference the live default repo, not a stale literal."""
    import argparse
    from config import Config
    parser = create_parser()
    subaction = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    download_help = subaction.choices["download"].format_help()
    # HelpFormatter wraps long tokens across lines; drop all whitespace to
    # reassemble the default repo ID before matching.
    flat = "".join(download_help.split())
    assert Config.DEFAULTS["default_model_id"] in flat, download_help


def test_build_config_quant_flag_sets_config():
    """--quant on `download` must land in the built config (main() wiring)."""
    from hf_rclone_streamer import build_config  # not extracted yet → red
    with tempfile.TemporaryDirectory() as td:
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": td}, clear=True):
            args = create_parser().parse_args(["download", "--quant", "q4_k_xl"])
            cfg = build_config(args)
    assert cfg.quant == "q4_k_xl", f"quant: {cfg.quant!r}"


def test_build_config_explicit_yaml_loaded():
    """--config <path> must load that YAML file into the built config."""
    from hf_rclone_streamer import build_config  # not extracted yet → red
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "c.yaml"
        cfg_path.write_text("dest_dir: /Models/X\n")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": td}, clear=True):
            args = create_parser().parse_args(["--config", str(cfg_path), "download"])
            cfg = build_config(args)
    assert cfg.dest_dir == "/Models/X", f"dest_dir: {cfg.dest_dir!r}"


def test_build_config_env_dest_dir_survives():
    """HF_RCLONE_DEST_DIR must survive when the --dest-dir flag is absent."""
    from hf_rclone_streamer import build_config
    with tempfile.TemporaryDirectory() as td:
        with mock.patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": td, "HF_RCLONE_DEST_DIR": "/EnvWins"},
            clear=True,
        ):
            args = create_parser().parse_args(["download"])
            cfg = build_config(args)
    assert cfg.dest_dir == "/EnvWins", f"dest_dir: {cfg.dest_dir!r}"


def test_build_config_cli_dest_dir_wins_over_yaml():
    """An explicit --dest-dir flag must override the YAML file."""
    from hf_rclone_streamer import build_config
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "c.yaml"
        cfg_path.write_text("dest_dir: /Models/X\n")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": td}, clear=True):
            args = create_parser().parse_args(
                ["--config", str(cfg_path), "--dest-dir", "/CliWins", "download"]
            )
            cfg = build_config(args)
    assert cfg.dest_dir == "/CliWins", f"dest_dir: {cfg.dest_dir!r}"


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    run()
