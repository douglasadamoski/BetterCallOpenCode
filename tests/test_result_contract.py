"""The RESULT contract: every exit path's last stdout line is RESULT=<WORD>.

SKILL.md tells Claude to branch on that line. A path that exits without it reads as a
crash and, per the skill's own instructions, invites a retry that spends the request
again. The 2026-08-03 audit reproduced EIGHT paths that violated it, including three
that fired *after* the request had already been paid for.

Every case here is free: no path reaches the network. Cases that would spend a request
are driven to a gate that refuses first (non-free model, cap 0, bad args, unreadable
ledger, unfilled template).
"""
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "scripts" / "opencode_review.sh"
MULTI = ROOT / "scripts" / "multi_review.sh"

# Every word the skill is allowed to emit. Must equal the SKILL.md table.
DOCUMENTED = {
    "OK", "TRUNCATED", "AUTH", "CAP", "QUOTA", "TIMEOUT", "UNREACHABLE",
    "ERROR", "PAID_BLOCKED", "REFUSED", "PARTIAL", "BAD_ARGS", "INTERRUPTED",
}


@pytest.fixture
def env(tmp_path):
    """Isolated state dir and HOME so tests never touch the real ledger."""
    e = dict(os.environ)
    e["BCOPENCODE_STATE_DIR"] = str(tmp_path / "state")
    e["HOME"] = str(tmp_path / "home")
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    # Guarantee no key is resolvable, so nothing can reach OpenRouter even by accident.
    e.pop("OPENROUTER_API_KEY", None)
    e["BCOPENCODE_API_KEY"] = ""
    return e


def run(args, env, cwd=None, timeout=90, stdin=None):
    p = subprocess.run(
        ["bash", str(REVIEW)] + args,
        capture_output=True, text=True, env=env, cwd=cwd, timeout=timeout, input=stdin,
    )
    return p


def last_stdout_line(p):
    lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def assert_contract(p, expected=None):
    last = last_stdout_line(p)
    assert last.startswith("RESULT="), (
        f"last stdout line was {last!r}, not RESULT=\n"
        f"--- stdout ---\n{p.stdout}\n--- stderr ---\n{p.stderr[-1500:]}"
    )
    word = last.split("=", 1)[1]
    assert word in DOCUMENTED, f"undocumented RESULT word {word!r} (add it to SKILL.md)"
    if expected is not None:
        assert word == expected, f"expected RESULT={expected}, got RESULT={word}"
    return word


