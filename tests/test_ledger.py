"""The usage ledger is the cap. It was only ever tested indirectly.

`bcoc_cap_used` decides whether the next review is allowed to spend one of the user's
finite free daily requests, and `bcoc_usage_append` is the only thing that tells it a
request was spent. Every interesting failure here is silent:

  * a row counted that never reached the model -> the user is locked out early;
  * a row not counted that did -> the cap overshoots and OpenRouter starts 429ing;
  * a read error reported as `0` -> the cap becomes infinite on exactly the filesystem
    condition where it matters most.

These drive the real shell functions rather than a reimplementation, because the bug
this file is guarding against is always in the shell.
"""
import json
import os
import shlex
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "scripts" / "_bcoc_common.sh"

# The outcomes that provably never reached the model. Mirrors NOT_BILLED in the reader.
NEVER_BILLED = ["AUTH", "QUOTA", "UNREACHABLE", "PAID_BLOCKED", "REFUSED", "CAP", "BAD_ARGS"]
# The outcomes that must count: the request was made, answered or not.
ALWAYS_BILLED = ["OK", "TRUNCATED", "ERROR", "TIMEOUT", "INTERRUPTED"]


@pytest.fixture
def env(tmp_path):
    """HOME and STATE_DIR redirected: a test must never touch the real ledger."""
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    e = dict(os.environ)
    e["HOME"] = str(home)
    e["XDG_CONFIG_HOME"] = str(home / ".config")
    e["BCOPENCODE_STATE_DIR"] = str(tmp_path / "state")
    e.pop("OPENROUTER_API_KEY", None)
    return e


@pytest.fixture
def ledger(env):
    return Path(env["BCOPENCODE_STATE_DIR"]) / "usage.jsonl"


def sh(snippet, env, cwd=None, timeout=120):
    """Run a snippet with _bcoc_common.sh sourced."""
    script = "source %s\n%s" % (shlex.quote(str(COMMON)), snippet)
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, env=env, cwd=cwd, timeout=timeout,
    )


def append(env, result, billed="true", model="x/y:free", backend="or-api", mode="A",
           free="true", pt="", ct="", tt="", cost=""):
    args = " ".join(shlex.quote(a) for a in
                    [result, model, backend, mode, free, billed, pt, ct, tt, cost])
    p = sh("bcoc_usage_append " + args, env)
    assert p.returncode == 0, p.stderr
    return p


def cap_used(env):
    p = sh("bcoc_cap_used", env)
    assert p.returncode == 0, (
        "bcoc_cap_used must never fail: callers run under `set -e` and a non-zero exit "
        "kills the run before it can print RESULT=\n" + p.stderr
    )
    return p.stdout.strip()


def rows(ledger):
    return [json.loads(ln) for ln in ledger.read_text().splitlines() if ln.strip()]


def utc_day(offset_days=0):
    return (datetime.now(timezone.utc) + timedelta(days=offset_days)).strftime("%Y-%m-%d")


def write_raw(ledger, objs):
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as f:
        for o in objs:
            f.write((o if isinstance(o, str) else json.dumps(o)) + "\n")


# --- round trip -----------------------------------------------------------------


def test_append_then_read_round_trip(env, ledger):
    append(env, "OK", pt="120", ct="45", tt="165", cost="0")
    assert ledger.is_file()
    (row,) = rows(ledger)
    assert row["result"] == "OK"
    assert row["day"] == utc_day()
    assert row["model"] == "x/y:free"
    assert row["backend"] == "or-api"
    assert row["mode"] == "A"
    assert row["free"] is True
    assert row["billed"] is True
    assert row["prompt_tokens"] == 120
    assert row["completion_tokens"] == 45
    assert row["total_tokens"] == 165
    assert row["cost"] == 0.0
    assert row["ts"].endswith("Z")
    assert cap_used(env) == "1"


def test_state_dir_is_created_and_private(env, ledger):
    append(env, "OK")
    mode = (ledger.parent.stat().st_mode & 0o777)
    assert mode == 0o700, (
        f"state dir is {oct(mode)}; it holds a plaintext copy of every reviewed repo"
    )


def test_missing_ledger_reads_as_zero(env, ledger):
    assert not ledger.exists()
    assert cap_used(env) == "0"


def test_counts_accumulate(env):
    for _ in range(4):
        append(env, "OK")
    assert cap_used(env) == "4"


def test_non_numeric_token_counts_become_null_not_a_crash(env, ledger):
    append(env, "OK", pt="abc", ct="", tt="12.7", cost="notanumber")
    (row,) = rows(ledger)
    assert row["prompt_tokens"] is None
    assert row["completion_tokens"] is None
    assert row["total_tokens"] == 12
    assert row["cost"] is None


