#!/usr/bin/env bash
# _bcoc_common.sh — shared helpers for BetterCallOpenCode wrappers.
#
# shellcheck shell=bash
# shellcheck disable=SC2034
#   This is a sourced library. shellcheck analyses it in isolation and cannot see that
#   CLIENT_PY, PACK_PY and the DEFAULT_* vars are consumed by opencode_review.sh and
#   multi_review.sh. Every SC2034 here is that false positive.

# --- bash version gate (must precede `declare -A` below) ---
# Floor is 4.4, not 4.0: `declare -A` needs 4.0, `${ext,,}` 4.0, `${val:1:-1}` 4.2, and
# expanding an empty array under `set -u` needs 4.4. macOS ships 3.2.57, where this file
# fails at parse time — so the gate has to be the first thing that runs, and must not
# itself use any 4.x syntax.
if [ -z "${BASH_VERSINFO:-}" ] || [ "${BASH_VERSINFO[0]}" -lt 4 ] ||
   { [ "${BASH_VERSINFO[0]}" -eq 4 ] && [ "${BASH_VERSINFO[1]}" -lt 4 ]; }; then
  echo "bcoc: requires bash >= 4.4 (found ${BASH_VERSION:-unknown})." >&2
  echo "bcoc: macOS ships bash 3.2 — install a modern one: brew install bash" >&2
  echo "RESULT=ERROR"
  exit 1
fi

_BCOC_SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$_BCOC_SCRIPTS/.." && pwd)"
CLIENT_PY="$_BCOC_SCRIPTS/or_client.py"
PACK_PY="$_BCOC_SCRIPTS/pack_context.py"

bcoc_utc_day() { date -u +%Y-%m-%d; }
bcoc_utc_ts() { date -u +%Y%m%dT%H%M%SZ; }

# --- config files ---------------------------------------------------------------
# Loaded BEFORE the DEFAULT_* assignments below, because a config file that cannot set
# the model or the cap is not a config file. Previously the defaults were captured at
# source time and the config load ran later from the wrapper, so BCOPENCODE_MODEL/CAP/
# STATE_DIR were silently ignored while ALLOW_PAID/AGENT/OPENCODE_CONFIG_DIR did take
# effect — exactly the wrong subset.
#
# Never `source`d: parsed as KEY=VALUE so a hostile file cannot execute anything.

# What a USER-owned config file may set (~/.config/… and the skill's own dir).
_BCOC_KEYS_USER="MODEL CAP MAX_TOKENS MAX_INPUT_TOKENS TIMEOUT TEMPERATURE BACKEND \
STATE_DIR FREE_RPM REASONING_MAX_TOKENS AGENT OPENCODE_CONFIG_DIR ALLOW_PAID \
UNSAFE_OPENCODE KEEP_RUN STRICT_SCAN API_KEY"

# What a file inside the REVIEWED REPO may set. Deliberately tiny.
#
# The reviewed repository is untrusted input. A `.bettercallopencode.env` committed to a
# project used to be loaded LAST, so it beat the user's own config and could set
# BCOPENCODE_ALLOW_PAID=1 (spend the user's credits), BCOPENCODE_UNSAFE_OPENCODE=1 and
# BCOPENCODE_BACKEND=opencode (run an unrestricted agent with shell access), or
# OPENROUTER_API_KEY (bill the whole packed codebase to an attacker's account, where
# prompt logging can read it). Reproduced end to end. None of those are on this list,
# and OPENROUTER_* is never accepted from a repo at all.
_BCOC_KEYS_REPO="MODEL MAX_TOKENS MAX_INPUT_TOKENS TIMEOUT TEMPERATURE FREE_RPM \
REASONING_MAX_TOKENS"

_bcoc_key_allowed() {  # $1=bare key (no BCOPENCODE_ prefix), $2=allowlist
  local k
  for k in $2; do [[ "$k" == "$1" ]] && return 0; done
  return 1
}

_bcoc_load_one_config() {  # $1=file, $2=allowlist, $3=label
  local f="$1" allow="$2" label="$3" line key val bare
  [[ -f "$f" ]] || return 0
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
    # shellcheck disable=SC2016
    #   The '$(' and '`' below are LITERALS being matched, not expansions — this glob
    #   is what rejects command substitution smuggled into a config value.
    if [[ "$val" == *$'\n'* || "$val" == *'`'* || "$val" == *'$('* ]]; then
      echo "bcoc: refusing suspicious value for $key in $f" >&2
      continue
    fi
    # Anything already exported in the caller's environment always wins over any file.
    [[ -n "${_BCOC_PRESET[$key]+x}" ]] && continue

    if [[ "$key" == OPENROUTER_* ]]; then
      bare="__OPENROUTER__"
    else
      bare="${key#BCOPENCODE_}"
    fi
    if ! _bcoc_key_allowed "$bare" "$allow"; then
      # Loud, never silent: the user must be able to see that a repo tried this.
      echo "bcoc: ignoring $key from $label config ($f) — not permitted from there" >&2
      continue
    fi
    export "$key=$val"
  done < "$f"
}

