"""Unit tests for rclone_client._parse_free_space.

``rclone about`` emits the ``Free`` field in two suffix styles depending on
flags:
  default:  "4.999TiB"   (binary suffix)
  --full:   "12.5GBytes" (decimal-style suffix)

_parse_free_space must handle both, and an unrecognized unit (e.g. a future
"EiB") must yield 0 (treated as unknown) rather than a handful of bytes.

Run: .venv/bin/python test_rclone_client.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rclone_client import _parse_free_space


def check(name, free_str, expected):
    got = _parse_free_space({"Free": free_str})
    assert got == expected, f"{name}: {free_str!r} -> {got}, expected {expected}"


def test_binary_suffixes():
    check("TiB", "4.999TiB", int(4.999 * 1024**4))
    check("GiB", "12.5GiB", int(12.5 * 1024**3))
    check("MiB", "500MiB", int(500 * 1024**2))
    check("KiB", "8KiB", int(8 * 1024))
    check("plain B at 0", "0B", 0)
    check("plain B", "1024B", 1024)


def test_full_style_suffixes():
    check("GBytes", "12.5GBytes", int(12.5 * 1024**3))
    check("TBytes", "1TBytes", 1024**4)
    check("MBytes", "200MBytes", int(200 * 1024**2))


def test_whitespace_and_pib():
    check("space before unit", "3 PiB", int(3 * 1024**5))
    check("PiB no space", "3PiB", int(3 * 1024**5))


def test_unknown_unit_is_zero():
    # An unknown unit must NOT be misread as bytes (value * 1).
    assert _parse_free_space({"Free": "5 EiB"}) == 0
    assert _parse_free_space({"Free": "17 frobnicates"}) == 0


def test_missing_or_malformed():
    assert _parse_free_space({}) == 0
    assert _parse_free_space({"Free": "not-a-number"}) == 0


def main():
    test_binary_suffixes()
    test_full_style_suffixes()
    test_whitespace_and_pib()
    test_unknown_unit_is_zero()
    test_missing_or_malformed()
    print("All test_rclone_client tests passed.")


if __name__ == "__main__":
    main()
