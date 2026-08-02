# Maximize free OpenRouter usage

## Know your bucket

| `is_free_tier` | Free RPD | Free RPM |
|----------------|----------|----------|
| true | 50 | 20 |
| false (≥$10 purchased ever) | **1000** | 20 |

This skill’s author machine tops up **$10 once** → 1000 free RPD. Credits remain for paid models only if you opt in.

```bash
python3 scripts/or_client.py --preflight -m openrouter/openai/gpt-oss-20b:free
# key.free_rpd_bucket → 50 or 1000
```

## Prefer 1 request per review

| Approach | Free requests |
|----------|----------------|
| `or-api` + packed context | **1** |
| OpenCode agent with N tool hops | **~N** (often 10–30) |
| Multi-model panel of K models | **K** |
| Staged structure+security+tests | **3+** |

Default backend is **`or-api`**.

## Split work, not credentials

1. **File chunks** — `scripts/split_scope.py` for huge trees; review chunk-by-chunk; Claude merges.
2. **Concern stages** — `--stages structure` then `security` then `tests` with short prompts (`templates/chunk_prompt.md`).
3. **Follow-ups** — first pass “top 10 only”; second pass deep-dive one file.
4. **Model fit**
   - Full pack: Nemotron **Ultra** free (1M ctx) or Super
   - Many small hops: Nano / Gemma flash / free router
5. **Multi-model** — `multi_review.sh --preset coding-panel` with `--sleep 3` (respect 20 RPM).
6. **On 429** — stop; optionally try **one** other free model; never spin.

## Local skill cap

Default `--cap 200` (under 1000 RPD) so a runaway agent cannot burn the whole OpenRouter day. Raise if you need:

```bash
bash scripts/opencode_review.sh ... --cap 500
```

## Multi-model presets

| Preset | Models (all free) |
|--------|-------------------|
| `coding-panel` | Ultra, Super, north-mini-code, gpt-oss-20b |
| `fast-panel` | nano-30b, gemma-4-26b, ling-3.0-flash, openrouter/free |
| `nvidia-panel` | Ultra, Super, nano-30b, nano-9b |

## Timeouts

OpenRouter does not publish a single free wall-clock. Upstream may stall. Mitigations:

- Lower pack size (not answer tokens) if the *request* is huge
- Stage concerns
- Switch to Nano after Ultra timeout
- Skill `TIMEOUT` → report partial, one retry max

## TRUNCATED / `finish_reason=length` (common on free reasoning models)

**Cause:** OpenRouter `max_tokens` is a **completion** ceiling. Models like
`openai/gpt-oss-20b:free` spend many tokens on **reasoning/thinking** first. If
you pass a small `--max-tokens` (e.g. 400–800 for a smoke test), the run ends
with `finish_reason=length`, often with little or no final `content`.

**Fixes (skill defaults already apply these):**

| Knob | Default | Effect |
|------|---------|--------|
| `--max-tokens` / `BCOPENCODE_MAX_TOKENS` | **16384** | Room for thinking + findings |
| `BCOPENCODE_REASONING_MAX_TOKENS` | **2048** | Caps reasoning budget via OpenRouter `reasoning.max_tokens` when supported |
| Smaller pack / stages | — | Fewer input tokens, tighter answers |
| Prefer Nemotron Ultra/Super free | — | Often better structured code-review answers than tiny reasoning models |

The skill still returns **`RESULT=TRUNCATED`** when the ceiling is hit so Claude
does **not** treat a partial critique as complete — but the partial body is kept
in the report when available.

## Don’t

- Don’t pass `opencode run --auto`
- Don’t multi-model in tight parallel without sleep (RPM)
- Don’t silently use paid models
- Don’t put API keys in reports or git
