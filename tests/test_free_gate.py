#!/usr/bin/env python3
"""Offline tests for free-model gate helpers."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("or_client", ROOT / "scripts" / "or_client.py")
or_client = importlib.util.module_from_spec(spec)
sys.modules["or_client"] = or_client
spec.loader.exec_module(or_client)


def test_free_ids():
    assert or_client.is_free_model("nvidia/nemotron-3-ultra-550b-a55b:free")
    assert or_client.is_free_model("openrouter/nvidia/nemotron-3-ultra-550b-a55b:free")
    assert or_client.is_free_model("openrouter/free")
    assert or_client.is_free_model("openrouter/openrouter/free")
    assert or_client.is_free_model("free")
    assert not or_client.is_free_model("openrouter/openai/gpt-4o")
    assert not or_client.is_free_model("anthropic/claude-sonnet-4.5")


def test_normalize():
    oc, mid = or_client.normalize_model("openrouter/nvidia/x:free")
    assert oc == "openrouter/nvidia/x:free"
    assert mid == "nvidia/x:free"
    oc2, mid2 = or_client.normalize_model("nvidia/x:free")
    assert oc2 == "openrouter/nvidia/x:free"
    assert mid2 == "nvidia/x:free"
    # Free router must keep API id openrouter/free (not bare "free")
    oc3, mid3 = or_client.normalize_model("openrouter/free")
    assert mid3 == "openrouter/free"
    assert oc3 == "openrouter/openrouter/free"
    oc4, mid4 = or_client.normalize_model("free")
    assert mid4 == "openrouter/free"


if __name__ == "__main__":
    test_free_ids()
    test_normalize()
    print("OK")
