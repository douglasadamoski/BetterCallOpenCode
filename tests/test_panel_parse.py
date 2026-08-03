"""`panel_run.parse_report` is the only thing that turns a Mode C run into a summary row.

It reads a file the wrapper wrote and that a model's output was spliced into, so its
input is effectively untrusted: a truncated report, a CRLF report, an empty file, a
report from the opencode backend (which emits no token line at all). It runs inside
`run_one`, after the request has already been paid for — an exception there loses the
result of a spent request and takes the rest of the panel down with it.

So the contract asserted here is: always a dict with every key, never an exception.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

_spec = importlib.util.spec_from_file_location("panel_run", SCRIPTS / "panel_run.py")
panel_run = importlib.util.module_from_spec(_spec)
sys.modules["panel_run"] = panel_run
_spec.loader.exec_module(panel_run)

parse_report = panel_run.parse_report

KEYS = {
    "result", "finish_reason", "prompt_tokens", "completion_tokens",
    "reasoning_tokens", "total_tokens", "cost",
}

OR_API_REPORT = """# BetterCallOpenCode review

- **When:** 20260803T101500Z
- **Mode:** A
- **Backend:** or-api (OpenRouter direct)
- **Model:** `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free`
- **Free model:** true
- **Scope:** `/tmp/x`
- **Pack:** 12 files, 40000 tokens
- **RESULT:** OK
- **Billed:** true
- **finish_reason:** stop
- **max_tokens requested:** 16384
- **Tokens:** prompt=40000 completion=1200 reasoning=300 total=41200 cost=0
- **Reasoning fallback:** no
- **Usage ledger:** `/home/u/.bettercallopencode/usage.jsonl`

## Findings

Nothing alarming.
"""

# The opencode backend emits no token line and no finish_reason at all.
OPENCODE_REPORT = """# BetterCallOpenCode review

- **When:** 20260803T101500Z
- **Mode:** A
- **Backend:** opencode (agentic)
- **Model:** `openrouter/openai/gpt-oss-20b:free`
- **RESULT:** TRUNCATED
- **Billed:** true
- **opencode exit:** 0
"""


def write(tmp_path, text, name="report.md", newline=None):
    p = tmp_path / name
    # Not Path.write_text(newline=...): that keyword only exists from 3.10 and CI runs 3.9.
    if newline is not None:
        text = text.replace("\n", newline)
    p.write_bytes(text.encode("utf-8"))
    return p


def assert_shape(out):
    assert isinstance(out, dict)
    assert set(out) == KEYS, "run_one reads every key unconditionally"
    assert all(isinstance(v, str) for v in out.values())


# --- the happy paths --------------------------------------------------------------


def test_well_formed_or_api_report(tmp_path):
    out = parse_report(write(tmp_path, OR_API_REPORT))
    assert_shape(out)
    assert out["result"] == "OK"
    assert out["finish_reason"] == "stop"
    assert out["prompt_tokens"] == "40000"
    assert out["completion_tokens"] == "1200"
    assert out["reasoning_tokens"] == "300"
    assert out["total_tokens"] == "41200"
    assert out["cost"] == "0"


def test_report_with_no_token_line(tmp_path):
    """The opencode backend never writes one; the row must still carry the result."""
    out = parse_report(write(tmp_path, OPENCODE_REPORT))
    assert_shape(out)
    assert out["result"] == "TRUNCATED"
    assert out["finish_reason"] == ""
    assert out["prompt_tokens"] == ""
    assert out["total_tokens"] == ""
    assert out["cost"] == ""


def test_bare_result_line_is_the_fallback(tmp_path):
    """Everything the wrapper can still emit after a crash is the RESULT= contract line."""
    out = parse_report(write(tmp_path, "some stderr noise\nRESULT=TIMEOUT\n"))
    assert out["result"] == "TIMEOUT"
    assert out["finish_reason"] == ""


def test_the_report_header_beats_a_trailing_result_line(tmp_path):
    out = parse_report(write(tmp_path, OR_API_REPORT + "\nRESULT=ERROR\n"))
    assert out["result"] == "OK", "the structured field is authoritative over the tail"


def test_result_line_only_file_with_no_trailing_newline(tmp_path):
    assert parse_report(write(tmp_path, "RESULT=OK"))["result"] == "OK"


def test_token_placeholders_are_returned_verbatim(tmp_path):
    text = "- **RESULT:** ERROR\n- **Tokens:** prompt=? completion=? reasoning=? total=? cost=0\n"
    out = parse_report(write(tmp_path, text))
    assert out["prompt_tokens"] == "?"
    assert out["cost"] == "0"


def test_first_result_wins_when_repeated(tmp_path):
    text = "- **RESULT:** OK\n- **RESULT:** ERROR\n"
    assert parse_report(write(tmp_path, text))["result"] == "OK"


# --- shapes that must degrade, not explode ----------------------------------------


def test_crlf_report(tmp_path):
    """A report written on Windows, or one round-tripped through a CRLF-normalising tool."""
    out = parse_report(write(tmp_path, OR_API_REPORT, newline="\r\n"))
    assert out["result"] == "OK", "CRLF must not stop the RESULT field being found"
    assert out["finish_reason"] == "stop"
    assert out["total_tokens"] == "41200"
    for k, v in out.items():
        assert "\r" not in v, f"{k} carried a stray CR into summary.csv"


def test_cr_only_line_endings(tmp_path):
    out = parse_report(write(tmp_path, OR_API_REPORT.replace("\n", "\r")))
    assert_shape(out)          # classic-Mac line endings: no crash is the requirement


def test_empty_file(tmp_path):
    out = parse_report(write(tmp_path, ""))
    assert_shape(out)
    assert out["result"] == "?"
    assert out["cost"] == ""


def test_whitespace_only_file(tmp_path):
    assert parse_report(write(tmp_path, "\n\n   \n\t\n"))["result"] == "?"


def test_malformed_report(tmp_path):
    text = "- **RESULT:**\n- **Tokens:** prompt= completion=\n- **finish_reason:**\n"
    out = parse_report(write(tmp_path, text))
    assert_shape(out)
    assert out["result"] == "?", "an empty RESULT field must not read as a result"
    assert out["prompt_tokens"] == ""


def test_truncated_report_mid_line(tmp_path):
    """The wrapper was killed while writing: the file just stops."""
    out = parse_report(write(tmp_path, OR_API_REPORT[: len(OR_API_REPORT) // 3]))
    assert_shape(out)
    assert out["result"] in ("?", "OK")


def test_indented_fields_are_not_matched(tmp_path):
    """Pinning the anchor: only a line-initial field is trusted, so model prose quoting
    `- **RESULT:** OK` inside an indented block cannot forge the run's outcome."""
    out = parse_report(write(tmp_path, "    - **RESULT:** OK\n"))
    assert out["result"] == "?"


