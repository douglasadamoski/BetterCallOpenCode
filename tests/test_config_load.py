"""Config loading in `_bcoc_common.sh`: what may set what, and from where.

Two independent things are asserted here, and both were regressions once:

  * A user-owned config file must actually work. The defaults used to be captured at
    source time with the config load running afterwards, so BCOPENCODE_MODEL/CAP/
    STATE_DIR were silently ignored while ALLOW_PAID/AGENT did take effect.
  * A `.bettercallopencode.env` in the current directory must do NOTHING. Repo-local
    config was removed outright: the project under review is untrusted input and does
    not get to choose the model, the token budget or the timeout of its own review.

The parser never `source`s a config file, so the third axis is injection: a value
carrying `$(`, a backtick or a newline must be refused rather than expanded.

Every case runs against a COPY of the script in a tmp SKILL_DIR, so the developer's own
~/.config and the checkout's own config file cannot influence the result.
"""
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "scripts" / "_bcoc_common.sh"

# Built at runtime: a literal key-shaped string in this file would trip the packer's own
# secret scan (tests/test_pack_secrets.py packs this entire repo).
KEY_NAME = "OPENROUTER_API" + "_KEY"
FAKE_KEY = "sk-" + "or-v1-" + "c" * 48

PROBE = "\n".join([
    'printf "MODEL=%s\\n" "$DEFAULT_MODEL"',
    'printf "CAP=%s\\n" "$DEFAULT_CAP"',
    'printf "TIMEOUT=%s\\n" "$DEFAULT_TIMEOUT"',
    'printf "MAX_TOKENS=%s\\n" "$DEFAULT_MAX_TOKENS"',
    'printf "MAX_INPUT=%s\\n" "$DEFAULT_MAX_INPUT_TOKENS"',
    'printf "STATE_DIR=%s\\n" "$STATE_DIR"',
    # `-` not `:-`: an exported-but-empty key must be distinguishable from an unset one,
    # since "" is exactly how the suite guarantees no credential is resolvable.
    'printf "KEY=%s\\n" "${%s-<unset>}"' % ("%s", KEY_NAME),
    'printf "AGENT=%s\\n" "${BCOPENCODE_AGENT-<unset>}"',
    'printf "RPM=%s\\n" "${BCOPENCODE_FREE_RPM-<unset>}"',
    'printf "EVIL=%s\\n" "${BCOPENCODE_EVIL-<unset>}"',
    'printf "PATHVAL=%s\\n" "$PATH"',
])


@pytest.fixture
def skill(tmp_path):
    """A private copy of the library, so SKILL_DIR is not the developer's checkout."""
    d = tmp_path / "skill"
    (d / "scripts").mkdir(parents=True)
    shutil.copy2(COMMON, d / "scripts" / "_bcoc_common.sh")
    return d


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".config").mkdir(parents=True)
    return h


@pytest.fixture
def env(home, tmp_path):
    e = dict(os.environ)
    e["HOME"] = str(home)
    e["XDG_CONFIG_HOME"] = str(home / ".config")
    e["BCOPENCODE_STATE_DIR"] = str(tmp_path / "state")
    for k in list(e):
        if k.startswith("BCOPENCODE_") and k != "BCOPENCODE_STATE_DIR":
            del e[k]
        elif k.startswith("OPENROUTER_"):
            del e[k]
    return e


def load(skill, env, cwd=None, extra=""):
    script = "source %s\n%s\n%s" % (
        shlex.quote(str(skill / "scripts" / "_bcoc_common.sh")), extra, PROBE
    )
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env=env, cwd=cwd, timeout=60)
    assert p.returncode == 0, p.stderr
    out = {}
    for line in p.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out, p.stderr


def user_config(home, text):
    d = home / ".config" / "bettercallopencode"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.env").write_text(text)


DEFAULT_MODEL = "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"


# --- a user config file must work -----------------------------------------------


