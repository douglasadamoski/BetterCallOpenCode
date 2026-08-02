#!/usr/bin/env bash
# multi_review.sh — run the same critique against multiple free models sequentially.
#
# Usage:
#   multi_review.sh --prompt-file <f> --out-dir <dir> --scope <dir> \
#     [--models "id1,id2,id3"] [--preset coding-panel|fast-panel|nvidia-panel] \
#     [--backend or-api] [--sleep 3]
set -uo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$LIB_DIR/_bcoc_common.sh"
bcoc_load_config_files

die() { echo "$1" >&2; echo "RESULT=ERROR"; exit 1; }
need() { [[ -n "${2:-}" && "${2:0:1}" != "-" ]] || die "Missing value for $1"; }

PROMPT_FILE=""; OUT_DIR=""; SCOPE="$(pwd)"; BACKEND="or-api"
MODELS=""; PRESET=""; SLEEP_S=3; CAP="${DEFAULT_CAP}"
ALLOW_PAID=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prompt-file) need "$1" "${2:-}"; PROMPT_FILE="$2"; shift 2;;
    --out-dir)     need "$1" "${2:-}"; OUT_DIR="$2"; shift 2;;
    --scope)       need "$1" "${2:-}"; SCOPE="$2"; shift 2;;
    --models)      need "$1" "${2:-}"; MODELS="$2"; shift 2;;
    --preset)      need "$1" "${2:-}"; PRESET="$2"; shift 2;;
    --backend)     need "$1" "${2:-}"; BACKEND="$2"; shift 2;;
    --sleep)       need "$1" "${2:-}"; SLEEP_S="$2"; shift 2;;
    --cap)         need "$1" "${2:-}"; CAP="$2"; shift 2;;
    --allow-paid)  ALLOW_PAID=1; shift;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) die "Unknown arg: $1";;
  esac
done

[[ -n "$PROMPT_FILE" && -f "$PROMPT_FILE" ]] || die "Need --prompt-file"
[[ -n "$OUT_DIR" ]] || die "Need --out-dir"
mkdir -p "$OUT_DIR"

case "$PRESET" in
  coding-panel)
    MODELS="${MODELS:-nvidia/nemotron-3-ultra-550b-a55b:free,nvidia/nemotron-3-super-120b-a12b:free,cohere/north-mini-code:free,openai/gpt-oss-20b:free}"
    ;;
  fast-panel)
    MODELS="${MODELS:-nvidia/nemotron-3-nano-30b-a3b:free,google/gemma-4-26b-a4b-it:free,inclusionai/ling-3.0-flash:free,openrouter/free}"
    ;;
  nvidia-panel)
    MODELS="${MODELS:-nvidia/nemotron-3-ultra-550b-a55b:free,nvidia/nemotron-3-super-120b-a12b:free,nvidia/nemotron-3-nano-30b-a3b:free,nvidia/nemotron-nano-9b-v2:free}"
    ;;
  "") ;;
  *) die "Unknown --preset: $PRESET (coding-panel|fast-panel|nvidia-panel)";;
esac

[[ -n "$MODELS" ]] || MODELS="nvidia/nemotron-3-ultra-550b-a55b:free"

IFS=',' read -r -a ARR <<< "$MODELS"
INDEX="$OUT_DIR/MULTI_INDEX.md"
TS="$(bcoc_utc_ts)"
{
  echo "# BetterCallOpenCode multi-model review"
  echo
  echo "- **When:** $TS"
  echo "- **Scope:** \`$SCOPE\`"
  echo "- **Preset/models:** $PRESET / $MODELS"
  echo
  echo "| # | Model | RESULT | Report |"
  echo "|---|-------|--------|--------|"
} > "$INDEX"

n=0
for raw in "${ARR[@]}"; do
  raw="$(echo "$raw" | xargs)"  # trim
  [[ -n "$raw" ]] || continue
  m="$(bcoc_normalize_model "$raw")"
  if ! bcoc_is_free_model "$m" && [[ "$ALLOW_PAID" -ne 1 ]]; then
    echo "Skip non-free $m (no --allow-paid)" >&2
    echo "| - | \`$m\` | PAID_BLOCKED | — |" >> "$INDEX"
    continue
  fi
  n=$((n+1))
  safe="$(echo "$m" | tr '/:' '__')"
  out="$OUT_DIR/REVIEW_${n}_${safe}.md"
  flags=(--prompt-file "$PROMPT_FILE" --out "$out" --scope "$SCOPE" --backend "$BACKEND" --model "$m" --cap "$CAP")
  [[ "$ALLOW_PAID" -eq 1 ]] && flags+=(--allow-paid)
  echo "=== [$n] $m ===" >&2
  set +e
  bash "$LIB_DIR/opencode_review.sh" "${flags[@]}"
  rc=$?
  set -e
  res="$(tail -1 "$out" 2>/dev/null | grep RESULT= || true)"
  # also try last line of script stdout was already RESULT= — parse report header
  rline="$(grep -E '^\- \*\*RESULT:\*\*' "$out" 2>/dev/null | head -1 | sed 's/.*RESULT:\*\* //' || echo '?')"
  echo "| $n | \`$m\` | $rline | [\`$(basename "$out")\`]($(basename "$out")) |" >> "$INDEX"
  if [[ $n -lt ${#ARR[@]} ]]; then
    sleep "$SLEEP_S"
  fi
done

echo >> "$INDEX"
echo "Claude: merge findings across reports; prefer consensus CRITICAL/HIGH; note model disagreements." >> "$INDEX"
echo "Index: $INDEX" >&2
echo "RESULT=OK"
