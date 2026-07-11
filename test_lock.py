"""Concurrent _update_state writes must keep the state file valid JSON.

Exercises the _state_lock: two threads hammer _update_state (which calls
_save_state -> json.dump under the lock) 400 times each. Without the lock the
truncated/interleaved writes would make json.load raise.

Run: .venv/bin/python test_lock.py
"""
import os
import sys
import json
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config
from transfer_manager import TransferManager, TransferState


def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["XDG_CONFIG_HOME"] = str(tmpdir)
        cfg = Config()
        cfg.set("cache_dir", str(tmpdir))
        mgr = TransferManager(config=cfg)

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

        print("PASS test_concurrent_state_writes (800 writes, JSON valid)")
        print("All test_lock tests passed.")


if __name__ == "__main__":
    main()
