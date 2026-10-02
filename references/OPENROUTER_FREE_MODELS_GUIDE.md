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

Use **direct OpenRouter HTTP** (`or_client.py` / curl), or hand a task to a model with
`scripts/delegate.py`. Do not rely on the interactive TUI. Everything below is implemented
in the scripts; the raw `curl` forms are given so you can see what goes over the wire.

### Request fields that matter

| Need | Field | In this skill |
|---|---|---|
| Token + cost accounting | `"usage": {"include": true}` | always sent (OpenRouter only) |
| Machine-readable output | `"response_format": {"type": "json_object"}` | `delegate.py --json-mode` — only if the model's `supported_parameters` lists `response_format`; otherwise the request may 400 |
| Reasoning budget | `"reasoning": {"enabled": false}` or `{"effort": "low"}` | `BCOPENCODE_REASONING_EFFORT` (default `none`; see SKILL.md for the measurements) |
| Vision | `content: [{"type":"text",…},{"type":"image_url","image_url":{"url":"data:image/png;base64,…"}}]` | `delegate.py --image` — only to models whose `modalities` include `image` |
| Multi-turn | resend the `messages` array | `delegate.py --session ID` (state under `~/.bettercallopencode/sessions/`, mode 0600) |
| Attribution headers | `HTTP-Referer`, `X-Title` | sent by `or_client.py` |

### Files, code and PDFs

The model sees only the text you send. Code: pack it with `pack_context.py` (the same
secret filter every review uses) — do not `cat` files into a prompt by hand. PDFs: extract
text first (`pdftotext -layout file.pdf -`; `delegate.py --pdf` does this and passes the
text through the redactor). Images: base64 as above, vision models only.

### Fallback and retries

On `429` (rate limit) or a retired model `404`, switch to **another zero-cost model** rather
than hammering the first one: `delegate.py --fallback auto` (or an explicit list), capped
by `--max-attempts` (default 3).

- **Do not retry or fall back on** `401/403` (fix the key), `402` (out of credit — a
  different model will not help), a truncated answer, a timeout, or a paid-model block.
- A fallback never crosses the cost gate: a zero-cost request cannot land on a paid model.
- Free-tier limits are per **account**, not per model, so rotating models does not buy extra
  quota — it only routes around an upstream provider that is saturated.
- Several workers share one per-minute budget through `~/.bettercallopencode/rpm.jsonl`
  (`--rpm`, default 20 for OpenRouter free).

### Constraining the output

A system prompt asking the model not to emit files or paths is a *request*, not a control.
The controls that actually hold are structural: the `or-api` backend has no tools at all,
and the `opencode` backend refuses to run unless the agent's deny rules verifiably resolve.
`delegate.py` writes only its own result file.

### What the original draft got wrong (and was not carried over)

An earlier draft of this guide (kept outside the repo as a historical file) described a
CLI and config that do not exist. It was checked claim by claim against opencode 1.18 and
2.0.22 and OpenRouter, and these parts were **dropped**:

- `opencode chat`, `opencode review`, `--format json` on `chat`, `--provider` — not
  commands/flags. The real headless command is `opencode run`.
- A `providers`/`api_base`/`models.free[]` block in `opencode.json` — not the config schema.
  Connect OpenRouter with `opencode providers login` (v1) / `opencode auth login` (v2).
- `OPENCODE_MODEL`, `DEFAULT_MODEL`, `FORCE_JSON`, `NO_FILE_GENERATION` and similar
  environment variables — nothing reads them.
- Every model id it listed (`llama-3.1-*:free`, `mistral-7b-*:free`, `phi-3-*:free`,
  `deepseek-chat-v3:free`, …) — retired. Use `list_free_models.py` or
  `select_models.py --list` for the live roster.
- `~20 req/min, ~1000 req/day` as a flat rule — it is 20/min, and 50/day **or** 1000/day
  depending on credit history (preflight shows which bucket).
- Sending files by `cat`-ing them into JSON by hand, and an `auto_review.sh` that did so
  without any secret filter.

The parts that held up (fallback on 429, `usage.include`, JSON mode, vision, PDF-as-text,
multi-turn history, backoff, 402 as "out of credit") are the section above.

> [!NOTE]
> Those request shapes were exercised against fixtures and a stubbed transport in the test
> suite. A live OpenRouter call needs a key; re-verify against the live API when you
> upgrade (`delegate.py` writes the provider's error text into the result on any 4xx).
