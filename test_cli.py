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


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    run()
