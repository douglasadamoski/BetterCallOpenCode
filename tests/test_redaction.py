"""One redactor, and reports that do not carry secrets.

There used to be TWO implementations of credential redaction — a `sed` pipeline in
`bcoc_redact` (scripts/_bcoc_common.sh) and a Python pattern list in scripts/panel_run.py
— with different pattern sets. Text one masked the other let through, and the Python one
is what writes an error snippet into `summary.csv` inside the user's project directory.

scripts/redact.py is now the single authority. This file exists to keep it that way:

  * every vendor shape the packer knows about is actually masked;
  * the SHELL path and the PYTHON path produce byte-identical output (parity, in the same
    spirit as tests/test_gate_parity.py for the free gate);
  * the shell fails CLOSED — if the Python helper is missing or dies, the text is still
    redacted, never passed through;
  * ordinary prose about credentials is NOT mangled, which is the whole reason report
    bodies get the high-confidence subset instead of the full pattern set.

INVARIANT for this file, same as pack_context.py / scan_secrets.sh / redact.py: never
write a literal string that pack_context.py's patterns match. Every sample below is built
by CONCATENATION at runtime. Otherwise the packer withholds the redactor's own test suite
from every review of this repo — enforced by
tests/test_pack_secrets.py::test_packer_does_not_refuse_its_own_source.

Fully offline.
"""
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
COMMON_SH = SCRIPTS / "_bcoc_common.sh"
REVIEW_SH = SCRIPTS / "opencode_review.sh"
REDACT_PY = SCRIPTS / "redact.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


redact_mod = _load("redact", REDACT_PY)
pack_context = _load("pack_context_for_redaction", SCRIPTS / "pack_context.py")


# --- samples, all assembled at runtime -------------------------------------------
# Keyed by the rule label in redact.HIGH_CONFIDENCE, so a new rule without a sample is a
# test failure rather than an untested pattern (see test_every_rule_has_a_sample).
FAKE = {
    "openrouter-api-key": "sk-" + "or-v1-" + "0123456789abcdef0123456789abcdef",
    "anthropic-api-key": "sk-" + "ant-" + "api03-" + "A" * 40,
    "openai-project-key": "sk-" + "proj-" + "B" * 40,
    "stripe-secret-key": "sk" + "_live_" + "C" * 30,
    "github-token": "ghp_" + "D" * 36,
    "github-fine-grained-pat": "github_" + "pat_" + "E" * 30,
    "slack-token": "xox" + "b-" + "1234567890-" + "F" * 24,
    "slack-app-token": "xapp-" + "1-A012345678-" + "G" * 20,
    "google-api-key": "AIza" + "S" * 35,
    "aws-access-key-id": "AKIA" + "IOSFODNN7EXAMPLE",
    "gitlab-pat": "glpat-" + "H" * 20,
    "pem-private-key": "-----BEGIN " + "RSA PRIVATE KEY-----",
    "putty-private-key": "PuTTY-" + "User-Key-File" + "-2: ssh-rsa",
    "credentialed-url": "postgres" + "://" + "dbuser" + ":" + "Tr0ub4dor3xK" + "@"
    + "db.internal:5432/app",
}

# Heuristic-only shapes. Correct for machine output, deliberately NOT applied to a report
# body, so they are asserted separately.
HEURISTIC_SAMPLES = [
    ("bearer-token", "Bearer " + "eyJhbGciOi" + "JIUzI1NiJ9." + "Z" * 30),
    ("authorization-header", "Author" + "ization: " + "Basic " + "dXNlcjpwYXNzd29yZA=="),
    ("credential-assignment", "NPM" + "_TOKEN" + "=" + "npm" + "_" + "J" * 30),
]

# Prose that must survive untouched. Every one of these is the sort of sentence this
# repo's own reviews actually contain.
PROSE = [
    "The secret filter withholds any file whose contents look like a credential.",
    "Talking about the " + "sk-" + "or-v1-" + " prefix is not a leak; it is the finding.",
    "Set your API key in the environment, never in the repo.",
    "def load(api_key: str) -> Client:",
    "Author" + "ization headers are logged at debug level, which is the bug.",
    "The " + "AKIA" + " prefix identifies a long-lived AWS access key id.",
    "Rotate credentials quarterly; document the runbook.",
]