@pytest.fixture
def scope(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    (d / "app.py").write_text("def f():\n    return 1\n")
    return d


@pytest.fixture
def prompt(tmp_path):
    p = tmp_path / "prompt.md"
    p.write_text("Review this code. Be brief.\n")
    return p


# --- gate and argument paths (no request possible) --------------------------------

def test_help(env):
    assert_contract(run(["--help"], env), "OK")


def test_unknown_arg(env):
    assert_contract(run(["--nope"], env), "BAD_ARGS")


def test_missing_flag_value(env):
    assert_contract(run(["--model"], env), "BAD_ARGS")


def test_bad_mode(env, prompt, scope, tmp_path):
    assert_contract(run(["--mode", "bogus", "--prompt-file", str(prompt),
                         "--out", str(tmp_path / "o.md"), "--scope", str(scope)], env),
                    "BAD_ARGS")


def test_bad_backend(env, prompt, scope, tmp_path):
    assert_contract(run(["--backend", "bogus", "--prompt-file", str(prompt),
                         "--out", str(tmp_path / "o.md"), "--scope", str(scope)], env),
                    "BAD_ARGS")


@pytest.mark.parametrize(
    "args",
    [
        ["--cap", "abc"],
        ["--max-tokens", "abc"],
        ["--max-tokens", "0"],
        ["--timeout", "abc"],
        ["--timeout", "1"],          # below the 5s floor
        ["--temperature", "99"],
        ["--temperature", "abc"],
        ["--max-input-tokens", "0"],
    ],
)
def test_numeric_argument_validation(env, prompt, scope, tmp_path, args):
    """The audit found only --cap was validated; the rest reached python or `timeout`."""
    p = run(args + ["--prompt-file", str(prompt), "--out", str(tmp_path / "o.md"),
                    "--scope", str(scope)], env)
    assert_contract(p, "BAD_ARGS")


def test_scope_not_a_directory(env, prompt, tmp_path):
    assert_contract(run(["--prompt-file", str(prompt), "--out", str(tmp_path / "o.md"),
                         "--scope", str(tmp_path / "nope")], env), "BAD_ARGS")


def test_multiple_scopes_refused(env, prompt, scope, tmp_path):
    assert_contract(run(["--prompt-file", str(prompt), "--out", str(tmp_path / "o.md"),
                         "--scope", str(scope), "--scope", str(tmp_path)], env), "BAD_ARGS")


def test_missing_prompt_file(env, scope, tmp_path):
    assert_contract(run(["--prompt-file", str(tmp_path / "nope.md"),
                         "--out", str(tmp_path / "o.md"), "--scope", str(scope)], env),
                    "BAD_ARGS")


def test_out_is_a_directory(env, prompt, scope, tmp_path):
    """Reproduced as a silent exit-1 with EMPTY stdout, after the request was spent."""
    d = tmp_path / "outdir"
    d.mkdir()
    assert_contract(run(["--prompt-file", str(prompt), "--out", str(d),
                         "--scope", str(scope)], env), "BAD_ARGS")


def test_paid_model_blocked(env, prompt, scope, tmp_path):
    assert_contract(run(["--model", "openai/gpt-4o", "--prompt-file", str(prompt),
                         "--out", str(tmp_path / "o.md"), "--scope", str(scope)], env),
                    "PAID_BLOCKED")


def test_cap_zero(env, prompt, scope, tmp_path):
    assert_contract(run(["--cap", "0", "--prompt-file", str(prompt),
                         "--out", str(tmp_path / "o.md"), "--scope", str(scope)], env),
                    "CAP")


def test_unfilled_template_does_not_spend(env, scope, tmp_path):
    """An unsubstituted {{PLACEHOLDER}} used to be sent verbatim, burning a request."""
    pf = tmp_path / "tpl.md"
    pf.write_text("Review this.\n\n{{INTENT_DESCRIPTION}}\n\n{{SCOPE_NOTES}}\n")
    p = run(["--prompt-file", str(pf), "--out", str(tmp_path / "o.md"),
             "--scope", str(scope)], env)
    assert_contract(p, "BAD_ARGS")
    assert "INTENT_DESCRIPTION" in p.stderr


def test_opencode_backend_refuses_a_scope_carrying_plugins(env, prompt, scope, tmp_path):
    """A repo's .opencode/plugin/*.js is imported and executed by opencode before any
    agent, permission or model exists — arbitrary code execution on the host.

    Verified on 1.18.11 that a real `opencode run` session executes it even with
    --pure AND OPENCODE_DISABLE_PROJECT_CONFIG=1 (both DO block it for
    `opencode agent list`, which is what makes this easy to measure wrongly).
    Refusing the scope is the only defence available to the skill.
    """
    plugin_dir = scope / ".opencode" / "plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "evil.js").write_text("import fs from 'fs';\n")
    p = run(["--backend", "opencode", "--prompt-file", str(prompt),
             "--out", str(tmp_path / "o.md"), "--scope", str(scope)], env)
    assert_contract(p, "REFUSED")
    assert "plugin" in p.stderr.lower()


@pytest.mark.parametrize("bad_scope", ["/", "/tmp", "/etc", "/usr"])
def test_scope_breadth_refusal(env, prompt, tmp_path, bad_scope):
    """--scope / or $HOME uploads whatever the secret filter misses, and it is a heuristic."""
    assert_contract(run(["--prompt-file", str(prompt), "--out", str(tmp_path / "o.md"),
                         "--scope", bad_scope], env), "BAD_ARGS")


def test_scope_home_refused(env, prompt, tmp_path):
    home = tmp_path / "home"
    e = dict(env)
    e["HOME"] = str(home)
    assert_contract(run(["--prompt-file", str(prompt), "--out", str(tmp_path / "o.md"),
                         "--scope", str(home)], e), "BAD_ARGS")


# --- post-spend failure paths ------------------------------------------------------

def test_unreadable_ledger_fails_closed(env, prompt, scope, tmp_path):
    """The cap used to silently become infinite when the ledger could not be read.

    `bcoc_cap_used` ended with `|| echo 0`, so any read error read as "0 used today".
    """
    state = Path(env["BCOPENCODE_STATE_DIR"])
    state.mkdir(parents=True, exist_ok=True)
    ledger = state / "usage.jsonl"
    ledger.write_text('{"day":"2000-01-01","result":"OK","billed":true}\n')
    ledger.chmod(0o000)
    try:
        p = run(["--prompt-file", str(prompt), "--out", str(tmp_path / "o.md"),
                 "--scope", str(scope)], env)
        word = assert_contract(p)
        assert word == "ERROR", "an unreadable ledger must refuse, not run with cap=0"
    finally:
        ledger.chmod(0o600)


def test_failed_pack_does_not_spend(env, prompt, tmp_path):
    """A failed pack used to still send the request, with '(pack failed)' as the body."""
    empty = tmp_path / "emptyscope"
    empty.mkdir()
    p = run(["--prompt-file", str(prompt), "--out", str(tmp_path / "o.md"),
             "--scope", str(empty)], env)
    word = assert_contract(p)
    assert word in ("ERROR", "AUTH"), word


# --- signals -----------------------------------------------------------------------

@pytest.mark.parametrize("sig", ["INT", "TERM"])
def test_signal_still_prints_result(env, prompt, scope, tmp_path, sig):
    """SIGTERM used to kill the wrapper and orphan the client (ppid=1), which kept
    running, spent the request, and produced no report, no ledger row, no RESULT=."""
    import signal
    import time

    proc = subprocess.Popen(
        ["bash", str(REVIEW), "--prompt-file", str(prompt),
         "--out", str(tmp_path / "o.md"), "--scope", str(scope), "--timeout", "60"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        start_new_session=True,
    )
    time.sleep(2.5)
    proc.send_signal(getattr(signal, f"SIG{sig}"))
    try:
        out, _err = proc.communicate(timeout=45)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail(f"SIG{sig} did not stop the script within 45s")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines, f"SIG{sig}: no stdout at all"
    assert lines[-1].startswith("RESULT="), f"SIG{sig}: last line was {lines[-1]!r}"
    assert lines[-1].split("=", 1)[1] in DOCUMENTED


# --- documentation consistency -----------------------------------------------------

def test_skill_md_documents_every_word_the_scripts_emit():
    """`PARTIAL` was emitted by panel_run.py and absent from the SKILL.md table.

    Claude had no branch for it.
    """
    skill = (ROOT / "SKILL.md").read_text()
    table = skill.split("## RESULT handling", 1)[-1].split("\n## ", 1)[0]
    documented = {w for w in DOCUMENTED if f"| {w} " in table or f"| {w}\n" in table}
    emitted = set()
    import re
    for f in ROOT.glob("scripts/*.sh"):
        emitted |= set(re.findall(r'RESULT=([A-Z_]+)', f.read_text()))
    for f in ROOT.glob("scripts/*.py"):
        emitted |= set(re.findall(r'RESULT=\{?["\']?([A-Z_]{2,})', f.read_text()))
    emitted &= DOCUMENTED
    missing = emitted - documented
    assert not missing, f"emitted but not in the SKILL.md RESULT table: {sorted(missing)}"


def test_repo_local_config_is_ignored_entirely(env, prompt, scope, tmp_path):
    """The project under review does not get to configure its own review.

    A `.bettercallopencode.env` was loaded from $(pwd) — not even the resolved --scope,
    so running the skill from an unrelated directory let THAT directory influence the
    run while the actual repo's file was ignored. Every key it could set affects cost
    or how much source gets uploaded, so the feature was removed rather than relocated.
    """
    (scope / ".bettercallopencode.env").write_text(
        "BCOPENCODE_ALLOW_PAID=1\nBCOPENCODE_MAX_TOKENS=99999\nBCOPENCODE_TEMPERATURE=1.9\n"
    )
    p = run(["--model", "openai/gpt-4o", "--prompt-file", str(prompt),
             "--out", str(tmp_path / "o.md"), "--scope", str(scope)], env, cwd=str(scope))
    # ALLOW_PAID must not have been honoured.
    assert_contract(p, "PAID_BLOCKED")
    assert "being IGNORED" in p.stderr
