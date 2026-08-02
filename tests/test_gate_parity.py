"""Parity between the SHELL and PYTHON implementations of the free-model gate.

There are two implementations of one rule — `bcoc_normalize_model`/`bcoc_is_free_model`
in scripts/_bcoc_common.sh, and `normalize_model`/`is_free_model` in scripts/or_client.py.
They drifted: the shell version stripped `openrouter/` in a while-loop, collapsing
`openrouter/openrouter/free` to `openrouter/free`, so the opencode backend was sent
provider=openrouter model=free and the free router was broken — in a model that ships in
the `fast-panel` preset.

This file exists to make that drift impossible to reintroduce silently. Every id is driven
through BOTH implementations and they must agree. The parity assertion is the load-bearing
one; the expected-value assertions merely pin what "correct" is.

Fully offline.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMMON_SH = ROOT / "scripts" / "_bcoc_common.sh"

spec = importlib.util.spec_from_file_location("or_client", ROOT / "scripts" / "or_client.py")
or_client = importlib.util.module_from_spec(spec)
sys.modules["or_client"] = or_client
spec.loader.exec_module(or_client)


def sh_gate(model):
    """(opencode_form, is_free) as the SHELL implementation sees it."""
    script = (
        f'source "{COMMON_SH}" 2>/dev/null\n'
        f'printf "%s\\n" "$(bcoc_normalize_model "$1")"\n'
        f'if bcoc_is_free_model "$1"; then echo FREE; else echo PAID; fi\n'
    )
    p = subprocess.run(
        ["bash", "-c", script, "bash", model],
        capture_output=True, text=True, timeout=30,
    )
    assert p.returncode == 0, f"shell gate failed for {model!r}: {p.stderr}"
    lines = p.stdout.strip().split("\n")
    return lines[0], lines[-1] == "FREE"


# (id, expected opencode form, expected free?)
CASES = [
    # the free router, in every spelling — this is where the drift lived
    ("free", "openrouter/openrouter/free", True),
    ("openrouter/free", "openrouter/openrouter/free", True),
    ("openrouter/openrouter/free", "openrouter/openrouter/free", True),
    # ordinary free models, bare and prefixed
    ("nvidia/nemotron-3-ultra-550b-a55b:free", "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free", True),
    ("openrouter/nvidia/nemotron-3-ultra-550b-a55b:free", "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free", True),
    ("openai/gpt-oss-20b:free", "openrouter/openai/gpt-oss-20b:free", True),
    ("google/gemma-4-26b-a4b-it:free", "openrouter/google/gemma-4-26b-a4b-it:free", True),
    ("poolside/laguna-xs-2.1:free", "openrouter/poolside/laguna-xs-2.1:free", True),
    ("cohere/north-mini-code:free", "openrouter/cohere/north-mini-code:free", True),
    # paid — must never pass the gate
    ("openai/gpt-4o", "openrouter/openai/gpt-4o", False),
    ("openrouter/openai/gpt-4o", "openrouter/openai/gpt-4o", False),
    ("anthropic/claude-sonnet-4.5", "openrouter/anthropic/claude-sonnet-4.5", False),
    ("nvidia/nemotron-3-ultra-550b-a55b", "openrouter/nvidia/nemotron-3-ultra-550b-a55b", False),
    # opencode's own zero-cost tier bills an OpenCode Zen account, not OpenRouter.
    # It is deliberately NOT free to this gate: the rule must be decidable from the id
    # alone, offline, in both implementations, and `opencode/big-pickle` is not
    # self-describing. Recognising it would mean a hardcoded allowlist that goes stale.
    ("opencode/big-pickle", "openrouter/opencode/big-pickle", False),
    ("opencode/nemotron-3-ultra-free", "openrouter/opencode/nemotron-3-ultra-free", False),
    # near-misses that must not be mistaken for free
    ("vendor/free-tier-model", "openrouter/vendor/free-tier-model", False),
    ("vendor/model:freebie", "openrouter/vendor/model:freebie", False),
    ("vendor/freemodel", "openrouter/vendor/freemodel", False),
]


@pytest.mark.parametrize("model,expected_oc,expected_free", CASES)
def test_shell_and_python_agree(model, expected_oc, expected_free):
    sh_oc, sh_free = sh_gate(model)
    py_oc, _py_id = or_client.normalize_model(model)
    py_free = or_client.is_free_model(model)

    # The load-bearing assertion: the two implementations must not diverge.
    assert sh_oc == py_oc, f"normalize drift on {model!r}: shell={sh_oc!r} python={py_oc!r}"
    assert sh_free == py_free, f"free-gate drift on {model!r}: shell={sh_free} python={py_free}"

    # And both must be right.
    assert sh_oc == expected_oc, f"{model!r} -> {sh_oc!r}, expected {expected_oc!r}"
    assert sh_free is expected_free, f"{model!r} free={sh_free}, expected {expected_free}"


def test_free_router_never_collapses_to_bare_free():
    """The API rejects `model: "free"` with 502 Invalid URL.

    The opencode backend is passed the normalized form directly as `-m`, so a collapse
    here breaks a shipped preset rather than just an edge case.
    """
    for spelling in ("free", "openrouter/free", "openrouter/openrouter/free"):
        sh_oc, _ = sh_gate(spelling)
        assert sh_oc == "openrouter/openrouter/free"
        assert sh_oc != "openrouter/free", "collapsed the free router — fast-panel is broken"
        _oc, or_id = or_client.normalize_model(spelling)
        assert or_id == "openrouter/free", "the OpenRouter-side id must stay openrouter/free"
