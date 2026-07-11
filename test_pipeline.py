"""Depth-1 pipeline tests for TransferManager._transfer_model_pipelined.

Run: .venv/bin/python test_pipeline.py

Stubs _download_file / _upload_file by assigning them as *instance* attributes
(lookup finds them in the instance dict, so they are called WITHOUT ``self``)
and records (kind, path, time, thread_id) events to prove the overlap: upload
of shard x runs on a single worker thread while download of shard x+1 runs on
the main thread.
"""
import os
import sys
import time
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config
from cancel import NO_CANCEL
from hf_api import FileInfo
from transfer_manager import TransferManager, TransferState

DL_DURATION = 0.15
UL_DURATION = 0.15


def make_manager(tmpdir, **overrides):
    os.environ["XDG_CONFIG_HOME"] = str(tmpdir)
    cfg = Config()
    cfg.set("cache_dir", str(tmpdir))
    cfg.set("resume", False)
    cfg.set("cleanup", False)
    for k, v in overrides.items():
        cfg.set(k, v)
    return TransferManager(config=cfg)


def seed_states(mgr, model_id, files):
    for f in files:
        key = mgr._get_state_key(model_id, f.path)
        mgr.states[key] = TransferState(
            model_id=model_id,
            filename=f.path,
            status="pending",
            total_bytes=f.size,
        )


def test_overlap():
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = make_manager(tmpdir)
        model_id = "org/pipeline-overlap"
        files = [FileInfo(path=f"f{i}.bin", size=100) for i in range(3)]
        seed_states(mgr, model_id, files)

        events = []
        lock = threading.Lock()

        def rec(kind, path):
            with lock:
                events.append((kind, path, time.time(), threading.get_ident()))

        def dl(model_id, file_info, cancel=NO_CANCEL):
            rec("DL_START", file_info.path)
            time.sleep(DL_DURATION)
            rec("DL_END", file_info.path)
            return Path(str(tmpdir))  # truthy cache path

        def ul(model_id, file_info, cache_path, dest_dir, cancel=NO_CANCEL):
            rec("UL_START", file_info.path)
            time.sleep(UL_DURATION)
            rec("UL_END", file_info.path)
            return True

        mgr._download_file = dl
        mgr._upload_file = ul

        success = mgr._transfer_model_pipelined(model_id, files, "/Models")
        assert success is True, "all stubs succeed -> success should be True"

        def t(kind, path):
            return [e[2] for e in events if e[0] == kind and e[1] == path]

        ul_a_s, ul_a_e = t("UL_START", "f0.bin")[0], t("UL_END", "f0.bin")[0]
        dl_b_s, dl_b_e = t("DL_START", "f1.bin")[0], t("DL_END", "f1.bin")[0]
        ul_b_s, ul_b_e = t("UL_START", "f1.bin")[0], t("UL_END", "f1.bin")[0]
        ul_c_s = t("UL_START", "f2.bin")[0]

        # UL(a) overlaps DL(b) -- the whole point of the pipeline.
        assert ul_a_s < dl_b_e and dl_b_s < ul_a_e, "UL(a) must overlap DL(b)"

        # Single worker: uploads never overlap each other (depth-1).
        assert ul_a_e <= ul_b_s, "uploads must not overlap"
        assert ul_b_e <= ul_c_s, "uploads must not overlap"

        # Downloads on the main thread, uploads on one distinct worker thread.
        main_tid = threading.get_ident()
        dl_tids = {e[3] for e in events if e[0].startswith("DL")}
        ul_tids = {e[3] for e in events if e[0].startswith("UL")}
        assert dl_tids == {main_tid}, "downloads must run on the main thread"
        assert len(ul_tids) == 1, "uploads must run on a single worker thread"
        assert next(iter(ul_tids)) != main_tid, "uploads must run on a worker thread"


def test_download_failure_drains():
    """A failed download still drains the prior shard's in-flight upload."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = make_manager(tmpdir)
        model_id = "org/dl-fail"
        files = [FileInfo(path=f"f{i}.bin", size=100) for i in range(3)]
        seed_states(mgr, model_id, files)

        uploads = []

        def dl(model_id, file_info, cancel=NO_CANCEL):
            if file_info.path == "f1.bin":
                return None  # simulate download failure
            time.sleep(0.03)
            return Path(str(tmpdir))

        def ul(model_id, file_info, cache_path, dest_dir, cancel=NO_CANCEL):
            uploads.append(file_info.path)
            time.sleep(0.03)
            return True

        mgr._download_file = dl
        mgr._upload_file = ul

        success = mgr._transfer_model_pipelined(model_id, files, "/Models")
        assert success is False, "a download failure -> success False"
        assert "f0.bin" in uploads, "the prior shard's upload must still be drained"


def test_upload_failure_surfaces():
    """An upload failure surfaces one iteration late via _drain."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = make_manager(tmpdir)
        model_id = "org/ul-fail"
        files = [FileInfo(path=f"f{i}.bin", size=100) for i in range(3)]
        seed_states(mgr, model_id, files)

        attempted = []

        def dl(model_id, file_info, cancel=NO_CANCEL):
            time.sleep(0.02)
            return Path(str(tmpdir))

        def ul(model_id, file_info, cache_path, dest_dir, cancel=NO_CANCEL):
            attempted.append(file_info.path)
            time.sleep(0.02)
            return file_info.path != "f0.bin"  # f0's upload fails

        mgr._download_file = dl
        mgr._upload_file = ul

        success = mgr._transfer_model_pipelined(model_id, files, "/Models")
        assert success is False, "an upload failure surfaces -> success False"
        assert "f0.bin" in attempted, "f0's upload must have been attempted"


def main():
    for fn in (test_overlap, test_download_failure_drains, test_upload_failure_surfaces):
        fn()
        print(f"PASS {fn.__name__}")
    print("All test_pipeline tests passed.")


if __name__ == "__main__":
    main()
