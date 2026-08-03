---
name: BetterCallOpenCode
description: >-
  Delegate an independent, critical code review to free OpenRouter models through
  OpenCode (or direct OpenRouter API) and then implement the feedback yourself.
  The remote model ONLY criticizes and PROPOSES; free-only by default (IDs ending
  :free). Supports single-model, multi-model panels, and staged short prompts for
  free rate limits. Use when the user says "better call opencode", "review with
  free OpenRouter", "multi free models", "Nemotron free review", or wants a second
  opinion from OpenCode/OpenRouter without spending credits unless they explicitly
  ask for paid models.
---

# BetterCallOpenCode

Get a second opinion from **free OpenRouter models** (via **OpenCode** or direct
API), then **you (Claude)** triage and implement. The critic proposes; Claude executes.

`SKILL_DIR` = the folder containing this file.

Read before changing flags:

- `references/opencode_notes.md` — real OpenCode CLI
- `references/openrouter_free_models.md` — free roster + limits + paid gate
- `references/maximize_free_usage.md` — splitting / multi-model / 50 vs **1000 RPD**

## Step 0 — Show the banner (FIRST, every invocation)

Emit with **ONE `printf`** (NOT `cat`):

```bash
[[ -f "$SKILL_DIR/assets/banner.txt" ]] && printf '%s\n' "$(< "$SKILL_DIR/assets/banner.txt")"
```

If missing, skip silently. Regenerate:

```bash
{ bash "$SKILL_DIR/scripts/show_header.sh"; cat "$SKILL_DIR/assets/better_call_opencode.txt"; } > "$SKILL_DIR/assets/banner.txt"
```

## Core principles

1. **Critic does not change the codebase — and neither backend is a sandbox.**
   - `or-api` (default) is a plain HTTPS POST to OpenRouter: no filesystem, no shell, no
     reach into your machine. The model sees only the packed text this skill sends.
   - `--backend opencode` runs an agent **locally**, pointed at a **filtered mirror** of
     the scope (`pack_context.py --mirror-to`), never the raw tree. Its
     `edit`/`bash`/`webfetch`/`task`/`external_directory` deny rules are re-verified
     against `opencode agent list` **before every run** — `RESULT=REFUSED`, nothing spent,
     if they do not resolve. It is restricted, **not sandboxed**: opencode still persists
     the reviewed source to `~/.local/share/opencode/{opencode.db,log,snapshot}`, outside
     the scope. See `references/opencode_notes.md` for the live measurements.
   - Both run under **your** `PATH`. A compromised `python3` or `opencode` defeats every
     control described here. Never pass `--auto`.
2. **Free-only by default.** Models must end with `:free` or be `openrouter/free`. Non-free → `RESULT=PAID_BLOCKED` unless the user **explicitly** asks to spend credits and you pass `--allow-paid` / `BCOPENCODE_ALLOW_PAID=1`.
3. **Be broad.** Intent-first; invent tests; do not hand over the suite to rubber-stamp.
4. **Respect free limits.** On AUTH/CAP/QUOTA: **STOP** — no retry loops. Preflight shows
   free bucket (**50 vs 1000 RPD**) and credits. The local cap is check-then-act per run —
   see the note in Mode C.
5. **Prefer `or-api`** (1 free request per review). Use `--backend opencode` only when agentic exploration is wanted.
6. **Single pass default.** Multi-model / staged only when asked or the tree is huge.

## What is guaranteed, and what is not

Say these plainly to the user if they ask what the skill protects:

- **Secrets are withheld best-effort, and what was withheld is always recorded.** The
  filter is a catalogue of vendor token shapes (`SECRET_CONTENT_RE` in
  `scripts/pack_context.py` is the authoritative list) plus an assignment heuristic that
  classifies the *value*, not just the key name. It covers whole-file names (`.env`,
  `*.pem`, `auth.json`, …) and hidden directories (`.ssh`, `.aws`, …).
  **Every new vendor prefix is a miss until it is patched, and adversarial encoding will
  get through.** The part that *is* guaranteed: every file withheld as a secret is listed
  in the pack metadata (`secrets_skipped_by_name` / `secrets_skipped_by_content`, both
  untruncated), so "not reviewed" is never indistinguishable from "reviewed and clean".
- **The finished report is scanned, and only high-confidence shapes are masked.** The
  skill's own diagnostics go through the full redactor (`scripts/redact.py`); the finished
  report — front matter, critic output and stderr blocks alike — is scanned with the
  **high-confidence** rules only (a vendor prefix plus 16+ opaque characters). A hit is
  masked in place, announced in a `[!CAUTION]` block inside the report, and reported as
  `SECRET_SCAN=MASKED` on stderr. On `MASKED`, **tell the user and tell them to rotate that
  credential.** On `SECRET_SCAN=UNVERIFIED` the scan could not run at all — say so, and tell
  them to read the report before committing it. The `RESULT=` word is deliberately unchanged
  in both cases: this is not a reason to retry and spend again.
  The heuristic rules are *not* applied to the critic's prose, because they fire on ordinary
  argument about credentials and would mangle the review.
