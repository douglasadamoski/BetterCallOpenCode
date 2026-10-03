# Changelog

All notable changes to BetterCallOpenCode. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The **RESULT contract** — the last stdout line of every entrypoint — is the public API of
this skill. Adding a word to it, or changing what an existing word means, is a breaking
change.

## [1.1.0] — 2026-10-02

Adds provider discovery, a capability cache, model selection and delegated workers. The
`RESULT=` vocabulary is unchanged; `delegate.py` reuses it.

### Added

- `scripts/discover_providers.py` — asks opencode which providers/models are connected, then
  the provider's `/models` and the public models.dev catalog what each can do; saves
  `capabilities.json` (0600, no credentials). A ~1 s fingerprint check on every call
  re-probes only what changed. Provider-agnostic: nothing is specific to one vendor.
- Per-provider zero-cost policy (`--set-policy`) for providers that publish no pricing.
  Unknown cost is blocked, not assumed free.
- `scripts/select_models.py` — ranked `--list` for the user to choose from, deterministic
  diverse `--auto` when nobody is there to ask. Tasks: review, research, code, fast, vision.
- `scripts/delegate.py` + `agents/bcoc-delegate.md` — run one task on any chosen model as a
  worker, so a deep-research or panel workflow can put some agents on OpenRouter/other
  providers. Roles `researcher|analyst|reviewer|coder-readonly`; `or-api` and `opencode`
  backends; fallback chain that never crosses the cost gate; JSON mode; vision; PDF-as-text;
  multi-turn sessions; a request budget shared across parallel workers.
- `scripts/ledger.py` — the ledger and the shared RPM window for Python callers, written in
  the exact row shape `_bcoc_common.sh` reads (a test asserts the shell counts these rows).
- `agents/bcoc-research.md` (opencode 1.x researcher agent), `references/providers_and_capabilities.md`.
- SKILL.md: provider/model step and Mode E (delegate), with a deep-research recipe.

### Changed

- `or_client.chat` accepts `base_url` and `extra_body`. For a non-OpenRouter base URL it
  does not send the OpenRouter-only `usage`/`reasoning` fields and does not apply the
  `:free` name test; the caller owns the cost gate.
- `references/OPENROUTER_FREE_MODELS_GUIDE.md` merged with the verified parts of a newer
  agent-to-agent draft. The draft's invented commands, config schema, env vars and retired
  model ids were **not** carried over; the guide lists what was dropped and why.

### Found while doing it

- **opencode 2.x is a different CLI** (no `providers`, `agent list`, `--dir`, `--pure`;
  `OPENCODE_CONFIG_DIR` ignored for agents). The existing agentic review backend already
  fails closed there (`RESULT=REFUSED`, nothing spent) — verified — but **is not ported**:
  see `references/opencode_notes.md`, "opencode 2.x". `delegate.py --backend opencode` uses
  the built-in `explore` agent after verifying its resolved permissions.
- A non-interactive `opencode run` auto-rejects `ask` permissions (measured, 2.0.22).
- Ninth catch for `test_packer_does_not_refuse_its_own_source`: two new test files carried
  literal token shapes and were withheld from review. Tokens in tests are now assembled.
- `--session ""` was silently treated as "no session" (caught by a test).

### Hardened after an adversarial review of the above (before release)

- opencode probes (discovery, agent verification) run from an **empty directory with project
  config disabled**, and the agent check uses the same cwd/env as the real run. Previously a
  repository in the caller's cwd could choose the base URL and env-var name a credential is
  sent to, or make the permission check pass for a different config than the run used.
- Base URLs must be `https` (plain `http` only to localhost) before any credential is sent.
- opencode children get a minimal environment plus **only that provider's** credential variable.
- `verify_v2` also refuses a **scoped allow** of `bash`/`edit`/`subagent` (`bash allow "git *"`
  is code execution) and a default (`*`) that grants.
- A provider's zero-cost policy covers only the models that existed when it was set; a model
  added later is `unknown` again. Any non-zero price field (request, image, web search) makes
  a model `paid`, not just prompt/completion.
