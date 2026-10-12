"""Tests for the stdout output coordinator (_print_status / finish_progress_line).

The live progress line is drawn with a carriage return and no trailing newline,
so the cursor parks at the end of the ETA. Status messages ('Downloading…',
'Complete:', …) must start on their own row instead of gluing onto the parked
line:

    … | ETA 11h33mComplete: model-00055-of-00282.safetensors   ← the bug

``_print_status`` closes an open progress line first (a newline when output is
redirected; a clear-and-rewrite on a TTY) and serializes with the progress draw
under ``_output_lock``. ``_notify_progress`` marks the line open after each draw;
``finish_progress_line`` closes it at transfer end.

Under ``redirect_stdout`` the stream is not a TTY, so the non-TTY ``\n`` branch
is exercised deterministically.

Run: .venv/bin/python test_output.py
"""
import os
import sys
import io
import contextlib
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config
from transfer_manager import TransferManager, TransferState


class _TTYBuffer(io.StringIO):
    """A StringIO that reports itself as a terminal, to exercise the TTY branch."""

    def isatty(self):
        return True


def test_status_closes_open_progress_line(mgr):
    """A status print after a drawn progress line must break to a new row.

    Before the fix, 'Complete: x' glued onto '… ETA 11h33m'. _print_status now
    emits a leading newline (non-TTY) when a progress line is open, so the
    status lands on its own line.
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mgr._progress_line_open = True  # simulate a parked progress line
        mgr._print_status("Complete: model-00055-of-00282.safetensors")
    out = buf.getvalue()
    assert out == "\nComplete: model-00055-of-00282.safetensors\n", repr(out)


def test_status_no_spurious_blank_line(mgr):
    """No open progress line ⇒ no leading newline (no spurious blank line)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mgr._progress_line_open = False
        mgr._print_status("Downloading model-00057-of-00282.safetensors...")
    out = buf.getvalue()
    assert out == "Downloading model-00057-of-00282.safetensors...\n", repr(out)


def test_print_status_resets_open_flag(mgr):
    """After a status print that closed a line, _progress_line_open is False."""
    mgr._progress_line_open = True
    with contextlib.redirect_stdout(io.StringIO()):
        mgr._print_status("Complete: x")
    assert mgr._progress_line_open is False


def test_notify_progress_opens_line(mgr):
    """A progress-callback draw marks the line open for the next status print."""
    mgr._progress_line_open = False
    seen = {}

    def cb(progress):
        seen["called"] = True

    mgr.progress_callback = cb
    mgr.states["m/f.bin"] = TransferState(
        model_id="m", filename="f.bin", status="downloading", total_bytes=10
    )
    with contextlib.redirect_stdout(io.StringIO()):
        mgr._notify_progress()
    assert seen.get("called") is True
    assert mgr._progress_line_open is True


def test_finish_progress_line(mgr):
    """finish_progress_line closes an open line; no-op when already closed."""
    mgr._progress_line_open = True
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mgr.finish_progress_line()
    assert buf.getvalue() == "\n", repr(buf.getvalue())
    assert mgr._progress_line_open is False

    # Already closed ⇒ no-op, no spurious newline.
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        mgr.finish_progress_line()
    assert buf2.getvalue() == "", repr(buf2.getvalue())
    assert mgr._progress_line_open is False


def test_status_tty_closes_with_clear(mgr):
    """On a TTY, a status after a drawn line clears the row (\\r + erase) first.

    The redirected branch emits a bare newline; the TTY branch — the one users
    actually see — must emit \\r then erase-to-end-of-line so no stale ETA tail
    survives a shorter status. This branch was previously untested.
    """
    mgr._progress_line_open = True
    buf = _TTYBuffer()
    with contextlib.redirect_stdout(buf):
        mgr._print_status("Complete: model-00055-of-00282.safetensors")
    out = buf.getvalue()
    assert out == "\r\033[KComplete: model-00055-of-00282.safetensors\n", repr(out)
    assert mgr._progress_line_open is False


def test_status_tty_no_clear_when_line_closed(mgr):
    """On a TTY with no open line, a status prints plainly (no stray clear)."""
    mgr._progress_line_open = False
    buf = _TTYBuffer()
    with contextlib.redirect_stdout(buf):
        mgr._print_status("Downloading x...")
    out = buf.getvalue()
    assert out == "Downloading x...\n", repr(out)


def test_finish_line_tty_preserves_final_row(mgr):
    """finish_progress_line on a TTY emits a bare newline, keeping the 100% line.

    Unlike _print_status (which clears to rewrite the row), transfer end must NOT
    erase — the final progress line should stay on screen as the last output.
    """
    mgr._progress_line_open = True
    buf = _TTYBuffer()
    with contextlib.redirect_stdout(buf):
        mgr.finish_progress_line()
    out = buf.getvalue()
    assert out == "\n", repr(out)
    assert mgr._progress_line_open is False


def test_cleanup_status_closes_open_line(mgr):
    """Cleanup messages must close an open progress line, not glue onto it.

    `_cleanup_cache` used a bare print(), so "Cleaned up: …" glued onto the parked
    ETA line — the exact bug this module fixes everywhere else. It is now routed
    through _print_status (safe under the _state_lock -> _output_lock order).
    """
    # Seed a cached file matching the model prefix so _cleanup_cache unlinks it.
    marker = mgr.cache_dir / "mymodel_shard.bin"
    marker.write_bytes(b"x")
    mgr._progress_line_open = True
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mgr._cleanup_cache("mymodel")
    out = buf.getvalue()
    assert "Cleaned up: mymodel_shard.bin" in out, repr(out)
    # The open line was closed first (leading newline on redirect), not glued.
    assert out.startswith("\n"), repr(out)
    assert mgr._progress_line_open is False
    assert not marker.exists()


def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["XDG_CONFIG_HOME"] = str(tmpdir)
        cfg = Config()
        cfg.set("cache_dir", str(tmpdir))
        mgr = TransferManager(config=cfg)

        test_status_closes_open_progress_line(mgr)
        print("PASS test_status_closes_open_progress_line")

        test_status_no_spurious_blank_line(mgr)
        print("PASS test_status_no_spurious_blank_line")

        test_print_status_resets_open_flag(mgr)
        print("PASS test_print_status_resets_open_flag")

        test_notify_progress_opens_line(mgr)
        print("PASS test_notify_progress_opens_line")

        test_finish_progress_line(mgr)
        print("PASS test_finish_progress_line")

        test_status_tty_closes_with_clear(mgr)
        print("PASS test_status_tty_closes_with_clear")

        test_status_tty_no_clear_when_line_closed(mgr)
        print("PASS test_status_tty_no_clear_when_line_closed")

        test_finish_line_tty_preserves_final_row(mgr)
        print("PASS test_finish_line_tty_preserves_final_row")

        test_cleanup_status_closes_open_line(mgr)
        print("PASS test_cleanup_status_closes_open_line")

    print("All test_output tests passed.")


if __name__ == "__main__":
    main()
