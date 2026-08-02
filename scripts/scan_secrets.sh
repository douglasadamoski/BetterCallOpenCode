#!/usr/bin/env bash
# scan_secrets.sh — refuse to ship a commit containing anything credential-shaped.
#
# Runs in CI and locally (`bash scripts/scan_secrets.sh`). Exits non-zero on any hit.
#
# The pattern fragments below are CONCATENATED on purpose. Written as plain literals
# they would match this file, and pack_context.py's content backstop would then
# withhold the scanner itself from any review of this repo — see
# tests/test_pack_secrets.py::test_packer_does_not_refuse_its_own_source.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 2

pats=(
  "sk-""or-v1-[A-Za-z0-9]{16,}"
  "sk-""ant-[A-Za-z0-9_-]{20,}"
  "sk-""proj-[A-Za-z0-9_-]{20,}"
  "ghp_""[A-Za-z0-9]{20,}"
  "gho_""[A-Za-z0-9]{20,}"
  "github_pat_""[A-Za-z0-9_]{20,}"
  "xoxb-""[A-Za-z0-9-]{20,}"
  "AKIA""[0-9A-Z]{16}"
  "-----BEGIN ""[A-Z ]*PRIVATE KEY-----"
)

# Files that legitimately describe credential SHAPES rather than carrying one.
exclude=(
  ':!scripts/scan_secrets.sh'
  ':!scripts/pack_context.py'
  ':!tests/test_pack_secrets.py'
)

rc=0
ran=0
for p in "${pats[@]}"; do
  # -e is mandatory: the PEM pattern starts with '-' and would otherwise be parsed as
  # an option. That failure returned non-zero, which read as "no match found", and the
  # scan reported clean for a pattern that never executed. Never infer coverage from a
  # falsy exit status — distinguish found(0) / not-found(1) / error(>1) explicitly.
  git grep -nIE -e "$p" -- . "${exclude[@]}"
  case $? in
    0) echo "::error::credential-shaped string matched: ${p:0:12}…" >&2; rc=1; ran=$((ran + 1)) ;;
    1) ran=$((ran + 1)) ;;
    *) echo "::error::scan pattern failed to execute: ${p:0:12}…" >&2; rc=2 ;;
  esac
done

if [[ "$ran" -ne "${#pats[@]}" ]]; then
  echo "::error::only $ran/${#pats[@]} patterns ran — refusing to report clean" >&2
  rc=2
fi

if [[ $rc -eq 0 ]]; then
  echo "scan_secrets: clean — $ran/${#pats[@]} patterns over $(git ls-files | wc -l) tracked files"
fi
exit $rc
