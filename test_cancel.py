"""Ctrl-C cancellation tests for the pipelined + sequential transfer paths.

The transfer subprocesses (aria2c/rclone) are started with start_new_session,
so a terminal Ctrl-C does NOT reach them -- only the CancelToken does. These
tests prove that on cancellation:

  1. the in-flight subprocess is terminated within seconds (not left running
     for its full duration), because the inner KeyboardInterrupt handler calls
     cancel.set() before the executor's shutdown(wait=True);
  2. _transfer_model_pipelined re-raises KeyboardInterrupt (does not swallow);
  3. transfer_model's Ctrl-C wrapper saves state and re-raises.

We simulate Ctrl-C by having the stubbed _download_file raise KeyboardInterrupt
on the second shard (after registering a real long `sleep 30` subprocess on the
token, exactly as aria2c/rclone register themselves).

Run: .venv/bin/python test_cancel.py
"""
import os
import sys
import json
import time
import tempfile
import subprocess
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config
from cancel import NO_CANCEL
import transfer_manager as tm
from hf_api import ModelInfo, FileInfo
from transfer_manager import TransferManager, TransferState


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
            model_id=model_id, filename=f.path,
            status="pending", total_bytes=f.size,
        )


def _assert_dead(proc, grace=3.0):
    """proc must be terminated within `grace` seconds (not its full runtime)."""
    deadline = time.time() + grace
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.05)
    assert proc.poll() is not None, \
        "registered subprocess must be terminated by cancel.set(), not left running"


def test_pipelined_cancel_terminates_and_raises():
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = make_manager(tmpdir)  # pipeline defaults to True
        model_id = "org/cancel-pipelined"
        files = [FileInfo(path=f"f{i}.bin", size=100) for i in range(3)]
        seed_states(mgr, model_id, files)

        procs = []

        def dl(model_id, file_info, cancel=NO_CANCEL):
            # Mimic aria2c/rclone: a long subprocess in its own session,
            # registered on the (real) cancel token.
            proc = cancel.register(subprocess.Popen(
                ["sleep", "30"], start_new_session=True,
            ))
            procs.append(proc)
            if file_info.path == "f1.bin":
                raise KeyboardInterrupt  # simulate Ctrl-C mid-transfer
            time.sleep(0.02)
            cancel.unregister(proc)
            proc.terminate()
            return Path(str(tmpdir))

        def ul(model_id, file_info, cache_path, dest_dir, cancel=NO_CANCEL):
            time.sleep(0.02)
            return True

        mgr._download_file = dl
        mgr._upload_file = ul

        start = time.time()
        raised = False
        try:
            mgr._transfer_model_pipelined(model_id, files, "/Models")
        except KeyboardInterrupt:
            raised = True
        elapsed = time.time() - start

        assert raised, "pipelined transfer must raise KeyboardInterrupt on cancel"
        # Must not hang on the executor's shutdown(wait=True).
        assert elapsed < 5.0, f"unexpectedly slow unwind: {elapsed:.1f}s"
        # The sleep-30 subprocess must be dead within seconds, proving the inner
        # handler called cancel.set() -> terminate() before shutdown ran.
        _assert_dead(procs[-1])
        print("PASS test_pipelined_cancel_terminates_and_raises")


def test_transfer_model_cancel_saves_state_and_raises():
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = make_manager(tmpdir)  # pipeline True
        model_id = "m/cancel-tm"
        files = [FileInfo(path=f"f{i}.bin", size=100) for i in range(3)]

        long_proc = {}

        def dl(model_id, file_info, cancel=NO_CANCEL):
            proc = cancel.register(subprocess.Popen(
                ["sleep", "30"], start_new_session=True,
            ))
            long_proc["proc"] = proc
            if file_info.path == "f1.bin":
                raise KeyboardInterrupt
            time.sleep(0.02)
            cancel.unregister(proc)
            proc.terminate()
            return Path(str(tmpdir))

        def ul(model_id, file_info, cache_path, dest_dir, cancel=NO_CANCEL):
            time.sleep(0.02)
            return True

        mgr._download_file = dl
        mgr._upload_file = ul
        tm.get_model_info = lambda **kw: ModelInfo(
            model_id=kw["model_id"], files=files, total_size=300
        )
        tm.get_free_space = lambda *a, **k: 10 ** 12

        raised = False
        try:
            mgr.transfer_model(model_id, dest_dir="/Models")
        except KeyboardInterrupt:
            raised = True

        assert raised, "transfer_model must re-raise KeyboardInterrupt"
        # State must be checkpointed (valid JSON) so a resume run can pick up.
        assert mgr.state_file.exists(), "state file must be saved on cancel"
        with open(mgr.state_file) as f:
            json.load(f)  # must not raise
        _assert_dead(long_proc["proc"])
        print("PASS test_transfer_model_cancel_saves_state_and_raises")


def main():
    test_pipelined_cancel_terminates_and_raises()
    test_transfer_model_cancel_saves_state_and_raises()
    print("All test_cancel tests passed.")


if __name__ == "__main__":
    main()
