# Providers, capabilities and model choice

BetterCallOpenCode is not tied to one provider. It asks **opencode** what is connected,
asks the **provider** (and a public catalog) what each model can do, saves the answer, and
re-checks it cheaply on every call. OpenRouter is simply the provider with the richest
metadata and a `:free` tier.

## What is learned, and from where

| Step | Command / source | Gives |
|---|---|---|
| Connected providers + models | `opencode models`, `opencode auth list` (v2) · `opencode providers list` (v1) | `provider/model` ids, which providers have a credential |
| Routing | `opencode debug config` (v2) or `~/.config/opencode/opencode.json` | base URL and env-var name per provider (never the key) |
| Provider metadata | `GET {baseURL}/models` | OpenRouter: context, pricing, `supported_parameters`, modalities. A plain gateway: usually just ids |
| Public catalog | `https://models.dev/api.json` | context, output cap, `tool_call`, `reasoning`, modalities, cost — for any provider opencode knows |

Fields are merged first-source-wins and each model records which sources contributed
(`sources`). **A field nobody answered stays `null`**, and the model is marked
`capabilities: unknown`; nothing is guessed.

## The cache

`${BCOPENCODE_STATE_DIR:-~/.bettercallopencode}/capabilities.json`, mode `0600`, atomic
write. It holds capabilities only — **never a credential**. Keys are read from the
environment or opencode's auth store solely to authenticate the provider's own `/models`
request.

```jsonc
{ "schema": 1, "opencode_version": "2.0.22", "fingerprint": "…",
  "providers": { "<id>": { "status": "ok|needs_policy", "zero_cost_policy": null,
     "base_url": "…", "models": { "<model>": { "ctx": 262144, "max_out": 32768,
        "tools": true, "reasoning": true, "json_mode": true, "modalities": ["text"],
        "cost_class": "free|paid|unknown", "sources": ["provider","models.dev"],
        "capabilities": "known|unknown", "last_ok": "2026-10-02T19:57:45Z" } } } } }
```

## The quick check (every call, about a second)

`python3 scripts/discover_providers.py` hashes opencode's version + model list + auth list.

- same hash and younger than `BCOPENCODE_CAPS_TTL` (default 24 h) → prints
  `providers: 2, models: 24, cache fresh` and touches no network;
- changed or stale → only the providers whose own model list changed are re-probed;
- `--refresh` forces everything, `--offline` never uses the network.

opencode starts a background service on first use, so the very first `models` call can come
back empty; discovery retries once and never replaces a good cache with an empty answer.

## What "zero-cost" means for a provider

1. A published price wins: `0/0` → `free`, anything else → `paid`. OpenRouter's `:free`
   suffix counts only when pricing is silent.
2. Providers that publish **no** pricing (most self-hosted and institutional gateways)
   cannot be classified. Discovery marks them `needs_policy`, their models are excluded from
   selection, and the user is asked **once**:

```bash
python3 scripts/discover_providers.py --set-policy <provider> free   # or: paid
```

   The answer is remembered and only ever resolves `unknown`; it never overrides a price
   the provider actually published. Paid models stay blocked without `--allow-paid`.

## Choosing models

```bash
python3 scripts/select_models.py --task research --list          # ranked table for the user
python3 scripts/select_models.py --task research --n 3 --auto    # decide without asking
```

Tasks: `review | research | code | fast | vision`. Ranking uses context length, whether
the capabilities are actually known, tool/reasoning support for the task, a recent
successful call (`last_ok`), and carries forward the repo's own reliability findings
(a content-moderation classifier is not a reviewer; one model is permanently
rate-limited upstream). `--auto` spreads picks across providers and families.

Scripts never prompt. **The conversation does:** in plan mode Claude shows the `--list`
table and asks which to use; otherwise Claude runs `--auto` and says what it picked. A
model the user names always wins.

## Using a model as a worker

`scripts/delegate.py` runs one task on a chosen model and writes one result file — see
SKILL.md, Mode E. Two backends:

- **`or-api`** — one HTTPS POST to the provider's `/chat/completions`. Needs a key the
  script can read (environment or opencode `auth.json`). No filesystem, no shell, no tools.
- **`opencode`** — `opencode run` as an agent against a filtered mirror of the scope.
  Works with credentials that only the opencode service holds. Gated: see
  `opencode_notes.md`, "opencode 2.x".
