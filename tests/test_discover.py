"""discover_providers.py — provider-agnostic discovery and the capability cache.

Fully offline: the opencode CLI and every HTTP source are replaced with fixtures.
"""
import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import discover_providers as dp  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
SECRET = "sk-or-" + "v1-" + "0123456789abcdef" * 2  # assembled: a literal here would trip the packer's own filter


def fx(name):
    return (FIX / name).read_text()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BCOPENCODE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
    monkeypatch.delenv("BCOPENCODE_CAPS_TTL", raising=False)
    calls = {"http": []}

    def fake_http(url, headers=None, timeout=0):
        calls["http"].append((url, dict(headers or {})))
        if url.endswith("/openrouter.ai/api/v1/models") or "openrouter.ai" in url:
            return json.loads(fx("openrouter_models.json"))
        if "models.dev" in url:
            return json.loads(fx("models_dev.json"))
        raise OSError("no route")

    monkeypatch.setattr(dp, "_http_get_json", fake_http)
    monkeypatch.setattr(dp, "opencode_provider_config", lambda: {
        "provider-a": {"baseURL": "https://example-gateway.invalid/v1", "env": ["PROVIDER_A_API_KEY"]}})
    snap = {"version": "2.0.22", "models_raw": fx("opencode_v2_models.txt"),
            "auth_raw": fx("opencode_v2_auth_list.txt"),
            "models": dp.parse_models(fx("opencode_v2_models.txt")),
            "auth": dp.parse_auth_list(fx("opencode_v2_auth_list.txt")),
            "config": dp.opencode_provider_config()}
    return {"calls": calls, "snap": snap, "state": tmp_path / "state"}


def test_parse_models_groups_by_provider_and_keeps_nested_ids():
    m = dp.parse_models(fx("opencode_v2_models.txt"))
    assert m["opencode"] == ["big-pickle", "nemotron-3-ultra-free"]
    assert m["openrouter"] == ["nvidia/nemotron-3-super-120b-a12b:free", "some-vendor/paid-model"]


def test_parse_models_ignores_noise():
    assert dp.parse_models("warning: x\n\n  \nnot a model line\n") == {}
    assert dp.parse_models(None) == {}


def test_parse_auth_list_keeps_only_name_env_source():
    rows = dp.parse_auth_list(fx("opencode_v2_auth_list.txt"))
    assert rows[0] == {"name": "Provider A Gateway", "env": "PROVIDER_A_API_KEY", "source": "environment"}


def test_discovery_learns_openrouter_caps_from_the_provider(env):
    cache, verdict = dp.ensure(snapshot=env["snap"])
    assert verdict == "updated"
    m = cache["providers"]["openrouter"]["models"]["nvidia/nemotron-3-super-120b-a12b:free"]
    assert (m["ctx"], m["tools"], m["reasoning"], m["cost_class"]) == (262144, True, True, "free")
    assert m["source"] if "source" in m else "provider" in m["sources"]
    paid = cache["providers"]["openrouter"]["models"]["some-vendor/paid-model"]
    assert paid["cost_class"] == "paid" and paid["modalities"] == ["text", "image"]


def test_discovery_fills_other_providers_from_the_public_catalog(env):
    cache, _ = dp.ensure(snapshot=env["snap"])
    big = cache["providers"]["opencode"]["models"]["big-pickle"]
    assert big["ctx"] == 200000 and big["cost_class"] == "free" and big["sources"] == ["models.dev"]


def test_unknown_stays_null_and_never_invented(env):
    cache, _ = dp.ensure(snapshot=env["snap"])
    two = cache["providers"]["provider-a"]["models"]["model-two"]
    assert two["ctx"] is None and two["tools"] is None
    assert two["cost_class"] == "unknown" and two["capabilities"] == "unknown"


def test_provider_without_pricing_needs_a_policy_then_remembers_it(env):
    cache, _ = dp.ensure(snapshot=env["snap"])
    assert cache["providers"]["provider-a"]["status"] == "needs_policy"
    assert dp.effective_cost(cache, "provider-a", "model-two") == "unknown"
    assert dp.set_policy("provider-a", "free")
    cache = dp.load_cache()
    assert cache["providers"]["provider-a"]["status"] == "ok"
    assert dp.effective_cost(cache, "provider-a", "model-two") == "free"
    # a policy survives a forced refresh
    cache, _ = dp.ensure(refresh=True, snapshot=env["snap"])
    assert cache["providers"]["provider-a"]["zero_cost_policy"] == "free"


