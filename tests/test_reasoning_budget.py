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