def test_baseline_defaults_with_no_config(skill, env):
    got, _ = load(skill, env)
    assert got["MODEL"] == DEFAULT_MODEL
    assert got["CAP"] == "200"
    assert got["TIMEOUT"] == "600"
    assert got["KEY"] == "<unset>"


def test_user_config_sets_model_cap_timeout_and_key(skill, env, home):
    user_config(home, "\n".join([
        "# my settings",
        "BCOPENCODE_MODEL=openrouter/openai/gpt-oss-20b:free",
        "BCOPENCODE_CAP=7",
        "BCOPENCODE_TIMEOUT=45",
        KEY_NAME + "=" + FAKE_KEY,
        "",
    ]))
    got, _ = load(skill, env)
    assert got["MODEL"] == "openrouter/openai/gpt-oss-20b:free"
    assert got["CAP"] == "7"
    assert got["TIMEOUT"] == "45"
    assert got["KEY"] == FAKE_KEY


def test_home_config_is_read_when_xdg_is_unset(skill, env, home):
    del env["XDG_CONFIG_HOME"]
    user_config(home, "BCOPENCODE_CAP=13\n")
    got, _ = load(skill, env)
    assert got["CAP"] == "13"


def test_skill_dir_config_is_read(skill, env):
    (skill / ".bettercallopencode.env").write_text("BCOPENCODE_MODEL=skill/model:free\n")
    got, _ = load(skill, env)
    assert got["MODEL"] == "skill/model:free"


def test_user_config_can_set_the_state_dir(skill, env, tmp_path):
    """STATE_DIR was one of the keys the old ordering bug silently dropped."""
    del env["BCOPENCODE_STATE_DIR"]
    want = tmp_path / "elsewhere"
    (skill / ".bettercallopencode.env").write_text("BCOPENCODE_STATE_DIR=%s\n" % want)
    got, _ = load(skill, env)
    assert got["STATE_DIR"] == str(want)


@pytest.mark.parametrize("line,expect", [
    ('BCOPENCODE_MODEL="a/b:free"', "a/b:free"),
    ("BCOPENCODE_MODEL='a/b:free'", "a/b:free"),
    ("export BCOPENCODE_MODEL=a/b:free", "a/b:free"),
    ("  BCOPENCODE_MODEL=a/b:free", "a/b:free"),
    ("BCOPENCODE_MODEL =a/b:free", "a/b:free"),
    ('BCOPENCODE_MODEL="a b:free"', "a b:free"),
])
def test_line_forms(skill, env, home, line, expect):
    user_config(home, line + "\n")
    got, _ = load(skill, env)
    assert got["MODEL"] == expect


def test_comments_blank_lines_and_crlf(skill, env, home):
    user_config(home, "# comment\r\n\r\n   \r\nBCOPENCODE_CAP=9\r\n# trailing\r\n")
    got, _ = load(skill, env)
    assert got["CAP"] == "9", "a CRLF config file must not leave \\r glued to the value"


def test_final_line_without_a_newline_is_still_read(skill, env, home):
    user_config(home, "BCOPENCODE_CAP=11")
    got, _ = load(skill, env)
    assert got["CAP"] == "11"


# --- the current directory gets no say ------------------------------------------


