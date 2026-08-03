"""The opencode critic agent must stay in the shape opencode actually reads.

Two things went wrong here and both were silent:

1. `mode: subagent` — `opencode run --agent <name>` REJECTS a subagent and falls back to
   the fully-permissive `build` agent, printing only a warning. The agent's permissions
   were never consulted at all.
2. Top-level `edit: deny` / `bash: deny` keys — `AgentConfig` carries
   `[key: string]: unknown`, so these validate fine and are silently discarded. The
   permission keys are only read from the nested `permission:` block.

Neither failure produces an error, so only an explicit assertion catches them.
Offline: parses the file, does not invoke opencode.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agents" / "bcoc-review.md"
AGENT_COPY = ROOT / "opencode-config" / "agents" / "bcoc-review.md"


def frontmatter(path):
    text = path.read_text()
    assert text.startswith("---\n"), f"{path} has no frontmatter"
    return text.split("---", 2)[1]


def parse(fm):
    """Minimal YAML-ish parse: top-level keys and one level of nesting."""
    top, nested, current = {}, {}, None
    for line in fm.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        key, _, val = line.strip().partition(":")
        val = val.strip()
        if indent == 0:
            current = key if not val else None
            if val:
                top[key] = val
            else:
                nested[key] = {}
        elif current:
            nested[current][key] = val
    return top, nested


def test_mode_is_all_not_subagent():
    top, _ = parse(frontmatter(AGENT))
    assert top.get("mode") == "all", (
        "mode must be 'all'. With 'subagent', `opencode run --agent` silently falls back "
        "to the permissive `build` agent and this file has no effect whatsoever."
    )


@pytest.mark.parametrize("perm", ["edit", "bash", "webfetch", "doom_loop", "external_directory"])
def test_permission_block_denies(perm):
    _, nested = parse(frontmatter(AGENT))
    assert "permission" in nested, "the nested `permission:` block is the only one opencode reads"
    assert nested["permission"].get(perm) == "deny", f"permission.{perm} must be deny"


@pytest.mark.parametrize("tool", ["write", "edit", "patch", "bash", "webfetch", "task"])
def test_tools_block_disables(tool):
    _, nested = parse(frontmatter(AGENT))
    assert "tools" in nested, "the `tools:` block is what produces `task deny`"
    assert nested["tools"].get(tool) == "false", f"tools.{tool} must be false"


def test_no_top_level_permission_keys():
    """Top-level `edit: deny` is silently discarded — it must not look like protection."""
    top, _ = parse(frontmatter(AGENT))
    for key in ("edit", "bash", "webfetch", "write", "patch"):
        assert key not in top, (
            f"top-level `{key}:` is silently ignored by opencode. Put it under "
            f"`permission:` or `tools:`, or it is decoration that reads as a guarantee."
        )


def test_both_copies_are_identical():
    """The config dir needs its own copy; this filesystem cannot hold a symlink.

    Two byte-identical files is a drift bug waiting to happen, so assert it instead.
    """
    assert AGENT_COPY.exists(), f"missing {AGENT_COPY}"
    assert AGENT.read_bytes() == AGENT_COPY.read_bytes(), (
        "agents/bcoc-review.md and opencode-config/agents/bcoc-review.md have drifted. "
        "The one under opencode-config/ is the one opencode actually loads."
    )


def test_review_script_locks_down_project_config():
    """The reviewed repo must not be able to supply agents, config or instructions."""
    src = (ROOT / "scripts" / "opencode_review.sh").read_text()
    assert "OPENCODE_DISABLE_PROJECT_CONFIG=1" in src
    assert "--pure" in src
    assert "OPENCODE_PERMISSION" in src


def test_review_script_refuses_repos_with_opencode_plugins():
    """Plugin execution is the one thing the permission layer cannot stop.

    Verified on 1.18.11: a real `opencode run` session executes a repo's
    .opencode/plugin/*.js even with --pure AND OPENCODE_DISABLE_PROJECT_CONFIG=1.
    Refusing such a scope is the only defence available to this skill.
    """
    src = (ROOT / "scripts" / "opencode_review.sh").read_text()
    assert "_oc_plugins" in src, "the plugin scope-scan is missing"
    assert 'find "$PRIMARY/.opencode"' in src
