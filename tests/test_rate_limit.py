"""The free-RPM limiter had zero tests.

It is the only thing standing between `panel_run.py` and OpenRouter's 20 requests/minute
free-tier ceiling. Both failure directions cost the user something real: too permissive
and the panel burns the daily quota on 429s, too conservative and a 4-model panel stalls
for a minute per model for no reason.

Nothing here sleeps for a whole window. `SlidingWindowLimiter` takes `time_fn`/`sleep_fn`
so the 60-second cases run on a virtual clock and assert the *exact* wait rather than a
tolerance. The concurrency test below is the exception: it uses the real clock with a
50 ms window, because the property it checks (no window ever holds more than max_calls)
is only meaningful under genuine thread interleaving.
"""
import importlib.util
import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("rate_limit", ROOT / "scripts" / "rate_limit.py")
rate_limit = importlib.util.module_from_spec(_spec)
sys.modules["rate_limit"] = rate_limit
_spec.loader.exec_module(rate_limit)

SlidingWindowLimiter = rate_limit.SlidingWindowLimiter
free_rpm_limiter = rate_limit.free_rpm_limiter


class FakeClock:
    """Virtual monotonic clock. Only `sleep()` moves it, so waits are exact."""

    def __init__(self, start=1000.0):
        self.t = float(start)
        self.sleeps = []

    def time(self):
        return self.t

    def sleep(self, dt):
        self.sleeps.append(dt)
        self.t += max(0.0, float(dt))

    def advance(self, dt):
        self.t += float(dt)


def limiter(max_calls=20, period_s=60.0, start=1000.0):
    clock = FakeClock(start)
    lim = SlidingWindowLimiter(
        max_calls=max_calls, period_s=period_s, time_fn=clock.time, sleep_fn=clock.sleep
    )
    return lim, clock


# --- the budget itself ----------------------------------------------------------


def test_first_n_acquires_never_wait():
    lim, clock = limiter(max_calls=20, period_s=60.0)
    for i in range(20):
        assert lim.acquire() is True
        assert lim.slots_free() == 20 - (i + 1)
    assert clock.sleeps == [], "the free budget must be spendable without any delay"
    assert clock.t == 1000.0
    assert lim.total_acquired == 20
    assert lim.total_wait_s == 0.0


def test_call_n_plus_one_waits_until_the_oldest_slot_ages_out():
    """The 21st request waits for the 1st to leave the window — not a fixed 60s nap."""
    lim, clock = limiter(max_calls=20, period_s=60.0)
    for _ in range(20):
        lim.acquire()          # slots at t=1000, 1001, ... 1019
        clock.advance(1.0)
    clock.advance(-1.0)        # back to the instant of the 20th call, t=1019
    assert lim.wait_seconds() == pytest.approx(41.0), "oldest slot (t=1000) frees at t=1060"

    assert lim.acquire() is True
    assert len(clock.sleeps) == 1, "one sleep, not a spin loop"
    assert clock.sleeps[0] == pytest.approx(41.0, abs=0.05)
    assert clock.t == pytest.approx(1060.0, abs=0.05)
    assert lim.total_acquired == 21
    assert lim.total_wait_s == pytest.approx(41.0, abs=0.05)
    # Exactly one slot aged out and the new call took it: still 20 in the window.
    assert lim.snapshot()["in_window"] == 20
    assert lim.slots_free() == 0


def test_a_burst_at_one_instant_frees_as_one_block():
    """Twenty calls at the same instant all age out together — the next 20 run free."""
    lim, clock = limiter(max_calls=20, period_s=60.0)
    for _ in range(20):
        lim.acquire()
    assert lim.wait_seconds() == pytest.approx(60.0)
    assert lim.acquire() is True
    assert clock.sleeps[0] == pytest.approx(60.0, abs=0.05)
    assert lim.slots_free() == 19


