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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config
from transfer_manager import TransferManager, TransferState


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

    print("All test_output tests passed.")


if __name__ == "__main__":
    main()