def sh_redact(text, env=None, scripts_dir=None, force_fallback=False):
    """Run the SHELL path exactly as opencode_review.sh calls it."""
    script = 'source "%s" 2>/dev/null\n' % COMMON_SH
    if scripts_dir is not None:
        script += '_BCOC_SCRIPTS=%s\n' % _q(str(scripts_dir))
    if force_fallback:
        script += "BCOPENCODE_FORCE_SED_REDACT=1\n"
    script += "bcoc_redact\n"
    p = subprocess.run(
        ["bash", "-c", script],
        input=text.encode("utf-8"),
        capture_output=True,
        timeout=60,
        env=env,
    )
    assert p.returncode == 0, f"bcoc_redact exited {p.returncode}: {p.stderr[-500:]!r}"
    return p.stdout.decode("utf-8", "replace")


def _q(s):
    return "'" + s.replace("'", "'\\''") + "'"


# --- 1. every vendor shape is redacted --------------------------------------------


def test_every_rule_has_a_sample():
    """A rule with no sample is an untested pattern. Fail loudly rather than drift."""
    labels = {label for label, _rx, _sub in redact_mod.HIGH_CONFIDENCE}
    assert labels == set(FAKE), (
        f"high-confidence rules without a sample: {sorted(labels - set(FAKE))}; "
        f"samples with no rule: {sorted(set(FAKE) - labels)}"
    )
    heur = {label for label, _rx, _sub in redact_mod.HEURISTIC}
    assert heur == {label for label, _s in HEURISTIC_SAMPLES}


@pytest.mark.parametrize("label", sorted(FAKE))
def test_vendor_shape_is_redacted(label):
    secret = FAKE[label]
    text = f"line one\nhere it is: {secret}\nline three\n"
    out = redact_mod.redact(text)
    assert secret not in out, f"{label} survived the full redactor"
    assert "line three" in out, "redaction ate surrounding text"


@pytest.mark.parametrize("label", sorted(FAKE))
def test_vendor_shape_is_redacted_by_the_strict_subset_too(label):
    """The strict subset is what protects report bodies — it must cover every vendor shape."""
    secret = FAKE[label]
    assert secret not in redact_mod.redact_strict(f"critic said: {secret}\n")


@pytest.mark.parametrize("label,sample", HEURISTIC_SAMPLES)
def test_heuristic_shape_is_redacted_by_the_full_set_only(label, sample):
    out_full = redact_mod.redact(sample)
    assert out_full != sample, f"{label} survived the full redactor"
    # And is deliberately left alone by the strict subset — that is the Problem 2 choice.
    assert redact_mod.redact_strict(sample) == sample, (
        f"{label} is a heuristic rule; it must not be applied to report bodies"
    )


@pytest.mark.parametrize("label", sorted(FAKE))
def test_redacted_output_is_not_itself_credential_shaped(label):
    """The mask must not produce something the packer would then withhold.

    Masking a credentialed URL to `***:***@` left the OUTPUT still matching the userinfo
    pattern, so a report containing a *redacted* URL was withheld from its own review.
    """
    out = redact_mod.redact(f"value: {FAKE[label]}\n")
    assert not pack_context.looks_secret_content(out.encode("utf-8"))


# --- 2. shell / python parity ------------------------------------------------------


PARITY_INPUTS = (
    [FAKE[k] for k in sorted(FAKE)]
    + [s for _l, s in HEURISTIC_SAMPLES]
    + PROSE
    + [
        "",
        "\n",
        "no newline at end",
        "tab\tseparated\tvalues\n",
        "unicode: café ✓ 日本語\n",
        "two on one line: " + FAKE["github-token"] + " and " + FAKE["google-api-key"] + "\n",
    ]
)


@pytest.mark.parametrize("payload", PARITY_INPUTS)
def test_shell_and_python_paths_are_identical(payload):
    """The load-bearing assertion: there is ONE redactor.

    `bcoc_redact` now pipes through scripts/redact.py, so this holds by construction —
    which is the point. If anyone reintroduces a second pattern set in the shell, this
    test is what catches it, exactly as test_gate_parity.py does for the free gate.
    """
    text = payload if payload.endswith("\n") or payload == "" else payload + "\n"
    assert sh_redact(text) == redact_mod.redact(text)