def test_waits_only_the_remainder_when_the_window_is_partly_aged():
    lim, clock = limiter(max_calls=3, period_s=60.0)
    lim.acquire()             # t=1000
    clock.advance(50)
    lim.acquire()             # t=1050
    lim.acquire()             # t=1050
    assert lim.wait_seconds() == pytest.approx(10.0)
    lim.acquire()
    assert clock.sleeps[0] == pytest.approx(10.0, abs=0.05), "must not wait a full window"


def test_stale_slots_are_pruned_so_the_budget_refills():
    lim, clock = limiter(max_calls=5, period_s=60.0)
    for _ in range(5):
        lim.acquire()
    assert lim.slots_free() == 0
    clock.advance(59.0)
    assert lim.slots_free() == 0, "a slot must not free early — that overspends the quota"
    clock.advance(1.0)
    assert lim.slots_free() == 5, "at exactly one period the whole window must age out"
    assert lim.wait_seconds() == 0.0
    assert lim.acquire() is True
    assert clock.sleeps == []


def test_pruning_is_gradual_not_all_or_nothing():
    lim, clock = limiter(max_calls=4, period_s=60.0)
    lim.acquire()
    clock.advance(30)
    lim.acquire()
    clock.advance(30)         # t=1060: the first slot is now exactly one period old
    assert lim.slots_free() == 3


# --- refusal paths: they must not silently consume a slot -----------------------


def test_non_blocking_acquire_when_full_returns_false_and_records_nothing():
    lim, clock = limiter(max_calls=2, period_s=60.0)
    assert lim.acquire(block=False) is True
    assert lim.acquire(block=False) is True
    assert lim.acquire(block=False) is False
    assert lim.acquire(block=False) is False
    assert clock.sleeps == [], "non-blocking acquire must never sleep"
    assert lim.total_acquired == 2, "a refused acquire must not be counted as a call"
    # The refusals must not have consumed the window either.
    clock.advance(60)
    assert lim.slots_free() == 2


def test_non_blocking_acquire_succeeds_while_slots_remain():
    lim, _clock = limiter(max_calls=2, period_s=60.0)
    assert lim.acquire(block=False) is True
    assert lim.snapshot()["in_window"] == 1


def test_timeout_returns_false_without_recording_a_slot():
    lim, clock = limiter(max_calls=2, period_s=60.0)
    lim.acquire()
    lim.acquire()
    assert lim.acquire(timeout=5.0) is False
    assert lim.total_acquired == 2, "a timed-out acquire must not book a request"
    assert sum(clock.sleeps) == pytest.approx(5.0, abs=0.05), (
        "the wait must be capped by the timeout, not by the window"
    )
    # And the window is untouched: the caller that does wait still gets the full remainder.
    assert lim.wait_seconds() == pytest.approx(55.0, abs=0.05)


def test_timeout_zero_still_takes_a_free_slot():
    lim, clock = limiter(max_calls=1, period_s=60.0)
    assert lim.acquire(timeout=0.0) is True
    assert lim.acquire(timeout=0.0) is False
    assert clock.sleeps == []


def test_timeout_longer_than_the_wait_succeeds():
    lim, clock = limiter(max_calls=1, period_s=60.0)
    lim.acquire()
    assert lim.acquire(timeout=120.0) is True
    assert clock.t == pytest.approx(1060.0, abs=0.05)


# --- snapshot / bookkeeping -----------------------------------------------------


def test_snapshot_reports_the_live_window():
    lim, clock = limiter(max_calls=3, period_s=60.0)
    lim.acquire()
    lim.acquire()
    s = lim.snapshot()
    assert s["max_calls"] == 3
    assert s["period_s"] == 60.0
    assert s["in_window"] == 2
    assert s["slots_free"] == 1
    assert s["total_acquired"] == 2
    assert s["total_wait_s"] == 0.0
    clock.advance(60)
    assert lim.snapshot()["in_window"] == 0, "snapshot must prune, not report stale slots"


def test_wait_seconds_is_zero_while_slots_remain():
    lim, _clock = limiter(max_calls=2, period_s=60.0)
    assert lim.wait_seconds() == 0.0
    lim.acquire()
    assert lim.wait_seconds() == 0.0


