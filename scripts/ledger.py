#!/usr/bin/env python3
"""ledger.py — the usage ledger and the shared request-rate window, for Python callers.

`_bcoc_common.sh` owns the ledger for the shell wrappers (`bcoc_usage_append`,
`bcoc_cap_used`). `delegate.py` is Python and must write the SAME file in the SAME row
shape, so the daily cap sees every request regardless of which entry point made it.
tests/test_delegate.py asserts a row written here is counted by the shell's reader.

Rules carried over from the shell version, each learned the hard way:
  * A row is `billed` when the model may have been invoked. Bias to over-counting.
  * An unreadable ledger is None ("cannot tell"), never 0 — treating it as 0 makes the
    cap silently infinite exactly when it matters.
  * A malformed or non-object line is skipped, never fatal.
  * Appending fails OPEN on a filesystem that refuses flock (NFS/CIFS): an unlocked
    append of one short line beats losing the row.
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Outcomes that provably did not reach the model. Must match _bcoc_common.sh.
NOT_BILLED = {"AUTH", "CAP", "UNREACHABLE", "QUOTA", "PAID_BLOCKED", "REFUSED", "BAD_ARGS"}


def state_dir() -> Path:
    return Path(os.environ.get("BCOPENCODE_STATE_DIR") or (Path.home() / ".bettercallopencode"))


def usage_path(state: Optional[Path] = None) -> Path:
    return (state or state_dir()) / "usage.jsonl"


def utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def cap_used_today(state: Optional[Path] = None) -> Optional[int]:
    path = usage_path(state)
    if not path.exists():
        return 0
    day, n = utc_day(), 0
    try:
        # errors="replace": invalid UTF-8 in one line must not make the whole ledger
        # "unreadable" (and so crash the caller); that line just fails to parse and is skipped.
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(o, dict) or o.get("day") != day:
                    continue
                billed = o.get("billed")
                if billed is None:
                    billed = o.get("result") not in NOT_BILLED
                if billed:
                    n += 1
    except OSError:
        return None
    return n


def append_row(result: str, model: str, backend: str, mode: str, free: bool,
               usage: Optional[Dict[str, Any]] = None, state: Optional[Path] = None) -> bool:
    usage = usage or {}
    row = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "day": utc_day(), "result": result, "model": model, "backend": backend, "mode": mode,
        "free": bool(free), "billed": result not in NOT_BILLED,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"), "cost": usage.get("cost"),
    }
    path = usage_path(state)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            locked = False
            try:
                fcntl.flock(f, fcntl.LOCK_EX)
                locked = True
            except OSError as e:
                print(f"bcoc: ledger lock unavailable ({e}); appending unlocked", file=sys.stderr)
            try:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
            finally:
                if locked:
                    try:
                        fcntl.flock(f, fcntl.LOCK_UN)
                    except OSError:
                        pass
        return True
    except OSError as e:
        print(f"bcoc: WARNING could not record this call in the ledger: {e}", file=sys.stderr)
        print("bcoc: cap accounting may undercount until this is fixed.", file=sys.stderr)
        return False


def rpm_acquire(rpm: int, state: Optional[Path] = None, window_s: float = 60.0,
                sleep_fn=time.sleep, time_fn=time.time, max_wait_s: float = 120.0) -> float:
    """Block until fewer than `rpm` requests started in the last `window_s` ACROSS
    PROCESSES, then record this one. Returns seconds waited. rpm <= 0 disables.

    `rpm.jsonl` holds one float timestamp per line and is rewritten under flock. This is
    what lets several delegate workers (separate subagents, separate processes) share one
    per-minute budget instead of each assuming the whole window is theirs. Best effort: if
    the file cannot be locked it admits the call rather than deadlocking a worker.
    """
    if rpm <= 0:
        return 0.0
    path = (state or state_dir()) / "rpm.jsonl"
    waited = 0.0
    while True:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a+", encoding="utf-8") as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX)
                except OSError:
                    return waited
                try:
                    f.seek(0)
                    now = time_fn()
                    stamps: List[float] = []
                    for line in f:
                        try:
                            t = float(line.strip())
                        except ValueError:
                            continue
                        if now - t < window_s:
                            stamps.append(t)
                    if len(stamps) < rpm:
                        stamps.append(now)
                        f.seek(0)
                        f.truncate()
                        f.write("".join(f"{t}\n" for t in stamps))
                        f.flush()
                        return waited
                    wait = max(0.05, window_s - (now - min(stamps)))
                finally:
                    try:
                        fcntl.flock(f, fcntl.LOCK_UN)
                    except OSError:
                        pass
        except OSError:
            return waited
        if waited + wait > max_wait_s:
            return waited
        sleep_fn(wait)
        waited += wait
