#!/usr/bin/env python3
"""select_models.py — rank and pick models from the saved capabilities cache.

Reads what discover_providers.py saved; never prompts and never needs the network, so it
is safe from CI and from subagents. The interactive part ("which of these do you want?")
belongs to the caller: run --list, show the user the table, let them choose. When nobody
is there to ask, --auto picks deterministically and prints what it picked.

  select_models.py --task review --list
  select_models.py --task research --n 3 --auto
  select_models.py --task vision --n 1 --auto --json
  select_models.py --task code --min-ctx 100000 --list --include-paid

Tasks: review | research | code | fast | vision
Gate: only zero-cost models unless --include-paid. "Zero-cost" is the provider-published
price, or — for a provider that publishes none — the one-time answer recorded by
`discover_providers.py --set-policy`. An undecided provider contributes nothing.

Model ids are printed as `provider/model` (opencode's own form).
Exit: 0 ok (even when the list is empty — read stderr) · 2 usage.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discover_providers as dp  # noqa: E402

TASKS = ("review", "research", "code", "fast", "vision")

# Free, but not code/research workers (measured in this repo's reliability matrices).
# Ids are OpenRouter ids. Kept in sync with list_free_models.py by test_select.py.
NOT_REVIEWERS = {"nvidia/nemotron-3.5-content-safety:free"}
UNRELIABLE = {"google/gemma-4-31b-it:free"}

_NOT_CHAT_HINTS = ("embed", "rerank", "moderation", "content-safety", "guard", "whisper",
                   "tts", "image-gen", "dall-e")
_CODE_HINTS = ("coder", "code", "devstral", "codestral", "laguna")
_FAST_HINTS = ("flash", "nano", "mini", "lite", "small", "haiku", "xs", "lightning", "8b", "7b")


def family(mid: str) -> str:
    """Coarse model family for diversity: the vendor prefix, else the first name token."""
    base = mid.split("/")[0] if "/" in mid else mid
    return base.split("-")[0].split(":")[0].lower()


def candidates(cache: Dict[str, Any], include_paid: bool = False,
               include_non_reviewers: bool = False) -> List[Dict[str, Any]]:
    out = []
    for pid, prov in sorted((cache.get("providers") or {}).items()):
        for mid, caps in sorted((prov.get("models") or {}).items()):
            cost = dp.effective_cost(cache, pid, mid)
            if cost == dp.COST_UNKNOWN:
                continue                      # undecided money status never gets picked
            if cost == dp.COST_PAID and not include_paid:
                continue
            low = mid.lower()
            if any(h in low for h in _NOT_CHAT_HINTS) and not include_non_reviewers:
                continue
            if pid == "openrouter" and not include_non_reviewers and (
                    mid in NOT_REVIEWERS or mid in UNRELIABLE):
                continue
            out.append({"id": f"{pid}/{mid}", "provider": pid, "model": mid,
                        "cost": cost, **{k: caps.get(k) for k in (
                            "ctx", "max_out", "tools", "reasoning", "modalities",
                            "capabilities", "last_ok", "name")}})
    return out


def score(c: Dict[str, Any], task: str, min_ctx: int = 0) -> Optional[float]:
    """Higher is better; None means this model cannot do the task at all."""
    ctx = c.get("ctx")
    if min_ctx and (ctx is None or ctx < min_ctx):
        return None
    mods = [m.lower() for m in (c.get("modalities") or [])]
    if task == "vision" and "image" not in mods:
        return None
    s = 0.0
    if ctx:
        s += min(math.log10(ctx) - 3.5, 3.0)          # 32k ~ 1.0, 1M ~ 2.5
    if c.get("capabilities") == "known":
        s += 0.5
    low = c["model"].lower()
    if task == "research":
        s += 1.5 if c.get("tools") else 0.0
        s += 1.0 if c.get("reasoning") else 0.0
    elif task == "review":
        s += 1.0 if c.get("reasoning") else 0.0
        s += 0.5 if c.get("tools") else 0.0
    elif task == "code":
        s += 1.5 if any(h in low for h in _CODE_HINTS) else 0.0
        s += 0.5 if c.get("tools") else 0.0
    elif task == "fast":
        s += 2.0 if any(h in low for h in _FAST_HINTS) else 0.0
        s -= 0.5 if c.get("reasoning") else 0.0       # thinking costs latency
    elif task == "vision":
        s += 0.5 if c.get("reasoning") else 0.0
    if c.get("last_ok"):
        s += 0.5
    return round(s, 3)


def rank(cache: Dict[str, Any], task: str, include_paid: bool = False,
         min_ctx: int = 0, include_non_reviewers: bool = False) -> List[Dict[str, Any]]:
    rows = []
    for c in candidates(cache, include_paid, include_non_reviewers):
        sc = score(c, task, min_ctx)
        if sc is not None:
            rows.append({**c, "score": sc})
    rows.sort(key=lambda r: (-r["score"], r["id"]))
    return rows


def pick(rows: List[Dict[str, Any]], n: int) -> List[Dict[str, Any]]:
    """Greedy top-n with a diversity penalty: a second model from the same provider or
    family must beat an unseen one by a margin. Deterministic."""
    chosen: List[Dict[str, Any]] = []
    pool = list(rows)
    while pool and len(chosen) < n:
        seen_p = {c["provider"] for c in chosen}
        seen_f = {family(c["model"]) for c in chosen}

        def adj(r: Dict[str, Any]) -> Tuple[float, str]:
            pen = (0.6 if r["provider"] in seen_p else 0.0) + (
                0.8 if family(r["model"]) in seen_f else 0.0)
            return (-(r["score"] - pen), r["id"])

        best = min(pool, key=adj)
        chosen.append(best)
        pool.remove(best)
    return chosen


def render_table(rows: List[Dict[str, Any]]) -> str:
    head = f"{'#':>2}  {'MODEL':<58} {'CTX':>8} {'TOOLS':>5} {'REAS':>4} {'COST':<7} SCORE"
    lines = [head, "-" * len(head)]
    for i, r in enumerate(rows, 1):
        flag = lambda v: "-" if v is None else ("y" if v else "n")  # noqa: E731
        lines.append(f"{i:>2}  {r['id']:<58} {str(r['ctx'] or '-'):>8} "
                     f"{flag(r['tools']):>5} {flag(r['reasoning']):>4} {r['cost']:<7} {r['score']}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--task", choices=TASKS, default="review")
    ap.add_argument("--n", type=int, default=3)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="ranked table of every usable model")
    mode.add_argument("--auto", action="store_true", help="pick the top N, diverse, deterministic")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--min-ctx", type=int, default=0)
    ap.add_argument("--include-paid", action="store_true",
                    help="allow models that are not zero-cost (only with the user's consent)")
    ap.add_argument("--include-non-reviewers", action="store_true")
    args = ap.parse_args()
    if args.n < 1:
        print("--n must be >= 1", file=sys.stderr)
        return 2

    cache = dp.load_cache()
    rows = rank(cache, args.task, args.include_paid, args.min_ctx, args.include_non_reviewers)
    undecided = [p for p, v in (cache.get("providers") or {}).items()
                 if v.get("status") == "needs_policy"]
    if undecided:
        print(f"select: provider(s) {', '.join(undecided)} publish no pricing and have no "
              f"policy; their models are excluded. Ask the user, then run: "
              f"discover_providers.py --set-policy <provider> free|paid", file=sys.stderr)
    if not rows:
        print("select: no usable models. Run discover_providers.py first, or widen "
              "the filters.", file=sys.stderr)

    shown = rows if args.list or not args.auto else pick(rows, args.n)
    if args.json:
        print(json.dumps({"task": args.task, "models": shown,
                          "needs_policy": undecided}, indent=2))
    elif args.auto:
        for r in shown:
            print(r["id"])
    else:
        print(render_table(shown))
    return 0


if __name__ == "__main__":
    sys.exit(main())
