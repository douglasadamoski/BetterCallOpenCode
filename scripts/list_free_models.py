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
    args = ap.parse_args()

    key = resolve_api_key(args.api_key)
    models = fetch_models(key)
    free = [m for m in models if is_free_entry(m)]
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
