#!/usr/bin/env python3
"""Sliding-window rate limiter for OpenRouter free-model request caps.

Official free-variant limits (platform):
  - 20 requests / minute  (RPM)
  - 50 or 1000 requests / day (RPD, depends on credits history)

Usage as library:
  lim = SlidingWindowLimiter(max_calls=20, period_s=60.0)
  lim.acquire()   # blocks until a slot is free, then records this call

CLI self-test:
  rate_limit.py --demo
"""
from __future__ import annotations

import argparse
import threading
import time
from collections import deque
from typing import Callable, Deque, Optional


class SlidingWindowLimiter:
    """Thread-safe: at most `max_calls` acquires inside any rolling `period_s` window.

    `time_fn`/`sleep_fn` exist so the limiter can be tested. The whole point of this
    class is what happens at the 21st call in a 60-second window; verifying that with
    the real clock costs a minute per assertion, so the suite injects a virtual clock
    instead. Both default to the real thing and the default behaviour is unchanged.
    """

    def __init__(
        self,
        max_calls: int = 20,
        period_s: float = 60.0,
        name: str = "rpm",
        time_fn: Optional[Callable[[], float]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ):
        if max_calls < 1:
            raise ValueError("max_calls must be >= 1")
        if period_s <= 0:
            raise ValueError("period_s must be > 0")
        self.max_calls = int(max_calls)
        self.period_s = float(period_s)
        self.name = name
        self._time = time_fn if time_fn is not None else time.monotonic
        self._sleep = sleep_fn if sleep_fn is not None else time.sleep
        self._times: Deque[float] = deque()
        self._lock = threading.Lock()
        self.total_acquired = 0
        self.total_wait_s = 0.0

    def _prune(self, now: float) -> None:
        cutoff = now - self.period_s
        while self._times and self._times[0] <= cutoff:
            self._times.popleft()

    def slots_free(self) -> int:
        with self._lock:
            self._prune(self._time())
            return max(0, self.max_calls - len(self._times))

    def wait_seconds(self) -> float:
        """How long until at least one slot frees (0 if free now)."""
        with self._lock:
            now = self._time()
            self._prune(now)
            if len(self._times) < self.max_calls:
                return 0.0
            oldest = self._times[0]
            return max(0.0, oldest + self.period_s - now)

    def acquire(self, block: bool = True, timeout: Optional[float] = None) -> bool:
        """
        Reserve one request slot. Blocks until a slot is available if block=True.
        Returns False only when non-blocking and no slot, or timeout exceeded.
        """
        deadline = None if timeout is None else self._time() + timeout
        waited = 0.0
        while True:
            with self._lock:
                now = self._time()
                self._prune(now)
                if len(self._times) < self.max_calls:
                    self._times.append(now)
                    self.total_acquired += 1
                    self.total_wait_s += waited
                    return True
                if not block:
                    return False
                # Sleep until the oldest call ages out of the window.
                sleep_for = self._times[0] + self.period_s - now
            # The lock is released across the sleep: a waiter must not block
            # slots_free()/snapshot() or another thread's acquire for a whole window.
            # Nothing ever signals this limiter, so a plain sleep is exactly what the
            # previous Condition.wait(timeout=…) did — minus the untestable clock.
            if deadline is not None:
                remaining = deadline - now
                if remaining <= 0:
                    return False
                sleep_for = min(sleep_for, remaining)
            if sleep_for <= 0:
                sleep_for = 0.0  # spurious / clock skew: brief yield below
            t0 = self._time()
            self._sleep(sleep_for + 0.01)
            waited += self._time() - t0

    def snapshot(self) -> dict:
        with self._lock:
            now = self._time()
            self._prune(now)
            return {
                "name": self.name,
                "max_calls": self.max_calls,
                "period_s": self.period_s,
                "in_window": len(self._times),
                "slots_free": max(0, self.max_calls - len(self._times)),
                "total_acquired": self.total_acquired,
                "total_wait_s": round(self.total_wait_s, 3),
            }


def free_rpm_limiter(
    rpm: Optional[int] = None,
    time_fn: Optional[Callable[[], float]] = None,
    sleep_fn: Optional[Callable[[float], None]] = None,
) -> SlidingWindowLimiter:
    """Default free-model RPM limiter (OpenRouter :free = 20/min).

    A garbage BCOPENCODE_FREE_RPM must fall back to 20 rather than crash the panel:
    the env var is user-typed and the cost of guessing wrong is only a slower run.
    """
    import os

    n = rpm
    if n is None:
        raw = os.environ.get("BCOPENCODE_FREE_RPM", "20").strip()
        try:
            n = int(raw)
        except (TypeError, ValueError):
            n = 20
    return SlidingWindowLimiter(
        max_calls=max(1, n), period_s=60.0, name="free_rpm",
        time_fn=time_fn, sleep_fn=sleep_fn,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="OpenRouter free RPM sliding-window limiter")
    ap.add_argument("--rpm", type=int, default=20)
    ap.add_argument("--demo", action="store_true", help="Fire 25 acquire()s and show waits")
    args = ap.parse_args()
    lim = SlidingWindowLimiter(max_calls=args.rpm, period_s=60.0)
    if not args.demo:
        print(lim.snapshot())
        return 0
    print(f"Demo: acquire {args.rpm + 5} slots at rpm={args.rpm}")
    t0 = time.monotonic()
    for i in range(args.rpm + 5):
        lim.acquire()
        print(f"  #{i+1} at +{time.monotonic()-t0:.2f}s  free={lim.slots_free()}")
    print("snapshot", lim.snapshot())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
