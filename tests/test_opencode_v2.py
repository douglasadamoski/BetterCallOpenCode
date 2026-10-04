"""opencode_v2.py — the 2.x restriction mechanism. Offline: `opencode` is a stub."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import opencode_v2 as ov2  # noqa: E402


def test_config_is_default_deny_then_an_allowlist_in_that_order():
    perm = ov2.config_for("a", False)["agent"]["a"]["permission"]
    assert list(perm)[0] == "*" and perm["*"] == "deny"          # first: later keys win
    assert perm["read"]["*.env"] == "deny" and "webfetch" not in perm


def test_only_the_researcher_config_allows_web():
    perm = ov2.config_for("a", True)["agent"]["a"]["permission"]
    assert perm["webfetch"] == "allow" and perm["websearch"] == "allow"


def test_agent_is_primary_so_run_honours_it():
    assert ov2.config_for("a", False)["agent"]["a"]["mode"] == "primary"


def test_prepare_sets_aside_repo_config_and_writes_ours(tmp_path):
    for n in ("opencode.json", "opencode.jsonc", "AGENTS.md", "CLAUDE.md"):
        (tmp_path / n).write_text("repo says allow everything")
    (tmp_path / ".opencode" / "plugin").mkdir(parents=True)
    (tmp_path / ".opencode" / "plugin" / "evil.js").write_text("boom")
    (tmp_path / "keep.py").write_text("x")
    moved = ov2.prepare(tmp_path, "bcoc-review", False)
    assert set(moved) >= {"opencode.json", "AGENTS.md", "CLAUDE.md", ".opencode"}
    assert json.loads((tmp_path / "opencode.json").read_text())["agent"]["bcoc-review"]["permission"]["*"] == "deny"
    assert (tmp_path / "AGENTS.md.reviewed").read_text() == "repo says allow everything"   # kept, readable
    assert not (tmp_path / "AGENTS.md").exists() and not (tmp_path / ".opencode").exists()
    assert (tmp_path / "keep.py").exists()


def test_prepare_neutralises_a_symlinked_config(tmp_path):
    (tmp_path / "t").write_text("x")
    (tmp_path / "opencode.json").symlink_to(tmp_path / "t")
    ov2.prepare(tmp_path, "bcoc-review", False)
    assert not (tmp_path / "opencode.json").is_symlink()


@pytest.mark.parametrize("bad", ["../x", "a b", "", "-x", "a/b"])
def test_prepare_rejects_bad_agent_names(tmp_path, bad):
    with pytest.raises(ValueError):
        ov2.prepare(tmp_path, bad, False)


def resolved(*rs):
    return [{"action": a, "resource": r, "effect": e} for a, r, e in rs]


# what `opencode debug agents` really printed for an agent defined like ours (2.0.22)
OURS = resolved(("*", "*", "allow"), ("external_directory", "*", "ask"), ("read", "*.env", "ask"),
                ("external_directory", "/home/u/.local/share/opencode/tool-output/*", "allow"),
                ("*", "*", "deny"), ("read", "*", "allow"), ("read", "*.env", "deny"),
                ("grep", "*", "allow"), ("glob", "*", "allow"), ("list", "*", "allow"), ("browser", "*", "deny"))


def test_the_resolved_shape_of_our_agent_verifies():
    ok, why = ov2.verify_v2([{"id": "a", "permissions": OURS}], "a", ov2.must_block(False))
    assert ok, why


def test_shell_is_what_governs_commands_so_it_must_be_blocked_by_name_or_default():
    perms = resolved(("*", "*", "allow"), ("edit", "*", "deny"), ("shell", "*", "allow"), ("subagent", "*", "deny"),
                     ("external_directory", "*", "deny"), ("webfetch", "*", "deny"), ("websearch", "*", "deny"))
    ok, why = ov2.verify_v2([{"id": "a", "permissions": perms}], "a", ov2.must_block(False))
    assert not ok and "shell=allow" in why


def test_a_scoped_shell_allow_is_refused():
    perms = OURS + resolved(("shell", "git *", "allow"))
    ok, why = ov2.verify_v2([{"id": "a", "permissions": perms}], "a", ov2.must_block(False))
    assert not ok and "shell" in why


def test_web_allowed_means_the_no_web_check_fails_and_the_web_check_passes():
    perms = OURS + resolved(("webfetch", "*", "allow"), ("websearch", "*", "allow"))
    a = [{"id": "a", "permissions": perms}]
    assert not ov2.verify_v2(a, "a", ov2.must_block(False))[0]
    assert ov2.verify_v2(a, "a", ov2.must_block(True))[0]


def test_events_command_prints_text_and_reports_denials(capsys, monkeypatch):
    import io
    lines = [{"type": "tool_use", "part": {"state": {"status": "error", "error": "rejected"}}},
             {"type": "text", "part": {"text": "done"}}]
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(json.dumps(x) for x in lines)))
    assert ov2.main(["events"]) == 0
    cap = capsys.readouterr()
    assert cap.out == "done\n" and "rejected" in cap.err


def test_run_env_keeps_project_config_enabled_and_drops_permission_overrides(monkeypatch):
    monkeypatch.setenv("OPENCODE_DISABLE_PROJECT_CONFIG", "1")
    monkeypatch.setenv("OPENCODE_PERMISSION", "{}")
    e = ov2.run_env()
    assert "OPENCODE_DISABLE_PROJECT_CONFIG" not in e and "OPENCODE_PERMISSION" not in e


def test_narration_before_tool_calls_is_not_part_of_the_answer():
    lines = [{"type": "text", "part": {"text": "Let me search. "}},
             {"type": "tool_use", "part": {"state": {"status": "completed"}}},
             {"type": "text", "part": {"text": "Now fetching. "}},
             {"type": "tool_use", "part": {"state": {"status": "error", "error": "denied"}}},
             {"type": "text", "part": {"text": "## Answer\nfinal"}}]
    text, errs = ov2.parse_events("\n".join(json.dumps(x) for x in lines))
    assert text == "## Answer\nfinal" and errs == ["denied"]


def test_a_run_with_no_tool_calls_keeps_all_text():
    lines = [{"type": "text", "part": {"text": "A"}}, {"type": "text", "part": {"text": "B"}}]
    assert ov2.parse_events("\n".join(json.dumps(x) for x in lines))[0] == "AB"


def test_provider_error_events_are_read_not_ignored():
    out = json.dumps({"type": "error", "error": {"type": "provider.auth", "message": "free tier only inside OpenCode", "status": 403}})
    errs = ov2.parse_errors(out + "\nnot json\n" + json.dumps({"type": "text", "part": {"text": "x"}}))
    assert errs == [{"status": 403, "kind": "provider.auth", "message": "free tier only inside OpenCode"}]


@pytest.mark.parametrize("status,msg,word", [
    (403, "forbidden", "AUTH"), (401, "bad key", "AUTH"), (429, "slow down", "QUOTA"), (402, "credits", "QUOTA"),
    (None, "Rate limit exceeded", "QUOTA"), (503, "upstream down", "UNREACHABLE"), (400, "bad request", "ERROR"), (None, "weird", "ERROR")])
def test_provider_errors_map_to_the_result_vocabulary(status, msg, word):
    assert ov2.classify_provider_error({"status": status, "message": msg}) == word


def test_steps_is_written_into_the_agent_only_when_asked_for():
    assert "steps" not in ov2.config_for("a", True)["agent"]["a"]
    assert ov2.config_for("a", True, steps=3)["agent"]["a"]["steps"] == 3


def test_prepare_writes_steps_into_the_mirrors_opencode_json(tmp_path):
    ov2.prepare(tmp_path, "bcoc-researcher", True, steps=2)
    assert json.loads((tmp_path / "opencode.json").read_text())["agent"]["bcoc-researcher"]["steps"] == 2
