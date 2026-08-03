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
    assert "_oc_entries" in src, "the .opencode scope-scan is missing"
    assert 'find "$PRIMARY/.opencode" -mindepth 1' in src, (
        "the scan must cover the WHOLE .opencode tree. An extension+maxdepth filter was "
        "walked past by .opencode/plugin/nested/evil.js, evil.cjs, evil.mts and plugins/."
    )
    scan = src[src.index('find "$PRIMARY/.opencode"'):][:200]
    assert "-maxdepth" not in scan, "depth-limited .opencode scanning is bypassable"


# --- Regressions from the BetterCallGemini review round 1 ------------------------

import subprocess
import sys

VERIFY = ROOT / "scripts" / "verify_agent_permissions.py"


def _agent_list(mode="all", edit="deny", bash="deny", webfetch="deny", task="deny"):
    """A synthetic `opencode agent list` block, in the real output shape."""
    rules = [{"permission": "*", "action": "allow", "pattern": "*"}]
    for perm, action in (("edit", edit), ("bash", bash), ("webfetch", webfetch), ("task", task)):
        rules.append({"permission": perm, "action": action, "pattern": "*"})
    import json
    return f"bcoc-review ({mode})\n  {json.dumps(rules, indent=2)}\n"


def _verify(dump):
    p = subprocess.run([sys.executable, str(VERIFY), "bcoc-review",
                        "edit", "bash", "webfetch", "task"],
                       input=dump, capture_output=True, text=True, timeout=60)
    return p.returncode, p.stderr


def test_verifier_accepts_a_properly_denied_agent():
    rc, _ = _verify(_agent_list())
    assert rc == 0


def test_verifier_rejects_a_permissive_agent():
    """The original check grepped for `"permission": "edit"` and called that proof.

    It matched just as happily when the action was "allow", so verification passed with
    a fully permissive agent and the "verified before every run" claim was hollow.
    """
    rc, err = _verify(_agent_list(edit="allow", bash="allow"))
    assert rc == 1
    assert "did NOT resolve to deny" in err


def test_verifier_rejects_subagent_mode():
    """`subagent` makes `opencode run --agent` fall back to the permissive built-in."""
    rc, err = _verify(_agent_list(mode="subagent"))
    assert rc == 1
    assert "mode is not 'all'" in err


def test_verifier_honours_last_match_wins():
    """An earlier deny followed by a later allow is an ALLOW."""
    import json
    rules = [
        {"permission": "edit", "action": "deny", "pattern": "*"},
        {"permission": "bash", "action": "deny", "pattern": "*"},
        {"permission": "webfetch", "action": "deny", "pattern": "*"},
        {"permission": "task", "action": "deny", "pattern": "*"},
        {"permission": "edit", "action": "allow", "pattern": "*"},   # wins
    ]
    rc, err = _verify(f"bcoc-review (all)\n  {json.dumps(rules)}\n")
    assert rc == 1
    assert "edit=allow" in err


def test_verifier_rejects_a_missing_agent():
    rc, _ = _verify("someother (all)\n  []\n")
    assert rc == 1


def test_agent_file_is_refreshed_not_just_created():
    """`if [[ ! -f ]]` meant an installed copy was never updated.

    A user who had run an older version kept its agent file forever — including the
    `mode: subagent` version whose permissions did nothing. A security fix that never
    reaches existing installs is not a fix.
    """
    src = (ROOT / "scripts" / "opencode_review.sh").read_text()
    assert 'cp -f "$SKILL_DIR/agents/bcoc-review.md"' in src
    assert 'if [[ ! -f "$AGENT_FILE" ]]; then\n  if [[ -f' not in src


def test_opencode_backend_reads_a_filtered_mirror_not_the_raw_scope():
    """or-api withholds credential-shaped files; opencode handed the agent the raw repo.

    The agent reads a DIRECTORY rather than receiving packed text, so `--dir "$PRIMARY"`
    bypassed the secret filter entirely and relied on a soft prompt rule not to read
    `.env` — which a prompt injection in the repo could simply override.
    """
    src = (ROOT / "scripts" / "opencode_review.sh").read_text()
    assert "--mirror-to" in src, "the opencode backend must build a filtered mirror"
    assert '--dir "$MIRROR_DIR"' in src, "the agent must be pointed at the mirror"
    assert '--dir "$PRIMARY"' not in src, "the agent must never see the raw scope"


def test_verifier_covers_write_patch_and_external_directory():
    """The docs claimed "write deny confirmed" while only edit/bash/webfetch/task were
    checked. An agent denying those four but allowing `write` verified clean."""
    src = (ROOT / "scripts" / "opencode_review.sh").read_text()
    call = src[src.index("verify_agent_permissions.py"):][:300]
    # `write`/`patch` are deliberately absent: they are not opencode permission keys —
    # its loader folds tools.write/edit/patch into permission.edit, so requiring them
    # made every run refuse on a permission that cannot resolve to deny.
    for perm in ("edit", "bash", "webfetch", "task", "external_directory"):
        assert perm in call, f"{perm} is not verified before the run"


def test_prompt_never_names_the_real_scope_path():
    """Printing $PRIMARY handed a prompt-injected critic the absolute path to the
    UNFILTERED tree, leaving external_directory:deny as the only thing between it and
    the secrets the mirror exists to withhold."""
    src = (ROOT / "scripts" / "opencode_review.sh").read_text()
    backend = src[src.index("# ---- backend opencode"):]
    assert "printf 'Scope root: %s\\n' \"$PRIMARY\"" not in backend
    assert "filtered copy" in backend