# --- degenerate configuration ---------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, -100])
def test_max_calls_below_one_is_refused(bad):
    """A limiter of 0 could never grant a slot; it must fail loudly, not hang forever."""
    with pytest.raises(ValueError):
        SlidingWindowLimiter(max_calls=bad, period_s=60.0)


@pytest.mark.parametrize("bad", [0, 0.0, -1.0])
def test_non_positive_period_is_refused(bad):
    """period_s=0 would make every slot instantly stale — an unlimited limiter."""
    with pytest.raises(ValueError):
        SlidingWindowLimiter(max_calls=20, period_s=bad)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("20", 20),
        ("5", 5),
        (" 7 ", 7),
        ("", 20),            # empty -> documented default
        ("abc", 20),         # garbage -> documented default, never a crash
        ("20.5", 20),        # not an int -> default
        ("twenty", 20),
        ("0", 1),            # clamped: 0 would be a limiter that never grants
        ("-3", 1),
    ],
)
def test_free_rpm_env_var_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("BCOPENCODE_FREE_RPM", raw)
    lim = free_rpm_limiter()
    assert lim.max_calls == expected
    assert lim.period_s == 60.0
    assert lim.name == "free_rpm"


def test_free_rpm_defaults_to_twenty_when_unset(monkeypatch):
    monkeypatch.delenv("BCOPENCODE_FREE_RPM", raising=False)
    assert free_rpm_limiter().max_calls == 20


def test_explicit_rpm_argument_beats_the_env(monkeypatch):
    monkeypatch.setenv("BCOPENCODE_FREE_RPM", "5")
    assert free_rpm_limiter(11).max_calls == 11
    assert free_rpm_limiter(0).max_calls == 1


# --- the interface panel_run.py depends on --------------------------------------


def test_public_surface_used_by_panel_run_is_intact():
    """panel_run.py calls exactly these. Injecting the clock must not have moved them."""
    lim = free_rpm_limiter(2)
    assert lim.wait_seconds() == 0.0
    assert lim.acquire() is True
    assert lim.slots_free() == 1
    assert isinstance(lim.snapshot(), dict)


def test_default_limiter_uses_the_real_clock():
    """No injection: default behaviour must be unchanged for every existing caller."""
    lim = SlidingWindowLimiter(max_calls=2, period_s=60.0)
    t0 = time.monotonic()
    assert lim.acquire() is True
    assert lim.acquire() is True
    assert lim.acquire(block=False) is False
    assert time.monotonic() - t0 < 1.0
    assert lim.slots_free() == 0


# --- concurrency: the property that actually matters ----------------------------


class TracingClock:
    """Real clock that remembers the last value each thread observed.

    `acquire()` stores the value returned by its final `time_fn()` call and makes no
    further clock call before returning True, so after a successful acquire this thread's
    last observed value IS the timestamp in the limiter's window. That lets the test
    assert on the limiter's own timestamps without reaching into its internals.
    """

    def __init__(self):
        self._local = threading.local()

    def __call__(self):
        t = time.monotonic()
        self._local.last = t
        return t

    @property
    def last(self):
        return self._local.last


def test_concurrent_threads_never_exceed_max_calls_in_any_window():
    max_calls, period, n_threads = 4, 0.05, 20
    clock = TracingClock()
    lim = SlidingWindowLimiter(max_calls=max_calls, period_s=period, time_fn=clock)

    stamps = []
    guard = threading.Lock()
    start = threading.Barrier(n_threads)
    failures = []

    def worker():
        try:
            start.wait(timeout=10)
            assert lim.acquire() is True
            with guard:
                stamps.append(clock.last)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "a waiter never woke up — the limiter deadlocked"

    assert not failures, failures
    assert len(stamps) == n_threads
    assert lim.total_acquired == n_threads

    stamps.sort()
    for i in range(len(stamps) - max_calls):
        gap = stamps[i + max_calls] - stamps[i]
        assert gap >= period - 1e-9, (
            f"{max_calls + 1} calls landed inside one {period}s window "
            f"(slots {i}..{i + max_calls} span {gap:.4f}s) — the RPM cap was exceeded"
        )


