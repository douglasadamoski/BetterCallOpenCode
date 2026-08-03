# OpenRouter free models (BetterCallOpenCode)

> Snapshot date is in `free_models_snapshot.json`. Refresh with:
> `python3 scripts/list_free_models.py --refresh -o references/free_models_snapshot.json`

## Free-variant limits (official)

| All-time credits purchased | Free RPM | Free RPD |
|----------------------------|----------|----------|
| &lt; $10 (`is_free_tier: true`) | 20 | **50** |
| ≥ $10 (`is_free_tier: false`) | 20 | **1000** |

- Limits apply to model IDs ending in **`:free`** (account-level pool, **not** per model).
- Extra API keys do **not** raise free caps.
- Successful responses usually **omit** `X-RateLimit-*`; check `GET /api/v1/key` and 429 bodies.
- **$10 credits sit unused** while you only call `:free` models. Paid (non-`:free`) models spend them.
- Negative balance can block free models until topped up again.

Preflight prints your bucket:

```bash
python3 scripts/or_client.py --preflight --model openrouter/nvidia/nemotron-3-ultra-550b-a55b:free
```

## How to call free models

### OpenCode CLI

```bash
opencode run -m openrouter/nvidia/nemotron-3-ultra-550b-a55b:free "Review intent: …"
```

Format is always `provider/model` → `openrouter/<openrouter-id>`.

### Direct OpenRouter API (skill default backend `or-api`)

```bash
curl https://openrouter.ai/api/v1/chat/completions \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" \
  -H "Content-Type: application/json" \
  -H "HTTP-Referer: https://github.com/douglasadamoski/BetterCallOpenCode" \
  -H "X-Title: BetterCallOpenCode" \
  -d '{"model":"nvidia/nemotron-3-ultra-550b-a55b:free","messages":[{"role":"user","content":"OK?"}]}'
```

Auth key: `OPENROUTER_API_KEY` or OpenCode’s `~/.local/share/opencode/auth.json` → `openrouter.key`.

## Flagship free models (coding / review)

| OpenRouter ID | Context | OpenCode `-m` | Role |
|---------------|---------|---------------|------|
| `nvidia/nemotron-3-ultra-550b-a55b:free` | **1M** | `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free` | Default critic — frontier MoE (550B / 55B active) |
| `nvidia/nemotron-3-super-120b-a12b:free` | 262k | `openrouter/nvidia/nemotron-3-super-120b-a12b:free` | Strong multi-step reasoning |
| `nvidia/nemotron-3-nano-30b-a3b:free` | 256k | `openrouter/nvidia/nemotron-3-nano-30b-a3b:free` | Fast agentic / cheap hops |
| `nvidia/nemotron-nano-9b-v2:free` | 128k | `openrouter/nvidia/nemotron-nano-9b-v2:free` | Small/fast |
| `cohere/north-mini-code:free` | 256k | `openrouter/cohere/north-mini-code:free` | Code-oriented |
| `openai/gpt-oss-20b:free` | 131k | `openrouter/openai/gpt-oss-20b:free` | General OSS |
| `google/gemma-4-31b-it:free` | 262k | `openrouter/google/gemma-4-31b-it:free` | Multimodal-capable |
| `openrouter/free` | 200k | `openrouter/openrouter/free` | Free router (auto backend) |

Also free (see snapshot): Gemma-4 26B A4B, Poolside Laguna S/XS, Ling-3.0-flash, Nemotron Nano VL / Omni, Nemotron content-safety (guardrail — not a general critic).

### NVIDIA Nemotron free line (special view)

NVIDIA currently sponsors a large free slice on OpenRouter:

- **Ultra** — prefer for single-pass full-repo packs (1M context); still pack conservatively for reliability.
- **Super** — complex multi-file reasoning without Ultra latency.
- **Nano 30B / 9B** — staged reviews, multi-model panels, follow-ups.
- **Omni / VL** — only when images/video/audio matter.
- **Content Safety** — moderation/guardrail, not code review.

## Free-only gate (skill)

A model is free if:

- ID ends with `:free`, or
- ID is `openrouter/free` / `free`

Otherwise the skill returns **`RESULT=PAID_BLOCKED`** unless the user explicitly opts into paid (`--allow-paid` + clear consent).

