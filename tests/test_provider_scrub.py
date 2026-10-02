"""No tracked file may name one specific provider.

The skill is provider-agnostic; docs, tests and fixtures use generic names
(`provider-a`, `example-gateway`). Model names are fine. The forbidden token is assembled
at runtime so this file does not contain it.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKENS = ["sa" + "ia", "gw" + "dg", "academic" + "cloud"]


def test_no_tracked_file_or_commit_message_names_the_provider():
    if subprocess.run(["git", "-C", str(ROOT), "rev-parse"], capture_output=True).returncode != 0:
        return                                    # exported tarball: nothing to scan
    pats = [f"-e{t}" for t in TOKENS]
    files = subprocess.run(["git", "-C", str(ROOT), "grep", "-il", *pats, "--", "."],
                           capture_output=True, text=True).stdout.split()
    assert files == []
    log = subprocess.run(["git", "-C", str(ROOT), "log", "-i", "--format=%H", *[f"--grep={t}" for t in TOKENS]],
                         capture_output=True, text=True).stdout.split()
    assert log == []
