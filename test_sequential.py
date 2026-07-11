"""pipeline=False preserves strict download->upload per file (no overlap).

Drives the real transfer_model sequential branch by stubbing the network
(get_model_info) and rclone (get_free_space) module globals, plus the per-file
download/upload. The recorded event order must be fully serialized: for each
file, DL immediately followed by UL, with no interleaving across files (which
is what the pipelined path would produce).

Run: .venv/bin/python test_sequential.py
"""
import os
import sys
import time
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config
from cancel import NO_CANCEL
import transfer_manager as tm
from hf_api import ModelInfo, FileInfo
from transfer_manager import TransferManager


def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["XDG_CONFIG_HOME"] = str(tmpdir)
        cfg = Config()
        cfg.set("cache_dir", str(tmpdir))
        cfg.set("pipeline", False)
        cfg.set("resume", False)
        cfg.set("cleanup", False)
        mgr = TransferManager(config=cfg)

        events = []

        def dl(model_id, file_info, cancel=NO_CANCEL):
            events.append(("DL", file_info.path))
            time.sleep(0.01)
            return Path(str(tmpdir))

        def ul(model_id, file_info, cache_path, dest_dir, cancel=NO_CANCEL):
            events.append(("UL", file_info.path))
            time.sleep(0.01)
            return True

        mgr._download_file = dl
        mgr._upload_file = ul

        files = [FileInfo(path=f"f{i}.bin", size=100) for i in range(3)]
        # Stub network/rclone so transfer_model runs its sequential loop.
        tm.get_model_info = lambda **kw: ModelInfo(
            model_id=kw["model_id"], files=files, total_size=300
        )
        tm.get_free_space = lambda *a, **k: 10 ** 12

        success = mgr.transfer_model("m/seq", dest_dir="/Models")
        assert success is True, "all stubs succeed -> success should be True"

        expected = []
        for i in range(3):
            expected.append(("DL", f"f{i}.bin"))
            expected.append(("UL", f"f{i}.bin"))
        assert events == expected, f"sequential order violated: {events}"

        print("PASS test_sequential_strict_order")
        print("All test_sequential tests passed.")


if __name__ == "__main__":
    main()
