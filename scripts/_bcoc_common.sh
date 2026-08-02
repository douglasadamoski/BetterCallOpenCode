#!/usr/bin/env bash
# _bcoc_common.sh — shared helpers for BetterCallOpenCode wrappers.
# shellcheck shell=bash

_BCOC_SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$_BCOC_SCRIPTS/.." && pwd)"
# Install-independent ledger (plugin updates must not reset the cap).
STATE_DIR="${BCOPENCODE_STATE_DIR:-$HOME/.bettercallopencode}"
USAGE_LOG="$STATE_DIR/usage.jsonl"
CLIENT_PY="$_BCOC_SCRIPTS/or_client.py"
PACK_PY="$_BCOC_SCRIPTS/pack_context.py"
LIST_FREE_PY="$_BCOC_SCRIPTS/list_free_models.py"

DEFAULT_MODEL="${BCOPENCODE_MODEL:-openrouter/nvidia/nemotron-3-ultra-550b-a55b:free}"
DEFAULT_CAP="${BCOPENCODE_CAP:-200}"
DEFAULT_MAX_TOKENS="${BCOPENCODE_MAX_TOKENS:-8192}"
DEFAULT_MAX_INPUT_TOKENS="${BCOPENCODE_MAX_INPUT_TOKENS:-80000}"
DEFAULT_TIMEOUT="${BCOPENCODE_TIMEOUT:-600}"

mkdir -p "$STATE_DIR" 2>/dev/null || true

bcoc_utc_day() { date -u +%Y-%m-%d; }
bcoc_utc_ts() { date -u +%Y%m%dT%H%M%SZ; }

# Load optional config (never source — KEY=VALUE BCOPENCODE_* / OPENROUTER_API_KEY only).
bcoc_load_config_files() {
  local f line key val
  declare -A _BCOC_PRESET=()
  local _k
  for _k in $(compgen -e | grep -E '^(BCOPENCODE_|OPENROUTER_)' || true); do
    _BCOC_PRESET["$_k"]=1
  done
  for f in \
    "${XDG_CONFIG_HOME:-$HOME/.config}/bettercallopencode/config.env" \
    "$HOME/.config/bettercallopencode/config.env" \
    "$SKILL_DIR/.bettercallopencode.env" \
    "$(pwd)/.bettercallopencode.env"
  do
    [[ -f "$f" ]] || continue
    while IFS= read -r line || [[ -n "$line" ]]; do
      line="${line%$'\r'}"
      [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
      [[ "$line" =~ ^[[:space:]]*export[[:space:]]+ ]] && line="${line#*export }"
      line="${line#"${line%%[![:space:]]*}"}"
      [[ "$line" == *=* ]] || continue
      key="${line%%=*}"; val="${line#*=}"
      key="${key%"${key##*[![:space:]]}"}"
      [[ "$key" =~ ^(BCOPENCODE_|OPENROUTER_)[A-Z0-9_]+$ ]] || continue
      if [[ "$val" =~ ^\".*\"$ ]]; then val="${val:1:-1}"
      elif [[ "$val" =~ ^\'.*\'$ ]]; then val="${val:1:-1}"
      fi
      if [[ "$val" == *$'\n'* || "$val" == *'`'* || "$val" == *'$('* ]]; then
        echo "bcoc: refusing suspicious value for $key in $f" >&2
        continue
      fi
      [[ -n "${_BCOC_PRESET[$key]+x}" ]] && continue
      export "$key=$val"
    done < "$f"
  done
  unset _BCOC_PRESET
}

bcoc_cap_used() {
  local day
  day="$(bcoc_utc_day)"
  [[ -f "$USAGE_LOG" ]] || { echo 0; return; }
  python3 - "$USAGE_LOG" "$day" <<'PY' 2>/dev/null || echo 0
import json,sys
path, day = sys.argv[1], sys.argv[2]
n=0
try:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line=line.strip()
            if not line: continue
            try: o=json.loads(line)
            except Exception: continue
            if o.get("day")==day: n+=1
except FileNotFoundError:
    pass
print(n)
PY
}

bcoc_usage_append() {
  # args: result model backend mode free paid_blocked prompt_tokens completion_tokens total_tokens cost
  local result="$1" model="$2" backend="$3" mode="$4"
  local free="${5:-true}" paid_blocked="${6:-false}"
  local prompt_tokens="${7:-}" completion_tokens="${8:-}" total_tokens="${9:-}" cost="${10:-}"
  local day ts
  day="$(bcoc_utc_day)"
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  mkdir -p "$STATE_DIR"
  python3 - "$USAGE_LOG" "$ts" "$day" "$result" "$model" "$backend" "$mode" \
    "$free" "$paid_blocked" "$prompt_tokens" "$completion_tokens" "$total_tokens" "$cost" <<'PY'
import json,sys,fcntl
path=sys.argv[1]
def num(x):
    x=(x or "").strip()
    if not x: return None
    try: return int(float(x))
    except Exception: return None
def fnum(x):
    x=(x or "").strip()
    if not x: return None
    try: return float(x)
    except Exception: return None
row={
  "ts":sys.argv[2],"day":sys.argv[3],"result":sys.argv[4],"model":sys.argv[5],
  "backend":sys.argv[6],"mode":sys.argv[7],
  "free": sys.argv[8].lower() in ("1","true","yes"),
  "paid_blocked": sys.argv[9].lower() in ("1","true","yes"),
  "prompt_tokens":num(sys.argv[10]),"completion_tokens":num(sys.argv[11]),
  "total_tokens":num(sys.argv[12]),"cost":fnum(sys.argv[13]),
}
with open(path,"a",encoding="utf-8") as f:
    fcntl.flock(f, fcntl.LOCK_EX)
    try:
        f.write(json.dumps(row,ensure_ascii=False)+"\n")
        f.flush()
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
PY
}

bcoc_have_python() { command -v python3 >/dev/null 2>&1; }

bcoc_realpath() {
  local r
  r="$(readlink -f "$1" 2>/dev/null)" && [[ -n "$r" ]] && { printf '%s' "$r"; return 0; }
  python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$1" 2>/dev/null
}

# Normalize model to OpenCode form openrouter/<id> and bare OpenRouter form.
# Input may be: openrouter/foo/bar:free | foo/bar:free | openrouter/openrouter/free
bcoc_normalize_model() {
  local m="$1"
  m="${m#openrouter/}"
  # collapse double openrouter/
  while [[ "$m" == openrouter/* ]]; do m="${m#openrouter/}"; done
  printf '%s' "openrouter/$m"
}

bcoc_or_model_id() {
  # strip openrouter/ prefix for direct API
  local m
  m="$(bcoc_normalize_model "$1")"
  printf '%s' "${m#openrouter/}"
}

# Free gate: :free suffix, openrouter/free, or bare "free" router id.
bcoc_is_free_model() {
  local m id
  m="$(bcoc_normalize_model "$1")"
  id="${m#openrouter/}"
  [[ "$id" == *:free ]] && return 0
  [[ "$id" == "free" || "$id" == "openrouter/free" ]] && return 0
  return 1
}

bcoc_redact() {
  sed -E 's/[Bb]earer [A-Za-z0-9._~+\/-]{8,}/Bearer ***/g; s/(api[_-]?key["=: ]+)[^[:space:]"]+/\1***/Ig; s/sk-or-v1-[A-Za-z0-9]+/sk-or-v1-***/g'
}
