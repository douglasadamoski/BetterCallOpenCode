#!/usr/bin/env bash
# opencode_usage.sh — summarize BetterCallOpenCode usage ledger.
set -uo pipefail
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$LIB_DIR/_bcoc_common.sh"

DAY="${1:-$(bcoc_utc_day)}"
echo "Ledger: $USAGE_LOG"
echo "Day: $DAY"
[[ -f "$USAGE_LOG" ]] || { echo "No usage yet."; exit 0; }
python3 - "$USAGE_LOG" "$DAY" <<'PY'
import json,sys
from collections import Counter
path, day = sys.argv[1], sys.argv[2]
rows=[]
with open(path, encoding="utf-8") as f:
    for line in f:
        line=line.strip()
        if not line: continue
        try: o=json.loads(line)
        except Exception: continue
        if o.get("day")==day: rows.append(o)
print(f"Calls today: {len(rows)}")
if not rows: raise SystemExit(0)
print("By RESULT:", dict(Counter(r.get("result") for r in rows)))
print("By model:")
for m,c in Counter(r.get("model") for r in rows).most_common():
    print(f"  {c:4d}  {m}")
pt=sum((r.get("prompt_tokens") or 0) for r in rows)
ct=sum((r.get("completion_tokens") or 0) for r in rows)
cost=sum((r.get("cost") or 0) for r in rows)
print(f"Tokens: prompt={pt} completion={ct} cost_sum={cost}")
PY
