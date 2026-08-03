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

## Which free models are actually usable — measured

8 full 15-model matrices against this repo's own source, 2026-08-03. `OK` is out of 8;
`chars`/`finds` are medians over successful runs.

| model | OK | chars | finds | verdict |
|---|---|---|---|---|
| `poolside/laguna-s-2.1` | 8/8 | 6 790 | 12 | **default.** Most reliable substantive reviewer |
| `nvidia/nemotron-3-super-120b-a12b` | 8/8 | 3 976 | 5 | reliable, terser |
| `openrouter/free` (router) | 7/8 | 10 091 | 13 | reliable and rich |
| `nvidia/nemotron-3-nano-30b-a3b` | 7/8 | 6 665 | 10 | reliable |
| `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` | 7/8 | 6 189 | 7 | reliable |
| `poolside/laguna-xs-2.1` | 5/8 | 6 886 | 14 | good output, some 429s |
| `inclusionai/ling-3.0-flash` | 5/8 | 6 082 | 8 | 3 of its failures were the reasoning-burn bug, now fixed |
| `nvidia/nemotron-3-ultra-550b-a55b` | 4/8 | 9 684 | 18 | **richest when it works, fails half the time.** Was the default; now `deep-panel` only |
| `nvidia/nemotron-nano-9b-v2` | 4/8 | 2 791 | 5 | thin |
| `cohere/north-mini-code` | 3/8 | 10 728 | 44 | **most findings per success, least reliable.** Healthy on small prompts, unreliable at ~46k tokens — use Mode D chunking |
| `nvidia/nemotron-nano-12b-v2-vl` | 3/8 | 2 024 | 10 | vision model; frequently empty |
| `google/gemma-4-26b-a4b-it` | 2/8 | 4 677 | 10 | frequent 429 |
| `openai/gpt-oss-20b` | 1/8 | 3 918 | 10 | requires reasoning (rejects `enabled:false` with HTTP 400); heavily 429 |
| `google/gemma-4-31b-it` | **0/8** | — | — | **excluded.** 8/8 HTTP 429, upstream shared pool |
| `nvidia/nemotron-3.5-content-safety` | 8/8 | **93** | **0** | **excluded — not a code reviewer.** See below |

### The one that looks perfect and is worthless

`nemotron-3.5-content-safety` scored **8/8 OK**, the joint-best reliability in the table,
while replying `User Safety: safe` — 93 characters, zero findings. It is a content-
moderation classifier, not a reviewer.

In a consensus panel that is *worse than a model which fails outright*: it inflates the
panel's success rate, dilutes agreement between the models that did review, and reads as
a review that found nothing wrong. A model that always succeeds and never contributes is
the hardest kind of useless to notice — which is why it is now excluded by name and the
exclusion is asserted by a test rather than left to judgement.

Both exclusions are announced on stderr and reversible with
`list_free_models.py --include-non-reviewers`, because "is this free?" and "can this
review code?" are different questions and the roster tool should still answer the first.

### Presets, rebuilt from this data

| preset | for | models |
|---|---|---|
| `coding-panel` | default choice; 30/32 historical OK | laguna-s · nemotron-3-super · openrouter/free · nano-30b |
| `fast-panel` | small and quick | nano-30b · ling-3.0-flash · laguna-xs · openrouter/free |
| `deep-panel` | depth over certainty | nemotron-ultra · north-mini-code · laguna-s · openrouter/free |
| `nvidia-panel` | NVIDIA only | ultra · super · nano-30b · nano-omni |

### Caveat on the sample

8 runs, one day, one codebase, on a free tier whose capacity visibly varied by the hour.
Reliability figures are indicative, not stable characteristics of the models. The
`content-safety` and `gemma-4-31b` verdicts are the robust ones: the first is a category
error and the second failed every single attempt.

### Validation after the exclusions

Re-ran `--all-free` with the two exclusions in place: **11/13 OK (84%)**, the best result
recorded. The same 11 models succeeded as in the best prior 15-model run — so the
exclusions cost no review coverage and save two requests per panel. The two remaining
failures were `nemotron-nano-12b-v2-vl` (empty content) and `gpt-oss-20b` (429), the two
models the table already flags as weakest; both are retained but appear in no preset.

`coding-panel` returned 3/4 OK with all four slots producing usable output — the
non-OK was `openrouter/free` hitting the completion ceiling after writing the longest
review in the panel, which `TRUNCATED` correctly reports as partial.
