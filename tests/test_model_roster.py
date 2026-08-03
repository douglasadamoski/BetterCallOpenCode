"""Which free models are actually usable as code reviewers.

Measured across 8 full 15-model matrices against this repo's own source. "Free" and
"can review code" are different properties, and conflating them wastes requests.

The case that matters most is nvidia/nemotron-3.5-content-safety. It scored 8/8 OK — a
perfect success rate — while returning a 93-character "User Safety: safe" and zero
findings. It is a content-moderation classifier. In a consensus panel that is worse than
a model which fails outright: it inflates the success rate, dilutes agreement, and reads
as a review that found nothing wrong. A model that always succeeds and never contributes
is the hardest kind of useless to notice, which is why this is asserted rather than left
to judgement.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lfm = _load("list_free_models")


def test_content_moderation_model_is_not_a_reviewer():
    assert not lfm.is_reviewer("nvidia/nemotron-3.5-content-safety:free")
    assert not lfm.is_usable("nvidia/nemotron-3.5-content-safety:free")


def test_permanently_rate_limited_model_is_excluded():
    """0/8 OK, 8/8 HTTP 429 from an upstream shared pool."""
    assert not lfm.is_usable("google/gemma-4-31b-it:free")
    assert lfm.is_reviewer("google/gemma-4-31b-it:free"), "it CAN review; it just cannot be reached"


@pytest.mark.parametrize(
    "mid",
    [
        "poolside/laguna-s-2.1:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "nvidia/nemotron-3-nano-30b-a3b:free",
        "openrouter/free",
        "inclusionai/ling-3.0-flash:free",
    ],
)
def test_proven_reviewers_are_kept(mid):
    assert lfm.is_usable(mid)


def test_exclusions_are_opt_outable():
    """"Free model that exists" and "model that can review code" are different questions.
    The roster tool must still be able to answer the first."""
    src = (ROOT / "scripts" / "list_free_models.py").read_text()
    assert "--include-non-reviewers" in src


def test_exclusions_are_announced_not_silent():
    """A silently shorter roster is indistinguishable from a shrinking free tier."""
    src = (ROOT / "scripts" / "list_free_models.py").read_text()
    assert "excluding" in src and "stderr" in src


def test_presets_contain_no_excluded_models():
    for f in ("scripts/multi_review.sh", "scripts/panel_run.py"):
        src = (ROOT / f).read_text()
        for bad in lfm.NOT_REVIEWERS | lfm.UNRELIABLE:
            assert bad not in src, f"{f} still ships {bad} in a preset"


def test_default_model_is_one_of_the_reliable_ones():
    """The single-shot default is the most-used path; a failed review there costs the
    user a request and a round trip. Reliability beats depth for this one choice."""
    src = (ROOT / "scripts" / "_bcoc_common.sh").read_text()
    line = [ln for ln in src.splitlines() if ln.startswith("DEFAULT_MODEL=")][-1]
    for weak in ("nemotron-3-ultra", "north-mini-code", "gpt-oss-20b", "gemma-4"):
        assert weak not in line, f"default model is {weak}, which scored poorly across 8 matrices"
    assert lfm.is_usable(line.split(":-openrouter/")[1].rstrip('}"'))


def test_every_preset_model_is_usable():
    src = (ROOT / "scripts" / "panel_run.py").read_text()
    import re
    for mid in set(re.findall(r'"([a-z0-9-]+/[A-Za-z0-9._-]+:free)"', src)):
        assert lfm.is_usable(mid), f"preset ships unusable model {mid}"
