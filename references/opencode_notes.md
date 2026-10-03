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
it. **There is no flag that prevents it.**

The lesson generalises: **verify an enforcement claim with the command the skill actually
runs.** A cheap proxy (`agent list`) gave the opposite answer to the real one (`run`).

## RE-MEASUREMENT (2026-08-03): the mirror is the control, the refusal is defence in depth

The measurements above were taken with opencode pointed at the reviewed repository. The
skill no longer does that. `opencode_review.sh` builds a filtered mirror
(`pack_context.py --mirror-to`) and passes `--dir <mirror>`. That changes the answer, so it
had to be re-measured — a gate whose necessity is assumed rather than tested is how
over-refusal creeps in:

| invocation | plugin executed? |
|---|---|
| `opencode run --dir <hostile raw repo>` | **yes** |
| `opencode run --dir <filtered mirror>` | **no** |

The mirror is built by `iter_files()`, which prunes hidden directories wholesale, so
`.opencode` never reaches the tree opencode runs against. **The mirror is the primary
control.** The scope refusal is defence in depth: it catches a future code path that
forgets to build the mirror, and it declines to point an agent at a repo that ships plugin
code in the first place.

Because it is no longer the primary control, the refusal was narrowed from "any file under
`.opencode/`" — which also refused ordinary OpenCode projects that merely ship
`.opencode/agents/*.md`, a large false-positive blast radius for no measured gain. The
current rule is **executable extensions at any depth** under the scope's `.opencode/`:

```
js  cjs  mjs  jsx  ts  mts  cts  tsx  wasm  node
```

Depth is unbounded on purpose. The original gate used `-maxdepth 2` and three extensions,
and `plugin/nested/evil.js`, `.cjs`, `.mts` and a plural `plugins/` all walked straight
past it.

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

Note what this experiment does **not** say. It says the model did not succeed in editing,
creating or executing on opencode 1.18.11 with this configuration. It is a measurement of a
specific version, not a guarantee the vendor offers, and the CORRECTION above is what
happens when a measurement is taken with the wrong command. Re-run it after an upgrade.

## Still not covered

- **Persistence.** opencode persists reviewed source and prompts to
  `~/.local/share/opencode/opencode.db`, `log/opencode.log` and `snapshot/` — outside the
  scope, unencrypted, indefinitely (`strings opencode.db | grep <your code>` finds it).
  Nothing in this skill cleans that up.
- **`tool-output/`.** `external_directory allow …/tool-output/*` is appended after the
  denies, so writes there remain permitted. `verify_agent_permissions.py` reports such
  scoped allows on stderr rather than pretending they do not exist; only a *global* allow
  flips the verdict.
- **PATH.** Everything above assumes the `opencode` and `python3` that run are the ones you
  think they are. A compromised binary earlier on `PATH` defeats every layer here, and no
  in-process check can detect it. That is the accepted trust boundary for a developer tool
  — but it is a boundary, not an oversight.
- **The remote side.** The model is a third party. Free `:free` variants in particular have
  their own data-handling terms on OpenRouter and its upstream providers.

Use `--backend or-api` if the local-persistence item matters. Nothing here removes the
last item: both backends send your code to OpenRouter.

## opencode 2.x (measured on 2.0.22, 2026-10-02)

Everything above was measured on 1.18.x. 2.x is a different CLI and the 1.x gate cannot work
on it, so the skill has a separate 2.x path (`scripts/opencode_v2.py`, used by both
`opencode_review.sh --backend opencode` and `delegate.py --backend opencode`).

| Was (1.x) | Is (2.x) |
|---|---|
| `opencode providers login/list` | `opencode auth login/list/logout/export/import/switch` |
| `opencode agent list` (text) | `opencode debug agents` (JSON: `permissions: [{action, resource, effect}]`) |
| `opencode models <provider> --verbose` | `opencode models` (plain `provider/model` lines) |
| `opencode run --dir D --pure` | **no `--dir`, no `--pure`** — the working directory is the project |
| `OPENCODE_CONFIG_DIR` loads extra agents | **ignored** for agent discovery |
| bash permission is `bash` | the resolved action is named **`shell`** (a config `bash:` key is folded into it) |
| `--format default\|json` | same; `json` emits one event per line (`text`, `tool_use`, `step_finish`) |

What 2.x *does* do: it reads **project config from the working directory** — `opencode.json`
(an `agent` map, same schema idea as 1.x) and `.opencode/agents/*.md` both define agents — and
`opencode debug agents` reports each agent's fully resolved permissions for that directory.
That is what makes a verifiable restriction possible again:

1. The agent runs in the **filtered mirror** (hidden dirs pruned, secrets withheld).
2. `opencode_v2.py prepare` renames any root-level `opencode.json`, `opencode.jsonc`,
   `AGENTS.md`, `CLAUDE.md` (…) the reviewed repo shipped to `*.reviewed` — they would
   otherwise configure the run (a `plugin` entry is code execution) or inject standing
   instructions — and writes **this skill's** `opencode.json`.
3. Its agent is `mode: primary` (so `run --agent` honours it) with a **default-deny
   allowlist**, in this order, because the last match wins: `"*": "deny"`, then
   `read` (`*.env`/`*.env.*` denied), `grep`, `glob`, `list`, plus `webfetch`/`websearch` for the
   researcher role only.
4. `opencode_v2.py verify` runs `opencode debug agents` **in that same directory with the same
   environment the run will use** (project config is deliberately *not* disabled — the mirror's
   config is ours) and refuses unless `edit`, `shell`/`bash`, `subagent`, `external_directory`
   (and, for non-researchers, `webfetch`/`websearch`) resolve to deny/ask, the default `*` is not
   a grant, and no scoped `allow` exists for `shell`/`bash`/`edit`/`subagent`.
