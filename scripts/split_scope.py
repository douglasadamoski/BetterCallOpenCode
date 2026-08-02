#!/usr/bin/env python3
"""split_scope.py — partition a scope tree into token-budgeted file chunks for staged reviews.

Usage:
  split_scope.py <scope_dir> --max-input-tokens 40000 --out-dir /tmp/chunks
  Prints JSON list of chunk files to stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Reuse packer heuristics lightly
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".tox", ".mypy_cache", ".pytest_cache", "target", "vendor", ".next",
}
SKIP_FILES = {".env", ".env.local", ".env.rc"}
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


def approx_tokens(text: str) -> int:
    # rough: 4 chars ≈ 1 token
    return max(1, len(text) // 4)


def should_skip(path: Path) -> bool:
    name = path.name
    if name in SKIP_FILES or name.startswith(".env"):
        return True
    if name.endswith(SECRET_SUFFIXES):
        return True
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".zip", ".gz", ".so", ".dll", ".bin"}:
        return True
    return False


def collect_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            p = Path(dirpath) / fn
            if should_skip(p):
                continue
            try:
                if p.stat().st_size > 400_000:
                    continue
            except OSError:
                continue
            out.append(p)
    out.sort(key=lambda p: str(p))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scope")
    ap.add_argument("--max-input-tokens", type=int, default=40000)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--chunks", type=int, default=0, help="Force max chunk count (0=auto)")
    args = ap.parse_args()

    root = Path(args.scope).resolve()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    files = collect_files(root)
    budget = max(2000, args.max_input_tokens)
    chunks: list[list[tuple[Path, str]]] = []
    cur: list[tuple[Path, str]] = []
    cur_tok = 0

    for p in files:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        t = approx_tokens(text) + 20
        if cur and cur_tok + t > budget:
            chunks.append(cur)
            cur = []
            cur_tok = 0
        if t > budget:
            # single oversized file — truncate
            keep = budget * 4
            text = text[:keep] + "\n\n… [truncated by split_scope.py]\n"
            t = approx_tokens(text)
            chunks.append([(p, text)])
            continue
        cur.append((p, text))
        cur_tok += t
    if cur:
        chunks.append(cur)

    if args.chunks and args.chunks > 0 and len(chunks) > args.chunks:
        # merge greedily into N by rebalancing is complex; just cap by merging tail
        while len(chunks) > args.chunks:
            a = chunks.pop()
            chunks[-1].extend(a)

    paths = []
    for i, chunk in enumerate(chunks):
        body_lines = [f"# Chunk {i+1}/{len(chunks)} of {root}\n"]
        for p, text in chunk:
            rel = p.relative_to(root)
            body_lines.append(f"\n=== FILE: {rel} ===\n")
            body_lines.append(text)
            if not text.endswith("\n"):
                body_lines.append("\n")
        outp = out_dir / f"chunk_{i+1:03d}.txt"
        outp.write_text("".join(body_lines), encoding="utf-8")
        paths.append(str(outp))

    print(json.dumps({"scope": str(root), "n_chunks": len(paths), "chunks": paths, "n_files": len(files)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
