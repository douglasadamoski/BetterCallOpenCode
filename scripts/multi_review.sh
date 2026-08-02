#!/usr/bin/env bash
# multi_review.sh — same critique across multiple free models under free RPM budget.
#
# Default: parallel fan-out via panel_run.py (sliding-window 20 req/min free cap).
# Legacy sequential mode: --sequential
#
# Usage:
#   multi_review.sh --prompt-file <f> --out-dir <dir> --scope <dir> \
#     [--models "id1,id2"] [--preset coding-panel|fast-panel|nvidia-panel] \
#     [--all-free] [--backend or-api] [--rpm 20] [--max-workers N] \
#     [--max-tokens 4096] [--retry-quota] [--sequential] [--sleep 3]
set -uo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$LIB_DIR/_bcoc_common.sh"
bcoc_load_config_files

die() { echo "$1" >&2; echo "RESULT=ERROR"; exit 1; }
need() { [[ -n "${2:-}" && "${2:0:1}" != "-" ]] || die "Missing value for $1"; }

PROMPT_FILE=""; OUT_DIR=""; SCOPE="$(pwd)"; BACKEND="or-api"
MODELS=""; PRESET=""; SLEEP_S=3; CAP="${DEFAULT_CAP}"
ALLOW_PAID=0; ALL_FREE=0; SEQUENTIAL=0; RETRY_QUOTA=0
RPM="${BCOPENCODE_FREE_RPM:-20}"; MAX_WORKERS=0
MAX_TOKENS=4096; MAX_INPUT=8000; TIMEOUT=240

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
    --rpm)         need "$1" "${2:-}"; RPM="$2"; shift 2;;
    --max-workers) need "$1" "${2:-}"; MAX_WORKERS="$2"; shift 2;;
    --max-tokens)  need "$1" "${2:-}"; MAX_TOKENS="$2"; shift 2;;
    --max-input-tokens) need "$1" "${2:-}"; MAX_INPUT="$2"; shift 2;;
    --timeout)     need "$1" "${2:-}"; TIMEOUT="$2"; shift 2;;
    --all-free)    ALL_FREE=1; shift;;
    --sequential)  SEQUENTIAL=1; shift;;
    --retry-quota) RETRY_QUOTA=1; shift;;
    --allow-paid)  ALLOW_PAID=1; shift;;
    -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) die "Unknown arg: $1";;
  esac
done

[[ -n "$PROMPT_FILE" && -f "$PROMPT_FILE" ]] || die "Need --prompt-file"
[[ -n "$OUT_DIR" ]] || die "Need --out-dir"
mkdir -p "$OUT_DIR"

# ---- parallel path (default): sliding-window free RPM ----
if [[ "$SEQUENTIAL" -eq 0 ]]; then
  args=(
    --prompt-file "$PROMPT_FILE"
    --out-dir "$OUT_DIR"
    --scope "$SCOPE"
    --backend "$BACKEND"
    --rpm "$RPM"
    --cap "$CAP"
    --max-tokens "$MAX_TOKENS"
    --max-input-tokens "$MAX_INPUT"
    --timeout "$TIMEOUT"
  )
  [[ "$MAX_WORKERS" -gt 0 ]] && args+=(--max-workers "$MAX_WORKERS")
  [[ "$ALLOW_PAID" -eq 1 ]] && args+=(--allow-paid)
  [[ "$RETRY_QUOTA" -eq 1 ]] && args+=(--retry-quota)
  if [[ "$ALL_FREE" -eq 1 ]]; then
    args+=(--all-free)
  elif [[ -n "$PRESET" ]]; then
    args+=(--preset "$PRESET")
  elif [[ -n "$MODELS" ]]; then
    args+=(--models "$MODELS")
  else
    die "Need --models, --preset, or --all-free"
  fi
  python3 "$LIB_DIR/panel_run.py" "${args[@]}"
  exit $?
fi

# ---- legacy sequential path ----
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
  *) die "Unknown --preset: $PRESET";;
esac
[[ -n "$MODELS" || "$ALL_FREE" -eq 1 ]] || MODELS="nvidia/nemotron-3-ultra-550b-a55b:free"
if [[ "$ALL_FREE" -eq 1 ]]; then
  MODELS="$(python3 "$LIB_DIR/list_free_models.py" --json | python3 -c 'import json,sys;d=json.load(sys.stdin);print(",".join(m["id"] for m in d["models"]))')"
fi

IFS=',' read -r -a ARR <<< "$MODELS"
INDEX="$OUT_DIR/MULTI_INDEX.md"
TS="$(bcoc_utc_ts)"
{
  echo "# BetterCallOpenCode multi-model review (sequential)"
  echo; echo "- **When:** $TS"; echo "- **Scope:** \`$SCOPE\`"
  echo; echo "| # | Model | RESULT | Report |"
  echo "|---|-------|--------|--------|"
} > "$INDEX"

n=0
for raw in "${ARR[@]}"; do
  raw="$(echo "$raw" | xargs)"
  [[ -n "$raw" ]] || continue
  m="$(bcoc_normalize_model "$raw")"
  if ! bcoc_is_free_model "$m" && [[ "$ALLOW_PAID" -ne 1 ]]; then
    echo "| - | \`$m\` | PAID_BLOCKED | — |" >> "$INDEX"
    continue
  fi
  n=$((n+1))
  safe="$(echo "$m" | tr '/:' '__')"
  out="$OUT_DIR/REVIEW_${n}_${safe}.md"
  flags=(--prompt-file "$PROMPT_FILE" --out "$out" --scope "$SCOPE" --backend "$BACKEND" --model "$m" --cap "$CAP" --max-tokens "$MAX_TOKENS")
  [[ "$ALLOW_PAID" -eq 1 ]] && flags+=(--allow-paid)
  echo "=== [$n] $m ===" >&2
  set +e
  bash "$LIB_DIR/opencode_review.sh" "${flags[@]}"
  set -e
  rline="$(grep -E '^\- \*\*RESULT:\*\*' "$out" 2>/dev/null | head -1 | sed 's/.*RESULT:\*\* //' || echo '?')"
  echo "| $n | \`$m\` | $rline | [\`$(basename "$out")\`]($(basename "$out")) |" >> "$INDEX"
  [[ $n -lt ${#ARR[@]} ]] && sleep "$SLEEP_S"
done
echo >> "$INDEX"
echo "Claude: merge findings across reports." >> "$INDEX"
echo "Index: $INDEX" >&2
echo "RESULT=OK"