- Session files are stored redacted; a model change between turns is announced.
- A blocked fallback no longer overwrites the primary's real outcome; an unwritable ledger
  stops further spend; invalid UTF-8 in the ledger no longer breaks the cap reader;
  malformed opencode config / numeric env values no longer crash; `--agent` is validated and
  `pdftotext` gets `--`.
- Measured: a directory opencode has not seen answers empty on its first call (`models`,
  `debug agents`), so probes use a stable directory and retry once.
- **Known and unchanged:** the cap is still check-then-act across separately launched
  processes, so N parallel workers can overshoot it by up to N−1 (see the Quota note in
  README). Keep `--cap` well under the provider's real limit.

### opencode 2.x support (follow-up, same release)

- New `scripts/opencode_v2.py`: on 2.x the restricted agent is defined in an `opencode.json`
  this skill writes into the **filtered mirror** (repo-supplied `opencode.json`, `AGENTS.md`,
  `CLAUDE.md`… are renamed to `*.reviewed`), default-deny + allowlist, `mode: primary`, and
  verified with `opencode debug agents` run in that directory with the run's own environment.
  `opencode_review.sh --backend opencode` and `delegate.py --backend opencode` both use it,
  so **every** role works on 2.x (the earlier `explore`-only workaround is gone).
- The shell wrapper detects the major version (`opencode --version` prints `opencode v2.0.22`)
  and branches; the 1.x path is unchanged.
- Found: the 2.x action for commands is named `shell`, not `bash`; the verifier checks both.
- Verified live against a hostile repo (permissive `opencode.json` with a plugin entry,
  injecting `AGENTS.md`, permissive `.opencode/agents/`): nothing was edited, created or run.
- Still OpenRouter-only by design: `opencode_review.sh` normalises models to `openrouter/<id>`;
  use `delegate.py` for other providers.

- A 10-worker deep-research run (5 sub-questions × 2 models, researcher role, opencode backend,
  web tools) completed 8/10; the other two timed out at 900 s. `--timeout 1800` is recommended
  for researchers. `parse_events` now keeps only the text after the model's last tool call.

- `delegate.py` now **reports files the secret filter withheld** (result front matter
  `withheld_files:`, a note in the result, stderr, the JSON sidecar) and tells the model they
  exist. Found when a status sentence of the form `<short word>: <prose>` in a reviewed document
  read as a password assignment, so the whole file was silently withheld and the reviewers had
  to discover its absence. The filter itself is unchanged (it errs toward withholding on purpose).
- Deep-research validation run (two models, two critique rounds, mechanical citation check):
  see SKILL.md Mode E, "Validate before you trust". Two of the process's own mistakes were
  caught by it: a citation checker whose requests were refused by some CDNs reported live links
  as dead, and "both models agreed" labels were applied to points only one model made.

### Not verified

- No live OpenRouter request was made for this release (no key on the build host); OpenRouter
  request/response shapes are covered by fixtures and a stubbed transport. The
  `opencode` backend was verified live against one connected provider on 2.0.22.

## [1.0.0] — 2026-08-03

First release considered fit to publish. The skill worked from the first commit; almost
everything below is the difference between *working* and *not lying about what it does*.
Grouped by what was wrong, because the individual commits are only interesting as a record
of how each defect was found.

### Secret filter (the packer)

The packer decides what leaves your machine. Every item here was a way for a credential to
be sent to a third-party model.

- The filter skipped the sibling skills' `.bettercallmyai.env` but not
  `.bettercallopencode.env` — the one file this project's own docs tell you to put an
  OpenRouter key in. The whole `BetterCall*` family is now covered by construction rather
  than one filename at a time.
- The content scan read only the first 8 KiB of files packed up to 120 KB. A credential at
  offset 14,430 was packed and sent. **The scan window may never be narrower than the pack
  window.**
- Secret-shaped *names* now cover the sharp misses found in a dedicated audit: `prod.env`
  and `env.production` (no leading dot), `*.ppk`, `.pgpass`, `*.tfvars`, `kubeconfig`,
  `config/creds/*`.