def test_panel_run_uses_the_same_function_object():
    """panel_run.py must not carry its own copy — it used to, and the lists diverged."""
    panel_run = _load("panel_run_for_redaction", SCRIPTS / "panel_run.py")
    assert panel_run.redact is redact_mod.redact, (
        "panel_run.redact is not scripts/redact.py's redact — a second implementation "
        "has crept back in"
    )
    assert not hasattr(panel_run, "_REDACT"), "panel_run still defines its own pattern list"


def test_summary_csv_error_column_is_redacted():
    """The concrete leak: this string is written into the user's project directory."""
    err = "request failed, sent header Bearer " + FAKE["openrouter-api-key"]
    out = redact_mod.redact(err)[:200]
    assert FAKE["openrouter-api-key"] not in out


# --- 3. fail closed ----------------------------------------------------------------


@pytest.mark.parametrize("label", sorted(FAKE))
def test_shell_fails_closed_when_the_helper_is_missing(label, tmp_path):
    """redact.py gone: the shell must still redact, not pass the text through."""
    empty = tmp_path / "no_scripts"
    empty.mkdir()
    secret = FAKE[label]
    out = sh_redact(f"before {secret} after\n", scripts_dir=empty)
    assert secret not in out, f"{label} passed through when redact.py was unavailable"
    assert "before" in out and "after" in out, "fallback dropped the text instead of masking it"


@pytest.mark.parametrize("label", sorted(FAKE))
def test_shell_fails_closed_when_python_exits_nonzero(label, tmp_path):
    """python3 present but broken — the failure mode a missing-file check would miss."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "python3"
    shim.write_text("#!/bin/sh\nexit 1\n")
    shim.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    secret = FAKE[label]
    out = sh_redact(f"before {secret} after\n", env=env)
    assert secret not in out, f"{label} passed through when the helper died"
    assert "before" in out and "after" in out


@pytest.mark.parametrize("label,sample", HEURISTIC_SAMPLES)
def test_fallback_also_covers_the_heuristic_shapes(label, sample):
    out = sh_redact(sample + "\n", force_fallback=True)
    assert out.strip() != sample, f"{label} passed through the sed fallback"


def test_a_broken_helper_never_emits_the_text_twice(tmp_path):
    """A helper that writes some output and then dies must not leave a partial head on
    stdout followed by the fallback's full output."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "python3"
    # Write a plausible partial result, then fail.
    shim.write_text("#!/bin/sh\nprintf 'PARTIAL'\nexit 1\n")
    shim.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    out = sh_redact("marker line\n", env=env)
    assert "PARTIAL" not in out
    assert out.count("marker line") == 1


# --- 4. ordinary prose is not mangled ----------------------------------------------


@pytest.mark.parametrize("text", PROSE)
def test_prose_survives_the_strict_subset(text):
    """The report-body path must never alter a reviewer's argument."""
    assert redact_mod.redact_strict(text + "\n") == text + "\n"


def test_the_full_set_would_have_mangled_a_real_finding():
    """Documents WHY report bodies get the strict subset instead of the full set.

    Built at runtime: a reviewer reporting an unredacted header names that header the way
    a config file spells it, and the assignment heuristic would eat the reviewer's own
    example.
    """
    finding = "Bug: the " + "api_key" + ": " + "someRealValue" + " header is echoed to the log."
    assert redact_mod.redact(finding) != finding, "the heuristic rule is not firing at all"
    assert redact_mod.redact_strict(finding) == finding, (
        "the strict subset mangled a legitimate finding — report bodies would be destroyed"
    )


def test_bare_vendor_prefixes_in_prose_are_left_alone():
    """A live matrix run confirmed this repo's reviews say these prefixes as placeholders."""
    for prefix in ("sk-" + "or-v1-", "ghp_", "AIza", "glpat-", "xox" + "b-"):
        line = f"the {prefix} prefix identifies a token family\n"
        assert redact_mod.redact_strict(line) == line, f"bare {prefix!r} was mangled"


# --- 5. the report guard (Problem 2) -----------------------------------------------


def _report(body):
    return "# BetterCallOpenCode review\n\n- **RESULT:** OK\n\n## Critic output\n\n" + body