def test_only_todays_rows_count(env, ledger):
    write_raw(ledger, [
        {"day": utc_day(-1), "result": "OK", "billed": True},
        {"day": utc_day(-30), "result": "OK", "billed": True},
        {"day": utc_day(), "result": "OK", "billed": True},
    ])
    assert cap_used(env) == "1", "the cap is per UTC day; yesterday must not count"


# --- the billed decision --------------------------------------------------------


@pytest.mark.parametrize("result", NEVER_BILLED)
def test_unbilled_rows_do_not_consume_the_cap(env, result):
    """These outcomes never reached the model. Counting them locks the user out early."""
    append(env, result, billed="false")
    assert cap_used(env) == "0", f"{result} with billed=false must not consume the cap"


@pytest.mark.parametrize("result", ALWAYS_BILLED)
def test_billed_rows_consume_the_cap(env, result):
    """TIMEOUT and INTERRUPTED especially: the request ran, we just never saw the answer."""
    append(env, result, billed="true")
    assert cap_used(env) == "1", f"{result} with billed=true must consume the cap"


def test_billed_flag_overrides_the_result_word(env):
    """`billed` is the authority, not the outcome name.

    AUTH after the model was invoked (a mid-stream 401) is still a spent request, and an
    OK that was served from a local cache is not. The caller decides; the reader obeys.
    """
    append(env, "AUTH", billed="true")
    assert cap_used(env) == "1"
    append(env, "OK", billed="false")
    assert cap_used(env) == "1"


@pytest.mark.parametrize("spelling", ["true", "TRUE", "True", "1", "yes", "YES"])
def test_truthy_spellings_of_billed(env, ledger, spelling):
    append(env, "OK", billed=spelling)
    assert rows(ledger)[-1]["billed"] is True
    assert cap_used(env) == "1"


@pytest.mark.parametrize("spelling", ["false", "FALSE", "0", "no", "off"])
def test_falsy_spellings_of_billed(env, ledger, spelling):
    append(env, "OK", billed=spelling)
    assert rows(ledger)[-1]["billed"] is False
    assert cap_used(env) == "0"


def test_omitted_billed_argument_defaults_to_billed(env, ledger):
    """Bias to over-counting: an omitted flag must not silently make a call free."""
    p = sh("bcoc_usage_append OK m or-api A", env)
    assert p.returncode == 0, p.stderr
    assert rows(ledger)[-1]["billed"] is True
    assert cap_used(env) == "1"


def test_empty_billed_argument_defaults_to_billed(env, ledger):
    append(env, "OK", billed="")
    assert rows(ledger)[-1]["billed"] is True


# --- back-compat: rows written before `billed` existed --------------------------


@pytest.mark.parametrize("result", NEVER_BILLED)
def test_legacy_row_without_billed_falls_back_to_the_result_word(env, ledger, result):
    write_raw(ledger, [{"day": utc_day(), "result": result}])
    assert cap_used(env) == "0", (
        f"a legacy {result} row has no `billed` field and must fall back to the "
        "outcome name, not default to counted"
    )


@pytest.mark.parametrize("result", ALWAYS_BILLED)
def test_legacy_billed_row_without_billed_still_counts(env, ledger, result):
    write_raw(ledger, [{"day": utc_day(), "result": result}])
    assert cap_used(env) == "1"


def test_legacy_row_with_unknown_result_counts(env, ledger):
    """Unknown outcome + no flag: assume it was spent. Over-counting only delays."""
    write_raw(ledger, [{"day": utc_day(), "result": "SOMETHING_NEW"}])
    assert cap_used(env) == "1"


def test_legacy_and_current_rows_mix(env, ledger):
    write_raw(ledger, [
        {"day": utc_day(), "result": "OK"},                    # legacy, counts
        {"day": utc_day(), "result": "AUTH"},                  # legacy, does not
        {"day": utc_day(), "result": "OK", "billed": False},   # explicit, does not
        {"day": utc_day(), "result": "AUTH", "billed": True},  # explicit, counts
    ])
    assert cap_used(env) == "2"


# --- damaged ledgers ------------------------------------------------------------


def test_malformed_lines_are_skipped_not_fatal(env, ledger):
    write_raw(ledger, [
        "this is not json",
        '{"day": "unterminated',
        "",
        "   ",
        "\x00\x01 binary",
        '{"day": "%s", "result": "OK", "billed": true} trailing junk' % utc_day(),
        {"day": utc_day(), "result": "OK", "billed": True},
    ])
    assert cap_used(env) == "1", "one good row must still be counted past the garbage"


