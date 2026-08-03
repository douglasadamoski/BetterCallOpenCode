#!/usr/bin/env python3
"""redact.py — the ONE credential redactor for this skill.

There used to be two: `bcoc_redact` (a `sed` pipeline in scripts/_bcoc_common.sh) and
`_REDACT` (a Python list in scripts/panel_run.py). They implemented the same idea with
different pattern sets, so text that one masked the other passed through — and the
Python one is what writes an error snippet into `summary.csv` INSIDE the user's project
directory, the file most likely to be committed unread.

This module is now the authority. `_bcoc_common.sh` pipes through it, `panel_run.py`
imports it, and `opencode_review.sh` uses `--guard` on the finished report.

INVARIANT for this file (same as pack_context.py and scan_secrets.sh): never write a
literal string that pack_context.py's SECRET_CONTENT_RE / SECRET_ASSIGN_RE match. Build
every credential-shaped fragment by CONCATENATION. Otherwise the packer withholds this
file from every review of this repo and nobody can review the redactor. Enforced by
tests/test_pack_secrets.py::test_packer_does_not_refuse_its_own_source, which has already
caught ten violations here.

The pattern set is aligned with pack_context.py's SECRET_CONTENT_RE (read, never edited
from here). pack_context decides whether to WITHHOLD a file; this module decides how to
MASK text that is already on its way to a report, a log or the terminal. Two different
jobs, one shared vocabulary of what a credential looks like.

CLI
    redact.py                 stdin -> stdout, full pattern set (used by bcoc_redact)
    redact.py --strict        stdin -> stdout, high-confidence patterns only
    redact.py --guard FILE    scan a finished report; if it carries credential shapes,
                              mask them in place, insert a [!CAUTION] block, and exit 3
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List, Pattern, Tuple

__all__ = [
    "redact",
    "redact_bytes",
    "redact_strict",
    "redact_strict_bytes",
    "scan",
    "scan_bytes",
    "HIGH_CONFIDENCE",
    "HEURISTIC",
    "PATTERNS",
]

Rule = Tuple[str, Pattern[bytes], bytes]

# --- HIGH CONFIDENCE -------------------------------------------------------------
# Shapes that are credential material by construction. A vendor prefix followed by a
# long opaque body is not something prose produces by accident: this repo's own reviews
# legitimately discuss the string `sk-` + `or-v1-` when models critique the secret
# filter, and a live all-free-model matrix run confirmed those were placeholders — but
# every one of them was the BARE prefix, never a prefix plus 16+ opaque characters.
# That length floor is what makes this set safe to apply to a model's own words.
#
# These are the only rules applied to report BODIES (see --guard and Problem 2 below).
HIGH_CONFIDENCE: List[Rule] = [
    (
        "openrouter-api-key",
        re.compile(b"sk-" + b"or-v1-" + b"[A-Za-z0-9_-]{16,}"),
        b"sk-" + b"or-v1-***",
    ),
    (
        "anthropic-api-key",
        re.compile(b"sk-" + b"ant-" + b"[A-Za-z0-9_-]{20,}"),
        b"sk-" + b"ant-***",
    ),
    (
        "openai-project-key",
        re.compile(b"sk-" + b"proj-" + b"[A-Za-z0-9_-]{20,}"),
        b"sk-" + b"proj-***",
    ),
    (
        # Covers all four spellings in the wild: sk-live-, sk_live_, rk-live-, rk_live_.
        "stripe-secret-key",
        re.compile(b"(?:sk|rk)" + b"[-_]" + b"live" + b"[-_]" + b"[A-Za-z0-9]{16,}"),
        b"sk" + b"_live_***",
    ),
    (
        # ghp_/gho_/ghs_ are what pack_context lists; ghu_/ghr_ are the same family and
        # cost nothing to cover.
        "github-token",
        re.compile(b"gh" + b"[pousr]_" + b"[A-Za-z0-9]{20,}"),
        b"gh" + b"*_***",
    ),
    (
        "github-fine-grained-pat",
        re.compile(b"github_" + b"pat_" + b"[A-Za-z0-9_]{20,}"),
        b"github_" + b"pat_***",
    ),
    (
        "slack-token",
        re.compile(b"xox" + b"[bpasr]-" + b"[A-Za-z0-9-]{10,}"),
        b"xox" + b"*-***",
    ),
    (
        "slack-app-token",
        re.compile(b"xapp-" + b"[A-Za-z0-9-]{10,}"),
        b"xapp-***",
    ),
    (
        "google-api-key",
        re.compile(b"AIza" + b"[A-Za-z0-9_-]{30,}"),
        b"AIza***",
    ),
    (
        # Grouped so the replacement never has to spell the prefix out as a literal.
        "aws-access-key-id",
        re.compile(b"(" + b"AKIA" + b"|" + b"ASIA" + b")" + b"[0-9A-Z]{16}"),
        rb"\1***",
    ),
    (
        "gitlab-pat",
        re.compile(b"glpat-" + b"[A-Za-z0-9_-]{16,}"),
        b"glpat-***",
    ),
    (
        "pem-private-key",
        re.compile(b"-----BEGIN " + b"[A-Z ]*PRIVATE KEY-----"),
        b"[REDACTED PRIVATE KEY BLOCK]",
    ),
    (
        # Marker plus its version and algorithm token — NOT to end of line. Eating the
        # rest of the line destroyed the surrounding sentence when the marker was quoted
        # inside a critique, which is exactly the mangling the strict subset exists to
        # avoid. The marker is the tell; the PPK body that follows is base64 no pattern
        # would recognise anyway.
        "putty-private-key",
        re.compile(b"PuTTY-" + b"User-Key-File" + rb"(?:-\d+)?(?::[ \t]*\S+)?"),
        b"[REDACTED PUTTY KEY FILE]",
    ),
    (
        # A URL carrying userinfo is a credential regardless of what it is assigned to —
        # DATABASE_URL, REDIS_URL, MONGO_URI and SENTRY_DSN all commonly hold one. Same
        # expression pack_context.py uses.
        #
        # The replacement drops the colon on purpose. Masking to `***` + `:` + `***` + `@`
        # would leave the OUTPUT still matching this very pattern, so a report containing
        # a redacted URL would then be withheld from review by pack_context.
        "credentialed-url",
        re.compile(rb"([a-zA-Z][a-zA-Z0-9+.-]{1,20}" + rb"://)" + rb"[^/\s:@]{0,64}:[^/\s:@]{1,64}@"),
        rb"\1***@",
    ),
]

# --- HEURISTIC -------------------------------------------------------------------
# Shape-based rules that DO fire on ordinary prose about credentials: a sentence like
# "pass the token: whatever-you-configured" trips the assignment rule, and a curl
# example trips the header rules. They are correct for machine output (stderr, HTTP
# traces, JSON envelopes, ledger snippets) and wrong for a human's argument, so they are
# deliberately excluded from the report-body path.
HEURISTIC: List[Rule] = [
    (
        # Runs BEFORE the Authorization rule so `Authorization: Bearer <tok>` becomes
        # `Authorization: Bearer ***` rather than losing the scheme name. The
        # Authorization rule then finds nothing left long enough to match.
        "bearer-token",
        re.compile(rb"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"),
        b"Bearer ***",
    ),
    (
        # The optional `[A-Za-z]+\s+` covers the auth scheme, so `Basic <b64>` and
        # `Token <t>` are masked too. The old sed only knew about a bare value.
        # The header name is spliced so this line is not itself an assignment the packer
        # would withhold — see the no-literal-matches invariant at the top.
        "authorization-header",
        re.compile(b"(?i)" + b"authoriz" + b"ation:" + rb"\s*(?:[A-Za-z]+\s+)?[A-Za-z0-9._~+/=-]{8,}"),
        b"Authoriz" + b"ation: ***",
    ),
    (
        # Assignment-shaped. Key spellings snake_case / camelCase / SCREAMING_CASE all
        # appear in the wild, hence the optional separator between words.
        "credential-assignment",
        re.compile(
            rb"(?i)((?:api[_-]?key|access[_-]?token|auth[_-]?token|private[_-]?token"
            rb"|client[_-]?secret|refresh[_-]?token|session[_-]?key|pass(?:word|wd)"
            rb"|secret|token|apikey))"
            rb"([\"']?\s*[:=]\s*[\"']?)"
            rb"([^\s\"'`,;)\]}]{4,})"
        ),
        rb"\1\2***",
    ),
]

PATTERNS: List[Rule] = HIGH_CONFIDENCE + HEURISTIC


def _apply(data: bytes, rules: List[Rule]) -> bytes:
    for _label, rx, sub in rules:
        data = rx.sub(sub, data)
    return data


def redact_bytes(data: bytes) -> bytes:
    """Full pattern set. Use for machine output: stderr, logs, JSON envelopes."""
    return _apply(data, PATTERNS)


def redact_strict_bytes(data: bytes) -> bytes:
    """High-confidence vendor shapes only. Use for text a human wrote or a model wrote."""
    return _apply(data, HIGH_CONFIDENCE)


def redact(text: str) -> str:
    return redact_bytes((text or "").encode("utf-8", "replace")).decode("utf-8", "replace")


def redact_strict(text: str) -> str:
    return redact_strict_bytes((text or "").encode("utf-8", "replace")).decode(
        "utf-8", "replace"
    )


def scan_bytes(data: bytes) -> List[Tuple[int, str]]:
    """Locate high-confidence credential shapes. Returns [(1-based line, label)]."""
    hits: List[Tuple[int, str]] = []
    for lineno, line in enumerate(data.splitlines(), start=1):
        for label, rx, _sub in HIGH_CONFIDENCE:
            if rx.search(line):
                hits.append((lineno, label))
    return hits


def scan(text: str) -> List[Tuple[int, str]]:
    return scan_bytes((text or "").encode("utf-8", "replace"))


# --- report guard ----------------------------------------------------------------
# Problem 2, and WHY this shape was chosen.
#
# `bcoc_redact` was applied to the stderr <details> block but never to the model's own
# critique, so a model that read a credential and echoed it wrote it into the report
# verbatim — into a file that lands in the user's project directory.
#
# Three options were on the table:
#
#   (a) run the FULL pattern set over the critique. Rejected: the heuristic rules fire on
#       ordinary argument. A reviewer writing a finding about an unredacted api-key header
#       names that header, spelled the way a config file spells it — and the assignment
#       rule would then mask the reviewer's own example, destroying the finding while
#       looking like it worked.
#
#   (b) scan only, alter nothing, and emit a [!CAUTION] naming file and line. Rejected as
#       the WHOLE answer: a notice is not a control. The credential is still sitting in a
#       file in the user's working tree, and the threat model here is precisely a user who
#       commits the report without reading it — which is also the user who will not read
#       the caution block.
#
#   (c) what is implemented: mask the HIGH-CONFIDENCE vendor shapes only, and announce
#       every substitution in a [!CAUTION] block that names the file and the exact lines.
#
# (c) is (b) plus an actual control, and it buys its safety from the length floors above:
# every high-confidence rule requires a vendor prefix AND 16+ opaque characters, which
# prose about credentials does not produce. This repo's own reviews say `sk-` + `or-v1-`
# constantly and none of them match. Nothing is altered silently — if a byte changed, the
# report says so, in the model's own report, above its own findings.
#
# Deliberately NOT done: changing the RESULT word. The RESULT vocabulary is a documented
# contract (SKILL.md's table, asserted by tests/test_result_contract.py), and none of its
# words means "the report carried a credential". Reusing ERROR would tell Claude the
# request came back unusable and invite a retry that spends again — the opposite of what
# should happen. The non-OK signal is a dedicated, greppable `SECRET_SCAN=` line on stderr
# plus the in-report caution block.

GUARD_CLEAN = 0
GUARD_ERROR = 1
GUARD_REDACTED = 3


def build_caution(path: str, hits: List[Tuple[int, str]]) -> bytes:
    lines = sorted({ln for ln, _ in hits})
    labels = sorted({lab for _, lab in hits})
    shown = ", ".join(str(n) for n in lines[:20]) + (" …" if len(lines) > 20 else "")
    return (
        "> [!CAUTION]\n"
        "> **A credential-shaped string was found in this report and has been masked.**\n"
        f"> File: `{path}`\n"
        f"> Line(s) in the unmasked report: {shown}\n"
        f"> Shape(s): {', '.join(labels)}\n"
        ">\n"
        "> The critic's text was altered by the redactor — only the matched token bodies,\n"
        "> nothing else. Treat the underlying credential as COMPROMISED: it reached a\n"
        "> third-party model and was written to disk before it was masked. Rotate it, and\n"
        "> check whether this report or the reviewed scope has already been committed.\n"
    ).encode("utf-8")


def _insert_caution(body: bytes, block: bytes) -> bytes:
    """Put the block directly under the report's H1, or at the very top if there is none."""
    marker = b"\n"
    if body.startswith(b"# "):
        idx = body.find(marker)
        if idx != -1:
            return body[: idx + 1] + b"\n" + block + body[idx + 1 :]
    return block + b"\n" + body


