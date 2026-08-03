#!/usr/bin/env python3
"""One-line summary of a pack_context --mirror-to run, for the report header."""
import json
import sys

d = json.load(open(sys.argv[1], encoding="utf-8"))
n = len(d.get("secrets_skipped_by_name", [])) + len(d.get("secrets_skipped_by_content", []))
print(f"files={d.get('files_copied')} secrets_withheld={n}")