@pytest.mark.parametrize("scalar", ["null", "[]", "12345", '"a string"', "true"])
def test_valid_json_that_is_not_an_object_must_not_crash_the_reader(env, ledger, scalar):
    """A line that is VALID json but not an object once disabled the cap entirely.

    `null` / `[]` / `12345` survive the `except Exception: continue` guard (json.loads
    succeeds) and then die on `o.get(...)` with AttributeError. python3 exits 1, the
    function prints nothing, `USED="$(bcoc_cap_used | …)"` becomes the empty string, and
    `${USED:-0}` reads as 0 — silently removing the daily cap. Exactly the failure the
    UNAVAILABLE sentinel exists to prevent, reached by a different door.
    """
    write_raw(ledger, [scalar, {"day": utc_day(), "result": "OK", "billed": True}])
    assert cap_used(env) == "1"


def test_a_ledger_that_is_entirely_garbage_reads_as_zero(env, ledger):
    write_raw(ledger, ["garbage", "{{{", "\x00\x01"])
    assert cap_used(env) == "0"


def test_unreadable_ledger_reports_UNAVAILABLE_not_zero(env, ledger):
    """The regression that made the cap infinite.

    The old reader had `|| echo 0` on the python call, so an unreadable ledger looked
    exactly like an empty one and every subsequent run was allowed through.
    """
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.mkdir()              # a directory where a file belongs -> IsADirectoryError
    out = cap_used(env)
    assert out == "UNAVAILABLE", f"expected UNAVAILABLE, got {out!r}"
    assert out != "0"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_permission_denied_ledger_reports_UNAVAILABLE(env, ledger):
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"day": utc_day(), "result": "OK", "billed": True}) + "\n")
    ledger.chmod(0o000)
    try:
        p = sh("bcoc_cap_used", env)
        assert p.returncode == 0, "must not abort the caller"
        assert p.stdout.strip() == "UNAVAILABLE"
        assert "cannot read ledger" in p.stderr
    finally:
        ledger.chmod(0o600)


def test_append_failure_is_reported_but_does_not_abort(env, ledger):
    """A row that cannot be written is bad; dying before printing RESULT= is worse."""
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.mkdir()
    p = sh("bcoc_usage_append OK m or-api A true true; echo EXIT=$?; echo STILL_ALIVE", env)
    assert "STILL_ALIVE" in p.stdout, "the caller must survive a ledger write failure"
    assert "EXIT=1" in p.stdout, "the failure must be visible to the caller"
    assert "could not record" in p.stderr


# --- concurrency ----------------------------------------------------------------


def test_hundred_concurrent_appends_produce_hundred_intact_rows(env, ledger):
    """Mode C fires a whole panel at once; interleaved writes must not shred the ledger."""
    n = 100
    p = sh(
        "for i in $(seq %d); do "
        "  bcoc_usage_append OK \"m$i\" or-api C true true \"$i\" \"$i\" \"$i\" 0 & "
        "done; wait" % n,
        env, timeout=300,
    )
    assert p.returncode == 0, p.stderr
    lines = [ln for ln in ledger.read_text().splitlines() if ln.strip()]
    assert len(lines) == n, f"{len(lines)} lines survived out of {n}"
    parsed = [json.loads(ln) for ln in lines]        # raises if any line was interleaved
    assert {r["result"] for r in parsed} == {"OK"}
    assert len({r["model"] for r in parsed}) == n, "a row was lost or overwritten"
    assert cap_used(env) == str(n)


def test_concurrent_appends_are_all_counted_by_the_reader(env):
    p = sh(
        "for i in $(seq 25); do bcoc_usage_append OK m or-api C true true & done; wait",
        env, timeout=180,
    )
    assert p.returncode == 0, p.stderr
    assert cap_used(env) == "25"


# --- the cap gate this feeds ----------------------------------------------------


def test_reader_output_is_a_bare_token_callers_can_test(env, ledger):
    """Callers compare the output directly; stray whitespace or banners would break them."""
    append(env, "OK")
    p = sh("bcoc_cap_used", env)
    assert p.stdout == "1\n"
    ledger.unlink()
    assert sh("bcoc_cap_used", env).stdout == "0\n"


def test_reader_does_not_mutate_the_ledger(env, ledger):
    append(env, "OK")
    before = ledger.read_bytes()
    mtime = ledger.stat().st_mtime
    time.sleep(0.01)
    cap_used(env)
    assert ledger.read_bytes() == before
    assert ledger.stat().st_mtime == mtime