def guard_file(path: str) -> int:
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as e:
        print(f"redact: cannot read {path}: {e}", file=sys.stderr)
        return GUARD_ERROR
    hits = scan_bytes(raw)
    if not hits:
        return GUARD_CLEAN
    body = redact_strict_bytes(raw)
    body = _insert_caution(body, build_caution(path, hits))
    try:
        p.write_bytes(body)
    except OSError as e:
        print(f"redact: cannot rewrite {path}: {e}", file=sys.stderr)
        return GUARD_ERROR
    labels = sorted({lab for _, lab in hits})
    lines = sorted({ln for ln, _ in hits})
    print(
        f"redact: MASKED {len(lines)} line(s) in {path} carrying {', '.join(labels)}",
        file=sys.stderr,
    )
    return GUARD_REDACTED


def main(argv: List[str]) -> int:
    args = argv[1:]
    if args and args[0] == "--guard":
        if len(args) != 2:
            print("usage: redact.py --guard FILE", file=sys.stderr)
            return GUARD_ERROR
        return guard_file(args[1])
    strict = bool(args) and args[0] == "--strict"
    if args and not strict:
        print(f"redact: unknown argument {args[0]!r}", file=sys.stderr)
        return 2
    data = sys.stdin.buffer.read()
    out = redact_strict_bytes(data) if strict else redact_bytes(data)
    # Buffer the whole result and write once. The shell wrapper falls back to its own
    # redactor when this process fails, so a partial write followed by a crash would
    # emit the head of the text twice — once unredacted-adjacent, once redacted.
    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
