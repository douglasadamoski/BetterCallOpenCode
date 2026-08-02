#!/usr/bin/env bash
# run_local.sh — Claude executes an APPROVED, OpenCode critic-proposed script inside a sandbox
# dir. OpenCode critic (running read-only) never runs the scripts it proposes — it may inspect the
# repo with read-only commands, but cannot execute proposals or mutate anything; it only
# proposes script text in its report. Claude writes that script into a sandbox dir, reviews it, and then
# runs it here. Execution runs with cwd = the sandbox dir, in a conda env
# (--env / $BCOPENCODE_CONDA_ENV, default `base`). NOTE: this is NOT a security jail — it only
# verifies the script lives under the sandbox; review scripts before running them.
#
# Usage:
#   run_local.sh --sandbox <sandbox_dir> --script <path-under-sandbox> \
#                [--interpreter <cmd>] [--env <conda_env>] [--timeout <secs>] [-- <args...>]
#
# --interpreter overrides the extension heuristic and may include flags (e.g.
# "python -u", "ts-node", "node"). Pass it with whatever run command OpenCode critic specified
# for the script — don't rely on the heuristic for anything but .py/.r/.sh/.js/.rb/.pl.
# It is word-split on spaces (NO shell quoting), so keep it a simple "interpreter [flags]" —
# it can't carry quoted args like -c "code"; put that logic inside the script instead.
#
# Refuses to run if the script is not inside the sandbox dir.
set -uo pipefail

ENV="${BCOPENCODE_CONDA_ENV:-base}"; TIMEOUT=600; SANDBOX=""; SCRIPT=""; INTERP=""; ARGS=()
# Reject a missing value being swallowed from the next option (e.g. `--sandbox --script`).
need() { [[ -n "${2:-}" && "${2:0:1}" != "-" ]] || { echo "ERROR: $1 needs a value" >&2; exit 2; }; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sandbox)     need "$1" "${2:-}"; SANDBOX="$2"; shift 2;;
    --script)      need "$1" "${2:-}"; SCRIPT="$2"; shift 2;;
    --interpreter) need "$1" "${2:-}"; INTERP="$2"; shift 2;;
    --env)         need "$1" "${2:-}"; ENV="$2"; shift 2;;
    --timeout)     need "$1" "${2:-}"; TIMEOUT="$2"; shift 2;;
    --)            shift; ARGS=("$@"); break;;
    *) echo "Unknown arg: $1" >&2; exit 2;;
  esac
done
[[ -n "$SANDBOX" && -d "$SANDBOX" ]] || { echo "ERROR: --sandbox dir required/invalid" >&2; exit 2; }
[[ -n "$SCRIPT" && -f "$SCRIPT" ]] || { echo "ERROR: --script required/invalid" >&2; exit 2; }
[[ "$TIMEOUT" =~ ^[0-9]+[smhd]?$ ]] || { echo "ERROR: invalid --timeout: $TIMEOUT" >&2; exit 2; }

SANDBOX_REAL="$(readlink -f "$SANDBOX")"
SCRIPT_REAL="$(readlink -f "$SCRIPT")"
case "$SCRIPT_REAL" in
  "$SANDBOX_REAL"/*) : ;;
  *) echo "REFUSED: script ($SCRIPT_REAL) is not inside the sandbox ($SANDBOX_REAL)." >&2; exit 3;;
esac

# An explicit --interpreter wins (may carry args, e.g. "python -u"); otherwise pick by the
# BASENAME's extension (a dot in a parent dir must not be mistaken for an extension).
# Extensionless & unknown: run directly if executable, else bash. Prefer --interpreter for
# anything outside the known set below.
if [[ -n "$INTERP" ]]; then
  IFS=' ' read -ra CMD <<< "$INTERP"; CMD+=("$SCRIPT_REAL")
else
  bn="${SCRIPT_REAL##*/}"; ext=""; [[ "$bn" == *.* ]] && ext="${bn##*.}"; ext="${ext,,}"
  case "$ext" in
    py)          CMD=(python "$SCRIPT_REAL");;
    r)           CMD=(Rscript "$SCRIPT_REAL");;
    sh|bash)     CMD=(bash -- "$SCRIPT_REAL");;
    js|mjs|cjs)  CMD=(node "$SCRIPT_REAL");;
    rb)          CMD=(ruby "$SCRIPT_REAL");;
    pl)          CMD=(perl "$SCRIPT_REAL");;
    *)           if [[ -x "$SCRIPT_REAL" ]]; then CMD=("$SCRIPT_REAL"); else CMD=(bash -- "$SCRIPT_REAL"); fi;;
  esac
fi

echo "[run_local] env=$ENV cwd=$SANDBOX_REAL script=$SCRIPT_REAL timeout=${TIMEOUT}s"
echo "---------------- output ----------------"
# cwd = sandbox so relative paths stay local. `--` ends conda's option parsing.
# timeout is INSIDE conda run so it wraps the interpreter directly (conda run may not
# forward signals); --kill-after SIGKILLs a child that ignores SIGTERM.
( cd "$SANDBOX_REAL" && conda run --no-capture-output -n "$ENV" -- timeout --kill-after=10s "$TIMEOUT" "${CMD[@]}" "${ARGS[@]}" )
RC=$?
echo "----------------------------------------"
echo "[run_local] exit=$RC"
exit "$RC"
