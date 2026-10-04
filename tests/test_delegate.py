"""delegate.py — one task, any provider, as a worker. Offline: HTTP and the opencode CLI
are replaced. The things worth pinning are the ones that cost money or hide a failure:
the cost gate, the cap, fallback discipline, the ledger, redaction and the agent gate."""
import json
import os
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import delegate as dg  # noqa: E402
import ledger  # noqa: E402

KEY = "sk-or-" + "v1-" + "0123456789abcdef" * 2  # assembled: a literal here would trip the packer's own filter
FREE = "openrouter/nvidia/nemotron-3-super-120b-a12b:free"
FREE2 = "openrouter/poolside/laguna-s-2.1:free"


def M(cost="free", mods=("text",), **kw):
    return {"ctx": 100000, "max_out": None, "tools": True, "reasoning": False, "json_mode": None,
            "modalities": list(mods), "cost_class": cost, "capabilities": "known",
            "sources": ["provider"], "name": None, **kw}


CACHE = {"schema": 1, "providers": {
    "openrouter": {"status": "ok", "zero_cost_policy": None, "base_url": "https://openrouter.ai/api/v1",
                   "models": {"nvidia/nemotron-3-super-120b-a12b:free": M(), "poolside/laguna-s-2.1:free": M(),
                              "vendor/paid": M("paid"), "vendor/vision:free": M(mods=("text", "image"))}},
    "provider-a": {"status": "ok", "zero_cost_policy": None, "base_url": "https://example-gateway.invalid/v1",
                   "models": {"m1": M("unknown", cost_class="unknown"), "m2": M()}},
}}


def reply(text="hello", finish="stop", usage=None):
    return 200, "", {"id": "x", "model": "m", "choices": [{"message": {"content": text}, "finish_reason": finish}],
                     "usage": usage or {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12, "cost": 0}}


