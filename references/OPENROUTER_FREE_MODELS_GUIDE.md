# OpenCode + OpenRouter Free Models Guide (accurate)

This is the **rewritten** guide for agents and humans. Older drafts that mentioned
`opencode chat` / `opencode review` or Llama-3.1 free IDs are obsolete.

## Prerequisites

1. OpenRouter account + API key — https://openrouter.ai  
2. OpenCode installed — https://opencode.ai (`opencode --version`)  
3. Connect: `opencode providers login` → OpenRouter  

Optional env:

```bash
export OPENROUTER_API_KEY="sk-or-…"
```

## Free rate limits

| Credits purchased (all time) | Free RPM | Free RPD |
|------------------------------|----------|----------|
| &lt; $10 | 20 | 50 |
| ≥ $10 | 20 | **1000** |

Check:

```bash
curl -s -H "Authorization: Bearer $OPENROUTER_API_KEY" https://openrouter.ai/api/v1/key | jq .
# is_free_tier: false → 1000 free RPD
curl -s -H "Authorization: Bearer $OPENROUTER_API_KEY" https://openrouter.ai/api/v1/credits | jq .
```

## List free models

```bash
python3 scripts/list_free_models.py
# or
curl -s -H "Authorization: Bearer $OPENROUTER_API_KEY" \
  https://openrouter.ai/api/v1/models | jq -r '.data[]|select(.id|endswith(":free"))|.id'
```

## OpenCode usage (real CLI)

```bash
# headless one-shot
opencode run -m openrouter/nvidia/nemotron-3-ultra-550b-a55b:free "Reply OK"

# list models
opencode models openrouter | grep free

# continue last session
opencode run -c -m openrouter/nvidia/nemotron-3-nano-30b-a3b:free "Continue"
```

Config lives in `~/.config/opencode/opencode.json`. Auth in
`~/.local/share/opencode/auth.json`.

## BetterCallOpenCode skill wrappers

```bash
# preflight (shows free bucket 50 vs 1000)
bash scripts/opencode_review.sh --preflight --out /tmp/x.md --scope .

# critique via direct OpenRouter (1 free request)
bash scripts/opencode_review.sh \
  --prompt-file /tmp/prompt.md \
  --out BETTERCALLOPENCODE_REVIEW.md \
  --scope . \
  --backend or-api \
  --model openrouter/nvidia/nemotron-3-ultra-550b-a55b:free

# multi-model panel
bash scripts/multi_review.sh \
  --prompt-file /tmp/prompt.md \
  --out-dir ./reviews \
  --scope . \
  --preset coding-panel
```

## Paid models

Only when the user **explicitly** wants to spend credits:

```bash
bash scripts/opencode_review.sh ... \
  --model openrouter/anthropic/claude-sonnet-4.5 \
  --allow-paid
```

## Maximize free

See `maximize_free_usage.md`: pack once, stage concerns, multi-model with sleep,
prefer `or-api` over long agent loops, stop on 429.

## NVIDIA Nemotron free

Prefer **`nvidia/nemotron-3-ultra-550b-a55b:free`** (1M context) for single-pass reviews.
See `openrouter_free_models.md` for the full free table.

## Agent-to-agent (CLI without TUI)

Use **direct OpenRouter HTTP** (`or_client.py` / curl). Do not rely on the interactive TUI.
Maintain your own messages array for multi-turn; track tokens via `"usage":{"include":true}`.