- Assignment detection was rewritten twice. Matching the *value's charset* both missed real
  secrets (`P@ssw0rd…`, values ending in `!`) and withheld ordinary source
  (`accessToken = request.headers.authorization`). It now classifies the value: a reference
  to a credential is not a credential, but a bare identifier is not proof of code either.
- Placeholder and code-value exemptions became `fullmatch` rather than substring tests. As
  substring tests, a live value that merely *contained* `xxx`, `todo`, `${…}` or a bracket
  was waved through.
- Unquoted multi-word values assigned to password-shaped keys are now withheld; quoted
  *instructions* to an operator ("change me on first login") are not, because blanking a
  whole `docker-compose.yml` out of a review over one line of prose is its own failure.
- Values that reach the packer as prose (backticks, trailing punctuation) are normalised
  before classification, and trimming a value may never *shrink* a real secret below the
  length threshold.
- URLs carrying userinfo — a user and password pair before the `@` — are caught regardless
  of the variable name, because `DATABASE_URL`, `REDIS_URL` and `SENTRY_DSN` contain no
  key-like word.
- A catastrophic-backtracking regex hung the entire test suite on first run; the key-prefix
  pattern is now a bounded character class, with a linearity test to keep it that way.
- Symlinks are skipped entirely (a safe name can point at a secret, and `resolve()` can
  escape the scope), and pruned hidden directories are **recorded**, so the metadata can
  distinguish "no secrets in `.ssh`" from "never looked at `.ssh`".
- Secret skips are reported in full and separately from the general `skipped` list, which is
  truncated to 50 entries. "Which files did you withhold" must never fall off the end of a
  list.
- The skill's own review outputs (`BETTERCALL*_REVIEW_*.md`, `MULTI_INDEX.md`,
  `summary.csv`) are excluded, so review #2 does not upload review #1's reports.
- Reviewing `~/work/secrets/myproject` used to withhold every file in an ordinary project
  and return a confident report about an empty pack. Secret-looking path segments are now
  matched scope-relative; the scope root's own location is reported separately.
- Later additions to the same catalogue: Stripe **test** keys (the half people actually
  paste into a config file), Azure storage and Service Bus connection strings (the
  credential sits inside a semicolon-separated blob with no vendor prefix), and three-part
  JWTs — which had to go in the token catalogue rather than the assignment heuristic,
  because `header.payload.signature` fullmatches the "this is a dotted code path" exemption
  and every bare JWT was being actively dismissed as source.
- Key spellings the alternation missed because a name must *end* with one of its words:
  `secret_key`, `private_key`, `account_key`, `access_key`, `connection_string`,
  `passphrase`, `authorization`, and `dsn` for Sentry-style URLs.
- YAML block scalars put the key on one line and the value on the next, so the single-line
  pattern saw an "assignment" of one punctuation character and dismissed it as too short.
- `Authorization: Bearer <token>` was read as prose because of the space; the scheme word is
  now stripped before classification, while `Bearer $SOME_VAR` still reduces to a
  placeholder and is still packed.

### Report hygiene

- **One redactor, not two.** A `sed` pipeline in `_bcoc_common.sh` and a Python pattern list
  in `panel_run.py` implemented the same idea with different pattern sets, so text one
  masked the other passed through — and the Python one is what writes an error snippet into
  `summary.csv`, inside the user's project directory. `scripts/redact.py` is now the single
  authority; the shell function keeps its name and interface and **fails closed** to a
  strict-subset `sed` fallback if Python is unavailable.
- **The finished report is scanned.** Redaction was only ever applied to the stderr block,
  never to the critic's own answer, so a model that read a credential and echoed it wrote
  that credential into a file in the user's project. The guard now runs over the finished
  report — both backends, every section — masking only the high-confidence shapes, adding a
  `[!CAUTION]` block, and printing `SECRET_SCAN=MASKED` on stderr. The `RESULT` word is
  deliberately unchanged, because the nearest existing word would tell Claude to retry and
  spend again.
- The skill warns when a report it just wrote is not covered by the project's `.gitignore`.
  It does not edit that file: a tool that silently changes your ignore rules has decided
  something it was not asked to decide.

### The `opencode` backend

