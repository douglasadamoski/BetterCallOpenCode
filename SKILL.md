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

1. **Critic does not change the codebase.** `or-api` is a plain HTTP endpoint: no
   filesystem, no shell, no reach into your machine — it sees only the packed text this
   skill sends. `--backend opencode` runs an agent locally, restricted by an agent file
   whose deny rules are **verified resolved before every run** (`RESULT=REFUSED` if not).
   It is restricted, **not sandboxed** — see `references/opencode_notes.md` for exactly
   what that does and does not cover. Never pass `--auto`.
2. **Free-only by default.** Models must end with `:free` or be `openrouter/free`. Non-free → `RESULT=PAID_BLOCKED` unless the user **explicitly** asks to spend credits and you pass `--allow-paid` / `BCOPENCODE_ALLOW_PAID=1`.
3. **Be broad.** Intent-first; invent tests; do not hand over the suite to rubber-stamp.
4. **Respect free limits.** On AUTH/CAP/QUOTA: **STOP** — no retry loops. Preflight shows free bucket (**50 vs 1000 RPD**) and credits.
5. **Prefer `or-api`** (1 free request per review). Use `--backend opencode` only when agentic exploration is wanted.
6. **Single pass default.** Multi-model / staged only when asked or the tree is huge.

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

Requires `opencode` on PATH and OpenRouter connected. Uses agent `bcoc-review`, whose
`edit`/`bash`/`webfetch`/`task` deny rules are checked against `opencode agent list`
before the run — if they do not resolve, you get `RESULT=REFUSED` and nothing is spent.

> [!IMPORTANT]
> **Restricted, not sandboxed.** Verified on opencode 1.18.11: the critic cannot edit
> files, run shell, or fetch the web, and a reviewed repo cannot re-enable those via its
> own `.opencode/` agent or `opencode.json`. Two things remain true anyway:
> - opencode persists the reviewed source and prompts to
>   `~/.local/share/opencode/{opencode.db,log,snapshot}` — outside the scope, unencrypted.
> - A repo shipping `.opencode/plugin/*.js` gets that code **executed** by opencode
>   before any permission layer exists. No flag prevents it, so the skill **refuses**
>   such a scope (`RESULT=REFUSED`). Review those with `--backend or-api`.

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
| REFUSED | A safety gate refused **before spending anything** — the scope ships `.opencode/plugin/*.js` (opencode would execute it), or the agent's deny rules did not resolve. Read the stderr reason. **Do not** work around it; switch to `--backend or-api` and tell the user why |
| BAD_ARGS | A bad argument or a failed pre-flight gate. **Nothing was spent** — fix the invocation and re-run freely |
| UNREACHABLE | Reached OpenRouter but not the model — network failure, or upstream 502/503/504. Not billed. Retry **once**; if it persists the provider is down |
| ERROR | The request was rejected or came back unusable — including a 404/405, which means the **model id is wrong or retired**. Check the id against `list_free_models.py` before retrying |
| INTERRUPTED | Ctrl-C / SIGTERM. The request was already launched, so it is **counted as billed** — the answer was lost, not the quota |
| PARTIAL | Multi-model panel: some models failed. **Name which ones** in your summary — a merged critique missing N of M opinions is not a complete review |

## Report back

Summarize accepted vs rejected; scripts run; report path(s); **usage today vs cap**
(`scripts/opencode_usage.sh`); free bucket if preflight ran; which models replied.

## Sibling skills

- [BetterCallChatGPT](https://github.com/douglasadamoski/BetterCallChatGPT) — Codex  
- [BetterCallGemini](https://github.com/douglasadamoski/BetterCallGemini) — agy / Gemini  
- [BetterCallGrok](https://github.com/douglasadamoski/BetterCallGrok) — grok  
- BetterCallMyAI — self-hosted vLLM/Ollama  

## Notes

- Default model: **Nemotron 3 Ultra free** (1M context).
- Ledger: `${BCOPENCODE_STATE_DIR:-~/.bettercallopencode}/usage.jsonl`
- Never commit API keys. Never pass `opencode --auto`.
