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
# An env var naming an environment is as explicit as the flag: falling back to the
# ambient interpreter would silently run somewhere the user did not choose.
ENV_EXPLICIT="${BCOPENCODE_CONDA_ENV:+1}"; DRY_RUN=""
# Reject a missing value being swallowed from the next option (e.g. `--sandbox --script`).
need() { [[ -n "${2:-}" && "${2:0:1}" != "-" ]] || { echo "ERROR: $1 needs a value" >&2; exit 2; }; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sandbox)     need "$1" "${2:-}"; SANDBOX="$2"; shift 2;;
    --script)      need "$1" "${2:-}"; SCRIPT="$2"; shift 2;;
    --interpreter) need "$1" "${2:-}"; INTERP="$2"; shift 2;;
    --env)         need "$1" "${2:-}"; ENV="$2"; ENV_EXPLICIT=1; shift 2;;
    --dry-run)     DRY_RUN=1; shift;;
    --timeout)     need "$1" "${2:-}"; TIMEOUT="$2"; shift 2;;
    --)            shift; ARGS=("$@"); break;;
    *) echo "Unknown arg: $1" >&2; exit 2;;
  esac
done
[[ -n "$SANDBOX" && -d "$SANDBOX" ]] || { echo "ERROR: --sandbox dir required/invalid" >&2; exit 2; }
[[ -n "$SCRIPT" && -f "$SCRIPT" ]] || { echo "ERROR: --script required/invalid" >&2; exit 2; }
[[ "$TIMEOUT" =~ ^[0-9]+[smhd]?$ ]] || { echo "ERROR: invalid --timeout: $TIMEOUT" >&2; exit 2; }

# readlink -f does not exist on stock macOS. Without a fallback both vars came back
# empty, the containment check below never matched, and EVERY invocation returned
# REFUSED — so Mode B was doubly dead there.
_rl() {
  local r
  r="$(readlink -f "$1" 2>/dev/null)" && [[ -n "$r" ]] && { printf '%s' "$r"; return 0; }
  python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$1" 2>/dev/null
}
SANDBOX_REAL="$(_rl "$SANDBOX")"
SCRIPT_REAL="$(_rl "$SCRIPT")"
[[ -n "$SANDBOX_REAL" && -n "$SCRIPT_REAL" ]] || {
  echo "ERROR: could not resolve paths (need readlink -f or python3)" >&2; exit 2; }
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
    py)          CMD=(python3 "$SCRIPT_REAL");;   # python3: many envs ship no `python`
    r)           CMD=(Rscript "$SCRIPT_REAL");;
    sh|bash)     CMD=(bash -- "$SCRIPT_REAL");;
    js|mjs|cjs)  CMD=(node "$SCRIPT_REAL");;
    rb)          CMD=(ruby "$SCRIPT_REAL");;
    pl)          CMD=(perl "$SCRIPT_REAL");;
    *)           if [[ -x "$SCRIPT_REAL" ]]; then CMD=("$SCRIPT_REAL"); else CMD=(bash -- "$SCRIPT_REAL"); fi;;
  esac
fi

# Resolve a timeout binary; macOS has neither without `brew install coreutils`.
if command -v timeout >/dev/null 2>&1; then TO=timeout
elif command -v gtimeout >/dev/null 2>&1; then TO=gtimeout
else
  echo "ERROR: neither 'timeout' nor 'gtimeout' found (macOS: brew install coreutils)" >&2
  exit 2
fi

# NEVER put `--` before the command. `conda run` passes it through as the first WORD of
# the command, so it becomes `--: command not found` and the run exits 127 — every
# script, every host. Reproduced on conda 25.7.0; all three sibling skills hit this and
# left the same comment. If you are "fixing option parsing" here, you are re-breaking
# Mode B entirely.
CONDA_PREFIX_CMD=()
if command -v conda >/dev/null 2>&1; then
  CONDA_PREFIX_CMD=(conda run --no-capture-output -n "$ENV")
elif [[ -n "$ENV_EXPLICIT" ]]; then
  echo "ERROR: --env $ENV was requested but conda is not installed." >&2
  echo "       Refusing to run somewhere other than the environment you asked for." >&2
  exit 2
else
  echo "[run_local] WARNING: conda not found; running in the ambient environment." >&2
fi

echo "[run_local] env=$ENV cwd=$SANDBOX_REAL script=$SCRIPT_REAL timeout=${TIMEOUT}s"
if [[ -n "$DRY_RUN" ]]; then
  echo "[run_local] --dry-run: would execute:"
  printf '  %q' "${CONDA_PREFIX_CMD[@]+${CONDA_PREFIX_CMD[@]}}" "$TO" "--kill-after=10s" "$TIMEOUT" "${CMD[@]}" ${ARGS[@]+"${ARGS[@]}"}
  echo
  exit 0
fi
echo "---------------- output ----------------"
# cwd = sandbox so relative paths stay local. timeout is INSIDE conda run so it wraps the
# interpreter directly (conda run may not forward signals); --kill-after SIGKILLs a child
# that ignores SIGTERM.
( cd "$SANDBOX_REAL" && "${CONDA_PREFIX_CMD[@]+${CONDA_PREFIX_CMD[@]}}" "$TO" --kill-after=10s "$TIMEOUT" "${CMD[@]}" ${ARGS[@]+"${ARGS[@]}"} )
RC=$?
echo "----------------------------------------"
echo "[run_local] exit=$RC"
exit "$RC"
