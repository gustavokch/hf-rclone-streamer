"""Rate-estimator unit tests + a TransferManager integration smoke.

Run: .venv/bin/python test_rate_estimator.py

Style matches test_pipeline.py: plain ``assert`` functions, ``sys.path.insert``
for direct runs, a ``main()`` that prints PASS. Timestamps are injected
synthetically into RateEstimator (no real clock) so the unit cases are
deterministic. The integration smoke drives a TransferManager whose stubbed
_download_file/_upload_file feed _update_state with real-time bytes and
captures TransferProgress via progress_callback.
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
from rate_estimator import RateEstimator, format_rate, format_eta
from transfer_manager import TransferManager, TransferProgress, TransferState


# --------------------------------------------------------------------------- #
# Warmup
# --------------------------------------------------------------------------- #
def test_warmup_returns_none():
    est = RateEstimator()
    assert est.current_rate() is None
    assert est.median_rate() is None
    assert est.blended_rate() is None
    assert est.eta(1000) is None

    est.sample(0.0, 0)
    # One sample is still warmup for every rate that needs a delta.
    assert est.current_rate() is None
    assert est.median_rate() is None
    assert est.blended_rate() is None
    assert est.eta(1000) is None


# --------------------------------------------------------------------------- #
# current_rate
# --------------------------------------------------------------------------- #
def test_current_rate_basic():
    est = RateEstimator()
    est.sample(0.0, 0)
    est.sample(5.0, 500)
    assert est.current_rate() == 100.0


def test_current_rate_uses_oldest_when_window_incomplete():
    # Only 2s of history but a 5s window: ref falls back to the oldest sample.
    est = RateEstimator()
    est.sample(0.0, 0)
    est.sample(2.0, 200)
    assert est.current_rate() == 100.0


def test_current_rate_clamps_stall_to_zero():
    # Equal bytes across the window -> a stall, not None.
    est = RateEstimator()
    est.sample(0.0, 500)
    est.sample(5.0, 500)
    assert est.current_rate() == 0.0


# --------------------------------------------------------------------------- #
# median_rate
# --------------------------------------------------------------------------- #
def test_median_rate_basic():
    # 1 byte/s for 10s.
    est = RateEstimator()
    for t in range(10):
        est.sample(float(t), t)
    assert est.median_rate() == 1.0


def test_median_rate_robust_to_outlier():
    # Steady 1 B/s, but one second the total jumps +100 (file completes).
    est = RateEstimator()
    for t in range(10):
        bump = 100 if t == 5 else 1
        prev = est._samples[-1][1] if est._samples else 0
        est.sample(float(t), prev + bump)
    # Median of mostly-1 deltas ignores the single 100.
    assert est.median_rate() == 1.0


def test_median_rate_skips_non_adjacent_second_gap():
    # Buckets at seconds 0,1,3,4 (2 missing). Only the (0,1) and (3,4) deltas
    # count; the (1,3) gap is skipped so no delta pretends to be a 1s rate.
    est = RateEstimator()
    samples = [(0.0, 0), (1.0, 10), (3.0, 30), (4.0, 40)]
    for t, b in samples:
        est.sample(t, b)
    # Deltas: (0->1)=10, (3->4)=10 -> median 10. The (1->3) 2s span is skipped.
    assert est.median_rate() == 10.0


def test_median_rate_none_with_one_delta():
    est = RateEstimator()
    est.sample(0.0, 0)
    est.sample(1.0, 10)
    assert est.median_rate() is None  # need >= 2 adjacent-second deltas


# --------------------------------------------------------------------------- #
# Pruning
# --------------------------------------------------------------------------- #
def test_pruning_keeps_only_last_window():
    est = RateEstimator(window_seconds=60.0, min_sample_interval=0.0)
    for t in range(0, 121):  # 0..120s inclusive
        est.sample(float(t), t)
    # Everything older than t=120-60=60 is dropped (the sample at exactly 60
    # is kept; < 60 is removed).
    oldest = est._samples[0][0]
    assert oldest >= 60.0
    newest = est._samples[-1][0]
    assert newest == 120.0


# --------------------------------------------------------------------------- #
# Throttle
# --------------------------------------------------------------------------- #
def test_throttle_updates_in_place():
    est = RateEstimator(min_sample_interval=0.25)
    est.sample(0.0, 0)
    est.sample(0.1, 100)   # within interval -> in-place, same timestamp
    est.sample(0.2, 200)   # within interval -> in-place
    assert len(est._samples) == 1
    assert est._samples[0] == (0.0, 200)  # tail timestamp preserved, bytes fresh

    est.sample(0.30, 300)  # 0.30 - 0.0 >= 0.25 -> append
    assert len(est._samples) == 2
    assert est._samples[-1] == (0.30, 300)


# --------------------------------------------------------------------------- #
# Reset on backward bytes
# --------------------------------------------------------------------------- #
def test_backward_bytes_clears_history():
    est = RateEstimator(min_sample_interval=0.0)
    est.sample(0.0, 0)
    est.sample(1.0, 100)
    assert est.current_rate() == 100.0

    # A retry resets the running total below the last sample -> history cleared.
    est.sample(2.0, 50)
    assert len(est._samples) == 1
    assert est.current_rate() is None  # only one sample after the reset


# --------------------------------------------------------------------------- #
# blended_rate
# --------------------------------------------------------------------------- #
def test_blended_rate_warmup_falls_back_to_current():
    est = RateEstimator()
    est.sample(0.0, 0)
    est.sample(0.5, 100)  # within one second-bucket -> no median delta yet
    assert est.median_rate() is None
    assert est.blended_rate() == est.current_rate()


def test_blended_rate_weighted():
    # Steady 100 B/s anchors the median; a later surge raises current_rate.
    # blended must equal exactly 0.7*median + 0.3*current regardless of the
    # surge's exact value.
    est = RateEstimator(current_window_seconds=5.0)
    for t in range(10):
        est.sample(float(t), t * 100)
    assert est.median_rate() == 100.0
    # Surge: jump the total well past the 5s window's anchor.
    est.sample(15.0, 2000)
    current = est.current_rate()
    median = est.median_rate()
    assert median == 100.0  # the gap (9 -> 15) is skipped, median unchanged
    assert current > 100.0
    assert est.blended_rate() == 0.7 * median + 0.3 * current


def test_blended_rate_stall_is_zero():
    est = RateEstimator()
    est.sample(0.0, 500)
    est.sample(1.0, 500)
    est.sample(2.0, 500)
    assert est.blended_rate() == 0.0


# --------------------------------------------------------------------------- #
# eta
# --------------------------------------------------------------------------- #
def test_eta_basic():
    est = RateEstimator()
    for t in range(10):
        est.sample(float(t), t)  # 1 B/s
    assert est.eta(1000) == 1000.0  # 1 B/s -> 1000s


def test_eta_done_when_nothing_remains():
    est = RateEstimator()
    for t in range(10):
        est.sample(float(t), t)
    assert est.eta(0) == 0.0
    assert est.eta(-5) == 0.0


def test_eta_none_on_stall_and_warmup():
    est = RateEstimator()
    assert est.eta(1000) is None  # warmup
    est.sample(0.0, 0)
    est.sample(1.0, 0)
    est.sample(2.0, 0)
    assert est.eta(1000) is None  # stall (rate 0)


# --------------------------------------------------------------------------- #
# Formatters
# --------------------------------------------------------------------------- #
def test_format_rate_edges():
    assert format_rate(None) == "—"
    assert format_rate(-1.0) == "—"
    assert format_rate(0) == "0 B/s"
    assert format_rate(512.0) == "512.0 B/s"
    assert format_rate(1024.0) == "1.0 KB/s"
    assert format_rate(50.2 * 1024 * 1024) == "50.2 MB/s"


def test_format_eta_edges():
    assert format_eta(None) == "calculating…"
    assert format_eta(0) == "done"
    assert format_eta(-10) == "done"
    assert format_eta(45) == "45s"
    assert format_eta(59.6) == "1m0s"  # rounds to 60s
    assert format_eta(90) == "1m30s"
    assert format_eta(3600) == "1h0m"
    assert format_eta(5400) == "1h30m"


# --------------------------------------------------------------------------- #
# Thread-safety smoke
# --------------------------------------------------------------------------- #
def test_thread_safety_smoke():
    est = RateEstimator(min_sample_interval=0.0)  # force appends -> real contention
    counter = [0]

    def worker():
        for _ in range(1000):
            n = counter[0]
            counter[0] = n + 1
            est.sample(time.time(), n)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # No exception survived the joins; final state still computes sanely.
    rate = est.current_rate()
    assert rate is None or rate >= 0.0


# --------------------------------------------------------------------------- #
# Integration smoke: TransferManager populates rates/ETA via progress_callback
# --------------------------------------------------------------------------- #
def _make_manager(tmpdir):
    os.environ["XDG_CONFIG_HOME"] = str(tmpdir)
    cfg = Config()
    cfg.set("cache_dir", str(tmpdir))
    cfg.set("resume", False)
    cfg.set("cleanup", False)
    return TransferManager(config=cfg)


def test_integration_populates_rates():
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = _make_manager(tmpdir)
        model_id = "org/eta-int"
        files = [FileInfo(path="f0.bin", size=1000)]
        for f in files:
            key = mgr._get_state_key(model_id, f.path)
            mgr.states[key] = TransferState(
                model_id=model_id,
                filename=f.path,
                status="pending",
                total_bytes=f.size,
            )

        captured = []
        mgr.progress_callback = lambda p: captured.append(p)

        key = mgr._get_state_key(model_id, "f0.bin")

        def dl(model_id, file_info, cancel=NO_CANCEL):
            # Ramp download bytes over >1s so median has adjacent-second deltas.
            for i in range(1, 6):
                mgr._update_state(key, downloaded_bytes=i * 100)
                time.sleep(0.3)
            mgr._update_state(key, downloaded_bytes=1000)
            return Path(str(tmpdir))

        def ul(model_id, file_info, cache_path, dest_dir, cancel=NO_CANCEL):
            for i in range(1, 6):
                mgr._update_state(key, uploaded_bytes=i * 100)
                time.sleep(0.3)
            mgr._update_state(key, uploaded_bytes=1000)
            return True

        mgr._download_file = dl
        mgr._upload_file = ul

        success = mgr._transfer_model_pipelined(model_id, files, "/Models")
        assert success is True

        # After enough updates, rates and ETA populated on the captured progress.
        assert any(p.download_rate is not None for p in captured), "download_rate must populate"
        assert any(p.upload_rate is not None for p in captured), "upload_rate must populate"
        assert any(p.eta_seconds is not None for p in captured), "eta_seconds must populate"

        # And get_progress() reflects the last-known rates without a callback.
        mgr.progress_callback = None
        prog = mgr.get_progress()
        assert prog.upload_rate is not None
        assert prog.eta_seconds == 0.0  # uploaded_bytes == total_bytes -> done


# --------------------------------------------------------------------------- #
# ETA stall fallback (TransferManager._compute_eta) + throttle skew
# --------------------------------------------------------------------------- #
def test_eta_stall_does_not_fall_back_to_download():
    # Upload stalled (>=2 equal samples -> blended_rate() == 0.0) while download
    # is still advancing must NOT produce a download-based ETA. Pre-seed the
    # estimators directly and call _compute_eta with a hand-built progress so the
    # stall is exact and clock-free. (Without Fix 1 this returns 7.0 — 700
    # remaining bytes / 100 B/s download — instead of None.)
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = _make_manager(tmpdir)
        # Upload stalled: constant from the FIRST sample so the estimator never
        # sees a 0->N jump (a jump reads as progress, not a stall).
        for t in range(4):
            mgr._upload_estimator.sample(float(t), 500)
        # Download advancing: a positive rate cached in the download estimator.
        for t in range(4):
            mgr._download_estimator.sample(float(t), t * 100)
        prog = TransferProgress(
            total_files=1, completed_files=0, total_bytes=1000,
            downloaded_bytes=300, uploaded_bytes=500, failed_files=0,
        )
        assert mgr._compute_eta(prog) is None


def test_current_rate_approx_under_throttled_sampling():
    # Sub-throttle cadence: the in-place byte update attributes fresh bytes to an
    # older timestamp, but the constant offset cancels across the 5s window, so
    # current_rate() stays within tolerance of the true 100 B/s.
    est = RateEstimator(min_sample_interval=0.25, current_window_seconds=5.0)
    for i in range(61):  # 0.0 .. 6.0s, steady 100 B/s
        t = i * 0.1
        est.sample(t, int(t * 100))
    rate = est.current_rate()
    assert rate is not None
    assert 85.0 <= rate <= 115.0


def main():
    tests = [
        test_warmup_returns_none,
        test_current_rate_basic,
        test_current_rate_uses_oldest_when_window_incomplete,
        test_current_rate_clamps_stall_to_zero,
        test_median_rate_basic,
        test_median_rate_robust_to_outlier,
        test_median_rate_skips_non_adjacent_second_gap,
        test_median_rate_none_with_one_delta,
        test_pruning_keeps_only_last_window,
        test_throttle_updates_in_place,
        test_backward_bytes_clears_history,
        test_blended_rate_warmup_falls_back_to_current,
        test_blended_rate_weighted,
        test_blended_rate_stall_is_zero,
        test_eta_basic,
        test_eta_done_when_nothing_remains,
        test_eta_none_on_stall_and_warmup,
        test_format_rate_edges,
        test_format_eta_edges,
        test_thread_safety_smoke,
        test_integration_populates_rates,
        test_eta_stall_does_not_fall_back_to_download,
        test_current_rate_approx_under_throttled_sampling,
    ]
    for fn in tests:
        fn()
        print(f"PASS {fn.__name__}")
    print("All test_rate_estimator tests passed.")


if __name__ == "__main__":
    main()
