"""Regression tests for the secret filter in pack_context.py.

These exist because the filter once skipped `.env*`, `*.pem` and even the sibling
skill's `.bettercallmyai.env` — but not `.bettercallopencode.env`, the one file
SKILL.md tells users to store OPENROUTER_API_KEY in. Reviewing such a project
packed the key into the prompt and sent it to a third-party model.

Fully offline: nothing here touches the network.
"""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pack_context = _load("pack_context")

# Built by concatenation so this test file is not itself a credential-shaped
# string that the scanner (or a GitHub secret scanner) would flag.
FAKE_OPENROUTER = "sk-" + "or-v1-" + "a" * 48
FAKE_OPENROUTER_2 = "sk-" + "or-v1-" + "b" * 48
FAKE_GITHUB = "ghp_" + "d" * 36
FAKE_ANTHROPIC = "sk-" + "ant-" + "e" * 40
FAKE_AWS = "AKIA" + "IOSFODNN7EXAMPLE"
PEM_HEADER = "-----BEGIN " + "RSA PRIVATE KEY-----"


@pytest.fixture
def scope_secrets(tmp_path):
    """A project carrying credentials both in secret-NAMED and innocent-named files."""
    (tmp_path / "sub").mkdir()

    # Secret-shaped names — the name filter must catch these.
    (tmp_path / ".bettercallopencode.env").write_text(
        f"OPENROUTER_API_KEY={FAKE_OPENROUTER}\nBCOPENCODE_MODEL=openrouter/free\n"
    )
    (tmp_path / "auth.json").write_text(
        json.dumps({"openrouter": {"type": "api", "key": FAKE_OPENROUTER_2}})
    )
    (tmp_path / "config.env").write_text(f"OPENROUTER_API_KEY={FAKE_OPENROUTER}\n")
    (tmp_path / "id_rsa").write_text(f"{PEM_HEADER}\nMIIEow\n")

    # Innocent names, live credentials inside — only the content backstop sees these.
    (tmp_path / "sub" / "notes.md").write_text(f"# deploy\nexport TOKEN={FAKE_GITHUB}\n")
    (tmp_path / "sub" / "settings.yaml").write_text(f"anthropic: {FAKE_ANTHROPIC}\n")
    (tmp_path / "sub" / "terraform.tf").write_text(f'access_key = "{FAKE_AWS}"\n')

    (tmp_path / "ok.py").write_text('def hello():\n    return "world"\n')
    return tmp_path


def test_no_credential_reaches_the_packed_body(scope_secrets):
    body, _meta = pack_context.pack(scope_secrets)
    for secret in (
        FAKE_OPENROUTER,
        FAKE_OPENROUTER_2,
        FAKE_GITHUB,
        FAKE_ANTHROPIC,
        FAKE_AWS,
        PEM_HEADER,
    ):
        assert secret not in body, f"packer leaked {secret[:12]}…"


def test_only_the_clean_file_is_included(scope_secrets):
    _body, meta = pack_context.pack(scope_secrets)
    assert [i["path"] for i in meta["included"]] == ["ok.py"]


def test_secret_named_files_are_reported_not_silently_dropped(scope_secrets):
    """A withheld file must be visible: "not reviewed" must not look like "clean"."""
    _body, meta = pack_context.pack(scope_secrets)
    by_name = set(meta["secrets_skipped_by_name"])
    assert {".bettercallopencode.env", "auth.json", "config.env", "id_rsa"} <= by_name


def test_content_backstop_catches_innocent_filenames(scope_secrets):
    _body, meta = pack_context.pack(scope_secrets)
    by_content = set(meta["secrets_skipped_by_content"])
    assert {"sub/notes.md", "sub/settings.yaml", "sub/terraform.tf"} <= by_content


@pytest.mark.parametrize(
    "name",
    [
        ".bettercallopencode.env",
        ".bettercallmyai.env",
        ".bettercallgrok.env",  # a sibling that does not exist yet
        "auth.json",
        "config.env",
        "id_ecdsa",
        "server.p12",
        "state.tfstate",
        ".htpasswd",
    ],
)
def test_secret_names_are_covered_by_construction(name):
    assert pack_context.is_secret(Path(name), name), f"{name} would be packed"


@pytest.mark.parametrize("name", ["README.md", "config.py", "auth_test.py", "main.go"])
def test_ordinary_names_are_not_treated_as_secret(name):
    assert not pack_context.is_secret(Path(name), name), f"{name} wrongly withheld"


def test_packer_does_not_refuse_its_own_source():
    """The backstop scans for token shapes, and pack_context.py describes those shapes.

    If the patterns are ever rewritten as plain literals, the file matches itself and
    the skill silently stops packing its own most security-critical file for review —
    a failure that is invisible without this assertion.
    """
    repo = SCRIPTS.parent
    _body, meta = pack_context.pack(repo, max_input_tokens=80000)
    included = {i["path"] for i in meta["included"]}
    for critical in (
        "scripts/pack_context.py",
        "scripts/_bcoc_common.sh",
        "scripts/opencode_review.sh",
        "SKILL.md",
    ):
        assert critical in included, f"secret filter refused this repo's own {critical}"
    assert not meta["secrets_skipped_by_content"], meta["secrets_skipped_by_content"]
