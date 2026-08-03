#!/usr/bin/env python3
"""split_scope.py — partition a scope tree into token-budgeted file chunks for staged reviews.

Usage:
  split_scope.py <scope_dir> --max-input-tokens 40000 --out-dir /tmp/chunks
  Prints JSON with the chunk files and the withheld-file manifest.

This is Mode D's packer. It deliberately owns NO filtering logic of its own: it reuses
`pack_context`'s secret classification, gitignore handling, binary detection and directory
pruning. It previously had its own tiny skip list (`.env*`, `*.pem`, `*.key`, `*.p12`,
`*.pfx`) with no content scanning, no gitignore handling, no symlink defence and no
withheld-file accounting — so the documented staged workflow bypassed every hardening the
main packer had, and a credential in `settings.yaml`, `auth.json` or `.pgpass` would have
shipped. One classifier, one place to fix it.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Dict, List

_spec = importlib.util.spec_from_file_location(
    "pack_context", Path(__file__).resolve().parent / "pack_context.py"
)
pack_context = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pack_context)


def approx_tokens(text: str) -> int:
    return pack_context.est_tokens(text)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scope")
    ap.add_argument("--max-input-tokens", type=int, default=40000)
    ap.add_argument("--max-file-bytes", type=int, default=120000)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--chunks", type=int, default=0, help="Force max chunk count (0=auto)")
    args = ap.parse_args()

    root = Path(args.scope).resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 2
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    patterns = pack_context.load_gitignore(root)
    withheld_by_name: List[str] = []
    pruned: List[str] = []
    symlinks: List[str] = []
    ignored_files: List[str] = []
    ignored_with_creds: List[str] = []
    ignored_total = [0]
    ranked = sorted(
        pack_context.iter_files(
            root, patterns, withheld_by_name, pruned, symlinks,
            ignored_files, ignored_with_creds, ignored_total,
        ),
        key=lambda x: (-x[0], x[2]),
    )

    withheld_by_content: List[str] = []
    skipped: List[Dict[str, str]] = []
    budget = max(2000, args.max_input_tokens)

    chunk_files: List[Dict[str, object]] = []
    cur: List[str] = []
    cur_tok = 0

    def flush() -> None:
        nonlocal cur, cur_tok
        if not cur:
            return
        idx = len(chunk_files) + 1
        path = out_dir / f"chunk_{idx:03d}.txt"
        path.write_text("".join(cur), encoding="utf-8")
        chunk_files.append({"path": str(path), "tokens_est": cur_tok, "files": len(cur)})
        cur, cur_tok = [], 0

    for _score, full, rel in ranked:
        try:
            raw = full.read_bytes()
        except OSError as e:
            skipped.append({"path": rel, "reason": str(e)})
            continue
        # Same content backstop as the main packer: a credential in an innocently named
        # file must not reach a chunk either.
        if pack_context.looks_secret_content(raw):
            withheld_by_content.append(rel)
            skipped.append({"path": rel, "reason": "secret-content"})
            continue
        if pack_context.looks_binary(raw):
            skipped.append({"path": rel, "reason": "binary"})
            continue
        truncated = False
        if len(raw) > args.max_file_bytes:
            raw = raw[: args.max_file_bytes]
            truncated = True
        text = raw.decode("utf-8", errors="replace")
        if truncated:
            text += f"\n\n… [truncated at {args.max_file_bytes} bytes]\n"
        f = pack_context.fence_for(text)
        block = f"## File: {pack_context.safe_path(rel)}\n{f}text\n{text}\n{f}\n\n"
        t = approx_tokens(block)
        if t > budget:
            keep = budget * 4
            text = text[:keep] + "\n\n… [truncated for chunk budget]\n"
            f = pack_context.fence_for(text)
            block = f"## File: {pack_context.safe_path(rel)}\n{f}text\n{text}\n{f}\n\n"
            t = approx_tokens(block)
        if cur and cur_tok + t > budget:
            flush()
        cur.append(block)
        cur_tok += t
    flush()

    out = {
        "root": str(root),
        "chunks": chunk_files,
        "files_seen": len(ranked),
        "skipped": skipped[:50],
        # Same "never silent" contract as pack_context: a withheld file must stay visible.
        "secrets_skipped_by_name": sorted(withheld_by_name),
        "secrets_skipped_by_content": sorted(withheld_by_content),
        "pruned_dirs": sorted(pruned),
        "symlinks_skipped": sorted(symlinks),
        # Gitignored files are withheld from the chunks too, and must be just as
        # visible here as in pack()'s meta — same "never silent" contract.
        "ignored_files": sorted(ignored_files),
        "ignored_files_count": ignored_total[0],
        "ignored_files_with_credentials": sorted(ignored_with_creds),
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
