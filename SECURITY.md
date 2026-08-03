# Security Policy

## Reporting a vulnerability

Please report privately, not in a public issue:

- GitHub **private vulnerability reporting** on
  [this repository](https://github.com/douglasadamoski/BetterCallOpenCode/security/advisories/new)
  (Security → Report a vulnerability), or
- a direct message to the maintainer on GitHub
  ([@douglasadamoski](https://github.com/douglasadamoski)) asking for a private channel.

Please include the version or commit, the platform and `opencode --version` if relevant, the
exact command, and what you expected versus what happened. If a real credential was
involved, **redact it** — a shape (`sk-or-…`, 73 characters) is enough, and rotate the key
first.

Expect an acknowledgement within about a week. This is a personal project maintained in
spare time; there is no bounty. Fixes land on `main` with a `CHANGELOG.md` entry that
describes the defect honestly, which is how every entry in that file already reads.

Supported version: **the latest release**. There are no maintained backports.

## What this tool actually is

BetterCallOpenCode packs your source code and sends it to a third-party model, or points a
local agent at a filtered copy of it. **It is not a sandbox, and it does not claim to be.**
Everything below is a limit that exists in the shipped code, stated so that nobody has to
discover it the hard way.

### 1. The secret filter is best-effort withholding with guaranteed recording

`scripts/pack_context.py` withholds files that look like credentials before anything is
sent. It covers:

- **Names** — `.env` in every spelling (including `prod.env` and `env.production`), `.envrc`,
  `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.jks`, `*.ppk`, SSH private keys, `.netrc`,
  `.pypirc`, `.pgpass`, `.npmrc`, `.git-credentials`, `auth.json`, `kubeconfig`,
  `.docker/config.json`, `*.tfstate`, `*.tfvars`, `.htpasswd`, and every sibling skill's
  `.bettercall*.env` by construction.
- **Directories** — hidden directories are pruned wholesale, which is what actually keeps
  `.ssh`, `.aws` and `.gnupg` out, plus explicit `secrets/`, `creds/`, `api_keys/` segments.
- **Contents** — a catalogue of vendor token shapes: OpenRouter, Anthropic, OpenAI, Stripe
  (live *and* test), GitHub including fine-grained PATs, GitLab, Slack bot and app tokens,
  Google API keys, AWS access key ids and secret access keys, Azure storage and Service Bus
  connection strings, three-part JWTs, PEM and PuTTY private keys, and URLs carrying
  userinfo. `SECRET_CONTENT_RE` in `scripts/pack_context.py` is the authoritative list —
  this paragraph is a summary and may lag it.
- **Assignments** — a heuristic that classifies the *value* rather than the key name, on one
  line or in a YAML block scalar, so `token = process.env.GITHUB_TOKEN` is packed while a
  literal credential is not.
- **Symlinks** are skipped entirely, because a safe-looking name can point at a secret and
  path resolution can escape the scope.

**What it does not cover, and cannot:**

- **A vendor prefix nobody has added yet is a miss.** The token catalogue is a list. Each new
  provider is a hole until it is patched in.
- **Adversarial encoding gets through.** Base64, splitting a key across lines, storing it in
  a format the scanner does not model — none of that is defeated by a regex.
- The assignment heuristic is tuned against a real corpus and biases toward withholding, but
  it is still a heuristic in both directions: it can withhold a file it should have packed,
  and it can pack one it should have withheld.

**The part that is a guarantee** is the bookkeeping. Every file withheld as a secret is
recorded — by name in `secrets_skipped_by_name`, by content in `secrets_skipped_by_content`,
both untruncated, both surfaced in the report header as `secrets_withheld=N`. Pruned
directories and skipped symlinks are recorded too. This is deliberate: "not reviewed" must
never be indistinguishable from "reviewed and clean".

Evidence, not proof: a live panel of 15 free models reviewing this repository's own source
produced zero real credential leaks. That says the filter held on one codebase against 15
models. It says nothing about a token shape it has never seen.

**Do not point this at a scope you would not be willing to paste into a chat window.** The
broadest roots (`/`, `$HOME`, `/etc`, …) are refused outright for exactly this reason.

### 2. The two backends have different exposure

| | `--backend or-api` (default) | `--backend opencode` |
|---|---|---|
| What runs | one HTTPS POST | an agent process on your machine |
| What the model sees | the packed text, and nothing else | a **filtered mirror** of the scope, which it can list and read |
| Filesystem access | none — there is no filesystem | the mirror only; `edit`/`bash`/`webfetch`/`task`/`external_directory` verified denied before every run |
| Local residue | the run directory, `0700`, deleted on exit | the same, **plus** opencode's own state |
| Free requests | 1 per review | roughly one per tool hop |

`or-api` is not a security boundary in the sense of protecting your code — your code is sent
to OpenRouter either way. It is a boundary in the sense that there is nothing on the remote
side that could act on your machine.

`opencode` **persists the reviewed source and prompts** to
`~/.local/share/opencode/{opencode.db,log,snapshot}` — outside the scope, unencrypted,
indefinitely. Nothing in this skill cleans that up. Additionally, opencode appends a scoped
`external_directory allow …/tool-output/*` rule after the denies, so writes there remain
permitted; the permission verifier reports such scoped allows rather than pretending they do
not exist.

The write/shell/web restriction was **measured**, on opencode 1.18.11, against a repository
deliberately built to break out of it — a same-named permissive agent, an `opencode.json`
granting edit, and an `AGENTS.md` prompt injection demanding a file edit, a new file and a
shell command. None succeeded. That is a measurement of one version, not a guarantee the
vendor offers. Re-verify after an upgrade; `references/opencode_notes.md` has the procedure,
including the earlier occasion when a conclusion drawn from the wrong command had to be
retracted.

### 3. Project plugins, and why the skill refuses some scopes

opencode imports and executes JavaScript found under a project's `.opencode/` **before any
agent, permission or model exists**. No flag prevents it — `--pure` and
`OPENCODE_DISABLE_PROJECT_CONFIG=1` both fail to stop it during `opencode run`. In this
skill the filtered mirror is what protects you (hidden directories never reach the mirror,
so opencode is never pointed at the repository's `.opencode/`); on top of that, a scope
shipping executable code under `.opencode/` is refused outright with `RESULT=REFUSED`.

If you hit that refusal, **do not work around it**. Review the repository with
`--backend or-api`, which never executes anything.

### 4. A hostile `PATH` defeats everything here

Every control described in this document is implemented by `bash`, `python3` and possibly
`opencode`, resolved through your `PATH`. A malicious binary earlier on `PATH` — or a
compromised Python package shadowing the standard library — can do anything the controls
claim to prevent, and no in-process check can detect it.

This is the correct trade-off for a developer tool that has to run your interpreters, and it
is the same assumption `make`, `npm run` and every CLI you already use depend on. It is
stated here because an unstated assumption is indistinguishable from an oversight.

### 5. Other limits worth knowing

- **The report guard is narrow on purpose.** One redactor (`scripts/redact.py`) masks the
  skill's own diagnostics with the full pattern set. The finished report is then scanned
  with the **high-confidence** rules only — a vendor prefix followed by 16+ opaque
  characters — and any hit is masked in place, announced in a `[!CAUTION]` block inside the
  report, and signalled as `SECRET_SCAN=MASKED` on stderr. If you see that, treat the
  credential as compromised and rotate it. The heuristic rules are not applied to the
  critic's prose, because they fire on ordinary argument *about* credentials and a mangled
  report is not a review. So: a real vendor token echoed by a model is masked; a novel or
  encoded credential shape in the model's answer is not.
- **Reports land in your project directory.** They quote your source and error text. The
  skill warns when a report is not covered by your `.gitignore`, but deliberately does not
  edit it for you.
- **Mode B's `run_local.sh` is not a jail.** It verifies only that the script lives under the
  sandbox directory. **Your review of the proposed script is the security boundary.**
- **The reviewed repository is untrusted input.** It cannot configure the review — a
  `.bettercallopencode.env` inside the scope is ignored, loudly — and its file contents are
  fenced adaptively and its pathnames rendered inert, so neither can break out of the prompt
  structure. The critic is told to report anything that tries as a prompt-injection finding.
  That is mitigation, not immunity: prompt injection against an LLM has no complete defence.
- **The state directory is sensitive.** `~/.bettercallopencode` is `0700` and holds the
  usage ledger (which projects you reviewed, when, at what token cost) and, transiently, a
  plaintext copy of the reviewed source. Run directories are deleted on exit and reaped
  after 24 hours; `BCOPENCODE_KEEP_RUN=1` keeps them, with error streams redacted in place.
- **Your key is never written to a report or to git**, but it is read from the environment,
  from OpenCode's `auth.json`, or from a user-owned config file, and it is sent to
  OpenRouter in an `Authorization` header. Rotate it if any of those are exposed.

## Out of scope

- OpenRouter's or its upstream providers' handling of the code you send them. Read their
  terms; free `:free` variants often differ from paid ones.
- Vulnerabilities in `opencode` itself. Report those to
  [opencode](https://opencode.ai). This project will document a mitigation if one exists,
  as it has for the plugin-execution behaviour above.
- The quality or correctness of a model's review. The critic proposes; a human or Claude
  triages. That is the design, not a defect.
</content>
