"""Concurrency tests for the _state_lock around self.states.

Two cases:

1. ``test_concurrent_state_writes`` — two threads hammer ``_update_state``
   (which calls ``_save_state`` -> ``json.dump`` under the lock) 400 times each.
   Without the lock the truncated/interleaved writes would make ``json.load``
   raise.

2. ``test_get_progress_concurrent_with_state_growth`` — a reader thread calls
   ``get_progress()`` (which iterates ``self.states`` via
   ``_calculate_progress`` under ``_state_lock``) while a writer adds/clears
   keys (mirroring ``transfer_model``'s state-init loop and ``clear_state``).
   Without the reader-side lock the size change mid-iteration raises
   ``RuntimeError: dictionary changed size during iteration``.

Run: .venv/bin/python test_lock.py
"""
import os
import sys
import json
import time
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config
from transfer_manager import TransferManager, TransferState


def test_concurrent_state_writes(mgr):
    """Two threads hammer _update_state; the on-disk JSON must stay valid."""
    keys = [f"m/file{i}.bin" for i in range(2)]
    for k in keys:
        mgr.states[k] = TransferState(
            model_id="m",
            filename=k.split("/")[-1],
            status="downloading",
            total_bytes=1000,
        )
    mgr._save_state()

    def worker(key):
        for i in range(400):
            mgr._update_state(key, downloaded_bytes=i)

    t1 = threading.Thread(target=worker, args=(keys[0],))
    t2 = threading.Thread(target=worker, args=(keys[1],))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    with open(mgr.state_file) as f:
        data = json.load(f)  # must not raise
    assert all(k in data for k in keys), "both keys must be present"


def test_get_progress_concurrent_with_state_growth(mgr):
    """A reader calling get_progress() must survive a writer changing dict size.

    _calculate_progress iterates self.states five times; a writer adding or
    clearing keys changes dict size during that iteration. _state_lock on BOTH
    the reader (taken inside _calculate_progress) and the writer is what keeps
    it from raising ``RuntimeError: dictionary changed size during iteration``.
    The writer must change SIZE — a mere attribute update wouldn't trigger it.
    """
    for i in range(4):
        mgr.states[f"m/seed{i}.bin"] = TransferState(
            model_id="m",
            filename=f"seed{i}.bin",
            status="downloading",
            total_bytes=1000,
        )

    errors = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                mgr.get_progress()
            except Exception as e:  # captured for the assertion below
                errors.append(e)
                return

    def writer():
        n = 0
        while not stop.is_set():
            # Mirror the locked size-changing writers in transfer_manager:
            # transfer_model adds keys, clear_state clears — both under
            # _state_lock. Bound the dict so memory stays small while still
            # churning its size every iteration.
            with mgr._state_lock:
                if len(mgr.states) >= 2000:
                    mgr.states.clear()
                mgr.states[f"m/grown{n}.bin"] = TransferState(
                    model_id="m",
                    filename=f"grown{n}.bin",
                    status="pending",
                    total_bytes=1000,
                )
                n += 1

    t1 = threading.Thread(target=reader)
    t2 = threading.Thread(target=writer)
    t1.start()
    t2.start()

    time.sleep(0.5)
    stop.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not errors, f"reader raised during concurrent state growth: {errors!r}"


def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["XDG_CONFIG_HOME"] = str(tmpdir)
        cfg = Config()
        cfg.set("cache_dir", str(tmpdir))
        mgr = TransferManager(config=cfg)

        test_concurrent_state_writes(mgr)
        print("PASS test_concurrent_state_writes (800 writes, JSON valid)")

        test_get_progress_concurrent_with_state_growth(mgr)
        print("PASS test_get_progress_concurrent_with_state_growth (no RuntimeError)")

    print("All test_lock tests passed.")


if __name__ == "__main__":
    main()
