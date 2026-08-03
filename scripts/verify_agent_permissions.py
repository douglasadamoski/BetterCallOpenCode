#!/usr/bin/env python3
"""Verify that an opencode agent actually resolves the required permissions to `deny`.

Reads `opencode agent list` output on stdin. Exits 0 only if every required permission
resolves to deny for the named agent, and the agent's mode is `all`.

Why this is a separate, careful check rather than a grep:

  The first version grepped for `"permission": *"edit"` and treated a hit as proof of a
  denial. It matched just as happily when the action was "allow" — so the verification
  passed with a fully permissive agent, and the skill's "deny rules are verified before
  every run" claim was hollow. Confirmed by forcing OPENCODE_PERMISSION to allow: all
  four keys were present and it reported success.

Two things make this subtle enough to be worth real parsing:

  * Resolution is LAST-match-wins. An earlier `edit deny *` followed by a later
    `edit allow *` is an ALLOW. Any check that stops at the first match is wrong.
  * `mode` must be `all`. With `subagent`, `opencode run --agent <name>` REJECTS the
    agent and silently falls back to the permissive built-in `build` agent, so the
    permissions below are never consulted at all.

Usage: opencode agent list | verify_agent_permissions.py <agent> <perm> [<perm>...]
"""
import json
import re
import sys

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# Agent blocks start at column 0 with `name (mode)`.
BLOCK_SPLIT_RE = re.compile(r"(?m)^(?=\S.*\((?:subagent|primary|all)\))")


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: verify_agent_permissions.py <agent> <perm>...", file=sys.stderr)
        return 2
    agent, required = sys.argv[1], sys.argv[2:]
    raw = ANSI_RE.sub("", sys.stdin.read())

    block = None
    for chunk in BLOCK_SPLIT_RE.split(raw):
        if chunk.strip().startswith(agent):
            block = chunk
            break
    if block is None:
        print(f"agent {agent!r} not found in `opencode agent list`", file=sys.stderr)
        return 1

    header = block.strip().splitlines()[0]
    if not re.match(rf"^{re.escape(agent)}\s*\(all\)", header.strip()):
        print(f"agent {agent!r} mode is not 'all': {header.strip()!r}", file=sys.stderr)
        print("  `subagent` makes `opencode run --agent` fall back to the permissive "
              "built-in agent, so its permissions never apply.", file=sys.stderr)
        return 1

    m = re.search(r"\[.*\]", block, re.S)
    if not m:
        print(f"no resolved permission list for {agent!r}", file=sys.stderr)
        return 1
    try:
        rules = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        print(f"could not parse the resolved permission list: {e}", file=sys.stderr)
        return 1

    bad = []
    for perm in required:
        action = None
        for r in rules:                       # last match wins
            if not isinstance(r, dict):
                continue
            if r.get("permission") in (perm, "*"):
                action = r.get("action")
        if action != "deny":
            bad.append(f"{perm}={action or 'absent'}")
    if bad:
        print("permissions did NOT resolve to deny: " + ", ".join(bad), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