- **Reviewed code leaves your machine either way.** `or-api` sends it to OpenRouter;
  `opencode` sends it to OpenRouter *and* leaves a copy in opencode's local state.
- Full statement: `SECURITY.md`.

## Configuration

```bash
# optional — else key is read from OpenCode auth.json
export OPENROUTER_API_KEY="sk-or-…"
export BCOPENCODE_MODEL="openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"
export BCOPENCODE_BACKEND="or-api"          # or opencode
export BCOPENCODE_CAP="200"                 # local daily skill cap
export BCOPENCODE_STATE_DIR="$HOME/.bettercallopencode"
# paid only after explicit user consent:
# export BCOPENCODE_ALLOW_PAID=1
```

Optional config files (KEY=VALUE only, **not** shell-sourced), user-owned locations only:
`~/.config/bettercallopencode/config.env` and `$SKILL_DIR/.bettercallopencode.env`.

A `.bettercallopencode.env` **inside the project under review is ignored** — the reviewed
repository is untrusted input and does not get to configure its own review. If one is
present you get a note on stderr.

Smoke:

```bash
bash "$SKILL_DIR/scripts/opencode_review.sh" --preflight --out /tmp/bcoc_pre.md --scope .
# expect RESULT=OK and free_rpd_bucket 50 or 1000
```

## Mode A — Critique (default)

1. **Scope** = project dir (or user paths). Avoid bulk data.
2. **Prompt**: copy `templates/critique_prompt.md` → unique temp:

```bash
BCOC_STATE="${BCOPENCODE_STATE_DIR:-$HOME/.bettercallopencode}"; mkdir -p "$BCOC_STATE"
PF="$(mktemp "$BCOC_STATE/bcoc.prompt.XXXXXX")"
# fill {{INTENT_DESCRIPTION}} and {{SCOPE_NOTES}}; portable mktemp (XXXXXX at end)
```

3. **Run**:

```bash
TS=$(date -u +%Y%m%dT%H%M%SZ)
bash "$SKILL_DIR/scripts/opencode_review.sh" \
  --prompt-file "$PF" \
  --out "<project>/BETTERCALLOPENCODE_REVIEW_$TS.md" \
  --scope "<dir>" \
  --mode critique \
  --backend or-api \
  --model openrouter/nvidia/nemotron-3-ultra-550b-a55b:free
rm -f "$PF"
```

4. Branch on last line `RESULT=<WORD>`.
5. **Triage** each finding; implement accepted ones; verify.

### Agentic OpenCode backend

```bash
bash "$SKILL_DIR/scripts/opencode_review.sh" ... --backend opencode
```

Requires `opencode` on PATH and OpenRouter connected. What actually happens:

1. The scope is scanned for **executable code under `.opencode/`** — extensions
   `js/cjs/mjs/jsx/ts/mts/cts/tsx/wasm/node`, at **any depth**. Present → `RESULT=REFUSED`,
   nothing spent.
2. `pack_context.py --mirror-to` builds a **filtered mirror** of the scope: same secret,
   binary, size and hidden-directory rules as the `or-api` pack. The agent is pointed at
   the mirror (`--dir`), never at your repo, and is not told the real path.
3. Agent `bcoc-review` is reinstalled from `agents/bcoc-review.md`, then
   `edit`/`bash`/`webfetch`/`task`/`external_directory` are checked against
   `opencode agent list` (`verify_agent_permissions.py`, last-global-match-wins, mode must
   be `all`). Not all deny → `RESULT=REFUSED`, nothing spent.
4. The run is `--pure` with `OPENCODE_DISABLE_PROJECT_CONFIG=1` and `OPENCODE_PERMISSION`
   set, under a wall-clock `timeout`.

> [!IMPORTANT]
> **Restricted, not sandboxed.** Measured on opencode 1.18.11 against a deliberately
> hostile repo: the critic could not edit files, create files, or run shell, and the repo
> could not re-enable those with its own `.opencode/` agent, `opencode.json` or
> `AGENTS.md`. Re-verify after an opencode upgrade — this is a measurement, not a
> contract the vendor offers. What is **not** covered:
> - opencode persists the reviewed source and prompts to
>   `~/.local/share/opencode/{opencode.db,log,snapshot}` — outside the scope, unencrypted.
> - `external_directory allow …/tool-output/*` is appended after the denies, so writes
>   there remain permitted.
> - Project plugins are executed by opencode **before** any agent, permission or model
>   exists, and no flag prevents it. The **mirror** is what keeps them out of reach
>   (it prunes hidden directories, so `.opencode` never reaches the tree opencode runs
>   against — `--dir <raw repo>` executes the plugin, `--dir <mirror>` does not). The
>   step-1 refusal is defence in depth for a future path that forgets the mirror.
> - Everything runs under your `PATH`.

Prefer `--backend or-api`: 1 free request per review, and genuinely no filesystem access.

## Mode B — Experiment

