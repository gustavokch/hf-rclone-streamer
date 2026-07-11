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
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rclone_client import _parse_free_space, _parse_size_value, copy_direct


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


class _FakePopen:
    """Stand-in for subprocess.Popen: yields canned rclone stdout lines.

    copy_direct only needs ``stdout`` (iterable), ``wait()`` and ``poll()``
    (non-None so the ``terminate()`` cleanup is skipped).
    """

    def __init__(self, lines):
        self.stdout = list(lines)
        self.returncode = 0

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode


def test_rclone_transferred_bytes():
    """The parser must read rclone-fuse's label-less stats format.

    rclone-fuse emits e.g. ``2.930 GiB / 5.859 GiB, 50%, 0 B/s, ETA -`` with
    NO ``Transferred:`` prefix — the old ``Transferred:.*?%`` regex never
    matched this, so uploads reported no progress.
    """
    from rclone_client import _rclone_transferred_bytes as f
    GiB = 1024 ** 3

    assert f("2.930 GiB / 5.859 GiB, 50%, 0 B/s, ETA -") == int(2.930 * GiB)
    assert f("1.000 GiB / 5.000 GiB, 20%, 10 MiB/s, ETA 7m") == 1 * GiB
    assert f("0 B / 5.000 GiB, 0%, 0 B/s, ETA -") == 0

    # Lines without a ``X unit / Y unit`` transfer field → None (caller skips).
    assert f("Transferred: 1 / 1, 100%") is None   # file-count line, no bytes
    assert f("Checks: 2 / 2, 100%") is None
    assert f("0 B/s, ETA -") is None               # rate field, not transfer

    # A matched-but-garbage number (the ``[\d.]+`` group admits multi-dot
    # strings) must be skipped, not fed to the estimator as a 0-byte tick.
    # This runs on every rclone stdout line, so it must not raise either.
    assert f("1.2.3 GiB / 5 GiB, 50%") is None


def test_parse_size_value_never_raises():
    """The shared size primitive must not raise on bad input.

    It runs on every rclone stdout line (via ``_rclone_transferred_bytes``) and
    inside ``_parse_free_space``; a stray ``ValueError`` would abort a transfer
    mid-stream. Non-numeric → 0 (the "unknown" sentinel), matching unknown
    units.
    """
    assert _parse_size_value("abc", "GiB") == 0
    assert _parse_size_value("1.2.3", "GiB") == 0
    # Known-good still works.
    assert _parse_size_value("2.930", "GiB") == int(2.930 * 1024 ** 3)


def test_copy_direct_reports_progress_real_format():
    """copy_direct must stream per-second progress via NOTICE stats.

    Two regression guards in one, both covering the live "UL 0 B/s" bug:

    1. Flags must use ``--stats-log-level NOTICE`` and NOT ``--progress``.
       rclone ``-P`` to a non-TTY pipe updates in place with ``\\r`` and
       block-buffers the whole run, flushing only at exit — so no mid-run tick
       ever arrives and ``uploaded_bytes`` stays flat (the live bug, confirmed
       by the state file showing a 117s-stale upload shard). NOTICE is
       newline-flushed per second and streams (empirically verified).
    2. The parser must read the NOTICE one-line transfer field and skip the
       startup banner line (no ``X / Y`` byte pair → None).
    """
    lines = [
        "2026/07/11 16:03:35 NOTICE: drive-gkch{abc}: This remote uses shared drive",
        "2026/07/11 16:03:36 NOTICE:           1.000 GiB / 5.000 GiB, 20%, 10 MiB/s, ETA 7m",
        "2026/07/11 16:03:37 NOTICE:           2.000 GiB / 5.000 GiB, 40%, 10 MiB/s, ETA 6m",
        "2026/07/11 16:03:38 NOTICE:           3.000 GiB / 5.000 GiB, 60%, 10 MiB/s, ETA 4m",
        "2026/07/11 16:03:39 NOTICE:           4.000 GiB / 5.000 GiB, 80%, 10 MiB/s, ETA 2m",
    ]
    captured = []
    captured_cmd = []
    fake = _FakePopen(lines)

    def fake_popen(cmd, *args, **kwargs):
        captured_cmd.append(cmd)
        return fake

    # Tiny real file as src: copy_direct reads its size but (with Popen
    # patched) never actually transfers it.
    fd, path = tempfile.mkstemp()
    os.write(fd, b"x" * 100)
    os.close(fd)
    try:
        with mock.patch("subprocess.Popen", side_effect=fake_popen), \
                mock.patch("rclone_client.check_rclone_available"):
            copy_direct(
                src=Path(path),
                remote="drive-gkch",
                dest_path="/Models/x.bin",
                progress_callback=lambda copied, total: captured.append(copied),
            )
    finally:
        os.unlink(path)

    # Flag guard: NOTICE streaming, not -P buffering.
    cmd = captured_cmd[0]
    assert "--stats-log-level" in cmd and "NOTICE" in cmd, f"flags missing NOTICE: {cmd}"
    assert "--progress" not in cmd, f"--progress re-introduces buffer-to-EOF: {cmd}"

    # Parser guard: banner skipped (None), ticks 1-4 fire. Drop the leading (0)
    # and trailing (total) bookends copy_direct emits around the loop.
    ticks = captured[1:-1]
    expected = [int(g * 1024 ** 3) for g in (1, 2, 3, 4)]
    assert ticks == expected, f"per-tick callbacks {ticks!r} != {expected!r} (full: {captured!r})"


def main():
    test_binary_suffixes()
    test_full_style_suffixes()
    test_whitespace_and_pib()
    test_unknown_unit_is_zero()
    test_missing_or_malformed()
    test_rclone_transferred_bytes()
    test_parse_size_value_never_raises()
    test_copy_direct_reports_progress_real_format()
    print("All test_rclone_client tests passed.")


if __name__ == "__main__":
    main()