def test_concurrent_non_blocking_acquires_hand_out_exactly_max_calls():
    max_calls, n_threads = 5, 40
    lim = SlidingWindowLimiter(max_calls=max_calls, period_s=60.0)
    granted = []
    guard = threading.Lock()
    start = threading.Barrier(n_threads)

    def worker():
        start.wait(timeout=10)
        ok = lim.acquire(block=False)
        with guard:
            granted.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sum(1 for g in granted if g) == max_calls
    assert lim.total_acquired == max_calls


def test_a_waiting_thread_does_not_hold_the_lock():
    """A blocked acquirer used to be free to stall every other caller for a full window.

    panel_run.py calls `wait_seconds()` on the main thread to print progress while
    workers are queued; if the sleeping waiter held the mutex, that call would block for
    up to 60 seconds and the panel would look hung.
    """
    lim = SlidingWindowLimiter(max_calls=1, period_s=0.5)
    lim.acquire()
    waiter = threading.Thread(target=lim.acquire, daemon=True)
    waiter.start()
    time.sleep(0.05)          # let the waiter get into its sleep

    t0 = time.monotonic()
    lim.slots_free()
    lim.wait_seconds()
    lim.snapshot()
    elapsed = time.monotonic() - t0
    assert elapsed < 0.2, f"observers blocked for {elapsed:.2f}s behind a sleeping waiter"
    waiter.join(timeout=5)
    assert not waiter.is_alive()


# --- the offline guarantee the limiter exists to protect -------------------------
#
# The limiter paces a finite free budget; the conftest guard makes sure the test suite
# never spends any of it. An untested guard is a guard that silently stops working, and
# the symptom would be a slightly slower green build while the quota drains.


def test_conftest_blocks_urlopen():
    import urllib.request

    from conftest import NetworkAccessDuringTests

    with pytest.raises(NetworkAccessDuringTests) as ei:
        urllib.request.urlopen("http://openrouter.ai/api/v1/models", timeout=1)
    assert "offline" in str(ei.value)


def test_conftest_blocks_a_custom_opener():
    import urllib.request

    from conftest import NetworkAccessDuringTests

    opener = urllib.request.build_opener()
    with pytest.raises(NetworkAccessDuringTests):
        opener.open("http://openrouter.ai/api/v1/models")


def test_the_guard_survives_a_bare_except_Exception():
    """or_client.http_json relabels every Exception as ConnectionError.

    If the tripwire were an ordinary exception it would come back out of that handler
    looking like a normal offline failure — indistinguishable from the assertion a test
    was making, so a real request attempt would pass unnoticed.
    """
    import urllib.request

    from conftest import NetworkAccessDuringTests

    with pytest.raises(NetworkAccessDuringTests):
        try:
            urllib.request.urlopen("http://openrouter.ai/")
        except Exception as e:                      # noqa: BLE001 - the point of the test
            raise ConnectionError(str(e)) from e


@pytest.mark.allow_network
def test_the_opt_out_marker_restores_the_real_urlopen():
    """Documents the escape hatch. Asserts identity only — it dials nothing."""
    import urllib.request

    assert urllib.request.urlopen.__module__ == "urllib.request"
    assert "blocked" not in urllib.request.urlopen.__name__


def test_module_runs_as_a_script():
    """`rate_limit.py` advertises a CLI; a broken __main__ is a broken advertisement."""
    import subprocess

    p = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "rate_limit.py"), "--rpm", "3"],
        capture_output=True, text=True, timeout=60,
        env=dict(os.environ, BCOPENCODE_FREE_RPM="3"),
    )
    assert p.returncode == 0, p.stderr
    assert "free_rpm" in p.stdout or "max_calls" in p.stdout