## Paid models (opt-in only)

If the user **clearly** asks to spend OpenRouter credits:

```bash
bash scripts/opencode_review.sh ... \
  --model openrouter/anthropic/claude-sonnet-4.5 \
  --allow-paid
# or: export BCOPENCODE_ALLOW_PAID=1
```

Paid variants:

- Do **not** use the free 50/1000 RPD table.
- Draw from account credits (`GET /api/v1/credits`).
- The report header shows `Free model: false` and, when OpenRouter returns it,
  `cost=<amount>` on the Tokens line. The ledger row carries `"free": false` and the same
  cost figure. There is no separate "paid" word — `Billed:` means *the model was invoked*,
  not *money changed hands*.

Never pick paid models silently.

## Errors to stop on

The mapping is in `or_client.classify_http`.

| HTTP / skill RESULT | Meaning | Action |
|---------------------|---------|--------|
| 401/403 → AUTH | Bad key | Fix OpenRouter key / `opencode providers login` |
| 402 → QUOTA | Credits / key limit | Top up or raise key limit |
| 429 → QUOTA | Free RPD/RPM or provider | **STOP**, wait; optional one model rotate |
| 404/405 → **ERROR** | The API answered and rejected the **model id** — mistyped or retired | Check the id against `list_free_models.py`. This is *not* a network fault |
| 502/503/504 → **UNREACHABLE** | Reached OpenRouter; the upstream provider is down | Retry **once**, then try another free model |
| DNS/TCP/TLS failure → UNREACHABLE | The request never left the machine | Check connectivity; not billed |
| other 4xx / 5xx → ERROR | Rejected or unusable response | Read the error body in the report |
| TIMEOUT | Upstream / local | Smaller pack, stages, or retry once. **Counted as billed** — the request ran, the answer was lost |
| TRUNCATED | `finish_reason=length` — hit `max_tokens` | Raise `--max-tokens` (default **16384**); reasoning models burn budget on thinking |
| PAID_BLOCKED | Non-free without consent | Switch to `:free` or get user OK |

### Why smoke tests used to return TRUNCATED

Early verification used `--max-tokens 400` / `800` against `openai/gpt-oss-20b:free`.
That model fills **reasoning** first; the completion ceiling was hit before a normal
`content` message, so OpenRouter returned `finish_reason=length`. That was **not**
an OpenRouter outage — it was an undersized completion budget. Re-verified with
`--max-tokens 4096` → `RESULT=OK`, `finish_reason=stop`.

**Never retry-loop** on QUOTA/CAP.

## Per-model notes from live testing (2026-08-03)

Measured across three full 15-model panels against this repo's own source, plus targeted
probes. These are model/provider behaviours, not skill defects — recorded so a failure is
recognised rather than re-diagnosed.

| model | behaviour |
|---|---|
| `cohere/north-mini-code:free` | **Healthy on small prompts, unreliable on large ones.** 4/4 clean `finish=stop` in 6–10 s on a short prompt. On a ~46k-token packed prompt it variously stalled past 7 minutes, returned `finish_reason=error` with ~2k chars of partial output, or emitted 60k characters containing zero findings. Prefer it for staged/chunked reviews (Mode D), not whole-repo packs. |
| `inclusionai/ling-3.0-flash:free` | Ignores `reasoning.max_tokens` entirely. With reasoning enabled it spends the whole completion budget thinking (14 480 of 16 000 tokens) and returns ~1 800 chars. Fine with the default `BCOPENCODE_REASONING_EFFORT=none`. |
| `nvidia/nemotron-3-super-120b-a12b:free` | Same reasoning-burn pattern as ling at `effort=low`; fine at the `none` default. |
| `nvidia/nemotron-3-ultra-550b-a55b:free` | The one model where reasoning clearly pays: 10 findings at `effort=low` vs 6 at `none`. Worth setting `BCOPENCODE_REASONING_EFFORT=low` for this model specifically. |
| `google/gemma-4-*:free` | Frequently `429` from an upstream **shared pool** (`limit_source: upstream_provider_shared_pool`) — not your quota. Retry later or add your own provider key. |

`finish_reason=error` now yields `RESULT=ERROR` rather than `OK`; before that fix a model
aborting mid-generation was indistinguishable from a clean review.