1. Sandbox: `<project>/BetterCallOpenCode/sandbox/<task>/` (you create it).
2. Prompt from `templates/experiment_prompt.md`.
3. Run with `--mode experiment`.
4. Write fenced scripts into the sandbox yourself.
5. **REVIEW GATE** before `run_local.sh` (not a security jail).
6. Run approved scripts; feed results back if needed.

## Mode C — Multi-model panel

Default path is **parallel under free RPM** (sliding window, OpenRouter **20 req/min**
on `:free`). Fires as many workers as the budget allows, then waits until slots free
before more HTTP calls — no fixed sleep between models.

```bash
bash "$SKILL_DIR/scripts/multi_review.sh" \
  --prompt-file "$PF" \
  --out-dir "<project>/BetterCallOpenCode/multi_$TS" \
  --scope "<dir>" \
  --preset coding-panel   # or fast-panel | nvidia-panel | --models id1,id2
# all live free models:
#   --all-free --rpm 20 --max-workers 10 --retry-quota
# legacy fixed sleep:
#   --sequential --sleep 3
```

See `scripts/rate_limit.py` + `scripts/panel_run.py`. Merge reports: prefer consensus
CRITICAL/HIGH; note disagreements. `RESULT=PARTIAL` means *some* models produced a
review — **name which ones failed**; a merged critique missing N of M opinions is not a
complete review.

> [!NOTE]
> **The cap is not atomic across separately launched runs.** Each run checks the ledger
> then acts, so two runs started at the same moment can both pass a check at N-1. The
> panel admits at most `cap - used` models up front, which covers the fan-out case; the
> RPM limiter is likewise in-process. For a strict cap, run one review at a time.

## Mode D — Staged / short prompts (free time limits)

Use `templates/chunk_prompt.md` + `--stages structure` (then `security`, `tests`),
or `scripts/split_scope.py` for file chunks. Keep each turn short; stop on TIMEOUT/QUOTA.

## RESULT handling

| RESULT | Action |
|--------|--------|
| OK | Triage / review gate |
| TRUNCATED | Hit `max_tokens` (`finish_reason=length`). Findings may be partial — **say so**. Raise `--max-tokens` (default 16384), shrink pack, or stage. Reasoning free models (e.g. gpt-oss) burn completion budget on thinking first.
| AUTH | Fix OpenRouter key / `opencode providers login` — STOP |
| CAP | Local skill cap — wait or raise `--cap` if user insists |
| QUOTA | Free RPD/RPM or 402/429 — **STOP**, wait (1000 RPD after $10 top-up; still 20 RPM) |
| TIMEOUT | Smaller pack / stages; retry **once** |
| PAID_BLOCKED | Switch to `:free` or get explicit paid consent |
| REFUSED | An `opencode`-backend safety gate refused **before spending anything** — the scope ships executable code under `.opencode/`, or the agent's deny rules did not resolve. Read the stderr reason. **Do not** work around it; switch to `--backend or-api` and tell the user why |
| BAD_ARGS | A bad argument or a failed pre-flight gate. **Nothing was spent** — fix the invocation and re-run freely |
| UNREACHABLE | Either the request never left your machine (DNS/TCP/TLS failure) or OpenRouter answered **502/503/504**. Not billed. Retry **once**; if it persists the provider is down |
| ERROR | The request was rejected or came back unusable — including a 404/405, which means the **model id is wrong or retired** (the API *did* answer). Check the id against `list_free_models.py` before retrying |
| INTERRUPTED | Ctrl-C / SIGTERM. The request was already launched, so it is **counted as billed** — the answer was lost, not the quota |
| PARTIAL | Multi-model panel: at least one model produced a review and at least one failed. **Name which ones** in your summary — a merged critique missing N of M opinions is not a complete review. If *every* model fails you get that failure's word, not PARTIAL |

The `opencode` backend classifies from the exit code and stderr only, so it emits just
`OK`/`TIMEOUT`/`QUOTA`/`AUTH`/`ERROR`/`REFUSED` — never `TRUNCATED` or `UNREACHABLE`.
Those two come from the `or-api` client's HTTP layer.

## Report back

Summarize accepted vs rejected; scripts run; report path(s); **usage today vs cap**
(`scripts/opencode_usage.sh`); free bucket if preflight ran; which models replied.

If the report header shows `secrets_withheld=N`, say so: those files were **not reviewed**,
and the user may want to review them another way. Note that `opencode_usage.sh` counts
*all* ledger rows for the day, while the cap counts only the rows marked `billed`.

## Sibling skills

- [BetterCallChatGPT](https://github.com/douglasadamoski/BetterCallChatGPT) — Codex  
- [BetterCallGemini](https://github.com/douglasadamoski/BetterCallGemini) — agy / Gemini  
- [BetterCallGrok](https://github.com/douglasadamoski/BetterCallGrok) — grok  
- BetterCallMyAI — self-hosted vLLM/Ollama  

## Notes

- Default model: **Nemotron 3 Ultra free** (1M context).
- Ledger: `${BCOPENCODE_STATE_DIR:-~/.bettercallopencode}/usage.jsonl`
- Never commit API keys. Never pass `opencode --auto`.
