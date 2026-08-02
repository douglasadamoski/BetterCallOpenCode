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

> [!CAUTION]
> **Measured on opencode 1.18.11, 2026-08-03: top-level `edit:` / `bash:` / `webfetch:`
> keys in agent frontmatter are silently ignored.** They are not the schema. The agent
> still loads, no warning is printed, and the permissions resolve to `*: allow`.

Reproduce:

```bash
OPENCODE_CONFIG_DIR="$SKILL_DIR/opencode-config" opencode agent list
```

As shipped, `bcoc-review` resolves to:

```
*                   allow  *          ← governs everything
doom_loop           ask    *
external_directory  ask    *
question            deny   *
plan_enter          deny   *
plan_exit           deny   *
read                allow  *
read                ask    *.env
```

No `edit`, `write`, `patch`, `bash` or `webfetch` rule exists. `--auto` is irrelevant:
it only auto-approves rules whose action is `ask`; rules whose action is `allow` execute
unprompted anyway. **So the critic can write files and run shell.** The built-in `plan`
agent resolves `edit deny *` correctly, which shows the mechanism works when the shape
is right — this is our file being wrong, not opencode lacking the feature.

### The enforcement lever that does work

`OPENCODE_PERMISSION` is read from the environment and merged into the permission map.
Verified — with

```bash
OPENCODE_PERMISSION='{"edit":"deny","write":"deny","patch":"deny","bash":"deny","webfetch":"deny","task":"deny","external_directory":"deny"}'
```

all seven `deny` rules appear in the resolved list for `bcoc-review`.

**Caveat, not yet resolved:** `*  allow  *` still appears *first* in the resolved list and
the denies are appended after it. Whether later rules win has **not** been proven by a live
write attempt. Until it is, a resolved deny rule is evidence, not a guarantee — which is
why an edit-guard is required rather than optional.

Also relevant: `OPENCODE_DISABLE_PROJECT_CONFIG=1` gates discovery of the reviewed repo's
`opencode.json*`, `.opencode/agent*/`, `AGENTS.md` walk-up and instruction globs, and
`--pure` disables external plugins. Both close the channel by which a reviewed repository
could supply its own permissive agent.

### Current status in this skill

`--backend opencode` refuses unless `BCOPENCODE_UNSAFE_OPENCODE=1`. Prefer `or-api`:
it is genuinely unable to touch your disk, and costs 1 free request per review.

## Free vs paid via OpenCode

- Free: `-m openrouter/…:free` or `openrouter/openrouter/free`
- Paid: any non-`:free` id (spends credits) — BetterCallOpenCode blocks unless `--allow-paid`

## Auth for automation

Skill `or_client.py` resolves key as:

1. `OPENROUTER_API_KEY` / `BCOPENCODE_API_KEY`
2. Else OpenCode `auth.json` → `openrouter.key`

Never log full keys; reports go through `bcoc_redact`.
