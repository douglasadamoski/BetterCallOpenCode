<div align="center">

<img src="assets/BetterCallOpenCode_black.png" alt="BetterCallOpenCode" width="760">


# ⚖ BetterCallOpenCode ⚖

**Get a second opinion on your code from free OpenRouter models — then let Claude do the work.**
Route reviews through **OpenCode** or the **OpenRouter API**. Critics only *criticize* and
*propose*. Free models (`:free`) by default — including **NVIDIA Nemotron 3 Ultra** (1M context).
Claude triages findings and implements the rest.

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

Siblings: [BetterCallChatGPT](https://github.com/douglasadamoski/BetterCallChatGPT) ·
[BetterCallGemini](https://github.com/douglasadamoski/BetterCallGemini) ·
[BetterCallGrok](https://github.com/douglasadamoski/BetterCallGrok) ·
[BetterCallMyAI](https://github.com/douglasadamoski/BetterCallMyAI)

</div>

## 🚀 Let's get the job done

### 1️⃣ Connect OpenRouter (free models)

1. Create an API key at [openrouter.ai/settings/keys](https://openrouter.ai/settings/keys)
2. Install [OpenCode](https://opencode.ai) and connect:

```bash
opencode providers login    # choose OpenRouter, paste key
# optional:
export OPENROUTER_API_KEY="sk-or-…"
```

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

```
/plugin marketplace add douglasadamoski/BetterCallOpenCode
/plugin install better-call-opencode@bettercallopencode
```

Or clone:

```bash
git clone https://github.com/douglasadamoski/BetterCallOpenCode.git ~/.claude/skills/BetterCallOpenCode
```

### 3️⃣ Call it

```
/BetterCallOpenCode
```

…or say *"better call opencode on this folder"*, *"multi free model review"*, *"Nemotron free critique"*.

## Why

Vendor critics are great until you want **zero marginal cost** multi-model opinions. BetterCallOpenCode
defaults to OpenRouter **`:free`** models (Nemotron Ultra/Super/Nano, Gemma 4 free, gpt-oss free, …),
packs the scope (or runs a write-denied OpenCode agent), and leaves decisions with Claude.

| Mode | What the free model does | What Claude does |
|------|--------------------------|------------------|
| **A — Critique** | Prioritized findings over packed (or agent-explored) code | Triage, implement, verify |
| **B — Experiment** | Proposes scripts as fences | Review gate → `run_local.sh` |
| **C — Multi-model** | Same prompt → N free models | Merge / vote findings |
| **D — Staged** | Short concern- or chunk-scoped turns | Assemble full review |

## Free-only gate

Non-`:free` models return `RESULT=PAID_BLOCKED` unless you **explicitly** allow paid spend
(`--allow-paid` / `BCOPENCODE_ALLOW_PAID=1` after the user asked for paid models).

Default model: `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free`.

## Requirements

- [Claude Code](https://claude.com/claude-code) (skill host)
- `python3` (stdlib HTTP client — no pip package required for Mode A)
- OpenRouter API key (env or OpenCode auth)
- Optional: `opencode` on PATH for `--backend opencode`

## Security notes

- Keys only in env / OpenCode auth / local config files — never git or reports.
- Packer skips secret-shaped files; free-only gate prevents accidental billable models.
- Mode B: **you** review scripts before `run_local.sh` (not a jail).
- Residual risk: code in the prompt is sent to OpenRouter/upstream — use only for code you accept sharing with that path.

## Layout

```
SKILL.md                      # Claude Code skill
scripts/opencode_review.sh    # main entry (RESULT= last line)
scripts/or_client.py          # OpenRouter client + free gate
scripts/multi_review.sh       # multi-model fan-out
scripts/pack_context.py       # scope packer
templates/                    # critique / experiment / chunk prompts
references/                   # free models, OpenCode notes, maximize free
agents/bcoc-review.md         # write-denied OpenCode agent
assets/                       # banner art
```

## Docs

- [OpenRouter free models & limits](references/openrouter_free_models.md)
- [OpenCode CLI notes](references/opencode_notes.md)
- [Maximize free usage](references/maximize_free_usage.md)
- [Full free-models guide](references/OPENROUTER_FREE_MODELS_GUIDE.md)

## License

[GPL-3.0](LICENSE)
