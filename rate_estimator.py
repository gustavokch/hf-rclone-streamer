"""Transfer-rate estimation from the cumulative-bytes time series.

The streamer's progress signal is a stream of ``(timestamp, cumulative_bytes)``
samples flowing through ``_update_state`` → ``_notify_progress``. None of the
three transfer paths is a reliable source of an instantaneous speed string —
aria2c and rclone ``copyto`` emit speeds in their own formats, and the
Python-I/O ``copy_to_mount`` path emits *no* speed at all — so the rate is
*derived* from the cumulative bytes instead. That derivation covers all three
paths uniformly, because every path already reports cumulative bytes through
its progress callback.

A :class:`RateEstimator` keeps a short rolling history of samples and exposes
both a responsive ~5s current rate and a stable 60s median rate, plus a blended
rate suitable for a non-jumpy completion ETA. ``timestamp`` is an explicit
parameter (callers pass ``time.time()``) so tests inject synthetic clocks
deterministically — no real-clock reliance inside the estimator.
"""

import threading
from collections import deque
from typing import Deque, List, Optional, Tuple


class RateEstimator:
    """Estimate transfer rate from a cumulative-bytes time series.

    One instance per stream (download, upload). Thread-safe via an internal
    lock: ``sample()`` is called from ``_notify_progress`` under the transfer
    manager's ``_state_lock`` (download on the main thread, upload on a worker
    thread), while ``current_rate``/``median_rate``/``eta`` are also read from
    ``get_progress`` *without* that lock. Lock order is always
    ``_state_lock → estimator lock`` (the estimator never touches
    ``_state_lock``), so there is no deadlock.
    """

    def __init__(
        self,
        window_seconds: float = 60.0,
        min_sample_interval: float = 0.25,
        current_window_seconds: float = 5.0,
    ) -> None:
        self._window_seconds = window_seconds
        self._min_sample_interval = min_sample_interval
        self._current_window_seconds = current_window_seconds
        self._samples: Deque[Tuple[float, int]] = deque()
        self._lock = threading.Lock()

    def sample(self, timestamp: float, cumulative_bytes: int) -> None:
        """Record a cumulative-bytes sample at ``timestamp``.

        Samples closer than ``min_sample_interval`` update the last sample's
        bytes in-place (preserving its timestamp) rather than appending, so the
        per-1MB-chunk callbacks from ``copy_to_mount`` can't explode the deque.
        If bytes go backwards (a retry/reset on the aggregated sum) the history
        is irrelevant and is cleared.
        """
        with self._lock:
            if self._samples and cumulative_bytes < self._samples[-1][1]:
                # Retry/reset: the running total dropped, so old rate history
                # no longer describes the current transfer.
                self._samples.clear()

            if self._samples:
                last_t, _ = self._samples[-1]
                if timestamp - last_t < self._min_sample_interval:
                    # Throttle: refresh the tail's bytes in place, keeping its
                    # timestamp. (Updating the timestamp too would advance the
                    # tail every call and the deque would never grow past one
                    # entry.) Cumulative bytes are monotonic within an
                    # interval, so the latest value is the most accurate.
                    self._samples[-1] = (last_t, cumulative_bytes)
                    return

            self._samples.append((timestamp, cumulative_bytes))

            # Prune to the trailing window. Drop everything strictly older than
            # now - window, always keeping at least the newest sample. With the
            # 0.25s throttle this caps the deque at ~240 entries.
            cutoff = timestamp - self._window_seconds
            while len(self._samples) >= 2 and self._samples[0][0] < cutoff:
                self._samples.popleft()

    def current_rate(self) -> Optional[float]:
        """Secant rate over roughly the last ``current_window_seconds``.

        ``(last_bytes - ref_bytes) / (last_t - ref_t)`` where ``ref`` is the
        most recent sample at or before ``last_t - window``, falling back to the
        oldest sample when the window is incomplete (warmup). Clamped to >= 0;
        ``None`` if fewer than two samples.
        """
        with self._lock:
            if len(self._samples) < 2:
                return None
            last_t, last_b = self._samples[-1]
            target = last_t - self._current_window_seconds
            # Most recent sample at or before the window start; samples are
            # time-ordered, so the last one satisfying the bound is the ref.
            ref_t, ref_b = self._samples[0]
            for t, b in self._samples:
                if t <= target:
                    ref_t, ref_b = t, b
                else:
                    break
            dt = last_t - ref_t
            if dt <= 0:
                return None
            rate = (last_b - ref_b) / dt
            return rate if rate > 0 else 0.0

    def median_rate(self) -> Optional[float]:
        """Median per-second rate over the rolling window.

        Samples are bucketed by ``floor(t)`` (max bytes per bucket), then deltas
        are taken between *adjacent* second-buckets (k, k+1) — gaps are skipped
        so every delta is a clean 1s rate. The median ignores the single
        inflated delta when a file completes (the running total jumps to its
        size). ``None`` if fewer than two such deltas.
        """
        with self._lock:
            if len(self._samples) < 2:
                return None
            buckets = {}
            for t, b in self._samples:
                k = int(t)  # floor, for the non-negative timestamps in use
                if k not in buckets or b > buckets[k]:
                    buckets[k] = b
            keys = sorted(buckets)
            deltas: List[float] = [
                float(buckets[keys[i + 1]] - buckets[keys[i]])
                for i in range(len(keys) - 1)
                if keys[i + 1] - keys[i] == 1  # only adjacent seconds
            ]
            if len(deltas) < 2:
                return None
            deltas.sort()
            mid = len(deltas) // 2
            if len(deltas) % 2 == 1:
                rate = deltas[mid]
            else:
                rate = (deltas[mid - 1] + deltas[mid]) / 2.0
            return rate if rate > 0 else 0.0

    def blended_rate(self) -> Optional[float]:
        """Stable-but-responsive rate: 70% median + 30% current.

        Falls back to the current rate alone during warmup (median needs two
        adjacent-second deltas). Returns 0.0 on a stall (both zero). ``None``
        only before any two samples arrive.
        """
        current = self.current_rate()
        median = self.median_rate()
        if median is not None and current is not None:
            return 0.7 * median + 0.3 * current
        if current is not None:
            return current
        if median is not None:
            return median
        return None

    def eta(self, remaining_bytes: float) -> Optional[float]:
        """Seconds until completion at the blended rate.

        ``0.0`` when nothing remains; ``None`` while warming up (no rate yet)
        or when stalled (rate is zero — division would be infinite).
        """
        if remaining_bytes <= 0:
            return 0.0
        rate = self.blended_rate()
        if rate is None or rate <= 0:
            return None
        return remaining_bytes / rate


def format_rate(bytes_per_second: Optional[float]) -> str:
    """Format a byte rate for display.

    ``None``/negative → ``"—"`` (warmup/unknown); ``0`` → ``"0 B/s"`` (stall);
    otherwise base-1024 with a ``/s`` suffix, e.g. ``"50.2 MB/s"``. Mirrors the
    units of ``format_size`` in ``hf_api``/``rclone_client`` but accepts
    ``None``/``0`` and appends ``/s``.
    """
    if bytes_per_second is None or bytes_per_second < 0:
        return "—"
    if bytes_per_second == 0:
        return "0 B/s"
    value = float(bytes_per_second)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0:
            return f"{value:.1f} {unit}/s"
        value /= 1024.0
    return f"{value:.1f} PB/s"


def format_eta(seconds: Optional[float]) -> str:
    """Format a completion ETA for display.

    ``None`` → ``"calculating…"`` (warming up); ``<= 0`` → ``"done"``;
    otherwise a compact ``Xh Ym`` / ``Xm Ys`` / ``Xs`` form.
    """
    if seconds is None:
        return "calculating…"
    if seconds <= 0:
        return "done"
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60}s"
    return f"{s // 3600}h{(s % 3600) // 60}m"
