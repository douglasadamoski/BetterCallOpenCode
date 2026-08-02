# OpenCode CLI notes (verified)

Verified against **OpenCode 1.18.x** on Linux. Re-check flags after upgrades.

## Install / paths

- Binary: often `~/.opencode/bin/opencode` (ensure on `PATH`)
- Config: `~/.config/opencode/opencode.json` (or `.jsonc`)
- Auth: `~/.local/share/opencode/auth.json` (provider keys; **never commit**)
- State DB: `~/.local/share/opencode/opencode.db`
- Env overrides: `OPENCODE_CONFIG`, `OPENCODE_CONFIG_DIR`

## Real commands (not hallucinations)

| Task | Command |
|------|---------|
| Headless prompt | `opencode run "message…" -m openrouter/model:free` |
| List models | `opencode models openrouter` (`--verbose`, `--refresh`) |
| Auth | `opencode providers login` / `providers list` / `providers logout` |
| Stats | `opencode stats` |
| Sessions | `opencode session …`; resume with `-c` / `-s <id>` |
| Version | `opencode --version` |

**Do not use** (not real in 1.18): `opencode chat`, `opencode review`.

### `opencode run` flags that matter

- `-m, --model provider/model` — e.g. `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free`
- `--dir <path>` — project directory
- `--agent <name>` — custom agent
- `--format default|json`
- `-f, --file` — attach files
- `-c` / `-s` — continue / session id
- **`--auto`** — auto-approve permissions (**dangerous**; skill never passes this)

## OpenRouter provider

1. Create key at https://openrouter.ai/settings/keys  
2. `opencode providers login` → OpenRouter → paste key  
3. Models appear as `openrouter/<id>` including `:free` variants  

Optional config (`opencode.json`):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "openrouter": {
      "whitelist": []
    }
  }
}
```

Skill uses `OPENCODE_CONFIG_DIR=<skill>/opencode-config` for the **bcoc-review** agent so global config is not required.

## Permissions / agents

OpenCode agents can set `edit: deny`, `bash: deny`, `webfetch: deny` (see docs/agents).

Skill ships `agents/bcoc-review.md` / `opencode-config/agents/bcoc-review.md`:

- edit/bash/webfetch **deny**
- intended for criticize + propose only

Still treat agentic mode as **higher risk** than pure HTTP: wrap with care; prefer `or-api` for free RPD efficiency.

## Free vs paid via OpenCode

- Free: `-m openrouter/…:free` or `openrouter/openrouter/free`
- Paid: any non-`:free` id (spends credits) — BetterCallOpenCode blocks unless `--allow-paid`

## Auth for automation

Skill `or_client.py` resolves key as:

1. `OPENROUTER_API_KEY` / `BCOPENCODE_API_KEY`
2. Else OpenCode `auth.json` → `openrouter.key`

Never log full keys; reports go through `bcoc_redact`.
