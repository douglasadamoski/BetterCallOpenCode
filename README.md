<div align="center">

<img src="assets/BetterCallOpenCode_black.png" alt="BetterCallOpenCode" width="760">


# ⚖ BetterCallOpenCode ⚖

**Get a second opinion on your code from free OpenRouter models — then let Claude do the work.**
Route reviews through the **OpenRouter API** or a local **OpenCode** agent. Critics only
*criticize* and *propose*. Free models (`:free`) by default — including **NVIDIA Nemotron 3
Ultra** (1M context). Claude triages findings and implements the rest.

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

Siblings: [BetterCallChatGPT](https://github.com/douglasadamoski/BetterCallChatGPT) ·
[BetterCallGemini](https://github.com/douglasadamoski/BetterCallGemini) ·
[BetterCallGrok](https://github.com/douglasadamoski/BetterCallGrok)

</div>

## 🚀 Let's get the job done

### 1️⃣ Connect OpenRouter (free models)

1. Create an API key at [openrouter.ai/settings/keys](https://openrouter.ai/settings/keys)
2. Give it to the skill either way — env var, or OpenCode's credential store:

```bash
export OPENROUTER_API_KEY="sk-or-…"          # simplest
opencode providers login                     # or: choose OpenRouter, paste key
```

`opencode` is **optional**. The default backend is a plain HTTPS call and needs nothing but
`python3`.

**Free limits:** 20 requests/minute on `:free` models. **50 free requests/day** until you have
purchased **≥ $10** in OpenRouter credits (all time); then **1000 free requests/day**.
Those $10 are **not spent** by free models — they only unlock the higher free bucket (and pay for
paid models if you use them later).

Smoke:

```bash
python3 scripts/or_client.py --preflight --model openrouter/nvidia/nemotron-3-ultra-550b-a55b:free
# free_rpd_bucket: 50 or 1000
```

### 2️⃣ Put this skill in Claude Code

**As a plugin** (recommended):

```
/plugin marketplace add douglasadamoski/BetterCallOpenCode
/plugin install better-call-opencode@bettercallopencode
```

**As a skill directory:**

```bash
git clone https://github.com/douglasadamoski/BetterCallOpenCode.git \
  ~/.claude/skills/BetterCallOpenCode
```

**Standalone**, with no Claude Code at all — the scripts are a usable CLI on their own:

```bash
git clone https://github.com/douglasadamoski/BetterCallOpenCode.git && cd BetterCallOpenCode
bash scripts/opencode_review.sh --preflight --scope .
```

### 3️⃣ Call it

```
/BetterCallOpenCode
```

…or say *"better call opencode on this folder"*, *"multi free model review"*, *"Nemotron free critique"*.

## Why

Vendor critics are great until you want **zero marginal cost** multi-model opinions. BetterCallOpenCode
defaults to OpenRouter **`:free`** models (Nemotron Ultra/Super/Nano, Gemma 4 free, gpt-oss free, …),
packs the scope and sends it to a plain HTTP endpoint, and leaves every decision with Claude.

| Mode | What the free model does | What Claude does |
|------|--------------------------|------------------|
| **A — Critique** | Prioritized findings over packed (or agent-explored) code | Triage, implement, verify |
| **B — Experiment** | Proposes scripts as fences | Review gate → `run_local.sh` |
| **C — Multi-model** | Same prompt → N free models | Merge / vote findings |
| **D — Staged** | Short concern- or chunk-scoped turns | Assemble full review |

## What the critic can and cannot do

Two backends, two different answers. **Neither is a sandbox.**

**`--backend or-api` (default)** — an HTTPS POST to OpenRouter. There is no filesystem, no
shell and no agent: the model sees exactly the text `pack_context.py` produced and nothing
else. Your code still leaves the machine, so this is a *containment* property, not a privacy one.

**`--backend opencode`** — an agent runs **on your machine**, and four things constrain it,
each checked rather than assumed:

1. **Filtered mirror.** The agent is pointed at a copy of the scope built by
   `pack_context.py --mirror-to`, under the same secret/binary/size/hidden-directory rules as
   the packed prompt. It never sees the raw tree, and is not told where the raw tree is.
2. **Verified permissions.** `agents/bcoc-review.md` denies `edit`, `bash`, `webfetch`, `task`
   and `external_directory`; before every run `verify_agent_permissions.py` re-parses
   `opencode agent list` and confirms each one resolved to deny globally, with mode `all`.
   If not — `RESULT=REFUSED`, and nothing is spent.
3. **No project config.** `--pure` plus `OPENCODE_DISABLE_PROJECT_CONFIG=1`, so the reviewed
   repo cannot supply its own agent, `opencode.json` or `AGENTS.md`.
4. **Scope refusal.** A scope shipping executable code under `.opencode/` is refused outright.

> [!IMPORTANT]
> Measured on opencode 1.18.11 against a deliberately hostile repo — a same-named permissive
> agent, an `opencode.json` granting edit, and an `AGENTS.md` prompt injection — the critic
> created no file, modified no file and ran no shell. That is a **measurement, not a vendor
> guarantee**: re-verify after an opencode upgrade. And three things are simply not covered:
> opencode persists the reviewed source and prompts to
> `~/.local/share/opencode/{opencode.db,log,snapshot}` (outside the scope, unencrypted);
> writes into opencode's own `tool-output/` stay permitted; and everything here runs under
> your `PATH`, so a compromised `python3` or `opencode` defeats all of it.
> Experiments: [`references/opencode_notes.md`](references/opencode_notes.md).

## Free-only gate

Non-`:free` models return `RESULT=PAID_BLOCKED` unless you **explicitly** allow paid spend
(`--allow-paid` / `BCOPENCODE_ALLOW_PAID=1` after the user asked for paid models).
The rule is implemented twice — in bash and in Python — and
`tests/test_gate_parity.py` drives every model id through both, so the two cannot drift apart
unnoticed.

Default model: `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free`.

## Requirements

- [Claude Code](https://claude.com/claude-code) — to use it as a skill (the scripts also run standalone)
- **bash ≥ 4.4** — checked at startup; macOS ships 3.2, so `brew install bash` and run with that one
- `python3` (stdlib only — no pip install for Mode A) and GNU `timeout` (or `gtimeout`,
  `brew install coreutils`)
- An OpenRouter API key, in the environment or in OpenCode's `auth.json`
- Optional: `opencode` on PATH, for `--backend opencode`
- Optional: `setsid` — without it the wrapper cannot signal the whole process group on Ctrl-C,
  which it degrades to gracefully

## Usage

```bash
bash scripts/opencode_review.sh \
  --prompt-file <filled-template.md> \
  --out ./BETTERCALLOPENCODE_REVIEW_$(date -u +%Y%m%dT%H%M%SZ).md \
  --scope . \
  --backend or-api \
  --model openrouter/nvidia/nemotron-3-ultra-550b-a55b:free
```

The **last stdout line** is the contract, on every exit path including `--help`, every gate
refusal and Ctrl-C:

`RESULT=OK` · `TRUNCATED` · `AUTH` · `CAP` · `QUOTA` · `TIMEOUT` · `UNREACHABLE` · `ERROR` ·
`PAID_BLOCKED` · `REFUSED` · `PARTIAL` · `BAD_ARGS` · `INTERRUPTED`

`TRUNCATED` means the model hit its completion ceiling, so the review is **incomplete** —
common on free reasoning models, which spend the budget thinking first. `REFUSED` means a
safety gate stopped the run before anything was spent. `PARTIAL` (panel only) means some
models produced a review and some did not — name the ones that failed. The full table, with
what to do about each, is in [`SKILL.md`](SKILL.md).

Preflight costs nothing and needs no prompt:

```bash
bash scripts/opencode_review.sh --preflight --scope .
```

Multi-model panel, parallel under the free 20 RPM limit:

```bash
bash scripts/multi_review.sh --prompt-file <prompt.md> --out-dir ./panel --scope . \
  --preset coding-panel        # or --all-free / --models a:free,b:free
```

## Quota

Every call is appended to `~/.bettercallopencode/usage.jsonl` with an explicit `billed`
field, and the local daily cap (default **200**, well under the 1000 RPD bucket) counts that
rather than guessing from the outcome. Calls rejected before they reach the model — auth
failure, paid-model block, a gate refusal — are recorded unbilled and do not count. A run
interrupted mid-flight *is* counted, because it was already charged.

```bash
bash scripts/opencode_usage.sh            # today, by RESULT and by model
```

> [!NOTE]
> The cap is **not atomic across separately launched runs.** Each run reads the ledger and
> then acts, so two runs started at the same instant can both pass a check at N−1. The panel
> runner admits at most `cap − used` models up front, which covers the fan-out case, and the
> RPM limiter is in-process only. For a strict cap, run one review at a time.
> `opencode_usage.sh` prints *all* rows for the day; the cap counts only the billed ones.

## Layout

```
SKILL.md                            # the brain: what Claude does, step by step
scripts/opencode_review.sh          # main entry — last stdout line is RESULT=
scripts/_bcoc_common.sh             # config load, ledger, model gate, redaction
scripts/or_client.py                # OpenRouter HTTP client + free gate + classification
scripts/pack_context.py             # scope packer AND the filtered mirror builder
scripts/redact.py                   # the one credential redactor + the report guard
scripts/multi_review.sh             # multi-model fan-out (delegates to panel_run.py)
scripts/panel_run.py                # parallel panel, cap admission, merged index
scripts/rate_limit.py               # sliding-window free-RPM limiter
scripts/split_scope.py              # chunk a huge tree for Mode D
scripts/verify_agent_permissions.py # parses `opencode agent list`; refuses if not deny
scripts/run_local.sh                # Claude runs an APPROVED, critic-proposed script
scripts/opencode_usage.sh           # ledger summary
scripts/list_free_models.py         # live :free roster
agents/bcoc-review.md               # the OpenCode critic agent (mode: all + deny blocks)
opencode-config/                    # OPENCODE_CONFIG_DIR the skill points opencode at
templates/                          # critique / experiment / chunk prompts
references/                         # free models, verified OpenCode notes, maximize free
tests/                              # fully offline test suite; nothing reaches the network
assets/                             # banner art
state/                              # runtime: ledger, run dirs, mirrors (gitignored)
```

## Configuration

Env vars, or `KEY=VALUE` lines in `~/.config/bettercallopencode/config.env` or
`<skill>/.bettercallopencode.env`. Config files are **parsed, never sourced**, and only
user-owned locations are read — a `.bettercallopencode.env` inside the project under review
is ignored, loudly, because the repo being reviewed does not get to configure its own review.

| Knob | Where | Default |
|---|---|---|
| Model | `--model` / `$BCOPENCODE_MODEL` | `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free` |
| Backend | `--backend` / `$BCOPENCODE_BACKEND` | `or-api` (or `opencode`) |
| Daily call cap | `--cap` / `$BCOPENCODE_CAP` | `200` |
| Completion ceiling | `--max-tokens` / `$BCOPENCODE_MAX_TOKENS` | `16384` |
| Reasoning budget | `$BCOPENCODE_REASONING_MAX_TOKENS` | `2048` (`0` = provider default) |
| Pack size ceiling | `--max-input-tokens` / `$BCOPENCODE_MAX_INPUT_TOKENS` | `80000` |
| Wall-clock timeout | `--timeout` / `$BCOPENCODE_TIMEOUT` | `600` seconds |
| Sampling temperature | `--temperature` / `$BCOPENCODE_TEMPERATURE` | `0.2` |
| Free RPM budget | `--rpm` / `$BCOPENCODE_FREE_RPM` | `20` |
| State + ledger dir | `$BCOPENCODE_STATE_DIR` | `~/.bettercallopencode` |
| Paid models | `--allow-paid` / `$BCOPENCODE_ALLOW_PAID` | off — requires explicit user consent |
| OpenCode agent | `$BCOPENCODE_AGENT` | `bcoc-review` |
| Keep the run dir | `$BCOPENCODE_KEEP_RUN` | off (it holds a plaintext copy of your source) |
| Mode B conda env | `--env` / `$BCOPENCODE_CONDA_ENV` | `base` |

## Safety notes

> [!IMPORTANT]
> **The secret filter is best-effort withholding with guaranteed recording.** It knows a
> catalogue of vendor token shapes (OpenRouter, Anthropic, OpenAI, Stripe, GitHub, GitLab,
> Slack, Google, AWS, Azure connection strings, JWTs, PEM and PuTTY private keys, URLs
> carrying userinfo — `SECRET_CONTENT_RE` in `scripts/pack_context.py` is the authoritative
> list), a set of secret-shaped filenames (`.env` in every spelling, `*.pem`, `*.key`,
> `auth.json`, `kubeconfig`, `*.tfvars`,
> …), and an assignment heuristic that classifies the *value* rather than the key name. Hidden
> directories are pruned wholesale, which is what actually keeps `.ssh`, `.aws` and `.gnupg`
> out. **A vendor prefix nobody has added yet is a miss, and adversarial encoding will get
> through.** What *is* guaranteed is the bookkeeping: every file withheld as a secret is listed
> in the pack metadata and counted in the report header, so "not reviewed" is never
> indistinguishable from "reviewed and clean". Fifteen free models reviewing this repo's own
> source leaked zero real credentials — that is evidence, not proof.

- Keys live in the environment, OpenCode's `auth.json`, or a user-owned config file — never in
  git. One redactor (`scripts/redact.py`) masks the skill's own diagnostics; the **finished
  report** is then scanned with the high-confidence rules only — a vendor prefix plus 16+
  opaque characters — and a hit is masked in place with a `[!CAUTION]` block and
  `SECRET_SCAN=MASKED` on stderr. Deliberately narrow: the heuristic rules fire on ordinary
  argument *about* credentials, and a report that has been mangled is not a review. A
  scan-only warning would not have been a control either, since the secret would still be
  sitting in a file you are about to commit.
- `--scope /`, `--scope $HOME` and the other broad roots are refused outright. The filter is a
  heuristic, and pointing it at a home directory is not a thing a heuristic should be trusted with.
- Mode B: **you** review each proposed script before `run_local.sh`. It only checks that the
  script lives under the sandbox directory — it is **not** a jail.
- Residual risk, unavoidable and worth stating: the code in the prompt is sent to OpenRouter and
  its upstream provider. Use this only for code you accept sharing over that path.
- Full statement, and how to report a vulnerability: [`SECURITY.md`](SECURITY.md).

## Docs

- [OpenRouter free models & limits](references/openrouter_free_models.md)
- [OpenCode CLI notes](references/opencode_notes.md) — verified flags and live experiments
- [Maximize free usage](references/maximize_free_usage.md)
- [Full free-models guide](references/OPENROUTER_FREE_MODELS_GUIDE.md)
- [Changelog](CHANGELOG.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

## License

[GPL-3.0](LICENSE) © 2026 Douglas Adamoski.
