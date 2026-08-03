#!/usr/bin/env python3
"""list_free_models.py — list / refresh OpenRouter free models.

Usage:
  list_free_models.py              # print table to stdout
  list_free_models.py --json
  list_free_models.py --refresh -o references/free_models_snapshot.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Reuse key resolution from or_client when available
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from or_client import resolve_api_key, OPENROUTER_BASE, auth_headers
except ImportError:
    OPENROUTER_BASE = "https://openrouter.ai/api/v1"

    def resolve_api_key(explicit=None):
        return (explicit or os.environ.get("OPENROUTER_API_KEY") or "").strip()

    def auth_headers(api_key: str):
        return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}


def fetch_models(api_key: str = "") -> list:
    req = urllib.request.Request(
        f"{OPENROUTER_BASE}/models",
        headers=auth_headers(api_key) if api_key else {"Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("data") or []


# Free, but NOT a code reviewer. Measured across 8 full 15-model matrices against this
# repo's own source (see references/openrouter_free_models.md for the table).
#
# The dangerous one is the moderation classifier. `nemotron-3.5-content-safety` scored
# 8/8 OK — a perfect success rate — while returning a 93-character "User Safety: safe"
# and zero findings. In a consensus panel that is worse than a model that fails: it
# inflates the success rate, dilutes agreement, and reads as a review that found nothing
# wrong. A model that always succeeds and never contributes is the hardest kind of
# useless to notice.
#
# Excluded by default from --all-free and the presets. `--include-non-reviewers` opts
# back in, because "free model that exists" and "model that can review code" are
# different questions and the roster tool should still be able to answer the first.
NOT_REVIEWERS = {
    "nvidia/nemotron-3.5-content-safety:free",   # content moderation classifier
}

# Free and capable, but so heavily rate-limited on OpenRouter's shared upstream pool that
# a panel slot spent on them is usually wasted. Same opt-in flag applies.
UNRELIABLE = {
    "google/gemma-4-31b-it:free",                # 0/8 OK, 8/8 HTTP 429 upstream
}


def is_reviewer(mid: str) -> bool:
    return mid not in NOT_REVIEWERS


def is_usable(mid: str) -> bool:
    return mid not in NOT_REVIEWERS and mid not in UNRELIABLE


def is_free_entry(m: dict) -> bool:
    mid = m.get("id") or ""
    if mid.endswith(":free") or mid == "openrouter/free":
        return True
    p = m.get("pricing") or {}
    try:
        pr = float(p.get("prompt") or 0)
        cr = float(p.get("completion") or 0)
    except Exception:
        return False
    return pr == 0.0 and cr == 0.0 and mid.endswith(":free")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="Fetch live catalog")
    ap.add_argument("-o", "--output", help="Write JSON snapshot")
    ap.add_argument("--api-key", default=None)
    ap.add_argument(
        "--include-non-reviewers",
        action="store_true",
        help="Include free models that are not usable code reviewers (a content-moderation "
             "classifier, and models permanently rate-limited upstream). Excluded by default "
             "so --all-free spends requests on models that can actually review.",
    )
    args = ap.parse_args()

    key = resolve_api_key(args.api_key)
    models = fetch_models(key)
    free = [m for m in models if is_free_entry(m)]
    if not args.include_non_reviewers:
        excluded = [m for m in free if not is_usable(m.get("id") or "")]
        free = [m for m in free if is_usable(m.get("id") or "")]
        for m in excluded:
            mid = m.get("id") or ""
            why = ("not a code reviewer" if mid in NOT_REVIEWERS
                   else "rate-limited upstream to the point of being unusable")
            print(f"list_free_models: excluding {mid} — {why} "
                  f"(--include-non-reviewers to override)", file=sys.stderr)
    free.sort(key=lambda m: m.get("id") or "")

    rows = []
    for m in free:
        tp = m.get("top_provider") or {}
        arch = m.get("architecture") or {}
        rows.append(
            {
                "id": m.get("id"),
                "name": m.get("name"),
                "context_length": m.get("context_length"),
                "max_completion_tokens": tp.get("max_completion_tokens"),
                "modality": arch.get("modality"),
                "opencode_model": f"openrouter/{m.get('id')}",
                "description": (m.get("description") or "")[:280],
            }
        )

    snap = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(rows),
        "models": rows,
    }

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(snap, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.output} ({len(rows)} free models)", file=sys.stderr)

    if args.json:
        print(json.dumps(snap, indent=2))
    else:
        print(f"{'ID':<55} {'CTX':>8} {'MAX_OUT':>8}  NAME")
        print("-" * 100)
        for r in rows:
            print(
                f"{r['id']:<55} {str(r['context_length'] or '-'):>8} "
                f"{str(r['max_completion_tokens'] or '-'):>8}  {r['name']}"
            )
        print(f"\n{len(rows)} free models. OpenCode: -m openrouter/<id>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