def test_invalid_utf8_does_not_raise(tmp_path):
    p = tmp_path / "bin.md"
    p.write_bytes(b"- **RESULT:** OK\n\xff\xfe\x00binary garbage\n")
    out = parse_report(p)
    assert_shape(out)
    assert out["result"] == "OK"


def test_nul_bytes(tmp_path):
    p = tmp_path / "nul.md"
    p.write_bytes(b"\x00\x00\x00- **RESULT:** OK\n")
    assert_shape(parse_report(p))


# --- paths that are not a readable report -----------------------------------------


def test_missing_path(tmp_path):
    out = parse_report(tmp_path / "never_written.md")
    assert_shape(out)
    assert out["result"] == "?"


def test_path_is_a_directory(tmp_path):
    d = tmp_path / "a_directory.md"
    d.mkdir()
    out = parse_report(d)
    assert_shape(out)
    assert out["result"] == "?"


def test_path_is_a_dangling_symlink(tmp_path):
    link = tmp_path / "link.md"
    link.symlink_to(tmp_path / "gone.md")
    assert_shape(parse_report(link))


def test_symlink_to_a_real_report_is_followed(tmp_path):
    real = write(tmp_path, OR_API_REPORT, name="real.md")
    link = tmp_path / "link.md"
    link.symlink_to(real)
    assert parse_report(link)["result"] == "OK"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_unreadable_report_does_not_raise(tmp_path):
    p = write(tmp_path, OR_API_REPORT)
    p.chmod(0o000)
    try:
        out = parse_report(p)
        assert_shape(out)
        assert out["result"] == "?"
    finally:
        p.chmod(0o600)


# --- blanket "never raises" -------------------------------------------------------


@pytest.mark.parametrize("payload", [
    "",
    "\n",
    "RESULT=",
    "- **RESULT:** ",
    "- **Tokens:** ",
    "- **Tokens:** prompt=1",
    "- **Tokens:** prompt=1 completion=2 reasoning=3 total=4",
    "-**RESULT:**OK",
    "- **RESULT:**\tOK",
    "RESULT=OK RESULT=ERROR",
    "# " + "x" * 100000,
    "\x00" * 500,
    "- **RESULT:** " + "y" * 5000,
    "```\n- **RESULT:** OK\n```",
    "﻿- **RESULT:** OK",
])
def test_never_raises(tmp_path, payload):
    out = parse_report(write(tmp_path, payload))
    assert_shape(out)