def _guard(path):
    return subprocess.run(
        [sys.executable, str(REDACT_PY), "--guard", str(path)],
        capture_output=True, text=True, timeout=60,
    )


def test_guard_leaves_a_clean_report_byte_identical(tmp_path):
    f = tmp_path / "REVIEW.md"
    text = _report("\n".join(PROSE) + "\n")
    f.write_text(text)
    p = _guard(f)
    assert p.returncode == 0, p.stderr
    assert f.read_text() == text, "the guard rewrote a clean report"


@pytest.mark.parametrize("label", sorted(FAKE))
def test_guard_masks_a_credential_in_the_critic_output(tmp_path, label):
    secret = FAKE[label]
    f = tmp_path / "REVIEW.md"
    f.write_text(_report(f"The config contains {secret} which is live.\n"))
    p = _guard(f)
    assert p.returncode == 3, f"guard exit {p.returncode}, stderr={p.stderr!r}"
    out = f.read_text()
    assert secret not in out, f"{label} survived the report guard"
    assert "[!CAUTION]" in out, "the report was altered without saying so"
    assert "which is live." in out, "the guard destroyed surrounding prose"


def test_guard_names_the_file_and_the_line(tmp_path):
    secret = FAKE["openrouter-api-key"]
    f = tmp_path / "REVIEW.md"
    f.write_text(_report(f"filler\nfiller\nleaked: {secret}\n"))
    assert _guard(f).returncode == 3
    out = f.read_text()
    assert str(f) in out, "the caution block does not name the file"
    # The secret is on line 9 of the report built above.
    assert re.search(r"Line\(s\) in the unmasked report: .*\b9\b", out), out


def test_caution_block_sits_under_the_h1(tmp_path):
    f = tmp_path / "REVIEW.md"
    f.write_text(_report(f"x {FAKE['github-token']} y\n"))
    assert _guard(f).returncode == 3
    lines = f.read_text().splitlines()
    assert lines[0].startswith("# BetterCallOpenCode review")
    assert any(l.startswith("> [!CAUTION]") for l in lines[:5]), lines[:8]


def test_guard_does_not_apply_the_heuristic_rules(tmp_path):
    """A report full of prose about credentials must come back untouched."""
    f = tmp_path / "REVIEW.md"
    text = _report("\n".join(s for _l, s in HEURISTIC_SAMPLES) + "\n")
    f.write_text(text)
    assert _guard(f).returncode == 0
    assert f.read_text() == text


def test_guard_reports_an_unreadable_file_rather_than_claiming_clean(tmp_path):
    p = _guard(tmp_path / "does_not_exist.md")
    assert p.returncode == 1, "a missing report must not read as 'scanned, clean'"


# --- 6. reports land in the user's project (Problem 3) ------------------------------


def _extract_fn(name, path):
    """Pull one shell function out of a script that cannot be sourced (it runs)."""
    src = Path(path).read_text()
    m = re.search(rf"^{re.escape(name)}\(\).*?^\}}$", src, re.M | re.S)
    assert m, f"{name} not found in {path}"
    return m.group(0)


def _run_gitignore_warn(report_path):
    body = (
        'source "%s" 2>/dev/null\n' % COMMON_SH
        + _extract_fn("bcoc_warn_if_not_gitignored", REVIEW_SH)
        + "\nbcoc_warn_if_not_gitignored %s\n" % _q(str(report_path))
    )
    return subprocess.run(["bash", "-c", body], capture_output=True, text=True, timeout=60)


def _git_repo(tmp_path, gitignore=None):
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    if gitignore is not None:
        (repo / ".gitignore").write_text(gitignore)
    return repo


def test_warns_when_the_report_is_not_gitignored(tmp_path):
    repo = _git_repo(tmp_path)
    report = repo / "BETTERCALLOPENCODE_REVIEW_x.md"
    report.write_text("# r\n")
    p = _run_gitignore_warn(report)
    assert p.returncode == 0
    assert "NOT gitignored" in p.stderr, p.stderr
    assert "BETTERCALLOPENCODE_*.md" in p.stderr
    assert p.stdout == "", "the warning must go to stderr, never into the RESULT stream"