bcoc_load_config_files() {
  declare -gA _BCOC_PRESET=()
  local _k
  for _k in $(compgen -e | grep -E '^(BCOPENCODE_|OPENROUTER_)' || true); do
    _BCOC_PRESET["$_k"]=1
  done
  # Repo-local FIRST so the user's own config, loaded after, always wins over a value
  # supplied by the project under review.
  _bcoc_load_one_config "$(pwd)/.bettercallopencode.env" "$_BCOC_KEYS_REPO" "repo-local"
  _bcoc_load_one_config "$SKILL_DIR/.bettercallopencode.env" "$_BCOC_KEYS_USER" "skill"
  _bcoc_load_one_config "$HOME/.config/bettercallopencode/config.env" "$_BCOC_KEYS_USER" "user"
  _bcoc_load_one_config \
    "${XDG_CONFIG_HOME:-$HOME/.config}/bettercallopencode/config.env" \
    "$_BCOC_KEYS_USER" "user"
  unset _BCOC_PRESET
}

bcoc_load_config_files

# --- defaults (AFTER the config load, so config files actually work) ---
STATE_DIR="${BCOPENCODE_STATE_DIR:-$HOME/.bettercallopencode}"
USAGE_LOG="$STATE_DIR/usage.jsonl"

DEFAULT_MODEL="${BCOPENCODE_MODEL:-openrouter/nvidia/nemotron-3-ultra-550b-a55b:free}"
DEFAULT_CAP="${BCOPENCODE_CAP:-200}"
# 16k default: reasoning free models (gpt-oss, etc.) count thinking toward max_tokens.
DEFAULT_MAX_TOKENS="${BCOPENCODE_MAX_TOKENS:-16384}"
DEFAULT_MAX_INPUT_TOKENS="${BCOPENCODE_MAX_INPUT_TOKENS:-80000}"
DEFAULT_TIMEOUT="${BCOPENCODE_TIMEOUT:-600}"

# 0700: $RUN_DIR holds a full plaintext copy of the reviewed source. Under the default
# 0755 it was readable by every local user, for every project ever reviewed.
mkdir -p "$STATE_DIR" 2>/dev/null || true
chmod 700 "$STATE_DIR" 2>/dev/null || true

# --- ledger ---------------------------------------------------------------------
# THE definition of "billed": a row counts against the cap when the model was actually
# invoked. Set BCOC_BILLED=1 immediately before launching the client and clear it only
# on positive proof nothing reached the model. Bias to over-counting — under-counting
# spends the user's real quota, over-counting only delays them.
#
# Prints the count on stdout, or the literal string "UNAVAILABLE".
bcoc_cap_used() {
  local day
  day="$(bcoc_utc_day)"
  [[ -e "$USAGE_LOG" ]] || { echo 0; return 0; }
  # No `|| echo 0` fallback here. The old version turned any read failure into 0, so the
  # cap silently became infinite on exactly the filesystem condition where it matters.
  python3 - "$USAGE_LOG" "$day" <<'PY'
import json,sys
path, day = sys.argv[1], sys.argv[2]
# Outcomes that provably did not reach the model, for rows written before `billed` existed.
NOT_BILLED = {"AUTH", "CAP", "UNREACHABLE", "QUOTA", "PAID_BLOCKED", "REFUSED", "BAD_ARGS"}
n = 0
try:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue          # a malformed line is skipped, never fatal
            if o.get("day") != day:
                continue
            billed = o.get("billed")
            if billed is None:    # legacy row
                billed = o.get("result") not in NOT_BILLED
            if billed:
                n += 1
except OSError as e:
    print("UNAVAILABLE", file=sys.stdout)
    print(f"bcoc: cannot read ledger {path}: {e}", file=sys.stderr)
    sys.exit(0)
print(n)
PY
}

