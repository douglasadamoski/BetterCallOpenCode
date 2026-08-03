"""Mode B must actually run. It never did.

`run_local.sh` had `conda run --no-capture-output -n "$ENV" -- timeout …`. conda passes
the `--` through as the first WORD of the command, so it became `--: command not found`
and every approved script exited 127 — on every host. This is the BetterCall family's
HIGH 1: all three sibling skills hit it and left a comment forbidding its return.

On stock macOS it was dead twice over: `readlink -f` does not exist there, so the
containment check compared empty strings and every invocation returned REFUSED.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUN_LOCAL = ROOT / "scripts" / "run_local.sh"


@pytest.fixture
def sandbox(tmp_path):
    d = tmp_path / "sbx"
    (d / "task").mkdir(parents=True)
    (d / "task" / "probe.py").write_text('import sys\nprint("MODE_B_WORKS", sys.version_info[0])\n')
    return d


def run(args, timeout=180):
    return subprocess.run(["bash", str(RUN_LOCAL)] + args,
                          capture_output=True, text=True, timeout=timeout)


def test_approved_script_actually_executes(sandbox):
    p = run(["--sandbox", str(sandbox), "--script", str(sandbox / "task" / "probe.py"),
             "--timeout", "60"])
    assert "MODE_B_WORKS" in p.stdout, f"Mode B did not run:\n{p.stdout}\n{p.stderr}"
    assert "[run_local] exit=0" in p.stdout
    assert "command not found" not in p.stdout + p.stderr


def test_no_double_dash_before_the_command():
    """Guard the one-character regression directly, not just its symptom."""
    src = RUN_LOCAL.read_text()
    assert '-n "$ENV" -- ' not in src, (
        "`conda run … -- <cmd>` passes the -- through as the command's first word; "
        "this makes every Mode B run exit 127. Never re-add it."
    )


def test_python_scripts_use_python3(sandbox):
    p = run(["--sandbox", str(sandbox), "--script", str(sandbox / "task" / "probe.py"),
             "--dry-run"])
    assert "python3" in p.stdout, "many conda envs ship no bare `python`"


def test_script_outside_the_sandbox_is_refused(sandbox, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("print('escaped')\n")
    p = run(["--sandbox", str(sandbox), "--script", str(outside)])
    assert "REFUSED" in p.stderr
    assert p.returncode == 3


def test_dry_run_does_not_execute(sandbox):
    p = run(["--sandbox", str(sandbox), "--script", str(sandbox / "task" / "probe.py"),
             "--dry-run"])
    assert "would execute" in p.stdout
    assert "MODE_B_WORKS" not in p.stdout


@pytest.mark.skipif(shutil.which("conda") is None, reason="conda not installed")
def test_explicit_env_without_conda_would_refuse():
    """--env means "run HERE"; falling back silently would run somewhere else."""
    src = RUN_LOCAL.read_text()
    assert "ENV_EXPLICIT" in src and "Refusing to run somewhere other than" in src
