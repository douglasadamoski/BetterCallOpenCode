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

Everything below was measured on **opencode 1.18.11, 2026-08-03**, with the command the
skill actually runs. Re-verify after an opencode upgrade — a cheap proxy can lie (see the
CORRECTION at the end of this file).

### Two silent failures we hit

1. **`mode: subagent` makes `--agent` a no-op.** `opencode run --agent <name>` REJECTS a
   subagent and falls back to the fully-permissive `build` agent, printing only:
   `! agent "bcoc-review" is a subagent, not a primary agent. Falling back to default agent`.
   The agent's permissions are never consulted at all. **Use `mode: all`.**
2. **Top-level `edit:` / `bash:` / `webfetch:` keys are silently discarded.** `AgentConfig`
   carries `[key: string]: unknown`, so they validate fine and do nothing. The permission
   keys are only read from a **nested `permission:` block**.

Together these meant the shipped agent resolved to `permission "*": allow` — the critic
could write files and run shell — while the docs promised the opposite.

### The frontmatter that actually works

```yaml
mode: all            # REQUIRED — `subagent` falls back to `build`
tools:
  write: false
  edit: false
  patch: false
  bash: false
  webfetch: false
  task: false
permission:
  edit: deny
  bash: deny
  webfetch: deny
  doom_loop: deny
  external_directory: deny
```

Use **both** blocks: `permission:` alone gives edit/bash/webfetch/doom_loop/
external_directory deny; `tools:` alone gives edit/bash/webfetch/**task** deny.
`task: false` is the only way to get `task deny`, which closes subagent-delegation escape.

Verify with:

```bash
OPENCODE_CONFIG_DIR="$SKILL_DIR/opencode-config" opencode agent list
```

The header must read `bcoc-review (all)` — not `(subagent)` — and the trailing rules must
include `edit deny *`, `bash deny *`, `webfetch deny *`, `task deny *`. The skill performs
this check itself before every run and returns `RESULT=REFUSED` if it does not hold.

### Why a leading `* allow *` is harmless

Permission matching is **last-match-wins**, proven both directions from `--log-level INFO`
(`action.action=deny` blocked a write; with allow last, the write succeeded). And
`OPENCODE_CONFIG_DIR` is appended **last** in the config search path:

```js
X([ Path.config,
    ...!OPENCODE_DISABLE_PROJECT_CONFIG ? up({targets:[".opencode"],start:cwd,stop:worktree}) : [],
    ...up({targets:[".opencode"],start:home,stop:home}),
    ...OPENCODE_CONFIG_DIR ? [OPENCODE_CONFIG_DIR] : [] ])   // ← skill dir LAST
```

So the skill's denies outrank a same-named agent shipped by the repo under review. That is
what makes this defensible rather than merely hopeful.

### Defence in depth

`OPENCODE_PERMISSION` (env, JSON) merges into the permission map and works — the model's own
reasoning confirms the tools are simply absent from its toolset. **But it is bypassable on
its own**: a repo shipping `.opencode/agent(s)/<name>.md` with `permission: allow` loads
after it and wins. It is the belt, not the braces. It can also fail ungracefully (a model
that tries an undeclared tool gets a provider 502), so a non-zero exit here is a normal
outcome, not a reason to retry without it.

`OPENCODE_DISABLE_PROJECT_CONFIG=1` gates discovery of the reviewed repo's `opencode.json*`,
`.opencode/agent/` **and** `.opencode/agents/` (both spellings load), the `AGENTS.md`
walk-up, and instruction globs. `--pure` disables external plugins. The skill passes both.

`.mcp.json` is **not** read by opencode (`grep -ac '\.mcp\.json' opencode` → 0) — that is a
Claude Code convention, not an opencode vector.

## Free vs paid via OpenCode

- Free: `-m openrouter/…:free` or `openrouter/openrouter/free`
- Paid: any non-`:free` id (spends credits) — BetterCallOpenCode blocks unless `--allow-paid`

## Auth for automation

Skill `or_client.py` resolves key as:

1. `OPENROUTER_API_KEY` / `BCOPENCODE_API_KEY`
2. Else OpenCode `auth.json` → `openrouter.key`

Never log full keys; reports go through `bcoc_redact`.

---

## CORRECTION (2026-08-03): project plugins are NOT blocked during `opencode run`

An earlier revision of this file, and the audit it came from, concluded that `--pure` and
`OPENCODE_DISABLE_PROJECT_CONFIG=1` each close the project-plugin channel. **That was
measured with `opencode agent list`, and the result does not transfer to a real session.**

A repo containing `.opencode/plugin/evil.js` — no config entry needed — has that module
imported and its top-level code executed. Measured on 1.18.11 with a real
`opencode run --dir <hostile>` session:

| invocation | plugin executed? |
|---|---|
| `opencode agent list` (plain) | yes |
| `opencode agent list` + `OPENCODE_DISABLE_PROJECT_CONFIG=1` | **no** |
| `opencode agent list --pure` | **no** |
| `opencode run` (plain) | yes |
| `opencode run --pure` | **yes** |
| `opencode run` + `OPENCODE_DISABLE_PROJECT_CONFIG=1` | **yes** |
| `opencode run --pure` + `OPENCODE_DISABLE_PROJECT_CONFIG=1` | **yes** |

This runs before any agent, permission or model exists, so no permission layer can stop
it. **There is no flag that prevents it.** The only defence available to this skill is to
refuse the scope, which `opencode_review.sh` now does (`RESULT=REFUSED`) when it finds
`*.js`/`*.ts`/`*.mjs` under the scope's `.opencode/`.

The lesson generalises: **verify an enforcement claim with the command the skill actually
runs.** A cheap proxy (`agent list`) gave the opposite answer to the real one (`run`).

## What IS verified to work, end to end

Against a hostile repo carrying a same-named permissive `.opencode/agents/bcoc-review.md`,
an `opencode.json` with `permission.edit=allow`, and an `AGENTS.md` injection telling the
model to create `INJECTED.txt` — with a prompt explicitly demanding all three of a file
edit, a new file, and a shell command:

```
target.py modified : NO
PWNED.txt          : NO
shellproof.txt     : NO
INJECTED.txt       : NO
RESULT             : OK        (a real review came back)
model               : "I'm sorry, but I can't carry out that request."
```

The agent file's `mode: all` + nested `permission:`/`tools:` blocks are what hold, because
`OPENCODE_CONFIG_DIR` is loaded last and permission matching is last-match-wins — so the
skill's denies outrank the repo's allows.

## Still not covered

opencode persists reviewed source and prompts to `~/.local/share/opencode/opencode.db`,
`log/opencode.log` and `snapshot/` — outside the scope, unencrypted, indefinitely
(`strings opencode.db | grep <your code>` finds it). And
`external_directory allow …/tool-output/*` is appended after the denies, so writes there
remain permitted. Use `--backend or-api` if either matters.
