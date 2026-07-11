"""Cancellation support for long-running transfer subprocesses.

Transfers spawn aria2c (download) and rclone (upload) subprocesses that can run
for minutes on a multi-gigabyte shard. Without explicit cancellation, a Ctrl-C
(or a ``kill -INT`` to the Python process) leaves those subprocesses alive while
the upload worker's ``ThreadPoolExecutor`` shutdown blocks waiting for them — so
the process appears to hang until the in-flight transfer finishes.

A :class:`CancelToken` flips that around: every transfer subprocess registers
itself (started in its own session so terminal signals don't reach it directly),
and on cancellation we terminate each registered process promptly. The token is
the single source of truth for "should this subprocess still be running".
"""

import subprocess
import threading
from typing import List

# Grace period (seconds) between SIGTERM and SIGKILL when terminating a
# subprocess. aria2c/rclone exit on SIGTERM within milliseconds, so this only
# ever elapses for a wedged process.
_TERMINATE_GRACE_SECONDS = 2.0


class CancelToken:
    """Tracks cancellation state and the set of active transfer subprocesses.

    Thread-safe: the download runs on the main thread, the upload on a worker
    thread, and cancellation is requested from the main thread on Ctrl-C.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._procs: List[subprocess.Popen] = []

    def is_set(self) -> bool:
        """Whether cancellation has been requested."""
        return self._event.is_set()

    def register(self, proc: subprocess.Popen) -> subprocess.Popen:
        """Register a subprocess for termination on cancel.

        Pass the result of ``subprocess.Popen(..., start_new_session=True)``.
        If cancellation already fired, the process is terminated immediately.
        """
        with self._lock:
            if self._event.is_set():
                terminate(proc)
            else:
                self._procs.append(proc)
        return proc

    def unregister(self, proc: subprocess.Popen) -> None:
        """Drop a subprocess that has finished (so we don't signal a stale one)."""
        with self._lock:
            try:
                self._procs.remove(proc)
            except ValueError:
                pass

    def set(self) -> None:
        """Request cancellation and terminate every active subprocess."""
        self._event.set()
        with self._lock:
            procs = list(self._procs)
            self._procs.clear()
        for proc in procs:
            terminate(proc)


def terminate(proc: subprocess.Popen) -> None:
    """SIGTERM a process, escalating to SIGKILL after a short grace period."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()  # SIGTERM
    except (ProcessLookupError, OSError):
        return
    try:
        proc.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()  # SIGKILL
        except (ProcessLookupError, OSError):
            pass


# Module-level sentinel: callers that don't care about cancellation pass this,
# so call sites can do ``cancel.register(...)`` without None-checks. It is a
# no-op everywhere.
class _NullCancel:
    def is_set(self) -> bool:
        return False

    def register(self, proc: subprocess.Popen) -> subprocess.Popen:
        return proc

    def unregister(self, proc: subprocess.Popen) -> None:
        pass

    def set(self) -> None:
        pass


# Duck-typed to CancelToken; pass as the ``cancel`` arg when cancellation isn't
# needed (e.g. ad-hoc downloads not driven by TransferManager).
NO_CANCEL = _NullCancel()