- **The shipped agent ran with `permission "*": allow`.** Two silent opencode behaviours
  combined: `mode: subagent` makes `--agent` a no-op with a fallback to the permissive
  built-in agent, and top-level `edit:`/`bash:` keys validate fine and are discarded — only
  a nested `permission:` block is read. The README promised a read-only critic for months
  while `opencode agent list` said otherwise.
- Permissions are now **verified before every run** by parsing `opencode agent list`
  (`verify_agent_permissions.py`); failure is `RESULT=REFUSED` with nothing spent. The first
  version of that verifier grepped for the permission name and treated a hit as proof of a
  denial — it passed just as happily on `allow`. Resolution is last-match-wins, and only
  *global* rules decide the global answer.
- **The backend bypassed the secret filter entirely.** It pointed opencode at the raw
  repository and relied on a soft prompt rule not to read credential files — which a prompt
  injection in that repository could simply override. Both backends now sit behind the same
  filter: `opencode` reviews a **filtered mirror** built by `pack_context.py --mirror-to`.
  The prompt no longer names the real scope path either.
- Project plugins under a repo's `.opencode/` are imported and executed by opencode before
  any agent, permission or model exists, and **no flag prevents it** — `--pure` and
  `OPENCODE_DISABLE_PROJECT_CONFIG=1` block it for `opencode agent list` but not for
  `opencode run`. An earlier revision of the notes claimed otherwise because it measured the
  cheap proxy. Re-measured after the mirror landed: the mirror is the control, the scope
  refusal is defence in depth, and it is scoped to executable extensions at unbounded depth.
- `OPENCODE_CONFIG_DIR` is appended last in opencode's config search path and matching is
  last-match-wins, which is *why* the skill's denies outrank a hostile repo's — verified
  end-to-end against a repo carrying a same-named permissive agent, an `opencode.json`
  granting edit, and an `AGENTS.md` injection.
- The installed agent file is now overwritten on every run. Guarding the copy with
  `if [[ ! -f ]]` meant anyone who had ever run an older version kept its `mode: subagent`
  agent forever — a security fix that never reached existing installs.

### The RESULT contract

- Eight exit paths were reproduced that ended without a `RESULT=` line, three of them
  *after* the request had been paid for. Because the skill's own instructions tell Claude to
  branch on that line, a missing line reads as a crash and invites a retry that spends
  again. Every path — `--help`, every gate, `SIGINT`/`SIGTERM`, an unhandled Python
  exception in the panel runner — now ends with one, enforced by `tests/test_result_contract.py`.
- `PARTIAL` was emitted by the panel runner and absent from the documented table.
- A panel in which *every* model failed reported `RESULT=OK`, and Claude went on to triage
  reports containing no critique. `PARTIAL` now means "some models produced a usable
  review"; zero usable reviews returns the failure word.
- The `opencode` backend classified its outcome by grepping the *model's own output* for
  `auth|quota|rate limit|429`, so reviewing this very repository reported a good review as
  `AUTH`. Classification now uses the exit code and stderr only, and the timeout code is
  checked first.
- 404/405 were classified as `UNREACHABLE`, contradicting the documented meaning of that
  word and sending callers hunting a network fault when the real cause is a mistyped or
  retired model id. They are now `ERROR`; 502/503/504 keep `UNREACHABLE`.
- Argument validation: only `--cap` was checked. `--max-tokens`, `--timeout`,
  `--temperature` and `--max-input-tokens` reached Python or `timeout` as garbage.
- An unsubstituted `{{PLACEHOLDER}}` in a prompt template was sent verbatim, spending a real
  request on a template.
- A failed or empty pack still sent the request, with the literal text
  `(pack failed or empty)` as the body — a `RESULT=OK` review of zero source code.

### Cost and quota accounting

- The ledger gained an explicit `billed` field, and the cap counts that instead of inferring
  it from the outcome. Bias is deliberately toward over-counting: under-counting spends the
  user's real quota.
- `bcoc_cap_used` ended with `|| echo 0`, so an unreadable ledger silently made the cap
  infinite on exactly the filesystem condition where it matters. It now fails closed, and
  the run refuses before spending if the ledger is not writable.
