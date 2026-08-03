"""The reasoning budget control, and which one OpenRouter actually honours.

Reasoning models count *thinking* against max_tokens. The client used to send
`reasoning: {max_tokens: N}`; measured against the real 40k-token packed review prompt
at max_tokens=16000, that is the wrong control:

    model                    none                    low
    ling-3.0-flash           OK, 6412 chars          TRUNCATED, 0 usable findings
    nemotron-3-super-120b    OK, 5 findings          TRUNCATED, 0 findings
    nemotron-3-ultra-550b    OK, 6 findings          OK, 10 findings

ling ignores `reasoning.max_tokens` outright (completion=16000, reasoning=14480, and only
1861 characters of review). `effort` it does honour — but even `low` let two of the three
burn the entire budget thinking and emit no answer at all, so the client fell back to
scraping their reasoning stream: tens of thousands of characters, zero findings.

Hence the default is `none`. These tests pin the request body, which is the part that
regresses silently — a wrong value here costs a real request to discover.

Fully offline: `http_json` is replaced, nothing reaches the network.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _fresh_client():
    """A fresh module per test: the reasoning config is read at call time from os.environ,
    but importing once and mutating env is enough — reload keeps tests independent."""
    spec = importlib.util.spec_from_file_location("or_client_rb", ROOT / "scripts" / "or_client.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["or_client_rb"] = mod
    spec.loader.exec_module(mod)
    return mod


def sent_body(monkeypatch, **env):
    """Return the request body the client would POST, without sending it."""
    for k in ("BCOPENCODE_REASONING_EFFORT", "BCOPENCODE_REASONING_MAX_TOKENS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    oc = _fresh_client()
    captured = {}

    def fake_http_json(method, url, headers, body, timeout):
        captured.update(body)
        raise RuntimeError("stop-before-network")

    monkeypatch.setattr(oc, "http_json", fake_http_json)
    try:
        oc.chat("key", "vendor/model:free", [{"role": "user", "content": "hi"}], 16000, 0.2, 10)
    except RuntimeError:
        pass
    return captured


def test_default_disables_reasoning(monkeypatch):
    """The default must be `enabled: false`.

    Two of three tested free models burn the whole completion budget thinking and never
    emit an answer otherwise. A shallower review that exists beats a deeper one that
    does not.
    """
    assert sent_body(monkeypatch).get("reasoning") == {"enabled": False}


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_effort_levels_are_sent_as_effort(monkeypatch, effort):
    body = sent_body(monkeypatch, BCOPENCODE_REASONING_EFFORT=effort)
    assert body.get("reasoning") == {"effort": effort}


def test_off_sends_no_reasoning_field_at_all(monkeypatch):
    """`off` means "provider defaults" — the field must be absent, not false."""
    assert "reasoning" not in sent_body(monkeypatch, BCOPENCODE_REASONING_EFFORT="off")


def test_legacy_max_tokens_still_honoured(monkeypatch):
    """Existing configs set BCOPENCODE_REASONING_MAX_TOKENS; it must keep working."""
    body = sent_body(monkeypatch, BCOPENCODE_REASONING_MAX_TOKENS="1024")
    assert body.get("reasoning") == {"max_tokens": 1024}


def test_legacy_zero_still_means_provider_defaults(monkeypatch):
    assert "reasoning" not in sent_body(monkeypatch, BCOPENCODE_REASONING_MAX_TOKENS="0")


def test_legacy_max_tokens_wins_over_effort(monkeypatch):
    """OpenRouter rejects both: "Only one of reasoning.effort and reasoning.max_tokens
    can be specified." An explicit legacy setting is an explicit choice, so it wins —
    but the two must never be sent together."""
    body = sent_body(
        monkeypatch,
        BCOPENCODE_REASONING_MAX_TOKENS="512",
        BCOPENCODE_REASONING_EFFORT="high",
    )
    r = body.get("reasoning")
    assert "effort" not in r and "max_tokens" in r


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"BCOPENCODE_REASONING_EFFORT": "low"},
        {"BCOPENCODE_REASONING_EFFORT": "none"},
        {"BCOPENCODE_REASONING_MAX_TOKENS": "2048"},
        {"BCOPENCODE_REASONING_MAX_TOKENS": "2048", "BCOPENCODE_REASONING_EFFORT": "low"},
    ],
)
def test_effort_and_max_tokens_are_never_both_sent(monkeypatch, env):
    """The API returns HTTP 400 if both appear. That is a whole wasted round-trip."""
    r = sent_body(monkeypatch, **env).get("reasoning") or {}
    assert not ("effort" in r and "max_tokens" in r), r


def test_garbage_legacy_value_falls_back_rather_than_crashing(monkeypatch):
    body = sent_body(monkeypatch, BCOPENCODE_REASONING_MAX_TOKENS="not-a-number")
    assert body.get("reasoning") == {"max_tokens": 2048}


# --- finish_reason classification -------------------------------------------------


def _classify(monkeypatch, finish, content, usage=None):
    """Drive the real parse path with a synthetic provider response."""
    oc = _fresh_client()
    parsed = {
        "choices": [{"finish_reason": finish, "message": {"content": content}}],
        "usage": usage or {},
        "model": "vendor/model:free",
    }
    monkeypatch.setattr(oc, "http_json", lambda *a, **k: (200, "", parsed))
    return oc.chat("key", "vendor/model:free", [{"role": "user", "content": "x"}], 16000, 0.2, 10)


@pytest.mark.parametrize("finish", ["error", "content_filter"])
def test_provider_signalled_failure_is_never_reported_as_success(monkeypatch, finish):
    """Only `length`/`max_tokens` used to be special-cased.

    Every other finish_reason fell through to OK as long as ANY content existed — so a
    response that explicitly said the generation failed was reported as a clean review,
    and Claude would triage a failed generation as complete. Observed live:
    cohere/north-mini-code returns finish_reason='error' with ~2k chars of partial
    output on a large prompt, and the run reported RESULT=OK.
    """
    result, env = _classify(monkeypatch, finish, "some partial text")
    assert result == "ERROR", f"finish_reason={finish!r} must not be OK"
    assert finish in env.get("error", ""), "the message must name the actual cause"


@pytest.mark.parametrize("finish", ["error", "content_filter"])
def test_partial_output_is_preserved_not_discarded(monkeypatch, finish):
    """The partial text is evidence. Losing it would make the failure harder to diagnose
    than it needs to be — the report should show what little came back."""
    _result, env = _classify(monkeypatch, finish, "half a finding")
    assert "half a finding" in (env.get("content") or "")


def test_error_result_still_counts_as_billed():
    """These stay ERROR rather than becoming REFUSED on purpose.

    In this skill REFUSED means a gate refused BEFORE spending anything and writes
    billed=false. A content_filter or provider abort WAS requested and billed; mapping it
    to REFUSED would silently under-count the cap.
    """
    src = (ROOT / "scripts" / "opencode_review.sh").read_text()
    block = src[src.index('BILLED="true"'):][:400]
    assert "ERROR" not in block.split("case")[1].split("esac")[0], (
        "ERROR must not be in the not-billed set — the request was made"
    )


@pytest.mark.parametrize("finish", ["stop", None])
def test_normal_completions_are_still_ok(monkeypatch, finish):
    result, _env = _classify(monkeypatch, finish, "real findings")
    assert result == "OK"


def test_length_is_still_truncated_not_error(monkeypatch):
    result, _env = _classify(monkeypatch, "length", "partial findings")
    assert result == "TRUNCATED"


# --- endpoints that REQUIRE reasoning ---------------------------------------------


def test_mandatory_reasoning_endpoint_falls_back_to_effort(monkeypatch):
    """Some endpoints reject `reasoning: {enabled: false}` outright.

    openai/gpt-oss-20b returns HTTP 400 "Reasoning is mandatory for this endpoint and
    cannot be disabled." Since `none` is the default, those models became a hard ERROR —
    a regression introduced by the very fix that stopped the others truncating, and one
    that only surfaced once report diagnostics started naming the cause.

    The 400 carries no `usage` and is not billed, so the retry costs one request in
    total, not two.
    """
    oc = _fresh_client()
    monkeypatch.delenv("BCOPENCODE_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("BCOPENCODE_REASONING_MAX_TOKENS", raising=False)
    sent = []

    def fake_http_json(method, url, headers, body, timeout):
        sent.append(dict(body.get("reasoning") or {}))
        if len(sent) == 1:
            return 400, '{"error":{"message":"Reasoning is mandatory for this endpoint and cannot be disabled."}}', \
                   {"error": {"message": "Reasoning is mandatory for this endpoint and cannot be disabled."}}
        return 200, "", {
            "choices": [{"finish_reason": "stop", "message": {"content": "findings"}}],
            "usage": {"completion_tokens": 10}, "model": "openai/gpt-oss-20b:free",
        }

    monkeypatch.setattr(oc, "http_json", fake_http_json)
    result, env = oc.chat("k", "openai/gpt-oss-20b:free",
                          [{"role": "user", "content": "x"}], 16000, 0.2, 10)
    assert sent[0] == {"enabled": False}, "first attempt should use the default"
    assert sent[1] == {"effort": "low"}, "retry must enable reasoning, not repeat the same body"
    assert len(sent) == 2, "exactly one retry"
    assert result == "OK"
    assert env.get("reasoning_forced") is True, "the report must be able to say the budget changed"


def test_other_400s_are_not_retried(monkeypatch):
    """The fallback must be narrow. A generic 400 is a real error, not a reasoning issue."""
    oc = _fresh_client()
    monkeypatch.delenv("BCOPENCODE_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("BCOPENCODE_REASONING_MAX_TOKENS", raising=False)
    calls = []

    def fake_http_json(method, url, headers, body, timeout):
        calls.append(1)
        return 400, '{"error":{"message":"context length exceeded"}}', \
               {"error": {"message": "context length exceeded"}}

    monkeypatch.setattr(oc, "http_json", fake_http_json)
    oc.chat("k", "vendor/m:free", [{"role": "user", "content": "x"}], 16000, 0.2, 10)
    assert len(calls) == 1, "a non-reasoning 400 must not trigger a retry"


def test_retry_does_not_fire_when_reasoning_was_not_disabled(monkeypatch):
    """If the user asked for effort=low, a mandatory-reasoning 400 is not our doing."""
    oc = _fresh_client()
    monkeypatch.setenv("BCOPENCODE_REASONING_EFFORT", "low")
    calls = []

    def fake_http_json(method, url, headers, body, timeout):
        calls.append(dict(body.get("reasoning") or {}))
        return 400, '{"error":{"message":"Reasoning is mandatory for this endpoint."}}', \
               {"error": {"message": "Reasoning is mandatory for this endpoint."}}

    monkeypatch.setattr(oc, "http_json", fake_http_json)
    oc.chat("k", "vendor/m:free", [{"role": "user", "content": "x"}], 16000, 0.2, 10)
    assert len(calls) == 1