def test_cwd_config_file_has_no_effect(skill, env, tmp_path):
    """Repo-local config was removed entirely; the reviewed repo does not configure its
    own review. Every key it used to be able to set (tokens, timeout, RPM) is a cost or
    an upload-size decision."""
    repo = tmp_path / "reviewed_repo"
    repo.mkdir()
    (repo / ".bettercallopencode.env").write_text("\n".join([
        "BCOPENCODE_MODEL=attacker/model",
        "BCOPENCODE_CAP=99999",
        "BCOPENCODE_TIMEOUT=1",
        "BCOPENCODE_MAX_INPUT_TOKENS=999999",
        "BCOPENCODE_FREE_RPM=1000",
        "BCOPENCODE_AGENT=evil-agent",
        "BCOPENCODE_ALLOW_PAID=1",
        KEY_NAME + "=" + FAKE_KEY,
        "",
    ]))
    got, err = load(skill, env, cwd=repo)
    assert got["MODEL"] == DEFAULT_MODEL
    assert got["CAP"] == "200"
    assert got["TIMEOUT"] == "600"
    assert got["MAX_INPUT"] == "80000"
    assert got["RPM"] == "<unset>"
    assert got["AGENT"] == "<unset>"
    assert got["KEY"] == "<unset>", "a repo-local file must never supply a credential"
    assert ".bettercallopencode.env" not in err or "reviewed_repo" not in err


def test_cwd_config_named_like_the_skill_dir_one_is_still_ignored(skill, env, tmp_path):
    """Same filename as the skill-dir config — location is what authorises it."""
    repo = tmp_path / "repo2"
    repo.mkdir()
    (repo / ".bettercallopencode.env").write_text("BCOPENCODE_MODEL=from/cwd\n")
    (skill / ".bettercallopencode.env").write_text("BCOPENCODE_MODEL=from/skill\n")
    got, _ = load(skill, env, cwd=repo)
    assert got["MODEL"] == "from/skill"


# --- precedence ------------------------------------------------------------------


def test_environment_beats_every_file(skill, env, home):
    env["BCOPENCODE_MODEL"] = "from/env:free"
    env["BCOPENCODE_CAP"] = "3"
    env[KEY_NAME] = FAKE_KEY + "env"
    user_config(home, "\n".join([
        "BCOPENCODE_MODEL=from/user:free",
        "BCOPENCODE_CAP=500",
        KEY_NAME + "=" + FAKE_KEY + "file",
        "",
    ]))
    (skill / ".bettercallopencode.env").write_text("BCOPENCODE_MODEL=from/skill:free\n")
    got, _ = load(skill, env)
    assert got["MODEL"] == "from/env:free"
    assert got["CAP"] == "3"
    assert got["KEY"] == FAKE_KEY + "env", "a file must never overwrite an exported key"


def test_an_exported_empty_value_still_beats_a_file(skill, env, home):
    """`BCOPENCODE_API_KEY=` is how the test suite guarantees no key is resolvable; a
    config file that could refill it would let a run reach the network."""
    env[KEY_NAME] = ""
    user_config(home, KEY_NAME + "=" + FAKE_KEY + "\n")
    got, _ = load(skill, env)
    assert got["KEY"] == ""


def test_user_config_overrides_the_skill_dir_config(skill, env, home):
    """Precedence among files: env > ~/.config > the skill checkout's own file.

    The "already exported wins" snapshot is taken once, before any file is read, so it
    protects only the caller's environment — a later file can still override an earlier
    one. Pinning it here so the order cannot drift unnoticed; a user's personal config
    beating the checked-out skill's file is the sane direction.
    """
    (skill / ".bettercallopencode.env").write_text("BCOPENCODE_CAP=1\n")
    user_config(home, "BCOPENCODE_CAP=2\n")
    got, _ = load(skill, env)
    assert got["CAP"] == "2"


def test_loading_the_same_user_config_twice_is_idempotent(skill, env, home):
    """With XDG_CONFIG_HOME unset the loader reads $HOME/.config/... twice."""
    del env["XDG_CONFIG_HOME"]
    user_config(home, "BCOPENCODE_CAP=4\nBCOPENCODE_EVIL=1\n")
    got, err = load(skill, env)
    assert got["CAP"] == "4"
    assert err.count("BCOPENCODE_EVIL") <= 2, "one refusal per read, not a warning storm"


# --- injection -------------------------------------------------------------------