- A `SIGTERM` killed the wrapper and orphaned the HTTP client (`ppid=1`), which kept
  running, spent the request, and produced no report, no ledger row and no `RESULT=`. The
  whole child process group is now signalled, and an interrupted-but-launched request is
  recorded as billed.
- `set -e` was toggled around the model call, which switched errexit on for the rest of the
  script; a failing ledger append then killed the script before it printed `RESULT=`.
- Parallel panels are admitted at most `cap − used` models up front. `--cap 1` with a
  four-model preset used to spend four requests.
- Free-model parallelism is paced by a sliding-window limiter (20 RPM) instead of a fixed
  sleep.

### Configuration and the trust boundary

- **Repo-local configuration was removed entirely.** A `.bettercallopencode.env` was loaded
  from `$(pwd)` — not even from the resolved `--scope` — so an unrelated directory could
  influence the run while the actual repo's file was ignored. Every key it could set affects
  cost or how much source is uploaded. The project under review does not get to configure
  its own review; if one is present you get a note on stderr.
- Config files are parsed as `KEY=VALUE`, never sourced, with an allowlist, and values
  containing command substitution are rejected. Config loading moved before the defaults are
  computed, because a config file that cannot set the model or the cap is not a config file.
- Broad scopes (`/`, `$HOME`, `/etc`, …) are refused. The secret filter is a heuristic and
  should not be pointed at a home directory.
- `$STATE_DIR` and run directories are `0700`. They hold a full plaintext copy of the
  reviewed source, and under the default umask every local user could read them.
- Stale run directories are reaped after 24 hours.
- Report and error output passes through a redactor that covers eight token families; the
  previous one covered two and half-redacted values containing `_` or `-`.
- Untrusted pathnames are rendered inert in the prompt (backticks and control characters),
  and file bodies get an adaptive fence — a repo containing a fence could otherwise close
  its own block and have the remainder read as instructions.
- The one `eval` in the wrapper consumes `shlex.quote`d output with fixed key names, and
  says so where it lives.

### Free-model gate and model ids

- Non-`:free` models return `RESULT=PAID_BLOCKED` unless the user explicitly consents.
- The bash and Python implementations of the gate drifted: the bash one collapsed
  `openrouter/openrouter/free` to `openrouter/free`, breaking the free router — a model that
  ships in the `fast-panel` preset. `tests/test_gate_parity.py` now drives every id through
  both.
- `max_tokens` defaults to 16384, with a reasoning-token budget, after early smoke tests
  returned `TRUNCATED` from an undersized completion ceiling rather than any outage.

### Tests, CI and documentation

- 218 fully offline tests. A test that reached OpenRouter would silently spend the
  maintainer's free daily requests.
- CI runs the committed-secret scan first, then shellcheck, pytest on Python 3.9/3.11/3.13,
  and a parse check of every script.
- `tests/test_pack_secrets.py::test_packer_does_not_refuse_its_own_source` asserts the
  packer still packs its own most security-critical files. It has caught the file matching
  its own patterns **ten times**.
- A doc test asserts the `SKILL.md` RESULT table lists every word the scripts can emit.
- Mode B never ran at all: `run_local.sh` depended on `readlink -f`, and every invocation
  returned `REFUSED` where that is absent.
- Every documented guarantee was audited against the code that enforces it, and the ones
  that were overstated were reworded rather than quietly kept. The secret filter is
  described as best-effort withholding with guaranteed *recording*; neither backend is
  described as a sandbox; the local persistence, the `tool-output/` exception and the `PATH`
  assumption are stated rather than left implicit.

### Verification

A live panel of 15 free targets (14 `:free` ids plus the free router, ~37 free requests)
reviewed this repository's own source. Zero real credentials appeared in any report — three
mechanical hits were models inventing illustrative keys while *discussing* the secret
filter, confirmed by comparing against the actual credential. That is evidence, not proof:
it says nothing about a vendor prefix the filter does not know.

The same panel found the 404/405 misclassification above, and had three other claims
refuted on inspection.
</content>