# args: result model backend mode free billed prompt_tokens completion_tokens total_tokens cost
# Returns non-zero if the row could not be appended. Callers MUST NOT let that abort the
# run — a spent request with no ledger row is bad, but a spent request with no RESULT=
# line is worse.
bcoc_usage_append() {
  local result="$1" model="$2" backend="$3" mode="$4"
  local free="${5:-true}" billed="${6:-true}"
  local prompt_tokens="${7:-}" completion_tokens="${8:-}" total_tokens="${9:-}" cost="${10:-}"
  local day ts
  day="$(bcoc_utc_day)"
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  mkdir -p "$STATE_DIR" 2>/dev/null || true
  python3 - "$USAGE_LOG" "$ts" "$day" "$result" "$model" "$backend" "$mode" \
    "$free" "$billed" "$prompt_tokens" "$completion_tokens" "$total_tokens" "$cost" <<'PY'
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
def flag(x): return (x or "").strip().lower() in ("1","true","yes")
row={
  "ts":sys.argv[2],"day":sys.argv[3],"result":sys.argv[4],"model":sys.argv[5],
  "backend":sys.argv[6],"mode":sys.argv[7],
  "free": flag(sys.argv[8]),
  "billed": flag(sys.argv[9]),
  "prompt_tokens":num(sys.argv[10]),"completion_tokens":num(sys.argv[11]),
  "total_tokens":num(sys.argv[12]),"cost":fnum(sys.argv[13]),
}
try:
    with open(path,"a",encoding="utf-8") as f:
        # Fail OPEN. CIFS and NFS return ENOLCK/EACCES for flock; treating that as fatal
        # meant the row was never written AND the script died before printing RESULT=.
        # An unlocked append of a single short line is far better than no row at all.
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
            locked = True
        except OSError as e:
            locked = False
            print(f"bcoc: ledger lock unavailable ({e}); appending unlocked", file=sys.stderr)
        try:
            f.write(json.dumps(row,ensure_ascii=False)+"\n")
            f.flush()
        finally:
            if locked:
                try: fcntl.flock(f, fcntl.LOCK_UN)
                except OSError: pass
except OSError as e:
    print(f"bcoc: WARNING could not record this call in the ledger: {e}", file=sys.stderr)
    print("bcoc: cap accounting may undercount until this is fixed.", file=sys.stderr)
    sys.exit(1)
PY
}

bcoc_have_python() { command -v python3 >/dev/null 2>&1; }

# Resolve a working timeout binary once. macOS has neither without `brew install coreutils`.
bcoc_resolve_timeout() {
  if command -v timeout >/dev/null 2>&1; then printf 'timeout'; return 0; fi
  if command -v gtimeout >/dev/null 2>&1; then printf 'gtimeout'; return 0; fi
  return 1
}

bcoc_realpath() {
  local r
  r="$(readlink -f "$1" 2>/dev/null)" && [[ -n "$r" ]] && { printf '%s' "$r"; return 0; }
  python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$1" 2>/dev/null
}

# --- model id -------------------------------------------------------------------
# MUST stay an exact mirror of or_client.normalize_model. It drifted once: this function
# collapsed openrouter/openrouter/free -> openrouter/free in a while-loop, which sends the
# opencode backend provider=openrouter model=free and breaks the free router — a model
# that ships in the `fast-panel` preset. tests/test_free_gate.py asserts parity between
# the two implementations on every id; do not edit one without the other.
bcoc_normalize_model() {
  local m="$1"
  # The free router is a real model id whose provider segment IS "openrouter".
  case "$m" in
    free|openrouter/free|openrouter/openrouter/free)
      printf '%s' "openrouter/openrouter/free"; return 0;;
  esac
  m="${m#openrouter/}"
  printf '%s' "openrouter/$m"
}

# Free gate: :free suffix, or the free router.
bcoc_is_free_model() {
  local m id
  m="$(bcoc_normalize_model "$1")"
  id="${m#openrouter/}"
  [[ "$id" == *:free ]] && return 0
  [[ "$id" == "openrouter/free" ]] && return 0
  return 1
}

# --- redaction ------------------------------------------------------------------
# Kept deliberately broad; it is applied to text that is about to be shown or written.
# The old version caught 2 token families out of 8 and half-redacted `sk-or-v1-a_b`
# because its character class excluded _ and -.
bcoc_redact() {
  sed -E \
    -e 's/[Bb]earer[[:space:]]+[A-Za-z0-9._~+/-]{8,}/Bearer ***/g' \
    -e 's/[Aa]uthorization:[[:space:]]*[A-Za-z0-9._~+/-]{8,}/Authorization: ***/g' \
    -e 's/(api[_-]?key|access[_-]?token|secret)(["=: ]+)[^[:space:]"'"'"']+/\1\2***/Ig' \
    -e 's/sk-or-v1-[A-Za-z0-9_-]+/sk-or-v1-***/g' \
    -e 's/sk-ant-[A-Za-z0-9_-]+/sk-ant-***/g' \
    -e 's/sk-proj-[A-Za-z0-9_-]+/sk-proj-***/g' \
    -e 's/gh[pousr]_[A-Za-z0-9]{20,}/gh*_***/g' \
    -e 's/github_pat_[A-Za-z0-9_]{20,}/github_pat_***/g' \
    -e 's/xox[bpasr]-[A-Za-z0-9-]{10,}/xox*-***/g' \
    -e 's/(AKIA|ASIA)[0-9A-Z]{16}/\1***/g' \
    -e 's/AIza[A-Za-z0-9_-]{30,}/AIza***/g' \
    -e 's/-----BEGIN [A-Z ]*PRIVATE KEY-----/-----BEGIN PRIVATE KEY [REDACTED]-----/g'
}