@pytest.mark.parametrize("value", [
    "a$(id)b",
    "$(touch pwned)",
    "a`id`b",
    "`touch pwned`",
    'x"$(whoami)"y',
    "${IFS}$(echo hi)",
])
def test_command_substitution_in_a_value_is_refused(skill, env, home, value, tmp_path):
    user_config(home, "BCOPENCODE_MODEL=" + value + "\n")
    got, err = load(skill, env, cwd=tmp_path)
    assert got["MODEL"] == DEFAULT_MODEL, "a refused value must not be applied"
    assert "refusing suspicious value" in err
    assert not (tmp_path / "pwned").exists(), "the config file was EXECUTED"


def test_a_refused_value_does_not_stop_the_rest_of_the_file(skill, env, home):
    user_config(home, "BCOPENCODE_MODEL=$(id)\nBCOPENCODE_CAP=42\n")
    got, err = load(skill, env)
    assert got["MODEL"] == DEFAULT_MODEL
    assert got["CAP"] == "42", "one bad line must not silently discard the whole config"


def test_no_value_can_carry_a_newline(skill, env, home):
    """The parser is line-based, so a quoted 'multi-line' value cannot smuggle one in."""
    user_config(home, 'BCOPENCODE_MODEL="first\nsecond"\nBCOPENCODE_CAP=5\n')
    got, _ = load(skill, env)
    assert "\n" not in got["MODEL"]
    assert "second" not in got["MODEL"]
    assert got["CAP"] == "5"


def test_a_config_file_is_never_sourced(skill, env, home, tmp_path):
    """Whole-line shell, not just a value: nothing in the file may run."""
    user_config(home, "\n".join([
        "touch " + str(tmp_path / "executed"),
        "rm -rf /",
        "BCOPENCODE_CAP=8",
        "",
    ]))
    got, _ = load(skill, env)
    assert not (tmp_path / "executed").exists()
    assert got["CAP"] == "8"


# --- the allowlist ----------------------------------------------------------------


def test_a_key_outside_the_allowlist_is_ignored_loudly(skill, env, home):
    user_config(home, "BCOPENCODE_EVIL=1\n")
    got, err = load(skill, env)
    assert got["EVIL"] == "<unset>"
    assert "not permitted" in err and "BCOPENCODE_EVIL" in err


def test_a_non_bcopencode_key_is_ignored(skill, env, home):
    """PATH in a config file must not become PATH in the process that runs the client."""
    user_config(home, "PATH=/definitely/not/a/path\nLD_PRELOAD=/evil.so\nBCOPENCODE_CAP=6\n")
    got, _ = load(skill, env)
    assert "/definitely/not/a/path" not in got["PATHVAL"]
    assert got["CAP"] == "6"


def test_lowercase_keys_are_ignored(skill, env, home):
    user_config(home, "bcopencode_model=lower/case\n")
    got, _ = load(skill, env)
    assert got["MODEL"] == DEFAULT_MODEL


@pytest.mark.parametrize("key", [
    "BCOPENCODE_MODEL", "BCOPENCODE_CAP", "BCOPENCODE_MAX_TOKENS",
    "BCOPENCODE_MAX_INPUT_TOKENS", "BCOPENCODE_TIMEOUT", "BCOPENCODE_TEMPERATURE",
    "BCOPENCODE_BACKEND", "BCOPENCODE_STATE_DIR", "BCOPENCODE_FREE_RPM",
    "BCOPENCODE_REASONING_MAX_TOKENS", "BCOPENCODE_AGENT",
    "BCOPENCODE_OPENCODE_CONFIG_DIR", "BCOPENCODE_ALLOW_PAID", "BCOPENCODE_KEEP_RUN",
    "BCOPENCODE_STRICT_SCAN", "BCOPENCODE_API_KEY",
])
def test_documented_user_keys_are_all_accepted(skill, env, home, key):
    user_config(home, key + "=probe\n")
    _got, err = load(skill, env)
    assert "not permitted" not in err, f"{key} is documented as user-settable but was refused"
