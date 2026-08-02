#!/usr/bin/env python3
"""panel_run.py — parallel multi-model reviews under OpenRouter free RPM.

Fires as many reviews as the free RPM budget allows (default 20/min), waits for
slots to free in a sliding 60s window, then continues until every model is done.

Does NOT retry-loop on QUOTA from OpenRouter — records the result and moves on
(unless --retry-quota once after a full window sleep).

Usage:
  panel_run.py --prompt-file p.md --out-dir ./out --scope ./src \\
    --models "a:free,b:free" | --preset coding-panel | --all-free \\
    [--backend or-api] [--rpm 20] [--max-workers 20] [--max-tokens 4096]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from rate_limit import free_rpm_limiter  # noqa: E402

PRESETS = {
    "coding-panel": [
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "cohere/north-mini-code:free",
        "openai/gpt-oss-20b:free",
    ],
    "fast-panel": [
        "nvidia/nemotron-3-nano-30b-a3b:free",
        "google/gemma-4-26b-a4b-it:free",
        "inclusionai/ling-3.0-flash:free",
        "openrouter/free",
    ],
    "nvidia-panel": [
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "nvidia/nemotron-3-nano-30b-a3b:free",
        "nvidia/nemotron-nano-9b-v2:free",
    ],
}


def normalize_model(m: str) -> str:
    """OpenCode form openrouter/<or-id>. Preserve free router id openrouter/free."""
    m = m.strip()
    if m in ("free", "openrouter/free", "openrouter/openrouter/free"):
        return "openrouter/openrouter/free"
    if m.startswith("openrouter/"):
        return m if m.count("/") >= 1 else f"openrouter/{m}"
    return f"openrouter/{m}"


def is_free(m: str) -> bool:
    nm = normalize_model(m)
    mid = nm[len("openrouter/") :] if nm.startswith("openrouter/") else nm
    return mid.endswith(":free") or mid in ("free", "openrouter/free")


def list_all_free() -> List[str]:
    # Prefer live API via list_free_models.py
    env = os.environ.copy()
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "list_free_models.py"), "--json"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    if proc.returncode != 0:
        raise SystemExit(f"list_free_models failed: {proc.stderr[:500]}")
    data = json.loads(proc.stdout)
    return [m["id"] for m in data.get("models") or []]


def parse_report(path: Path) -> Dict[str, str]:
    out = {
        "result": "?",
        "finish_reason": "",
        "prompt_tokens": "",
        "completion_tokens": "",
        "reasoning_tokens": "",
        "total_tokens": "",
        "cost": "",
    }
    if not path.is_file():
        return out
    text = path.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^\- \*\*RESULT:\*\* (\S+)", text, re.M)
    if m:
        out["result"] = m.group(1)
    m = re.search(r"^\- \*\*finish_reason:\*\* (\S+)", text, re.M)
    if m:
        out["finish_reason"] = m.group(1)
    m = re.search(
        r"^\- \*\*Tokens:\*\* prompt=(\S+) completion=(\S+) reasoning=(\S+) total=(\S+) cost=(\S+)",
        text,
        re.M,
    )
    if m:
        out["prompt_tokens"] = m.group(1)
        out["completion_tokens"] = m.group(2)
        out["reasoning_tokens"] = m.group(3)
        out["total_tokens"] = m.group(4)
        out["cost"] = m.group(5)
    # fallback RESULT= line at end of stderr capture — also scan file
    m = re.search(r"^RESULT=(\S+)", text, re.M)
    if m and out["result"] == "?":
        out["result"] = m.group(1)
    return out


def run_one(
    *,
    model: str,
    prompt_file: Path,
    out_path: Path,
    scope: Path,
    backend: str,
    cap: int,
    max_tokens: int,
    max_input_tokens: int,
    timeout: int,
    allow_paid: bool,
    review_sh: Path,
    limiter,
    idx: int,
    total: int,
) -> Dict[str, object]:
    # Acquire free-RPM slot *just before* the billed HTTP call starts.
    wait_before = limiter.wait_seconds()
    t_acq0 = time.monotonic()
    limiter.acquire()
    waited = time.monotonic() - t_acq0
    if wait_before > 0.05:
        print(
            f"[{idx}/{total}] rate-limit: waited {waited:.1f}s for free RPM slot "
            f"(model={model})",
            file=sys.stderr,
            flush=True,
        )

    cmd = [
        "bash",
        str(review_sh),
        "--prompt-file",
        str(prompt_file),
        "--out",
        str(out_path),
        "--scope",
        str(scope),
        "--backend",
        backend,
        "--model",
        model,
        "--cap",
        str(cap),
        "--max-tokens",
        str(max_tokens),
        "--max-input-tokens",
        str(max_input_tokens),
        "--timeout",
        str(timeout),
    ]
    if allow_paid:
        cmd.append("--allow-paid")

    print(f"[{idx}/{total}] START {model}", file=sys.stderr, flush=True)
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 60,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
    except subprocess.TimeoutExpired as e:
        stdout = (e.stdout or "") if isinstance(e.stdout, str) else ""
        stderr = (e.stderr or "") if isinstance(e.stderr, str) else str(e)
        elapsed = time.monotonic() - t0
        row = {
            "model": model,
            "result": "TIMEOUT",
            "finish_reason": "",
            "prompt_tokens": "",
            "completion_tokens": "",
            "reasoning_tokens": "",
            "total_tokens": "",
            "cost": "",
            "elapsed_s": round(elapsed, 1),
            "rate_wait_s": round(waited, 2),
            "report": str(out_path),
            "error": "subprocess timeout",
        }
        print(f"[{idx}/{total}] TIMEOUT {model} ({elapsed:.0f}s)", file=sys.stderr, flush=True)
        return row

    elapsed = time.monotonic() - t0
    # Prefer RESULT= from stdout (wrapper prints it last)
    result = "?"
    for line in reversed((stdout + "\n" + stderr).splitlines()):
        if line.startswith("RESULT="):
            result = line.split("=", 1)[1].strip()
            break
    parsed = parse_report(out_path)
    if parsed.get("result") and parsed["result"] != "?":
        result = parsed["result"]

    row = {
        "model": model,
        "result": result,
        "finish_reason": parsed.get("finish_reason") or "",
        "prompt_tokens": parsed.get("prompt_tokens") or "",
        "completion_tokens": parsed.get("completion_tokens") or "",
        "reasoning_tokens": parsed.get("reasoning_tokens") or "",
        "total_tokens": parsed.get("total_tokens") or "",
        "cost": parsed.get("cost") or "",
        "elapsed_s": round(elapsed, 1),
        "rate_wait_s": round(waited, 2),
        "report": str(out_path.name),
        "error": "",
    }
    if result not in ("OK", "TRUNCATED"):
        # keep a short redacted error snippet
        err = (stderr or stdout)[-300:].replace("\n", " ")
        row["error"] = err[:200]
    print(
        f"[{idx}/{total}] {result:10s} {model}  "
        f"finish={row['finish_reason'] or '?'}  "
        f"{elapsed:.0f}s  wait={waited:.1f}s",
        file=sys.stderr,
        flush=True,
    )
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description="Parallel free-model panel under RPM limit")
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--scope", default=".")
    ap.add_argument("--backend", default="or-api", choices=["or-api", "opencode"])
    ap.add_argument("--models", default="", help="Comma-separated model ids")
    ap.add_argument("--preset", default="", choices=["", *PRESETS.keys()])
    ap.add_argument("--all-free", action="store_true", help="Every live :free model")
    ap.add_argument("--rpm", type=int, default=int(os.environ.get("BCOPENCODE_FREE_RPM", "20")))
    ap.add_argument(
        "--max-workers",
        type=int,
        default=0,
        help="Parallel workers (default=min(rpm, n_models)). Capped by rpm.",
    )
    ap.add_argument("--cap", type=int, default=int(os.environ.get("BCOPENCODE_CAP", "500")))
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument("--max-input-tokens", type=int, default=8000)
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--allow-paid", action="store_true")
    ap.add_argument(
        "--retry-quota",
        action="store_true",
        help="After all first-pass done, retry QUOTA models once after waiting for a full RPM window",
    )
    args = ap.parse_args()

    prompt = Path(args.prompt_file).resolve()
    if not prompt.is_file():
        print("Missing prompt file", file=sys.stderr)
        return 2
    scope = Path(args.scope).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    models: List[str] = []
    if args.all_free:
        models = list_all_free()
    elif args.preset:
        models = list(PRESETS[args.preset])
    elif args.models:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
    else:
        print("Need --models, --preset, or --all-free", file=sys.stderr)
        return 2

    final_models: List[str] = []
    for m in models:
        nm = normalize_model(m)
        if not is_free(nm) and not args.allow_paid:
            print(f"Skip non-free {nm}", file=sys.stderr)
            continue
        final_models.append(nm)

    if not final_models:
        print("No models to run", file=sys.stderr)
        return 2

    rpm = max(1, args.rpm)
    workers = args.max_workers or min(rpm, len(final_models))
    workers = max(1, min(workers, rpm, len(final_models)))
    limiter = free_rpm_limiter(rpm)

    print(
        f"Panel: {len(final_models)} models | free RPM={rpm} | workers={workers} | "
        f"backend={args.backend} | out={out_dir}",
        file=sys.stderr,
        flush=True,
    )

    review_sh = SCRIPTS / "opencode_review.sh"
    results: List[Dict[str, object]] = []

    def submit_all(model_list: List[str], label: str) -> List[Dict[str, object]]:
        rows: List[Dict[str, object]] = []
        total = len(model_list)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures: Dict[Future, Tuple[int, str]] = {}
            # Stagger submissions through the limiter inside each worker.
            # Queue all tasks; each acquire() serializes the RPM budget.
            for i, model in enumerate(model_list, 1):
                mid = model[len("openrouter/") :]
                safe = re.sub(r"[^A-Za-z0-9._-]+", "_", mid)
                out_path = out_dir / f"REVIEW_{label}_{i:02d}_{safe}.md"
                fut = ex.submit(
                    run_one,
                    model=model,
                    prompt_file=prompt,
                    out_path=out_path,
                    scope=scope,
                    backend=args.backend,
                    cap=args.cap,
                    max_tokens=args.max_tokens,
                    max_input_tokens=args.max_input_tokens,
                    timeout=args.timeout,
                    allow_paid=args.allow_paid,
                    review_sh=review_sh,
                    limiter=limiter,
                    idx=i,
                    total=total,
                )
                futures[fut] = (i, model)

            pending = set(futures.keys())
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for fut in done:
                    try:
                        rows.append(fut.result())
                    except Exception as e:
                        i, model = futures[fut]
                        rows.append(
                            {
                                "model": model,
                                "result": "ERROR",
                                "finish_reason": "",
                                "prompt_tokens": "",
                                "completion_tokens": "",
                                "reasoning_tokens": "",
                                "total_tokens": "",
                                "cost": "",
                                "elapsed_s": 0,
                                "rate_wait_s": 0,
                                "report": "",
                                "error": str(e)[:200],
                            }
                        )
        # stable order by model list
        order = {m: i for i, m in enumerate(model_list)}
        rows.sort(key=lambda r: order.get(str(r["model"]), 999))
        return rows

    results = submit_all(final_models, "p1")

    if args.retry_quota:
        quota_models = [str(r["model"]) for r in results if r.get("result") == "QUOTA"]
        if quota_models:
            wait_s = 60.0
            print(
                f"Retrying {len(quota_models)} QUOTA models after {wait_s:.0f}s window…",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(wait_s)
            retry_rows = submit_all(quota_models, "p2")
            by_m = {str(r["model"]): r for r in results}
            for r in retry_rows:
                by_m[str(r["model"])] = r
            results = [by_m[m] for m in final_models if m in by_m]

    # Write summary CSV + markdown index
    csv_path = out_dir / "summary.csv"
    fields = [
        "model",
        "result",
        "finish_reason",
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "total_tokens",
        "cost",
        "elapsed_s",
        "rate_wait_s",
        "report",
        "error",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in results:
            w.writerow({k: r.get(k, "") for k in fields})

    index = out_dir / "MULTI_INDEX.md"
    ok_n = sum(1 for r in results if r["result"] == "OK")
    trunc_n = sum(1 for r in results if r["result"] == "TRUNCATED")
    fail_n = len(results) - ok_n - trunc_n
    with index.open("w", encoding="utf-8") as f:
        f.write("# BetterCallOpenCode multi-model panel\n\n")
        f.write(f"- **Models:** {len(results)}\n")
        f.write(f"- **Free RPM budget:** {rpm}/min (sliding window)\n")
        f.write(f"- **Workers:** {workers}\n")
        f.write(f"- **OK / TRUNCATED / other:** {ok_n} / {trunc_n} / {fail_n}\n")
        f.write(f"- **Limiter:** `{limiter.snapshot()}`\n\n")
        f.write("| # | Model | RESULT | finish | tokens | wait_s | Report |\n")
        f.write("|---|-------|--------|--------|--------|--------|--------|\n")
        for i, r in enumerate(results, 1):
            f.write(
                f"| {i} | `{r['model']}` | {r['result']} | {r.get('finish_reason') or '—'} | "
                f"{r.get('total_tokens') or '—'} | {r.get('rate_wait_s')} | "
                f"`{r.get('report') or '—'}` |\n"
            )
        f.write(
            "\nClaude: merge findings; prefer consensus CRITICAL/HIGH; note disagreements.\n"
        )

    # stdout summary for humans
    print(json.dumps({"ok": ok_n, "truncated": trunc_n, "other": fail_n, "csv": str(csv_path), "index": str(index)}, indent=2))
    print("RESULT=OK" if fail_n == 0 else f"RESULT=PARTIAL", flush=True)
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