5. The run is `opencode run --agent bcoc-<role> --format json -f <prompt>` from inside the
   mirror. Nothing is spent unless step 4 passed (`RESULT=REFUSED` otherwise).

Measured, same version:

- **`ask` is blocked**: a non-interactive `opencode run` without `--auto` auto-rejects every
  `ask` (`This non-interactive run cannot ask the user for permission, so the request was
  rejected`). Probe: reading `/etc/hostname` from the mirror returned
  `external_directory (/etc/*); auto-rejecting`. **The skill never passes `--auto`.**
- A directory opencode has not seen yet answers `[]` / empty on its **first** call (`models`,
  `debug agents`) while it registers the project; the second call is correct. Probes use a
  stable directory and retry once.
- Hostile repo (`opencode.json` allowing everything + a `plugin` entry that writes a canary
  file, an `AGENTS.md` saying "create INJECTED.txt", a permissive `.opencode/agents/` agent),
  prompt demanding an edit, a new file and a shell command: **no file changed or created,
  the plugin did not run, the model reported each action denied.** (One model, one run — a
  measurement, not a vendor guarantee; re-run after an upgrade.)
- opencode persists the session in its own database, as on 1.x ("Still not covered", above).

The 1.x path (`bcoc-review` / `bcoc-research` agent files + `opencode agent list` +
`verify_agent_permissions.py`) is kept for 1.x installs and is exercised in the test suite only
against a stub `opencode`; it was not re-measured on a real 1.x binary for this release.

`opencode_review.sh` itself remains an **OpenRouter** review wrapper: it normalises every
model to `openrouter/<id>` and applies the `:free` gate. To review through another provider's
model, use `delegate.py --role reviewer --backend opencode --scope DIR`.

## Running many sessions (measured on 2.0.22, 2026-10-03)

Findings from driving hundreds of opencode sessions through `fanout.py`. The numbers describe one
provider on one afternoon — treat them as shapes, not constants.

**Events stream live.** `opencode run --format json` prints one JSON line per event as it
happens (`step_start`, `tool_use`, `text`, `step_finish`), each carrying `sessionID`. That is what
makes a stall watchdog possible, and `-s <sessionID>` resumes that session **with its context**
(a resumed session recalled a code word from its first turn). `delegate.py` uses both: it kills a
session that has been silent for `--stall-timeout`, then resumes it and asks for the report from
what it already found (`RESULT=TRUNCATED`, `salvaged: True`). Seen working on a real session.

**Provider failures arrive as `{"type":"error"}` events and the process still exits 0.** Fields:
`error.status`, `error.type`, `error.message`. Ignoring them turns every provider failure into
"returned no text". `delegate.py` now maps 401/403 → `AUTH`, 402/429 → `QUOTA`, 5xx →
`UNREACHABLE`, anything else → `ERROR`, with the provider's own message in the result.

**Zen free models refuse custom agents.** `opencode/*-free` models answer 403
(`OpenCode's free tier can only be used from within OpenCode`) when the run uses a *custom* agent
— ours, `bcoc-<role>` — but work with the built-in agents (`build`, `plan`) from the same
directory. Removing the agent's custom prompt did not help. So the restricted agent this skill
needs cannot use the Zen free tier; use another provider (OpenRouter `:free`, or a provider you
configured). The failure is immediate and now reported as `AUTH` with that message.

**Latency has a heavy tail, so wall time is set by the slowest call, not the average.** Trivial
tasks ("reply PONG") on one provider: median 2–5 s, but ~5–8 % of calls took 40–125 s, with gaps
of up to ~86 s *between events of a call that then succeeded*. Consequences:
- a stall timeout below ~90–120 s kills calls that would have finished; for real research
  units use 150–300 s;
- 40 trivial units at parallelism 16 took 100 s — about the slowest call — so batches of small
  units are tail-bound, not throughput-bound;
- throughput rose only sublinearly with parallelism: 0.18 units/s at 3, 0.32 at 8, 0.40 at 16.

**Pushing to 25–100 concurrent sessions degraded the provider for ~10 minutes.** Runs at
parallelism 25 were slower per call than runs at 10; afterwards every call to that provider
(and, briefly, others via the same service) returned nothing within 75 s until the load had been
off for several minutes. Killing a client does **not** cancel its session on the server, so
abandoned sessions keep occupying it. Keep parallelism modest (6–12 per provider worked cleanly)
and let `fanout.py`'s adaptive limiter react to rate-limit answers.

**Housekeeping.** A new working directory is a new "project": its first `models`/`debug agents`
call can answer empty, so probes use a stable directory and retry once, and `fanout.py` prepares
and verifies **one** mirror per (role, scope) for all its units. Do not
`pkill -f 'opencode run'` from a shell whose own command line contains that text — it kills the
shell.

**A restart helps, but multi-step research units still stalled (measured the same day).** After
`opencode service restart` a plain call answered in 1 s and `fanout.py` resumed a 36-unit web-research
job at parallelism 4. Over 30 minutes 5 more units finished and 22 attempts went silent for the full
150 s stall limit (their resume-to-finish attempts stalled too), while one-step tasks kept working.
A direct test showed a `websearch` tool call itself completing in ~2 s and the model's *next* turn
then hanging — so the stalls were at the model call that follows a tool result, not in the web tool.
Cause not established (provider queueing under token load is the likeliest). Practical rules:
- research units with web tools: parallelism **1–2** per provider and `stall_timeout` ≥ 300;
- after aborting a run, **wait ~1–2 minutes** before judging the provider: the service keeps
  working through the sessions you abandoned, and a plain call made in that window hangs
  (observed: 70 s of silence that cleared on its own);
- `opencode service restart` clears a wedged service, and it is a machine-wide action.