def test_silent_when_the_pattern_is_already_ignored(tmp_path):
    repo = _git_repo(tmp_path, gitignore="BETTERCALLOPENCODE_REVIEW_*.md\n")
    report = repo / "BETTERCALLOPENCODE_REVIEW_x.md"
    report.write_text("# r\n")
    p = _run_gitignore_warn(report)
    assert p.stderr.strip() == "", p.stderr


def test_silent_outside_any_git_repo(tmp_path):
    outside = tmp_path / "plain"
    outside.mkdir()
    report = outside / "BETTERCALLOPENCODE_REVIEW_x.md"
    report.write_text("# r\n")
    p = _run_gitignore_warn(report)
    assert p.stderr.strip() == "", p.stderr


def test_the_users_gitignore_is_never_modified(tmp_path):
    repo = _git_repo(tmp_path, gitignore="*.log\n")
    report = repo / "BETTERCALLOPENCODE_REVIEW_x.md"
    report.write_text("# r\n")
    _run_gitignore_warn(report)
    assert (repo / ".gitignore").read_text() == "*.log\n", "the tool edited the user's .gitignore"


# --- 7. emit_report wiring ----------------------------------------------------------
# The guard and the gitignore warning are only worth anything if emit_report actually
# calls them. Drive the real function text rather than trusting that it does.


def _run_emit_report(out_path, body):
    script = (
        'source "%s" 2>/dev/null\n' % COMMON_SH
        + "OUT=%s\n" % _q(str(out_path))
        + "OUT_DIR=%s\n" % _q(str(Path(out_path).parent))
        + 'TMP_REPORT=""\n'
        + _extract_fn("bcoc_guard_report", REVIEW_SH)
        + "\n"
        + _extract_fn("bcoc_warn_if_not_gitignored", REVIEW_SH)
        + "\n"
        + _extract_fn("emit_report", REVIEW_SH)
        + "\nemit_report\necho \"scan=$BCOC_REPORT_SECRETS\"\n"
    )
    return subprocess.run(
        ["bash", "-c", script], input=body, capture_output=True, text=True, timeout=60
    )


def test_emit_report_masks_a_credential_in_the_critic_output(tmp_path):
    repo = _git_repo(tmp_path, gitignore="BETTERCALLOPENCODE_REVIEW_*.md\n")
    out = repo / "BETTERCALLOPENCODE_REVIEW_x.md"
    secret = FAKE["openrouter-api-key"]
    p = _run_emit_report(out, _report(f"the key is {secret}\n"))
    assert p.returncode == 0, p.stderr
    text = out.read_text()
    assert secret not in text, "emit_report wrote a live credential into the user's project"
    assert "[!CAUTION]" in text
    assert "scan=masked" in p.stdout
    assert "SECRET_SCAN=MASKED" in p.stderr, "no machine-readable signal on stderr"


def test_emit_report_leaves_a_clean_report_alone(tmp_path):
    repo = _git_repo(tmp_path, gitignore="BETTERCALLOPENCODE_REVIEW_*.md\n")
    out = repo / "BETTERCALLOPENCODE_REVIEW_x.md"
    body = _report("\n".join(PROSE) + "\n")
    p = _run_emit_report(out, body)
    assert p.returncode == 0, p.stderr
    assert out.read_text() == body
    assert "scan=clean" in p.stdout
    assert "SECRET_SCAN" not in p.stderr
    assert "NOT gitignored" not in p.stderr


def test_emit_report_warns_about_an_ungitignored_report(tmp_path):
    repo = _git_repo(tmp_path)
    out = repo / "BETTERCALLOPENCODE_REVIEW_x.md"
    p = _run_emit_report(out, _report("clean prose\n"))
    assert p.returncode == 0, p.stderr
    assert "NOT gitignored" in p.stderr


# --- 8. the no-literal-matches invariant -------------------------------------------


@pytest.mark.parametrize("path", ["scripts/redact.py", "tests/test_redaction.py"])
def test_redaction_sources_are_not_withheld_from_review(path):
    """These two files describe credential SHAPES. Written as plain literals they match
    pack_context.py's own patterns, the packer withholds them, and the redactor becomes
    unreviewable. Ten violations have already been caught in this repo."""
    data = (ROOT / path).read_bytes()
    assert not pack_context.looks_secret_content(data), (
        f"{path} matches the packer's own patterns — split the literal by concatenation"
    )