@pytest.fixture
def h(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    monkeypatch.setenv("BCOPENCODE_STATE_DIR", str(state))
    monkeypatch.setenv("BCOPENCODE_CAP", "200")
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.setenv("PROVIDER_A_API_KEY", "provider-a-secret")
    monkeypatch.setattr(dg.dp, "ensure", lambda **k: (json.loads(json.dumps(CACHE)), "fresh"))
    monkeypatch.setattr(dg.dp, "opencode_provider_config",
                        lambda: {"provider-a": {"baseURL": "https://example-gateway.invalid/v1", "env": ["PROVIDER_A_API_KEY"]}})
    monkeypatch.setattr(dg.dp, "record_ok", lambda *a, **k: None)
    prompt = tmp_path / "task.md"
    prompt.write_text("Summarise X.")
    sent, script = [], []

    def fake_http(method, url, headers, body, timeout):
        sent.append({"url": url, "headers": headers, "body": body})
        item = script.pop(0) if script else reply()
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(dg.orc, "http_json", fake_http)

    class H:
        pass

    o = H()
    o.state, o.sent, o.script, o.prompt, o.tmp = state, sent, script, prompt, tmp_path

    def run(*args):
        capsys.readouterr()
        rc = dg.main(["--prompt-file", str(prompt), "--rpm", "0", *args])
        cap = capsys.readouterr()
        o.err = cap.err
        lines = cap.out.strip().splitlines()
        o.lines = lines
        return lines[-1] if lines else ""

    o.run = run
    o.rows = lambda: [json.loads(l) for l in (state / "usage.jsonl").read_text().splitlines()] \
        if (state / "usage.jsonl").exists() else []
    return o


# ----------------------------------------------------------------------------- or-api
def test_ok_path_writes_result_row_and_uses_the_providers_endpoint(h):
    out = h.tmp / "r.md"
    assert h.run("--role", "analyst", "--model", FREE, "--out", str(out)) == "RESULT=OK"
    text = out.read_text()
    assert "result: OK" in text and "hello" in text and "role: analyst" in text
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    req = h.sent[0]
    assert req["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert req["headers"]["Authorization"] == f"Bearer {KEY}"
    assert req["body"]["model"] == "nvidia/nemotron-3-super-120b-a12b:free"
    assert req["body"]["messages"][0]["role"] == "system" and "analyst" in req["body"]["messages"][0]["content"]
    row = h.rows()[-1]
    assert (row["result"], row["billed"], row["free"], row["mode"], row["total_tokens"]) == ("OK", True, True, "analyst", 12)
    assert KEY not in text and KEY not in json.dumps(h.rows())


def test_every_role_gets_its_own_system_prompt(h):
    seen = set()
    for role in dg.ROLES:
        h.run("--role", role, "--model", FREE, "--out", str(h.tmp / f"{role}.md"))
        seen.add(h.sent[-1]["body"]["messages"][0]["content"])
    assert len(seen) == len(dg.ROLES)


def test_paid_model_is_blocked_before_any_request(h):
    assert h.run("--role", "analyst", "--model", "openrouter/vendor/paid", "--out", str(h.tmp / "r.md")) == "RESULT=PAID_BLOCKED"
    assert h.sent == []
    assert h.rows()[-1]["billed"] is False


def test_allow_paid_lets_a_paid_model_through(h):
    assert h.run("--role", "analyst", "--model", "openrouter/vendor/paid", "--allow-paid", "--out", str(h.tmp / "r.md")) == "RESULT=OK"
    assert h.rows()[-1]["free"] is False


def test_unknown_cost_is_blocked_until_the_provider_has_a_policy(h):
    assert h.run("--role", "analyst", "--model", "provider-a/m1", "--out", str(h.tmp / "r.md")) == "RESULT=PAID_BLOCKED"
    assert "set-policy" in (h.tmp / "r.md").read_text()
    assert h.sent == []


def test_other_providers_use_their_own_base_url_and_no_openrouter_fields(h):
    assert h.run("--role", "analyst", "--model", "provider-a/m2", "--out", str(h.tmp / "r.md")) == "RESULT=OK"
    req = h.sent[0]
    assert req["url"] == "https://example-gateway.invalid/v1/chat/completions"
    assert req["headers"]["Authorization"] == "Bearer provider-a-secret"
    assert req["body"]["model"] == "m2"
    assert "usage" not in req["body"] and "reasoning" not in req["body"]


def test_missing_credential_is_AUTH_and_spends_nothing(h, monkeypatch):
    monkeypatch.delenv("PROVIDER_A_API_KEY")
    monkeypatch.setattr(dg.dp.Path, "home", classmethod(lambda c: h.tmp))
    assert h.run("--role", "analyst", "--model", "provider-a/m2", "--out", str(h.tmp / "r.md")) == "RESULT=AUTH"
    assert h.sent == [] and h.rows()[-1]["billed"] is False


def test_fallback_moves_to_the_next_model_on_429_only(h):
    h.script += [(429, "rate", {"error": {"message": "slow down"}}), reply("second")]
    assert h.run("--role", "analyst", "--model", FREE, "--fallback", FREE2, "--out", str(h.tmp / "r.md")) == "RESULT=OK"
    assert [s["body"]["model"] for s in h.sent] == ["nvidia/nemotron-3-super-120b-a12b:free", "poolside/laguna-s-2.1:free"]
    assert "second" in (h.tmp / "r.md").read_text() and "laguna" in (h.tmp / "r.md").read_text()
    assert [r["result"] for r in h.rows()] == ["QUOTA", "OK"]


@pytest.mark.parametrize("status,word", [(401, "AUTH"), (402, "QUOTA")])
def test_no_fallback_on_auth_or_out_of_credit(h, status, word):
    h.script.append((status, "no", {"error": {"message": "no"}}))
    assert h.run("--role", "analyst", "--model", FREE, "--fallback", FREE2, "--out", str(h.tmp / "r.md")) == f"RESULT={word}"
    assert len(h.sent) == 1


def test_no_fallback_after_a_truncated_answer(h):
    h.script.append(reply("partial", finish="length"))
    assert h.run("--role", "analyst", "--model", FREE, "--fallback", FREE2, "--out", str(h.tmp / "r.md")) == "RESULT=TRUNCATED"
    assert len(h.sent) == 1 and "may be incomplete" in (h.tmp / "r.md").read_text()


def test_fallback_on_a_retired_model_404(h):
    h.script += [(404, "gone", {"error": {"message": "no such model"}}), reply()]
    assert h.run("--role", "analyst", "--model", FREE, "--fallback", FREE2, "--out", str(h.tmp / "r.md")) == "RESULT=OK"
    assert len(h.sent) == 2


def test_attempts_are_capped(h):
    h.script += [(429, "x", {"error": "x"})] * 5
    assert h.run("--role", "analyst", "--model", FREE, "--fallback", f"{FREE2},{FREE}", "--max-attempts", "2",
                 "--out", str(h.tmp / "r.md")) == "RESULT=QUOTA"
    assert len(h.sent) == 2


def test_fallback_never_crosses_into_paid_models_and_keeps_the_primarys_outcome(h):
    h.script.append((429, "x", {"error": {"message": "slow down"}}))
    assert h.run("--role", "analyst", "--model", FREE, "--fallback", "openrouter/vendor/paid",
                 "--out", str(h.tmp / "r.md")) == "RESULT=QUOTA"
    assert len(h.sent) == 1
    body = (h.tmp / "r.md").read_text()
    assert "slow down" in body and "fallback skipped" in body and "vendor/paid" in body


def test_auto_fallback_picks_other_zero_cost_models(h):
    h.script += [(429, "x", {"error": "x"}), reply()]
    assert h.run("--role", "analyst", "--model", FREE, "--fallback", "auto", "--out", str(h.tmp / "r.md")) == "RESULT=OK"
    assert h.sent[1]["body"]["model"] != h.sent[0]["body"]["model"]
    assert "paid" not in h.sent[1]["body"]["model"]


def test_model_auto_picks_a_zero_cost_model(h):
    assert h.run("--role", "researcher", "--model", "auto", "--out", str(h.tmp / "r.md")) == "RESULT=OK"
    assert "paid" not in h.sent[0]["body"]["model"] and h.sent[0]["body"]["model"] != "m1"


def test_daily_cap_blocks_before_any_request(h):
    for _ in range(3):
        ledger.append_row("OK", "x/y", "or-api", "analyst", True)
    assert h.run("--role", "analyst", "--model", FREE, "--cap", "3", "--out", str(h.tmp / "r.md")) == "RESULT=CAP"
    assert h.sent == []


def test_unreadable_ledger_blocks_instead_of_becoming_infinite(h, monkeypatch):
    monkeypatch.setattr(dg.ledger, "cap_used_today", lambda *a, **k: None)
    assert h.run("--role", "analyst", "--model", FREE, "--out", str(h.tmp / "r.md")) == "RESULT=CAP"
    assert h.sent == []


def test_ledger_rows_written_here_are_counted_by_the_shell_reader(h):
    h.run("--role", "analyst", "--model", FREE, "--out", str(h.tmp / "r.md"))
    h.script.append((429, "x", {"error": "x"}))
    h.run("--role", "analyst", "--model", FREE, "--out", str(h.tmp / "r2.md"))
    r = subprocess.run(["bash", "-c", f"source {ROOT / 'scripts/_bcoc_common.sh'}; bcoc_cap_used"],
                       capture_output=True, text=True,
                       env={**os.environ, "BCOPENCODE_STATE_DIR": str(h.state), "HOME": str(h.tmp)})
    assert r.stdout.strip() == "1"          # the OK counts, the 429 does not
    assert ledger.cap_used_today() == 1


def test_result_is_scanned_and_secrets_masked(h):
    h.script.append(reply(f"leaked key {KEY} oops"))
    assert h.run("--role", "analyst", "--model", FREE, "--out", str(h.tmp / "r.md")) == "RESULT=OK"
    assert KEY not in (h.tmp / "r.md").read_text()
    assert "SECRET_SCAN=MASKED" in h.err


def test_json_out_sidecar_has_no_body_secrets_and_is_private(h):
    h.run("--role", "analyst", "--model", FREE, "--json-out", "--out", str(h.tmp / "r.md"))
    side = h.tmp / "r.md.json"
    d = json.loads(side.read_text())
    assert d["result"] == "OK" and d["model"].endswith("a12b:free") and stat.S_IMODE(side.stat().st_mode) == 0o600
    assert any(l.startswith("JSON=") for l in h.lines)


def test_json_mode_sets_response_format(h):
    h.run("--role", "analyst", "--model", FREE, "--json-mode", "--out", str(h.tmp / "r.md"))
    assert h.sent[0]["body"]["response_format"] == {"type": "json_object"}


# ----------------------------------------------------------------------------- inputs
@pytest.mark.parametrize("args", [
    ["--session", "../evil"], ["--session", ""], ["--max-attempts", "0"], ["--max-tokens", "0"],
])
def test_bad_arguments_spend_nothing(h, args):
    assert h.run("--role", "analyst", "--model", FREE, *args, "--out", str(h.tmp / "r.md")) == "RESULT=BAD_ARGS"
    assert h.sent == []


def test_missing_prompt_file_is_BAD_ARGS(h, capsys):
    capsys.readouterr()
    dg.main(["--role", "analyst", "--model", FREE, "--prompt-file", str(h.tmp / "nope.md")])
    assert capsys.readouterr().out.strip().endswith("RESULT=BAD_ARGS")


def test_unknown_provider_is_BAD_ARGS(h):
    assert h.run("--role", "analyst", "--model", "nobody/x", "--out", str(h.tmp / "r.md")) == "RESULT=BAD_ARGS"


def test_image_only_goes_to_a_model_known_to_accept_images(h):
    img = h.tmp / "a.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20)
    assert h.run("--role", "analyst", "--model", FREE, "--image", str(img), "--out", str(h.tmp / "r.md")) == "RESULT=BAD_ARGS"
    assert h.sent == []
    assert h.run("--role", "analyst", "--model", "openrouter/vendor/vision:free", "--image", str(img),
                 "--out", str(h.tmp / "r.md")) == "RESULT=OK"
    parts = h.sent[0]["body"]["messages"][-1]["content"]
    assert parts[0]["type"] == "text" and parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_oversized_or_wrong_type_images_are_rejected(h):
    bad = h.tmp / "a.txt"
    bad.write_text("x")
    assert h.run("--role", "analyst", "--model", "openrouter/vendor/vision:free", "--image", str(bad),
                 "--out", str(h.tmp / "r.md")) == "RESULT=BAD_ARGS"


def test_scope_context_goes_through_the_packer_and_withholds_secrets(h):
    scope = h.tmp / "proj"
    scope.mkdir()
    (scope / "a.py").write_text("print('hi')\n")
    (scope / ".env").write_text("OPENROUTER_API" + "_KEY=" + KEY + "\n")  # split: see KEY
    h.run("--role", "reviewer", "--model", FREE, "--scope", str(scope), "--out", str(h.tmp / "r.md"))
    sent = json.dumps(h.sent[0]["body"])
    assert "print('hi')" in sent and KEY not in sent


# ----------------------------------------------------------------------------- sessions
def test_session_history_is_replayed_and_private(h):
    h.script += [reply("first answer"), reply("second answer")]
    h.run("--role", "analyst", "--model", FREE, "--session", "s1", "--out", str(h.tmp / "a.md"))
    h.prompt.write_text("Now continue.")
    h.run("--role", "analyst", "--model", FREE, "--session", "s1", "--out", str(h.tmp / "b.md"))
    msgs = h.sent[1]["body"]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[2]["content"] == "first answer"
    sp = h.state / "sessions" / "s1.json"
    assert stat.S_IMODE(sp.stat().st_mode) == 0o600 and stat.S_IMODE(sp.parent.stat().st_mode) == 0o700


def test_a_failed_turn_is_not_added_to_the_session(h):
    h.script.append((500, "boom", {"error": "boom"}))
    h.run("--role", "analyst", "--model", FREE, "--session", "s2", "--out", str(h.tmp / "a.md"))
    assert not (h.state / "sessions" / "s2.json").exists()


# ----------------------------------------------------------------------------- opencode gate
def rules(*rs):
    return [{"action": a, "resource": r, "effect": e} for a, r, e in rs]


# What `opencode debug agents` resolves for the agent opencode_v2.py writes (measured on 2.0.22):
# the built-in `* allow` and scoped scratch-dir allows first, then OUR default-deny + allowlist.
EXPLORE = rules(("*", "*", "allow"), ("external_directory", "*", "ask"),
                ("external_directory", "/tmp/opencode/*", "allow"),
                ("*", "*", "deny"), ("read", "*", "allow"), ("read", "*.env", "deny"),
                ("grep", "*", "allow"), ("glob", "*", "allow"), ("list", "*", "allow"),
                ("webfetch", "*", "allow"), ("websearch", "*", "allow"), ("browser", "*", "deny"))
NOWEB = [r for r in EXPLORE if r["action"] not in ("webfetch", "websearch")]


def agent(perms, aid="explore"):
    return [{"id": aid, "mode": "primary", "permissions": perms}]


def test_verify_v2_accepts_a_read_only_researcher():
    ok, why = dg.verify_v2(agent(EXPLORE), "explore", dg._must_block("researcher", True))
    assert ok, why


def test_verify_v2_refuses_web_tools_for_roles_that_may_not_use_them():
    ok, why = dg.verify_v2(agent(EXPLORE), "explore", dg._must_block("analyst", True))
    assert not ok and "webfetch" in why


def test_verify_v2_refuses_an_allow_all_agent():
    perms = rules(("*", "*", "allow"), ("external_directory", "*", "ask"))
    ok, why = dg.verify_v2(agent(perms, "build"), "build", dg._must_block("researcher", True))
    assert not ok and "edit=allow" in why and "bash=allow" in why


def test_verify_v2_is_last_match_wins():
    perms = rules(("edit", "*", "deny"), ("*", "*", "deny"), ("edit", "*", "allow"), ("bash", "*", "deny"),
                  ("subagent", "*", "deny"), ("external_directory", "*", "deny"))
    ok, why = dg.verify_v2(agent(perms), "explore", dg._must_block("researcher", True))
    assert not ok and "edit=allow" in why


def test_verify_v2_scoped_external_directory_allow_is_tolerated():
    perms = EXPLORE + rules(("external_directory", "/tmp/opencode/*", "allow"))
    ok, why = dg.verify_v2(agent(perms), "explore", dg._must_block("researcher", True))
    assert ok, why


@pytest.mark.parametrize("action", ["bash", "edit", "subagent"])
def test_verify_v2_refuses_a_scoped_allow_of_a_dangerous_action(action):
    # `bash allow "git *"` is arbitrary code execution; a scoped edit allow is a write
    perms = EXPLORE + rules((action, "git *", "allow"))
    ok, why = dg.verify_v2(agent(perms), "explore", dg._must_block("researcher", True))
    assert not ok and action in why


def test_verify_v2_refuses_when_the_default_is_a_grant():
    perms = rules(("*", "*", "deny"), ("edit", "*", "deny"), ("bash", "*", "deny"), ("subagent", "*", "deny"),
                  ("external_directory", "*", "deny"), ("*", "*", "allow"), ("edit", "*", "deny"),
                  ("bash", "*", "deny"), ("subagent", "*", "deny"), ("external_directory", "*", "deny"))
    ok, why = dg.verify_v2(agent(perms), "explore", dg._must_block("researcher", True))
    assert not ok and "default" in why


def test_verify_v2_missing_agent_or_garbage_is_refused():
    assert not dg.verify_v2(None, "explore", ["edit"])[0]
    assert not dg.verify_v2(agent(EXPLORE), "nope", ["edit"])[0]


STUB = textwrap.dedent("""\
    #!/bin/bash
    echo "$@" >> "$OPENCODE_STUB_LOG"
    case "$1" in
      --version) echo "v${OPENCODE_STUB_VERSION:-2.0.22}" ;;
      debug) cat "$OPENCODE_STUB_AGENTS" ;;
      agent) cat "$OPENCODE_STUB_AGENTS" ;;
      run) cat "$OPENCODE_STUB_RUN_OUT"; exit ${OPENCODE_STUB_RUN_RC:-0} ;;
    esac
""")


@pytest.fixture
def stub(h, monkeypatch):
    b = h.tmp / "bin"
    b.mkdir()
    (b / "opencode").write_text(STUB)
    (b / "opencode").chmod(0o755)
    monkeypatch.setenv("PATH", f"{b}:{os.environ['PATH']}")
    monkeypatch.setenv("OPENCODE_STUB_LOG", str(h.tmp / "stub.log"))
    monkeypatch.setenv("OPENCODE_STUB_AGENTS", str(h.tmp / "agents.json"))
    monkeypatch.setenv("OPENCODE_STUB_RUN_OUT", str(h.tmp / "run.out"))
    (h.tmp / "agents.json").write_text(json.dumps(
        agent(EXPLORE, "bcoc-researcher") + agent(NOWEB, "bcoc-analyst") + agent(NOWEB, "bcoc-reviewer")))
    (h.tmp / "run.out").write_text(json.dumps({"type": "text", "part": {"type": "text", "text": "researched!"}}) + "\n")
    monkeypatch.setattr(dg.dp, "opencode_version", lambda: os.environ.get("OPENCODE_STUB_VERSION", "v2.0.22"))
    h.log = lambda: (h.tmp / "stub.log").read_text() if (h.tmp / "stub.log").exists() else ""
    return h


def test_opencode_backend_runs_a_verified_agent_and_never_passes_auto(stub):
    assert stub.run("--role", "researcher", "--backend", "opencode", "--model", "openrouter/vendor/vision:free",
                    "--out", str(stub.tmp / "r.md")) == "RESULT=OK"
    log = stub.log()
    assert "run " in log and "--agent bcoc-researcher" in log and "--auto" not in log
    assert "-m openrouter/vendor/vision:free" in log and "--format json" in log
    assert "researched!" in (stub.tmp / "r.md").read_text()
    assert stub.rows()[-1]["backend"] == "opencode" and stub.rows()[-1]["billed"] is True


def test_opencode_backend_refuses_without_spending_when_the_agent_is_not_restricted(stub):
    (stub.tmp / "agents.json").write_text(json.dumps(agent(rules(("*", "*", "allow")))))
    assert stub.run("--role", "researcher", "--backend", "opencode", "--model", "openrouter/vendor/vision:free",
                    "--out", str(stub.tmp / "r.md")) == "RESULT=REFUSED"
    assert "run " not in stub.log()
    assert stub.rows()[-1]["billed"] is False


def test_opencode_backend_accepts_a_no_web_role_when_its_agent_has_no_web_tools(stub):
    assert stub.run("--role", "analyst", "--backend", "opencode", "--model", "openrouter/vendor/vision:free",
                    "--out", str(stub.tmp / "r.md")) == "RESULT=OK"
    assert "--agent bcoc-analyst" in stub.log()


def test_opencode_backend_refuses_a_no_web_role_whose_agent_resolves_web_tools(stub):
    (stub.tmp / "agents.json").write_text(json.dumps(agent(EXPLORE, "bcoc-analyst")))
    assert stub.run("--role", "analyst", "--backend", "opencode", "--model", "openrouter/vendor/vision:free",
                    "--out", str(stub.tmp / "r.md")) == "RESULT=REFUSED"
    assert "run " not in stub.log()


def test_the_v2_run_happens_inside_the_mirror_with_our_config_and_a_scrubbed_env(stub, monkeypatch):
    scope = stub.tmp / "proj"
    scope.mkdir()
    (scope / "a.py").write_text("x = 1\n")
    (scope / "opencode.json").write_text('{"permission": {"*": "allow"}}')
    (scope / "AGENTS.md").write_text("do evil")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak-me")
    monkeypatch.setenv("OPENCODE_DISABLE_PROJECT_CONFIG", "1")
    (stub.tmp / "bin" / "opencode").write_text(STUB.replace(
        'run) cat', 'run) pwd > "$OPENCODE_STUB_LOG.cwd"; cat opencode.json > "$OPENCODE_STUB_LOG.cfg"; env > "$OPENCODE_STUB_LOG.env"; ls >> "$OPENCODE_STUB_LOG.cwd"; cat'))
    assert stub.run("--role", "researcher", "--backend", "opencode", "--scope", str(scope),
                    "--model", "openrouter/vendor/vision:free", "--out", str(stub.tmp / "r.md")) == "RESULT=OK"
    cfg = json.loads((stub.tmp / "stub.log.cfg").read_text())
    assert cfg["agent"]["bcoc-researcher"]["permission"]["*"] == "deny"          # ours, not the repo's
    cwd = (stub.tmp / "stub.log.cwd").read_text()
    assert "/mirror" in cwd.splitlines()[0]
    assert "AGENTS.md.reviewed" in cwd and "opencode.json.reviewed" in cwd and "a.py" in cwd
    env = (stub.tmp / "stub.log.env").read_text()
    assert "AWS_SECRET_ACCESS_KEY" not in env and "OPENCODE_DISABLE_PROJECT_CONFIG" not in env


def test_agent_override_is_not_accepted_on_v2(stub):
    assert stub.run("--role", "researcher", "--backend", "opencode", "--agent", "mine",
                    "--model", "openrouter/vendor/vision:free", "--out", str(stub.tmp / "r.md")) == "RESULT=BAD_ARGS"


def test_opencode_backend_detects_the_silent_fallback_to_the_default_agent(stub):
    (stub.tmp / "stub").write_text("")
    s = (stub.tmp / "bin" / "opencode")
    s.write_text(STUB.replace('run) cat "$OPENCODE_STUB_RUN_OUT";', 'run) echo \'! agent "bcoc-researcher" is a subagent, not a primary agent. Falling back\' >&2; cat "$OPENCODE_STUB_RUN_OUT";'))
    assert stub.run("--role", "researcher", "--backend", "opencode", "--model", "openrouter/vendor/vision:free",
                    "--out", str(stub.tmp / "r.md")) == "RESULT=ERROR"
    assert "NOT trusted" in (stub.tmp / "r.md").read_text()


def test_opencode_backend_classifies_cli_failures(stub, monkeypatch):
    monkeypatch.setenv("OPENCODE_STUB_RUN_RC", "1")
    (stub.tmp / "run.out").write_text("")
    assert stub.run("--role", "researcher", "--backend", "opencode", "--model", "openrouter/vendor/vision:free",
                    "--out", str(stub.tmp / "r.md")) == "RESULT=ERROR"


def test_classify_cli_failure_words():
    assert dg.classify_cli_failure(1, "HTTP 429 Too Many Requests") == "QUOTA"
    assert dg.classify_cli_failure(1, "401 Unauthorized") == "AUTH"
    assert dg.classify_cli_failure(1, "segfault") == "ERROR"


def test_opencode_backend_rejects_or_api_only_options(stub):
    assert stub.run("--role", "researcher", "--backend", "opencode", "--model", FREE, "--json-mode",
                    "--out", str(stub.tmp / "r.md")) == "RESULT=BAD_ARGS"


def test_parse_v2_events_collects_text_and_denied_tool_calls():
    out = "\n".join(json.dumps(o) for o in [
        {"type": "step_start", "part": {}},
        {"type": "tool_use", "part": {"state": {"status": "error", "error": "rejected"}}},
        {"type": "text", "part": {"text": "A"}}, {"type": "text", "part": {"text": "B"}}]) + "\nnot json\n"
    assert dg.parse_v2_events(out) == ("AB", ["rejected"])


# ----------------------------------------------------------------------------- shared rpm window
def test_rpm_window_blocks_the_next_worker_until_a_slot_frees(tmp_path):
    t = [1000.0]
    slept = []

    def sleep(s):
        slept.append(s)
        t[0] += s

    for _ in range(3):
        assert ledger.rpm_acquire(3, tmp_path, time_fn=lambda: t[0], sleep_fn=sleep) == 0.0
    waited = ledger.rpm_acquire(3, tmp_path, time_fn=lambda: t[0], sleep_fn=sleep)
    assert waited == pytest.approx(60.0, abs=0.1) and slept


def test_rpm_zero_disables_the_window(tmp_path):
    assert ledger.rpm_acquire(0, tmp_path) == 0.0 and not (tmp_path / "rpm.jsonl").exists()


# ----------------------------------------------------------------------------- hardening (review round 1)
def test_child_env_passes_only_this_providers_credential(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "a")
    monkeypatch.setenv("PROVIDER_A_API_KEY", "b")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "c")
    monkeypatch.setattr(dg.dp, "opencode_provider_config",
                        lambda: {"provider-a": {"baseURL": "https://x.invalid/v1", "env": ["PROVIDER_A_API_KEY"]}})
    e = dg.child_env("provider-a")
    assert e["PROVIDER_A_API_KEY"] == "b" and "OPENROUTER_API_KEY" not in e and "AWS_SECRET_ACCESS_KEY" not in e
    assert e["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert "OPENROUTER_API_KEY" in dg.child_env("openrouter")


def test_a_plain_http_or_odd_base_url_never_receives_a_credential(h):
    c = json.loads(json.dumps(CACHE))
    c["providers"]["provider-a"]["base_url"] = "http://evil.invalid/v1"
    import pytest as _p
    with _p.MonkeyPatch.context() as mp:
        mp.setattr(dg.dp, "ensure", lambda **k: (c, "fresh"))
        assert h.run("--role", "analyst", "--model", "provider-a/m2", "--allow-paid", "--out", str(h.tmp / "r.md")) == "RESULT=BAD_ARGS"
    assert h.sent == []


def test_session_is_stored_redacted_and_notes_a_model_change(h, capsys):
    h.script += [reply("echo " + KEY), reply("ok")]
    h.run("--role", "analyst", "--model", FREE, "--session", "s9", "--out", str(h.tmp / "a.md"))
    assert KEY not in (h.state / "sessions" / "s9.json").read_text()
    h.run("--role", "analyst", "--model", FREE2, "--session", "s9", "--out", str(h.tmp / "b.md"))
    assert "last answered by" in h.err


@pytest.mark.parametrize("bad", ["../x", "a/b", "-x", ""])
def test_agent_name_is_validated(h, bad):
    assert h.run("--role", "analyst", "--model", FREE, f"--agent={bad}", "--out", str(h.tmp / "r.md")) == "RESULT=BAD_ARGS"


def test_unwritable_ledger_stops_further_spend(h, monkeypatch):
    h.script += [(429, "x", {"error": "x"}), reply()]
    monkeypatch.setattr(dg.ledger, "append_row", lambda *a, **k: False)
    h.run("--role", "analyst", "--model", FREE, "--fallback", FREE2, "--out", str(h.tmp / "r.md"))
    assert len(h.sent) == 1


def test_garbage_numeric_env_does_not_crash(monkeypatch):
    monkeypatch.setenv("BCOPENCODE_CAP", "lots")
    monkeypatch.setenv("BCOPENCODE_TIMEOUT", "")
    assert dg.build_parser().parse_args(["--role", "analyst", "--prompt-file", "x"]).cap == 200


# ----------------------------------------------------------------------------- withheld files are never silent
def test_files_the_secret_filter_withheld_are_reported_and_told_to_the_model(h):
    scope = h.tmp / "proj2"
    scope.mkdir()
    (scope / "ok.py").write_text("x = 1\n")
    # ordinary prose that reads as a password assignment: the filter errs toward withholding
    (scope / "notes.md").write_text("First " + "pa" + "ss: 8/10 in 18.5 min (10 workers, 5 at a time); reran them\n")  # assembled: see test_pack_secrets
    h.run("--role", "reviewer", "--model", FREE, "--scope", str(scope), "--out", str(h.tmp / "r.md"))
    sent = json.dumps(h.sent[0]["body"])
    assert "notes.md" in sent and "withheld" in sent            # the model is told it exists and was withheld
    assert "8/10 in 18.5" not in sent                           # ...and still does not see it
    body = (h.tmp / "r.md").read_text()
    assert "withheld_files: 1" in body and "not reviewed" in body and "notes.md" in body
    assert "withheld the" in h.err or "withheld 1" in h.err


def test_no_withheld_note_when_nothing_was_withheld(h):
    scope = h.tmp / "proj3"
    scope.mkdir()
    (scope / "ok.py").write_text("x = 1\n")
    h.run("--role", "reviewer", "--model", FREE, "--scope", str(scope), "--out", str(h.tmp / "r.md"))
    assert "withheld_files: 0" in (h.tmp / "r.md").read_text()
    assert "NOTE from the harness" not in json.dumps(h.sent[0]["body"])


# ----------------------------------------------------------------------------- streaming, stall watchdog, salvage
STREAM_STUB = textwrap.dedent("""\
    #!/bin/bash
    echo "$@" >> "$OPENCODE_STUB_LOG"
    case "$1" in
      --version) echo "v2.0.22" ;;
      debug) cat "$OPENCODE_STUB_AGENTS" ;;
      models|auth) echo "STUB-DISCOVERY-CALLED" >> "$OPENCODE_STUB_LOG.disc" ;;
      run)
        resumed=0; for a in "$@"; do [ "$a" = "-s" ] && resumed=1; done
        if [ $resumed = 1 ]; then
          if [ "$OPENCODE_STUB_RESUME" = "stall" ]; then
            echo '{"type":"step_start","sessionID":"ses_stub1","part":{}}'; sleep 30
          fi
          echo '{"type":"text","sessionID":"ses_stub1","part":{"text":"SALVAGED REPORT"}}'
        else
          echo '{"type":"step_start","sessionID":"ses_stub1","part":{}}'
          case "$OPENCODE_STUB_MODE" in
            stall) echo '{"type":"text","sessionID":"ses_stub1","part":{"text":"half a thought"}}'; sleep 30 ;;
            drip) for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
                    echo '{"type":"tool_use","sessionID":"ses_stub1","part":{"state":{"status":"completed"}}}'; sleep 0.5
                  done
                  echo '{"type":"text","sessionID":"ses_stub1","part":{"text":"late"}}' ;;
            *) echo '{"type":"text","sessionID":"ses_stub1","part":{"text":"normal answer"}}' ;;
          esac
        fi ;;
    esac
""")


@pytest.fixture
def stream(stub, monkeypatch):
    (stub.tmp / "bin" / "opencode").write_text(STREAM_STUB)
    monkeypatch.delenv("OPENCODE_STUB_MODE", raising=False)
    monkeypatch.delenv("OPENCODE_STUB_RESUME", raising=False)
    return stub


def _run_oc(s, *extra):
    return s.run("--role", "researcher", "--backend", "opencode", "--cache-only",
                 "--model", "openrouter/vendor/vision:free", "--out", str(s.tmp / "r.md"), *extra)


def test_events_are_read_live_and_the_session_id_and_gaps_are_recorded(stream):
    assert _run_oc(stream, "--json-out") == "RESULT=OK"
    side = json.loads((stream.tmp / "r.md.json").read_text())
    assert side["opencode_session"] == "ses_stub1" and side["max_gap_s"] is not None
    assert "normal answer" in (stream.tmp / "r.md").read_text()


def test_a_stalled_session_is_killed_early_and_resumed_to_write_its_report(stream, monkeypatch):
    monkeypatch.setenv("OPENCODE_STUB_MODE", "stall")
    t0 = time.time()
    assert _run_oc(stream, "--stall-timeout", "2", "--timeout", "60", "--json-out") == "RESULT=TRUNCATED"
    assert time.time() - t0 < 15, "the watchdog must not wait for the 30 s hang or the 60 s total"
    body = (stream.tmp / "r.md").read_text()
    assert "SALVAGED REPORT" in body and "partial research" in body and "salvaged: True" in body
    log = stream.log()
    assert "-s ses_stub1" in log and "Stop researching" in log          # it resumed THAT session
    assert json.loads((stream.tmp / "r.md.json").read_text())["salvaged"] is True
    assert stream.rows()[-1]["result"] == "TRUNCATED" and stream.rows()[-1]["billed"] is True


def test_no_salvage_gives_timeout_and_keeps_what_was_said(stream, monkeypatch):
    monkeypatch.setenv("OPENCODE_STUB_MODE", "stall")
    assert _run_oc(stream, "--stall-timeout", "2", "--no-salvage") == "RESULT=TIMEOUT"
    body = (stream.tmp / "r.md").read_text()
    assert "stalled" in body and "half a thought" in body
    assert "-s ses_stub1" not in stream.log()


def test_a_resumed_session_that_also_stalls_ends_as_timeout(stream, monkeypatch):
    monkeypatch.setenv("OPENCODE_STUB_MODE", "stall")
    monkeypatch.setenv("OPENCODE_STUB_RESUME", "stall")
    t0 = time.time()
    assert _run_oc(stream, "--stall-timeout", "2", "--salvage-timeout", "3") == "RESULT=TIMEOUT"
    assert time.time() - t0 < 20


def test_the_total_timeout_applies_even_while_events_keep_arriving(stream, monkeypatch):
    monkeypatch.setenv("OPENCODE_STUB_MODE", "drip")            # an event every 0.5 s for 6 s
    t0 = time.time()
    assert _run_oc(stream, "--stall-timeout", "5", "--timeout", "2", "--no-salvage") == "RESULT=TIMEOUT"
    assert time.time() - t0 < 6 and "no answer within 2s" in (stream.tmp / "r.md").read_text()


def test_steady_events_do_not_trip_the_stall_watchdog(stream, monkeypatch):
    monkeypatch.setenv("OPENCODE_STUB_MODE", "drip")
    assert _run_oc(stream, "--stall-timeout", "3", "--timeout", "30") == "RESULT=OK"


def _live_sleepers():
    out = subprocess.run(["pgrep", "-f", "sleep 30"], capture_output=True, text=True).stdout.split()
    found = []
    for pid in out:
        try:
            cmd = open(f"/proc/{pid}/cmdline").read().replace("\0", " ")
        except OSError:
            continue
        if cmd.strip() == "sleep 30":
            found.append(pid)
    return found


def test_the_killed_process_group_is_really_gone(stream, monkeypatch):
    before = set(_live_sleepers())
    monkeypatch.setenv("OPENCODE_STUB_MODE", "stall")
    _run_oc(stream, "--stall-timeout", "1", "--no-salvage")
    # the stub's `sleep 30` is a grandchild: it only dies if the whole process group was killed
    for _ in range(20):
        mine = set(_live_sleepers()) - before
        if not mine:
            break
        time.sleep(0.25)
    assert not mine, "the watchdog killed the stub but left its child running"


def test_workdir_is_prepared_and_verified_once_and_reused(stream):
    wd = stream.tmp / "wd"
    for i in range(3):
        assert _run_oc(stream, "--workdir", str(wd), "--out", str(stream.tmp / f"r{i}.md")) == "RESULT=OK"
    assert stream.log().count("debug agents") == 1, "verification must happen once per prepared workdir"
    assert (wd / ".bcoc_ready.json").is_file() and (wd / "opencode.json").is_file()


def test_changing_the_scope_rebuilds_the_workdir(stream):
    wd = stream.tmp / "wd"
    s1, s2 = stream.tmp / "s1", stream.tmp / "s2"
    for d, name in ((s1, "one.py"), (s2, "two.py")):
        d.mkdir()
        (d / name).write_text("x = 1\n")
    _run_oc(stream, "--workdir", str(wd), "--scope", str(s1), "--out", str(stream.tmp / "a.md"))
    assert (wd / "one.py").exists()
    _run_oc(stream, "--workdir", str(wd), "--scope", str(s2), "--out", str(stream.tmp / "b.md"))
    assert (wd / "two.py").exists() and not (wd / "one.py").exists()
    assert stream.log().count("debug agents") == 2


def test_prepare_only_spends_nothing_and_needs_no_prompt(stream):
    p = stream.tmp / "wd2"
    out = stream.run("--role", "analyst", "--backend", "opencode", "--cache-only", "--prepare-only",
                     "--model", "openrouter/vendor/vision:free", "--workdir", str(p))
    assert out == "RESULT=OK" and (p / ".bcoc_ready.json").is_file()
    assert "run " not in stream.log() and stream.rows() == []


def test_prepare_only_refuses_an_unrestricted_agent(stream):
    (stream.tmp / "agents.json").write_text(json.dumps(agent(rules(("*", "*", "allow")), "bcoc-researcher")))
    out = stream.run("--role", "researcher", "--backend", "opencode", "--cache-only", "--prepare-only",
                     "--model", "openrouter/vendor/vision:free", "--workdir", str(stream.tmp / "wd3"))
    assert out == "RESULT=REFUSED" and not (stream.tmp / "wd3" / ".bcoc_ready.json").exists()


def test_cache_only_runs_no_discovery_calls(stream):
    _run_oc(stream)
    assert not (stream.tmp / "stub.log.disc").exists()


def test_a_ready_marker_from_a_different_agent_is_not_trusted(stream):
    wd = stream.tmp / "wd4"
    _run_oc(stream, "--workdir", str(wd), "--out", str(stream.tmp / "a.md"))
    stream.run("--role", "analyst", "--backend", "opencode", "--cache-only", "--model", "openrouter/vendor/vision:free",
               "--workdir", str(wd), "--out", str(stream.tmp / "b.md"))
    assert stream.log().count("debug agents") == 2


def test_two_processes_meeting_an_unbuilt_workdir_do_not_clobber_each_other(stream):
    """Both run the real delegate.py against one fresh --workdir at once; the lock serialises the build."""
    wd = stream.tmp / "race"
    scope = stream.tmp / "rscope"
    scope.mkdir()
    (scope / "a.py").write_text("x = 1\n")
    env = {**os.environ}
    cmd = [sys.executable, str(ROOT / "scripts" / "delegate.py"), "--role", "researcher", "--backend", "opencode",
           "--cache-only", "--model", "openrouter/vendor/vision:free", "--workdir", str(wd), "--scope", str(scope),
           "--prompt-file", str(stream.prompt)]
    ps = [subprocess.Popen(cmd + ["--out", str(stream.tmp / f"race{i}.md")], stdout=subprocess.PIPE, text=True, env=env)
          for i in range(4)]
    outs = [p.communicate(timeout=60)[0].strip().splitlines()[-1] for p in ps]
    assert outs == ["RESULT=OK"] * 4
    assert stream.log().count("debug agents") == 1, "only the first process may build and verify"


@pytest.mark.parametrize("status,word", [(403, "AUTH"), (429, "QUOTA"), (502, "UNREACHABLE")])
def test_an_opencode_error_event_becomes_the_matching_result_word_with_its_real_message(stream, monkeypatch, status, word):
    err = json.dumps({"type": "error", "sessionID": "ses_stub1", "error": {"type": "provider.x", "message": "the provider said no", "status": status}})
    (stream.tmp / "bin" / "opencode").write_text(STREAM_STUB.replace(
        "*) echo '{\"type\":\"text\",\"sessionID\":\"ses_stub1\",\"part\":{\"text\":\"normal answer\"}}' ;;",
        "*) echo '" + err + "' ;;"))
    assert _run_oc(stream) == f"RESULT={word}"
    body = (stream.tmp / "r.md").read_text()
    assert "the provider said no" in body and str(status) in body and "returned no text" not in body


def test_sigterm_kills_the_opencode_child_removes_the_temp_dir_and_records_the_row(stream, monkeypatch):
    """The opencode child runs in its own process group: killing delegate.py alone would orphan it."""
    import glob
    import signal as _sig
    env = {**os.environ, "OPENCODE_STUB_MODE": "stall"}
    before = set(glob.glob("/tmp/bcoc.deleg.*"))
    cmd = [sys.executable, str(ROOT / "scripts" / "delegate.py"), "--role", "researcher", "--backend", "opencode",
           "--cache-only", "--model", "openrouter/vendor/vision:free", "--prompt-file", str(stream.prompt),
           "--out", str(stream.tmp / "sig.md"), "--stall-timeout", "60", "--timeout", "120"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, env=env)
    time.sleep(3)                                   # let it start the stub, which sleeps 30 s
    assert any("sleep 30" in open(f"/proc/{x}/cmdline").read().replace("\0", " ") for x in
               subprocess.run(["pgrep", "-f", "sleep 30"], capture_output=True, text=True).stdout.split()
               if os.path.exists(f"/proc/{x}/cmdline"))
    p.send_signal(_sig.SIGTERM)
    out = p.communicate(timeout=15)[0]
    assert p.returncode == 130 and "RESULT=INTERRUPTED" in out
    time.sleep(0.5)
    assert set(glob.glob("/tmp/bcoc.deleg.*")) - before == set(), "the run directory must be removed"
    assert stream.rows()[-1]["result"] == "INTERRUPTED" and stream.rows()[-1]["billed"] is True


def test_steps_reaches_the_agent_config_and_a_different_value_rebuilds_the_workdir(stream):
    wd = stream.tmp / "wsteps"
    _run_oc(stream, "--workdir", str(wd), "--steps", "2", "--out", str(stream.tmp / "s1.md"))
    assert json.loads((wd / "opencode.json").read_text())["agent"]["bcoc-researcher"]["steps"] == 2
    _run_oc(stream, "--workdir", str(wd), "--steps", "2", "--out", str(stream.tmp / "s2.md"))
    assert stream.log().count("debug agents") == 1, "same steps: reuse the verified workdir"
    _run_oc(stream, "--workdir", str(wd), "--steps", "5", "--out", str(stream.tmp / "s3.md"))
    assert json.loads((wd / "opencode.json").read_text())["agent"]["bcoc-researcher"]["steps"] == 5
    assert stream.log().count("debug agents") == 2


def test_steps_must_be_positive(stream):
    assert _run_oc(stream, "--steps", "0") == "RESULT=BAD_ARGS"