def test_policy_never_overrides_a_published_price(env):
    cache, _ = dp.ensure(snapshot=env["snap"])
    dp.set_policy("openrouter", "free")
    cache = dp.load_cache()
    assert dp.effective_cost(cache, "openrouter", "some-vendor/paid-model") == "paid"


def test_set_policy_rejects_garbage_and_unknown_providers(env):
    dp.ensure(snapshot=env["snap"])
    with pytest.raises(ValueError):
        dp.set_policy("provider-a", "maybe")
    assert dp.set_policy("nope", "free") is False


def test_quick_check_is_fresh_and_makes_no_http(env):
    dp.ensure(snapshot=env["snap"])
    env["calls"]["http"].clear()
    cache, verdict = dp.ensure(snapshot=env["snap"])
    assert verdict == "fresh" and env["calls"]["http"] == []


def test_ttl_expiry_triggers_a_reprobe(env, monkeypatch):
    dp.ensure(snapshot=env["snap"])
    monkeypatch.setenv("BCOPENCODE_CAPS_TTL", "0")
    env["calls"]["http"].clear()
    _, verdict = dp.ensure(snapshot=env["snap"])
    assert verdict == "updated" and env["calls"]["http"]


def test_only_the_changed_provider_is_reprobed(env):
    dp.ensure(snapshot=env["snap"])
    env["calls"]["http"].clear()
    snap = dict(env["snap"])
    snap["models"] = {**snap["models"], "provider-a": ["model-one", "model-two", "model-three"]}
    snap["models_raw"] += "provider-a/model-three\n"
    _, verdict = dp.ensure(snapshot=snap)
    urls = [u for u, _ in env["calls"]["http"]]
    assert verdict == "updated"
    assert any("example-gateway" in u for u in urls)
    assert not any("openrouter.ai" in u for u in urls), "unchanged provider must not be re-probed"


def test_provider_that_disappears_is_dropped(env):
    dp.ensure(snapshot=env["snap"])
    snap = dict(env["snap"])
    snap["models"] = {k: v for k, v in snap["models"].items() if k != "provider-a"}
    snap["models_raw"] = "\n".join(l for l in snap["models_raw"].splitlines() if not l.startswith("provider-a"))
    cache, _ = dp.ensure(snapshot=snap)
    assert "provider-a" not in cache["providers"]


def test_offline_never_touches_the_network(env):
    env["calls"]["http"].clear()
    cache, _ = dp.ensure(offline=True, snapshot=env["snap"])
    assert env["calls"]["http"] == []
    assert cache["providers"]["opencode"]["models"]["big-pickle"]["ctx"] is None


def test_empty_answer_never_overwrites_a_good_cache(env):
    dp.ensure(snapshot=env["snap"])
    empty = {"version": "2.0.22", "models_raw": "", "auth_raw": "", "models": {}, "auth": [], "config": {}}
    cache, verdict = dp.ensure(snapshot=empty)
    assert verdict == "offline-stale" and "opencode" in cache["providers"]


def test_cache_is_0600_and_dir_0700_and_leaves_no_temp(env):
    dp.ensure(snapshot=env["snap"])
    p = dp.cache_path(env["state"])
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(env["state"].stat().st_mode) == 0o700
    assert [f.name for f in env["state"].iterdir()] == ["capabilities.json"]


def test_no_credential_ever_reaches_the_cache_or_the_request_log_shape(env, monkeypatch):
    monkeypatch.setenv("PROVIDER_A_API_KEY", "provider-a-secret-value-123456")
    dp.ensure(snapshot=env["snap"])
    blob = dp.cache_path(env["state"]).read_text()
    assert SECRET not in blob and "provider-a-secret-value-123456" not in blob
    # the key IS used as a bearer for the provider's own /models call, and only there
    sent = [h for u, h in env["calls"]["http"] if "openrouter.ai" in u]
    assert sent and sent[0].get("Authorization") == f"Bearer {SECRET}"
    assert all("Authorization" not in h for u, h in env["calls"]["http"] if "models.dev" in u)


def test_corrupt_cache_is_treated_as_absent(env):
    env["state"].mkdir(parents=True)
    dp.cache_path(env["state"]).write_text("{not json")
    assert dp.load_cache()["providers"] == {}


def test_provider_models_survives_a_bare_openai_style_payload():
    out = dp.from_provider_models({"data": [{"id": "x"}, {"nope": 1}, "junk"]})
    assert list(out) == ["x"] and out["x"]["ctx"] is None and out["x"]["cost_class"] == "unknown"


def test_free_suffix_is_a_cost_signal_only_when_pricing_is_silent():
    out = dp.from_provider_models({"data": [{"id": "a:free"}, {"id": "b:free", "pricing": {"prompt": "1", "completion": "1"}}]})
    assert out["a:free"]["cost_class"] == "free"
    assert out["b:free"]["cost_class"] == "paid"      # a published price beats the name


