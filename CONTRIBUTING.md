# Contributing to BetterCallOpenCode

Contributions are welcome. Two invariants below have each been broken repeatedly, in ways
that are invisible until something has already gone wrong — please read those first.

## Before anything else

```bash
python3 -m pytest -q tests/                            # fully offline; nothing reaches the network
for f in scripts/*.sh; do bash -n "$f"; done           # every script must parse
shellcheck -S warning --exclude=SC2034 scripts/*.sh    # this is the blocking level in CI
bash scripts/scan_secrets.sh                           # scans what is actually committed
```

Style- and info-level shellcheck findings are advisory. `SC2034` is excluded from the
blocking pass because `scripts/_bcoc_common.sh` is a sourced library whose variables are
consumed by callers shellcheck analyses separately — it still shows in the advisory pass, so
check there before dismissing one.

CI runs the secret scan **first**, on purpose: this project's whole promise is that it does
not leak keys.

No test may reach OpenRouter. A networked test silently spends the maintainer's free daily
requests, and it fails for every contributor without a key. Everything that would otherwise
need the network is driven to a gate that refuses first, or uses an injected clock.

---

## Invariant 1 — never write a string that `pack_context.py`'s own patterns match

`scripts/pack_context.py` decides which files leave the machine. It scans file *contents*
for credential shapes. So the moment a file in this repository contains a literal string
matching one of those patterns, the packer withholds **that file** from every review of this
repository — including the security-critical file you were trying to get reviewed.

This has happened **more than ten times** — the running count lives in the header comment of
`scripts/pack_context.py`. It has withheld the secret filter itself, the test suite that
covers the secret filter, the redactor, `SKILL.md`, and two ordinary source files whose only
crime was a type annotation (`api_key: str) -> Dict[str, str]:` matches a password-shaped key
followed by a spaced value). The invariant applies to every file in the repository, but most
sharply to `scripts/pack_context.py`, `scripts/redact.py` and `scripts/scan_secrets.sh`,
whose whole job is to describe what a credential looks like.

The failure is silent. Nothing errors. The file just quietly stops being reviewed.

**What to do instead:**

| Instead of | Write |
|---|---|
| A literal vendor token in a pattern list | Concatenated fragments: `b"PuTTY-" + b"User-Key-File"` |
| A literal example in a comment or doc | Prose describing the shape, or a placeholder ending in a UTF-8 ellipsis (`…`) |
| A credential fixture in a test | Build it at runtime — see `_assign()` and the `FAKE_*` constants in `tests/test_pack_secrets.py` |
| An example assignment in Markdown | Give the value an ellipsis or an obvious placeholder shape (`<your-key>`, `${VAULT_SECRET}`) |

Watch out for prose too. A Markdown line consisting of a password-shaped word, a colon, and
a dozen or more characters of ordinary text is an assignment as far as the scanner is
concerned — and this paragraph had to be rewritten once because its own example tripped it.
Prefer a table cell, or a sentence that does not put a colon straight after the key word.

**Enforcement:** `tests/test_pack_secrets.py::test_packer_does_not_refuse_its_own_source`
packs this whole repository and asserts that nothing was withheld by content and that the
critical files are present. If you break the invariant, that test tells you which file.
Run it before opening a PR.

---

## Invariant 2 — the RESULT contract is API

Every entrypoint's **last stdout line** is `RESULT=<WORD>`, on **every** exit path: success,
`--help`, every gate refusal, a failed pack, an unwritable ledger, `SIGINT`, `SIGTERM`, and
an unhandled exception in the panel runner.

This is not a formatting preference. `SKILL.md` instructs Claude to branch on that line, so
a path that exits without one reads as a crash — and the documented response to a crash is a
retry, which spends another request. An audit reproduced **eight** such paths, three of them
firing *after* the request had already been paid for.

Rules for any change that touches an exit path:

1. **Print `RESULT=` before you exit.** Not after a `mv`, not after a ledger append that
   might fail. Anything that can fail goes before the line, or is guarded so it cannot abort
   the process.
2. **The vocabulary is closed.** `OK`, `TRUNCATED`, `AUTH`, `CAP`, `QUOTA`, `TIMEOUT`,
   `UNREACHABLE`, `ERROR`, `PAID_BLOCKED`, `REFUSED`, `PARTIAL`, `BAD_ARGS`, `INTERRUPTED`.
   Adding a word, or changing what one means, is a **breaking change**: bump the major
   version, update the table in `SKILL.md`, and update `DOCUMENTED` in
   `tests/test_result_contract.py`.
3. **A word means one thing.** `UNREACHABLE` means the model was not reached, not that a
   model id was wrong — that distinction is the difference between "check your network" and
   "check your spelling". `REFUSED` means a gate stopped the run *before* spending.
   `PARTIAL` means *some* models produced a review; if none did, return the failure.
4. **`billed` is not the same as `RESULT`.** A row counts against the cap when the model was
   actually invoked. A `TIMEOUT` is billed — the request ran, the answer was lost. Bias
   toward over-counting: under-counting spends the user's real quota.

**Enforcement:** `tests/test_result_contract.py` drives every gate, both signals and the
documentation consistency check. A doc test asserts the `SKILL.md` table lists every word
the scripts can emit, so a new word with no table row fails CI.

---

## Documentation is held to the same standard as code

This project shipped a README claiming the opencode critic ran write-denied while
`opencode agent list` showed `permission "*": allow`. That is the defect class this repo
takes most seriously.

- **Every guarantee in the docs must name the code that enforces it.** If you cannot point
  at the enforcement, reword the claim.
- **Where a guarantee is partial, say where it stops.** "Best-effort withholding, and it
  always records what it withheld" is the honest description of the secret filter;
  "withholds secrets" is not.
- **A measurement is not a contract.** `references/opencode_notes.md` records the exact
  opencode version, the exact command, and the observed result. Re-measure after an upgrade;
  do not inherit a conclusion.
- **Verify with the command the skill actually runs.** A cheap proxy (`opencode agent list`)
  once gave the exact opposite answer to the real one (`opencode run`), and a whole section
  of the notes had to be retracted.

## Style

- Bash: `set -uo pipefail`, no `set -e` in the entrypoints (see the header of
  `scripts/opencode_review.sh` for why). Floor is bash 4.4.
- Python: stdlib only in the runtime scripts. `pytest` is the only test dependency.
- Comments explain *why*, especially where the obvious implementation was tried and was
  wrong. Most of the comments in this repo are a record of a bug; keep them when you edit
  the code around them.

## Reporting a vulnerability

See [`SECURITY.md`](SECURITY.md). Please do not open a public issue for one.
</content>
