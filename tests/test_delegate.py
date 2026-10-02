"""delegate.py — one task, any provider, as a worker. Offline: HTTP and the opencode CLI
are replaced. The things worth pinning are the ones that cost money or hide a failure:
the cost gate, the cap, fallback discipline, the ledger, redaction and the agent gate."""
import json
import os
import stat
import subprocess
import sys
import textwrap
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


EXPLORE = rules(("*", "*", "allow"), ("external_directory", "*", "ask"),
                ("*", "*", "deny"), ("grep", "*", "allow"), ("read", "*", "allow"),
                ("webfetch", "*", "allow"), ("websearch", "*", "allow"),
                ("subagent", "*", "deny"), ("external_directory", "*", "ask"))


def agent(perms, aid="explore"):
    return [{"id": aid, "mode": "subagent", "permissions": perms}]


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
    (h.tmp / "agents.json").write_text(json.dumps(agent(EXPLORE)))
    (h.tmp / "run.out").write_text(json.dumps({"type": "text", "part": {"type": "text", "text": "researched!"}}) + "\n")
    monkeypatch.setattr(dg.dp, "opencode_version", lambda: os.environ.get("OPENCODE_STUB_VERSION", "v2.0.22"))
    h.log = lambda: (h.tmp / "stub.log").read_text() if (h.tmp / "stub.log").exists() else ""
    return h


def test_opencode_backend_runs_a_verified_agent_and_never_passes_auto(stub):
    assert stub.run("--role", "researcher", "--backend", "opencode", "--model", "openrouter/vendor/vision:free",
                    "--out", str(stub.tmp / "r.md")) == "RESULT=OK"
    log = stub.log()
    assert "run " in log and "--agent explore" in log and "--auto" not in log
    assert "-m openrouter/vendor/vision:free" in log and "--format json" in log
    assert "researched!" in (stub.tmp / "r.md").read_text()
    assert stub.rows()[-1]["backend"] == "opencode" and stub.rows()[-1]["billed"] is True


def test_opencode_backend_refuses_without_spending_when_the_agent_is_not_restricted(stub):
    (stub.tmp / "agents.json").write_text(json.dumps(agent(rules(("*", "*", "allow")))))
    assert stub.run("--role", "researcher", "--backend", "opencode", "--model", "openrouter/vendor/vision:free",
                    "--out", str(stub.tmp / "r.md")) == "RESULT=REFUSED"
    assert "run " not in stub.log()
    assert stub.rows()[-1]["billed"] is False


def test_opencode_backend_refuses_a_non_researcher_on_v2_because_explore_has_web_tools(stub):
    assert stub.run("--role", "analyst", "--backend", "opencode", "--model", "openrouter/vendor/vision:free",
                    "--out", str(stub.tmp / "r.md")) == "RESULT=REFUSED"
    assert "run " not in stub.log()


def test_opencode_backend_detects_the_silent_fallback_to_the_default_agent(stub):
    (stub.tmp / "stub").write_text("")
    s = (stub.tmp / "bin" / "opencode")
    s.write_text(STUB.replace('run) cat "$OPENCODE_STUB_RUN_OUT";', 'run) echo \'! agent "explore" is a subagent, not a primary agent. Falling back\' >&2; cat "$OPENCODE_STUB_RUN_OUT";'))
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