def test_record_ok_stamps_last_ok_and_survives_refresh(env):
    dp.ensure(snapshot=env["snap"])
    dp.record_ok("opencode", "big-pickle")
    cache, _ = dp.ensure(refresh=True, snapshot=env["snap"])
    assert cache["providers"]["opencode"]["models"]["big-pickle"]["last_ok"]


def test_cli_parsers_run_without_opencode_installed(monkeypatch):
    monkeypatch.setattr(dp.shutil, "which", lambda _n: None)
    assert dp._run(["opencode", "models"]) is None
    assert dp.opencode_snapshot()["models"] == {}


def test_policy_does_not_cover_models_added_later(env):
    dp.ensure(snapshot=env["snap"])
    dp.set_policy("provider-a", "free")
    snap = dict(env["snap"])
    snap["models"] = {**snap["models"], "provider-a": ["model-one", "model-two", "model-new"]}
    snap["models_raw"] += "provider-a/model-new\n"
    cache, _ = dp.ensure(snapshot=snap)
    assert dp.effective_cost(cache, "provider-a", "model-two") == "free"
    assert dp.effective_cost(cache, "provider-a", "model-new") == "unknown"
    assert cache["providers"]["provider-a"]["status"] == "needs_policy"


def test_any_nonzero_price_field_makes_a_model_paid():
    out = dp.from_provider_models({"data": [{"id": "x", "pricing": {"prompt": "0", "completion": "0", "request": "0.01"}},
                                            {"id": "y", "pricing": {"prompt": "0", "completion": "0", "image": "0"}}]})
    assert out["x"]["cost_class"] == "paid" and out["y"]["cost_class"] == "free"


def test_junk_cost_class_reads_as_unknown():
    c = {"providers": {"p": {"models": {"m": {"cost_class": None}}, "zero_cost_policy": "free", "policy_covers": []}}}
    assert dp.effective_cost(c, "p", "m") == "unknown"


@pytest.mark.parametrize("url,ok", [("https://a.example/v1", True), ("http://localhost:8080/v1", True),
                                    ("http://127.0.0.1/v1", True), ("http://evil.example/v1", False),
                                    ("file:///etc/passwd", False), ("https:///nohost", False), ("", False)])
def test_base_urls_must_be_https_or_loopback(url, ok):
    assert dp.safe_base_url(url) is ok


def test_malformed_opencode_config_does_not_crash(monkeypatch):
    monkeypatch.setattr(dp, "_run", lambda argv: json.dumps([{"info": {"providers": ["not", "a", "dict"]}},
                                                              {"info": {"provider": {"p": {"options": "str", "env": "x"}}}}]))
    assert dp.opencode_provider_config() == {"p": {"baseURL": "", "env": []}}


def test_probes_run_from_an_empty_dir_with_project_config_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("BCOPENCODE_STATE_DIR", str(tmp_path))
    seen = {}

    class P:
        returncode, stdout = 0, "ok"

    def fake_run(argv, **kw):
        seen.update(kw)
        return P()

    monkeypatch.setattr(dp.shutil, "which", lambda n: "/x/opencode")
    monkeypatch.setattr(dp.subprocess, "run", fake_run)
    assert dp._run(["opencode", "models"]) == "ok"
    assert seen["env"]["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert seen["cwd"] == str(tmp_path / "probe_cwd") and os.listdir(seen["cwd"]) == []


def test_an_empty_first_answer_is_retried_once_for_warm_up(monkeypatch, tmp_path):
    monkeypatch.setenv("BCOPENCODE_STATE_DIR", str(tmp_path))
    answers = iter(["", "opencode/x\n"])

    class P:
        returncode = 0

        def __init__(self, out):
            self.stdout = out

    monkeypatch.setattr(dp.shutil, "which", lambda n: "/x/opencode")
    monkeypatch.setattr(dp.subprocess, "run", lambda *a, **k: P(next(answers)))
    monkeypatch.setattr(dp.time, "sleep", lambda s: None)
    assert dp._run(["opencode", "models"]).strip() == "opencode/x"


def test_a_ledger_with_invalid_utf8_is_still_readable(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    import ledger
    monkeypatch.setenv("BCOPENCODE_STATE_DIR", str(tmp_path))
    (tmp_path / "usage.jsonl").write_bytes(b"\xff\xfe not json\n")
    assert ledger.cap_used_today() == 0
    ledger.append_row("OK", "a/b", "or-api", "analyst", True)
    assert ledger.cap_used_today() == 1
