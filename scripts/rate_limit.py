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
from typing import Deque, Optional


class SlidingWindowLimiter:
    """Thread-safe: at most `max_calls` acquires inside any rolling `period_s` window."""

    def __init__(self, max_calls: int = 20, period_s: float = 60.0, name: str = "rpm"):
        if max_calls < 1:
            raise ValueError("max_calls must be >= 1")
        if period_s <= 0:
            raise ValueError("period_s must be > 0")
        self.max_calls = int(max_calls)
        self.period_s = float(period_s)
        self.name = name
        self._times: Deque[float] = deque()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self.total_acquired = 0
        self.total_wait_s = 0.0

    def _prune(self, now: float) -> None:
        cutoff = now - self.period_s
        while self._times and self._times[0] <= cutoff:
            self._times.popleft()

    def slots_free(self) -> int:
        with self._lock:
            self._prune(time.monotonic())
            return max(0, self.max_calls - len(self._times))

    def wait_seconds(self) -> float:
        """How long until at least one slot frees (0 if free now)."""
        with self._lock:
            now = time.monotonic()
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
        deadline = None if timeout is None else time.monotonic() + timeout
        waited = 0.0
        with self._cond:
            while True:
                now = time.monotonic()
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
                if deadline is not None:
                    remaining = deadline - now
                    if remaining <= 0:
                        return False
                    sleep_for = min(sleep_for, remaining)
                if sleep_for > 0:
                    t0 = time.monotonic()
                    self._cond.wait(timeout=sleep_for + 0.01)
                    waited += time.monotonic() - t0
                else:
                    # Spurious / clock: brief yield
                    self._cond.wait(timeout=0.01)

    def snapshot(self) -> dict:
        with self._lock:
            now = time.monotonic()
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


def free_rpm_limiter(rpm: Optional[int] = None) -> SlidingWindowLimiter:
    """Default free-model RPM limiter (OpenRouter :free = 20/min)."""
    import os

    n = rpm
    if n is None:
        raw = os.environ.get("BCOPENCODE_FREE_RPM", "20").strip()
        try:
            n = int(raw)
        except ValueError:
            n = 20
    return SlidingWindowLimiter(max_calls=max(1, n), period_s=60.0, name="free_rpm")


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
