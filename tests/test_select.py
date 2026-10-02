"""select_models.py — gating, ranking, diversity, determinism. Offline."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import select_models as sm  # noqa: E402


def M(ctx=100000, tools=True, reasoning=False, cost="free", mods=("text",), caps="known", last_ok=None):
    return {"ctx": ctx, "max_out": None, "tools": tools, "reasoning": reasoning,
            "modalities": list(mods), "cost_class": cost, "capabilities": caps,
            "last_ok": last_ok, "name": None}


def cache(**providers):
    return {"providers": {pid: {"models": models, "zero_cost_policy": pol, "status": "ok",
                                "policy_covers": [m for m, c in models.items() if c["cost_class"] == "unknown"]}
                          for pid, (models, pol) in providers.items()}}


BASE = cache(
    p1=({"alpha-big": M(1_000_000, reasoning=True), "alpha-small-flash": M(32000),
         "beta-coder": M(128000), "paid-one": M(500000, cost="paid"),
         "mystery": M(None, tools=None, cost="unknown", caps="unknown"),
         "text-embedding-3": M(8000)}, None),
    p2=({"gamma": M(200000, reasoning=True)}, "free"),
    openrouter=({"nvidia/nemotron-3.5-content-safety:free": M(), "google/gemma-4-31b-it:free": M(),
                 "nvidia/nemotron-3-super-120b-a12b:free": M(262144, reasoning=True)}, None),
)


def ids(rows):
    return [r["id"] for r in rows]


def test_paid_and_unknown_cost_models_are_excluded_by_default():
    got = ids(sm.rank(BASE, "review"))
    assert "p1/paid-one" not in got and "p1/mystery" not in got


def test_include_paid_is_the_only_way_to_see_paid_models():
    assert "p1/paid-one" in ids(sm.rank(BASE, "review", include_paid=True))


def test_policy_resolves_unknown_cost_for_that_provider_only():
    c = cache(p1=({"m": M(cost="unknown", caps="unknown", ctx=None)}, "free"),
              p2=({"m": M(cost="unknown", caps="unknown", ctx=None)}, None))
    assert ids(sm.rank(c, "review")) == ["p1/m"]


def test_a_paid_policy_blocks_unknown_models():
    c = cache(p1=({"m": M(cost="unknown")}, "paid"))
    assert sm.rank(c, "review") == []
    assert ids(sm.rank(c, "review", include_paid=True)) == ["p1/m"]


def test_non_chat_models_and_known_bad_openrouter_models_are_dropped():
    got = ids(sm.rank(BASE, "review"))
    assert not any("embedding" in g or "content-safety" in g or "gemma-4-31b" in g for g in got)
    assert "openrouter/nvidia/nemotron-3-super-120b-a12b:free" in got


def test_include_non_reviewers_restores_them():
    got = ids(sm.rank(BASE, "review", include_non_reviewers=True))
    assert "openrouter/nvidia/nemotron-3.5-content-safety:free" in got


def test_vision_requires_an_image_modality():
    c = cache(p=({"t": M(), "v": M(mods=("text", "image"))}, None))
    assert ids(sm.rank(c, "vision")) == ["p/v"]


def test_min_ctx_excludes_small_and_unknown_context():
    got = ids(sm.rank(BASE, "review", min_ctx=150000))
    assert "p1/alpha-small-flash" not in got and "p1/alpha-big" in got


def test_task_changes_the_ranking():
    assert ids(sm.rank(BASE, "code"))[0] != ids(sm.rank(BASE, "fast"))[0]
    assert "p1/beta-coder" == ids(sm.rank(cache(p1=({"beta-coder": M(128000), "other": M(128000)}, None)), "code"))[0]
    assert "p1/alpha-small-flash" == ids(sm.rank(cache(p1=({"alpha-small-flash": M(32000), "other": M(32000)}, None)), "fast"))[0]


def test_recent_success_breaks_a_tie():
    c = cache(p=({"a": M(), "b": M(last_ok="2026-10-01T00:00:00Z")}, None))
    assert ids(sm.rank(c, "review"))[0] == "p/b"


def test_ranking_is_deterministic_and_ties_break_by_id():
    c = cache(p=({"z": M(), "a": M(), "m": M()}, None))
    assert ids(sm.rank(c, "review")) == ["p/a", "p/m", "p/z"] == ids(sm.rank(c, "review"))


def test_pick_prefers_diverse_providers_over_a_slightly_better_sibling():
    rows = sm.rank(BASE, "review")
    top3 = ids(sm.pick(rows, 3))
    assert len({i.split("/")[0] for i in top3}) >= 2


def test_pick_n_larger_than_pool_returns_the_pool():
    c = cache(p=({"a": M(), "b": M()}, None))
    assert len(sm.pick(sm.rank(c, "review"), 9)) == 2


def test_not_reviewer_lists_match_list_free_models():
    spec = importlib.util.spec_from_file_location("lfm", ROOT / "scripts" / "list_free_models.py")
    lfm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lfm)
    assert sm.NOT_REVIEWERS == lfm.NOT_REVIEWERS and sm.UNRELIABLE == lfm.UNRELIABLE


def test_cli_auto_prints_ids_and_warns_about_undecided_providers(tmp_path):
    state = tmp_path / "s"
    state.mkdir()
    c = cache(p=({"a": M()}, None))
    c.update({"schema": 1})
    c["providers"]["q"] = {"models": {"m": M(cost="unknown")}, "status": "needs_policy"}
    (state / "capabilities.json").write_text(json.dumps(c))
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "select_models.py"), "--task", "review", "--auto"],
                       capture_output=True, text=True, env={"BCOPENCODE_STATE_DIR": str(state), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0 and r.stdout.split() == ["p/a"]
    assert "q" in r.stderr and "set-policy" in r.stderr


def test_cli_empty_cache_exits_zero_and_explains(tmp_path):
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "select_models.py"), "--auto"],
                       capture_output=True, text=True, env={"BCOPENCODE_STATE_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0 and r.stdout == "" and "no usable models" in r.stderr
