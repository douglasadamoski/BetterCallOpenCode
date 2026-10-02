#!/usr/bin/env python3
"""delegate.py — hand ONE task to a model on any connected provider, as a worker.

This is what lets a larger Claude workflow (a deep-research fan-out, a panel, a
second-opinion step) put some of its workers on OpenRouter or any other provider opencode
has connected. It runs one task and writes one result file; a Claude subagent
(agents/bcoc-delegate.md) wraps it so Agent/Workflow can spawn many in parallel.

  delegate.py --role researcher --model auto --prompt-file task.md --out result.md
  delegate.py --role analyst --model openrouter/nvidia/nemotron-3-super-120b-a12b:free \\
              --prompt-file q.md --scope ./src --fallback auto
  delegate.py --role researcher --backend opencode --model opencode/big-pickle \\
              --prompt-file task.md --session my-research-1

Roles
  researcher       gather and synthesise; the only role that may use web tools (opencode
                   backend). Must say what it could not verify.
  analyst          reason over the supplied material; no tools.
  reviewer         critique and propose; never claims to have changed anything.
  coder-readonly   propose code in the reply. Nothing is ever written to disk by the model.

Backends
  or-api (default)  one HTTPS POST to the provider's /chat/completions. No filesystem, no
                    shell, no tools: the model sees only the text this script sends.
  opencode          `opencode run` as an agent against a FILTERED MIRROR of --scope.
                    Gated: nothing is spent unless the agent's deny rules verifiably
                    resolve (see _verify_* below); otherwise RESULT=REFUSED.

Money gate: a model must be zero-cost (published price, or the provider's recorded policy
— see discover_providers.py --set-policy) unless --allow-paid. Unknown cost is blocked.

Output: the result file, plus a final `RESULT=<WORD>` line on stdout (same vocabulary as
every other wrapper in this skill). `--json-out` also writes <out>.json. Credential shapes
in the result are masked in place and announced as `SECRET_SCAN=MASKED` on stderr.
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPTS = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import discover_providers as dp  # noqa: E402
import ledger  # noqa: E402
import select_models as sm  # noqa: E402
from redact import redact  # noqa: E402
import redact as _redact_mod  # noqa: E402

_spec = importlib.util.spec_from_file_location("or_client_for_delegate", SCRIPTS / "or_client.py")
orc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(orc)

ROLES: Dict[str, Dict[str, Any]] = {
    "researcher": {
        "task": "research", "web": True,
        "system": (
            "You are a research worker inside a larger investigation. Answer the task you are "
            "given as thoroughly as the material allows. Separate what you KNOW from what you "
            "INFER, and say plainly what you could not verify. Cite sources you actually used "
            "(URL or document name); never invent a citation. Do not claim to have run code, "
            "opened files you were not given, or changed anything."),
    },
    "analyst": {
        "task": "review", "web": False,
        "system": (
            "You are an analyst. Reason carefully over exactly the material provided. State "
            "your assumptions. Flag where the material is insufficient instead of guessing. "
            "Do not claim to have run code or accessed anything you were not given."),
    },
    "reviewer": {
        "task": "review", "web": False,
        "system": (
            "You are an outside critic. CRITICIZE and PROPOSE only; you do not change anything. "
            "Order findings by severity, give the evidence for each, and say what you did not "
            "check. Do not claim to have edited files or run tests."),
    },
    "coder-readonly": {
        "task": "code", "web": False,
        "system": (
            "You are a coding assistant with no ability to write files or run commands. Put any "
            "code in your reply in fenced blocks and state the target file for each. Never claim "
            "that you saved, ran or tested anything."),
    },
}

NO_RETRY = {"AUTH", "PAID_BLOCKED", "BAD_ARGS", "REFUSED", "TIMEOUT", "TRUNCATED", "OK", "CAP"}
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
MAX_PROMPT_BYTES = 4_000_000
MAX_IMAGE_BYTES = 10_000_000
MAX_PDF_CHARS = 400_000


class Bad(Exception):
    """A pre-flight failure. Nothing was spent."""


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")[:60] or "x"


# --------------------------------------------------------------------------- model resolve
def resolve_model(spec: str, cache: Dict[str, Any], role: str) -> Tuple[str, str]:
    """-> (provider, model). `auto` picks the best zero-cost model for the role's task."""
    if spec == "auto":
        rows = sm.rank(cache, ROLES[role]["task"])
        if not rows:
            raise Bad("--model auto: no usable zero-cost model in the capabilities cache. "
                      "Run discover_providers.py, and settle any NEEDS_POLICY provider.")
        top = sm.pick(rows, 1)[0]
        return top["provider"], top["model"]
    provs = cache.get("providers") or {}
    head, _, rest = spec.partition("/")
    if rest and head in provs:
        return head, rest
    # A bare OpenRouter id (`vendor/model:free`) or an explicit `openrouter/...` with no
    # cache yet: OpenRouter is the one provider whose base URL and key rules are known.
    if head == "openrouter" and rest:
        return "openrouter", rest
    if "openrouter" in provs or not provs:
        if ":free" in spec or spec == "openrouter/free":
            return "openrouter", spec
    raise Bad(f"cannot tell which provider serves {spec!r}. Use provider/model — "
              f"known providers: {', '.join(sorted(provs)) or 'none (run discover_providers.py)'}")


def cost_of(cache: Dict[str, Any], pid: str, model: str) -> str:
    c = dp.effective_cost(cache, pid, model)
    if c == dp.COST_UNKNOWN and pid == "openrouter":
        # not in the cache (e.g. discovery was skipped): the id suffix is the rule there
        c = dp.COST_FREE if orc.is_free_model(model) else dp.COST_UNKNOWN
    return c


def model_caps(cache: Dict[str, Any], pid: str, model: str) -> Dict[str, Any]:
    return ((cache.get("providers") or {}).get(pid) or {}).get("models", {}).get(model) or {}


# --------------------------------------------------------------------------- inputs
def read_prompt(path: str) -> str:
    p = Path(path)
    try:
        if p.stat().st_size > MAX_PROMPT_BYTES:
            raise Bad(f"prompt file is larger than {MAX_PROMPT_BYTES} bytes")
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise Bad(f"cannot read --prompt-file: {e}")


def pack_scope(scope: str, max_input_tokens: int) -> str:
    """Context goes through pack_context.py — the SAME secret filter every review uses."""
    if not Path(scope).is_dir():
        raise Bad(f"--scope is not a directory: {scope}")
    with tempfile.TemporaryDirectory(prefix="bcoc.pack.") as td:
        out = Path(td) / "pack.txt"
        r = subprocess.run([sys.executable, str(SCRIPTS / "pack_context.py"), scope,
                            "--max-input-tokens", str(max_input_tokens), "--out", str(out)],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            raise Bad("packing --scope failed or produced nothing: " + redact(r.stderr)[:300])
        return out.read_text(encoding="utf-8", errors="replace")


def pdf_text(path: str) -> str:
    if not shutil.which("pdftotext"):
        raise Bad("--pdf needs `pdftotext` (poppler-utils), which is not installed")
    r = subprocess.run(["pdftotext", "-layout", "--", path, "-"], capture_output=True, text=True,
                       timeout=120)
    if r.returncode != 0:
        raise Bad(f"pdftotext failed on {path}: {r.stderr[:200]}")
    # an attachment is the caller's explicit choice, but it still goes through the redactor
    return redact(r.stdout)[:MAX_PDF_CHARS]


def image_part(path: str) -> Dict[str, Any]:
    mime = mimetypes.guess_type(path)[0] or ""
    if mime not in IMAGE_TYPES:
        raise Bad(f"--image {path}: unsupported type {mime or 'unknown'}")
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        raise Bad(f"--image: {e}")
    if len(data) > MAX_IMAGE_BYTES:
        raise Bad(f"--image {path}: larger than {MAX_IMAGE_BYTES} bytes")
    b64 = base64.b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


# --------------------------------------------------------------------------- sessions
def session_path(sid: str, state: Path) -> Path:
    return state / "sessions" / f"{sid}.json"


def load_session(sid: str, state: Path) -> List[Dict[str, Any]]:
    try:
        data = json.loads(session_path(sid, state).read_text(encoding="utf-8"))
        return [m for m in data if isinstance(m, dict) and m.get("role") in ("user", "assistant")
                and isinstance(m.get("content"), str)]
    except (OSError, ValueError):
        return []


def session_model(sid: str, state: Path) -> Optional[str]:
    try:
        return json.loads(session_path(sid, state).with_suffix(".meta").read_text()).get("model")
    except (OSError, ValueError, AttributeError):
        return None


def save_session(sid: str, state: Path, history: List[Dict[str, Any]], model: str = "") -> None:
    p = session_path(sid, state)
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(history, f)
    os.replace(tmp, p)
    meta = p.with_suffix(".meta")
    fd = os.open(str(meta), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"model": model}, f)


# --------------------------------------------------------------------------- or-api backend
def run_or_api(pid: str, model: str, cache: Dict[str, Any], role: str, user_text: str,
               images: List[Dict[str, Any]], history: List[Dict[str, Any]], a) -> Tuple[str, Dict[str, Any]]:
    prov = (cache.get("providers") or {}).get(pid) or {}
    base = prov.get("base_url") or ("https://openrouter.ai/api/v1" if pid == "openrouter" else "")
    if not base:
        return "BAD_ARGS", {"error": f"no base URL known for provider {pid!r}; the or-api "
                            f"backend needs one (use --backend opencode)"}
    if not dp.safe_base_url(base):
        return "BAD_ARGS", {"error": f"refusing to send a credential to {base!r}: base URLs "
                            f"must be https (plain http only to localhost)"}
    cfg = dp.opencode_provider_config().get(pid, {})
    key = dp._provider_key(pid, cfg.get("env") or [])
    if not key:
        return "AUTH", {"error": f"no credential found for provider {pid!r} "
                        f"(env: {', '.join(cfg.get('env') or []) or 'n/a'})"}
    content: Any = user_text
    if images:
        content = [{"type": "text", "text": user_text}] + images
    messages = [{"role": "system", "content": ROLES[role]["system"]}, *history,
                {"role": "user", "content": content}]
    extra = {"response_format": {"type": "json_object"}} if a.json_mode else None
    return orc.chat(key, model, messages, max_tokens=a.max_tokens, temperature=a.temperature,
                    timeout=a.timeout, allow_paid=True,
                    base_url=None if pid == "openrouter" else base, extra_body=extra)


# --------------------------------------------------------------------------- opencode backend
def opencode_major() -> Optional[int]:
    m = re.search(r"(\d+)\.\d+", dp.opencode_version() or "")
    return int(m.group(1)) if m else None


def _global(resource: Any) -> bool:
    r = str(resource if resource is not None else "*").strip()
    return r == "" or re.fullmatch(r"[*/]+", r) is not None


def verify_v2(agents: Any, agent: str, must_block: List[str]) -> Tuple[bool, str]:
    """opencode v2: `opencode debug agents` is JSON; each agent has an ordered
    `permissions` list of {action, resource, effect}. Resolution is last-global-match-wins.

    `ask` counts as blocked: measured on 2.0.22, a non-interactive `opencode run` without
    --auto auto-rejects every `ask` ("This non-interactive run cannot ask the user for
    permission, so the request was rejected"). This script never passes --auto. A scoped
    (non-global) allow does not flip the verdict, same as the v1 verifier.
    """
    ag = next((x for x in agents if isinstance(x, dict) and x.get("id") == agent), None) \
        if isinstance(agents, list) else None
    if ag is None:
        return False, f"agent {agent!r} not found in `opencode debug agents`"
    rules = [r for r in (ag.get("permissions") or []) if isinstance(r, dict)]
    bad = []
    # A scoped allow is NOT harmless for these: `bash allow "git *"` is arbitrary code
    # execution (`git -c core.pager=...`), `edit allow <path>` is a write. (external_directory
    # is different: the built-in agents carry scoped allows for opencode's own scratch dirs.)
    for r in rules:
        if (r.get("effect") == "allow" and r.get("action") in ("bash", "edit", "subagent", "task")
                and not _global(r.get("resource"))):
            bad.append(f"{r.get('action')} allowed for {r.get('resource')!r}")
    # The default must not be a grant either: anything not named below (MCP tools, skills,
    # write-class actions) falls through to it.
    default = None
    for r in rules:
        if r.get("action") == "*" and _global(r.get("resource")):
            default = r.get("effect")
    if default not in ("deny", "ask"):
        bad.append(f"default(*)={default or 'absent'}")
    for perm in must_block:
        eff = None
        for r in rules:
            if r.get("action") in (perm, "*") and _global(r.get("resource")):
                eff = r.get("effect")
        if eff not in ("deny", "ask"):
            bad.append(f"{perm}={eff or 'absent'}")
    if bad:
        return False, "permissions are not blocked: " + ", ".join(bad)
    return True, ""


def _must_block(role: str, v2: bool) -> List[str]:
    base = ["edit", "bash", "external_directory"]
    base.append("subagent" if v2 else "task")
    if not ROLES[role]["web"]:
        base += ["webfetch", "websearch"] if v2 else ["webfetch"]
    return base


def _run_group(argv: List[str], env: Dict[str, str], cwd: str, timeout: int) -> Tuple[int, str, str, bool]:
    """Run in its own process group so a timeout kills the whole tree."""
    p = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         stdin=subprocess.DEVNULL, text=True, start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
        return p.returncode, out, err, False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        out, err = p.communicate()
        return 124, out or "", err or "", True


def parse_v2_events(out: str) -> Tuple[str, List[str]]:
    texts, errs = [], []
    for line in out.splitlines():
        try:
            o = json.loads(line)
        except ValueError:
            continue
        part = o.get("part") if isinstance(o, dict) else None
        if not isinstance(part, dict):
            continue
        if o.get("type") == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
        st = part.get("state")
        if o.get("type") == "tool_use" and isinstance(st, dict) and st.get("status") == "error":
            errs.append(str(st.get("error"))[:200])
    return "".join(texts).strip(), errs


def classify_cli_failure(rc: int, err: str) -> str:
    low = err.lower()
    if re.search(r"\b429\b|rate.?limit|quota|too many requests|credit", low):
        return "QUOTA"
    if re.search(r"unauthori[sz]ed|invalid api key|\b401\b|\b403\b|not authenticated|no credential", low):
        return "AUTH"
    return "ERROR"


_ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "TMPDIR", "SHELL",
             "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR",
             "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
             "https_proxy", "http_proxy", "no_proxy")


def child_env(pid: str) -> Dict[str, str]:
    """The environment an opencode child gets: the basics, OPENCODE_*, and ONLY this
    provider's own credential variable(s). Not os.environ: if the agent were ever not the
    restricted one, every other provider's key would be in its reach."""
    keep = set(_ENV_KEEP)
    keep.update(dp.opencode_provider_config().get(pid, {}).get("env") or [])
    if pid == "openrouter":
        keep.add("OPENROUTER_API_KEY")
    env = {k: v for k, v in os.environ.items() if k in keep or k.startswith("OPENCODE_")}
    env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
    return env


def run_opencode(pid: str, model: str, role: str, prompt_text: str, a) -> Tuple[str, Dict[str, Any]]:
    if not shutil.which("opencode"):
        return "BAD_ARGS", {"error": "`opencode` is not on PATH"}
    major = opencode_major()
    if major is None:
        return "REFUSED", {"error": "cannot determine the opencode version, so its agent "
                           "permissions cannot be verified"}
    v2 = major >= 2
    agent = a.agent or ("explore" if v2 else ("bcoc-research" if ROLES[role]["web"] else "bcoc-review"))

    run_dir = Path(tempfile.mkdtemp(prefix="bcoc.deleg."))
    os.chmod(run_dir, 0o700)
    try:
        # the agent runs in a filtered mirror of the scope, never the raw tree
        cwd = run_dir / "mirror"
        if a.scope:
            r = subprocess.run([sys.executable, str(SCRIPTS / "pack_context.py"), a.scope,
                                "--mirror-to", str(cwd)], capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                return "ERROR", {"error": "could not build a filtered mirror: " + redact(r.stderr)[:300]}
        else:
            cwd.mkdir()
        env = child_env(pid)

        if v2:
            # Verified with the SAME cwd and env the real run uses, so what is checked is
            # what will be resolved (a project config in some other cwd must not decide it).
            agents = None
            for attempt in range(3):
                raw = subprocess.run(["opencode", "debug", "agents"], capture_output=True, text=True,
                                     timeout=60, stdin=subprocess.DEVNULL, cwd=str(cwd), env=env).stdout
                try:
                    agents = json.loads(raw)
                except ValueError:
                    agents = None
                # A directory opencode has not seen yet answers `[]` the first time while it
                # registers the project (measured, 2.0.22). Empty is "not ready", not "no rules".
                if agents:
                    break
                time.sleep(2)
            ok, why = verify_v2(agents, agent, _must_block(role, True))
        else:
            cfgdir = Path(os.environ.get("BCOPENCODE_OPENCODE_CONFIG_DIR") or SKILL_DIR / "opencode-config")
            (cfgdir / "agents").mkdir(parents=True, exist_ok=True)
            src = SKILL_DIR / "agents" / f"{agent}.md"
            if src.is_file():
                shutil.copyfile(src, cfgdir / "agents" / f"{agent}.md")
            env["OPENCODE_CONFIG_DIR"] = str(cfgdir)
            deny = {"edit": "deny", "write": "deny", "patch": "deny", "bash": "deny",
                    "task": "deny", "external_directory": "deny"}
            if not ROLES[role]["web"]:
                deny["webfetch"] = "deny"
            env["OPENCODE_PERMISSION"] = json.dumps(deny)
            dump = subprocess.run(["opencode", "agent", "list"], capture_output=True, text=True,
                                  timeout=60, env=env, stdin=subprocess.DEVNULL, cwd=str(cwd)).stdout
            vr = subprocess.run([sys.executable, str(SCRIPTS / "verify_agent_permissions.py"),
                                 agent, *_must_block(role, False)], input=dump,
                                capture_output=True, text=True)
            ok, why = vr.returncode == 0, vr.stderr.strip()
        if not ok:
            return "REFUSED", {"error": f"could not verify agent {agent!r} is restricted "
                               f"(opencode {major}.x): {why}. Nothing was spent. Use --backend or-api."}

        pf = run_dir / "prompt.md"
        pf.write_text(prompt_text, encoding="utf-8")
        os.chmod(pf, 0o600)
        argv = ["opencode", "run",
                "Read the attached file: it contains your full task. Follow it and answer.",
                "--agent", agent, "-m", f"{pid}/{model}", "--title", f"bcoc-delegate {role}"]
        if v2:
            argv += ["--format", "json"]
        else:
            argv += ["--dir", str(cwd), "--pure", "--format", "default"]
        if a.session:
            argv += ["-s", a.session]
        argv += ["-f", str(pf)]       # message first, -f LAST: both are arrays (see opencode_review.sh)
        rc, out, err, timed_out = _run_group(argv, env, str(cwd), a.timeout)
        err = redact(err)
        if timed_out:
            return "TIMEOUT", {"error": f"no answer within {a.timeout}s", "model": f"{pid}/{model}"}
        if "is a subagent, not a primary agent" in err:
            return "ERROR", {"error": "opencode refused the agent as a subagent and fell back to "
                             "its default agent. The request was spent; the result is NOT trusted.",
                             "model": f"{pid}/{model}"}
        if v2:
            text, tool_errs = parse_v2_events(out)
        else:
            text, tool_errs = out.strip(), []
        if rc != 0 or not text:
            res = classify_cli_failure(rc, err) if rc != 0 else "ERROR"
            return res, {"error": (err.strip() or "opencode returned no text")[:800],
                         "model": f"{pid}/{model}"}
        return "OK", {"content": redact(text), "model": f"{pid}/{model}", "finish_reason": "stop",
                      "usage": {}, "denied_tool_calls": tool_errs}
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


# --------------------------------------------------------------------------- output
def write_result(path: Path, role: str, model: str, backend: str, result: str,
                 env: Dict[str, Any], session: Optional[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    usage = env.get("usage") or {}
    head = ["---", f"role: {role}", f"model: {model}", f"backend: {backend}",
            f"result: {result}", f"finish_reason: {env.get('finish_reason')}",
            f"tokens: {usage.get('total_tokens')}", f"session: {session or ''}",
            f"created: {_utc_stamp()}", "---", ""]
    body = env.get("content") or ""
    if result not in ("OK", "TRUNCATED") and env.get("error"):
        body = f"> [!WARNING]\n> {redact(str(env['error']))[:1500]}\n\n{body}"
    elif result == "TRUNCATED":
        body = ("> [!WARNING]\n> The answer hit the token limit and may be incomplete.\n\n" + body)
    path.write_text("\n".join(head) + body + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def secret_scan(path: Path) -> None:
    try:
        rc = _redact_mod.guard_file(str(path))
    except Exception:
        rc = 1
    if rc == _redact_mod.GUARD_REDACTED:
        print("bcoc: WARNING — the result carried a credential-shaped string; it was masked "
              f"in {path}. Treat that credential as COMPROMISED and rotate it.", file=sys.stderr)
        print("SECRET_SCAN=MASKED", file=sys.stderr)
    elif rc != _redact_mod.GUARD_CLEAN:
        print(f"bcoc: WARNING — could not scan {path}; read it before committing it.", file=sys.stderr)
        print("SECRET_SCAN=UNVERIFIED", file=sys.stderr)


# --------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--role", choices=sorted(ROLES), required=True)
    ap.add_argument("--model", default="auto", help="provider/model, or `auto`")
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--scope", help="directory to ship as context (filtered like every review)")
    ap.add_argument("--backend", choices=("or-api", "opencode"), default="or-api")
    ap.add_argument("--session", help="continue a multi-turn conversation (id: letters, digits, _ . -)")
    ap.add_argument("--out", help="result file (default: <state>/delegate/<ts>_<role>_<model>.md)")
    ap.add_argument("--json-out", action="store_true", help="also write <out>.json")
    ap.add_argument("--json-mode", action="store_true", help="ask the provider for a JSON object")
    ap.add_argument("--image", action="append", default=[], help="attach an image (vision models)")
    ap.add_argument("--pdf", action="append", default=[], help="attach a PDF as extracted text")
    ap.add_argument("--fallback", default="", help="comma list of provider/model, or `auto`")
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--max-tokens", type=int, default=_env_int("BCOPENCODE_MAX_TOKENS", 16384))
    ap.add_argument("--max-input-tokens", type=int, default=_env_int("BCOPENCODE_MAX_INPUT_TOKENS", 80000))
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--timeout", type=int, default=_env_int("BCOPENCODE_TIMEOUT", 600))
    ap.add_argument("--rpm", type=int, default=None,
                    help="shared per-minute budget across workers (default 20 for OpenRouter free, off otherwise)")
    ap.add_argument("--cap", type=int, default=_env_int("BCOPENCODE_CAP", 200))
    ap.add_argument("--agent", help="opencode agent override (opencode backend)")
    ap.add_argument("--allow-paid", action="store_true",
                    help="permit non-zero-cost models. ONLY with the user's explicit consent.")
    ap.add_argument("--offline", action="store_true", help="do not refresh the capabilities cache")
    return ap


def finish(result: str, out_path: Optional[Path] = None, model: str = "") -> int:
    if out_path:
        print(f"OUT={out_path}")
    if model:
        print(f"MODEL={model}")
    # Always exit 0: callers branch on the final RESULT= line, like every other wrapper in
    # this skill. A non-zero exit would read as a crash and hide the vocabulary.
    print(f"RESULT={result}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    a = build_parser().parse_args(argv)
    state = ledger.state_dir()
    try:
        if a.session is not None and not SESSION_RE.match(a.session):
            raise Bad("--session must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
        if a.max_attempts < 1 or a.max_tokens < 1 or a.timeout < 1:
            raise Bad("--max-attempts, --max-tokens and --timeout must be positive")
        if a.backend == "opencode" and (a.image or a.pdf or a.json_mode or a.fallback):
            raise Bad("--image/--pdf/--json-mode/--fallback are or-api features")
        if a.agent is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}", a.agent):
            raise Bad("--agent must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
        prompt = read_prompt(a.prompt_file)
        try:
            cache, _ = dp.ensure(offline=a.offline)
        except Exception as e:  # discovery must never be the reason a task fails to run
            print(f"bcoc: discovery skipped ({type(e).__name__}); using the saved cache", file=sys.stderr)
            cache = dp.load_cache()
        pid, model = resolve_model(a.model, cache, a.role)
        attempts: List[Tuple[str, str]] = [(pid, model)]
        if a.fallback:
            if a.fallback == "auto":
                tried = {f"{pid}/{model}"}
                rows = [r for r in sm.rank(cache, ROLES[a.role]["task"]) if r["id"] not in tried]
                attempts += [(r["provider"], r["model"]) for r in sm.pick(rows, a.max_attempts - 1)]
            else:
                for spec in [s.strip() for s in a.fallback.split(",") if s.strip()]:
                    attempts.append(resolve_model(spec, cache, a.role))
        attempts = attempts[: a.max_attempts]

        for img in a.image:
            if "image" not in [m.lower() for m in (model_caps(cache, pid, model).get("modalities") or [])]:
                raise Bad(f"--image: {pid}/{model} is not known to accept images "
                          f"(pick one with `select_models.py --task vision`)")
        history = load_session(a.session, state) if a.session else []
        if history:
            prev = session_model(a.session, state)
            if prev and prev != f"{pid}/{model}":
                print(f"bcoc: NOTE — session {a.session!r} was last answered by {prev}; its history "
                      f"is now being sent to {pid}/{model}.", file=sys.stderr)
        user_text = prompt
        if a.scope and a.backend == "or-api":
            user_text += "\n\n=== PROJECT CONTEXT ===\n" + pack_scope(a.scope, a.max_input_tokens)
        for pdf in a.pdf:
            user_text += f"\n\n=== PDF: {Path(pdf).name} ===\n" + pdf_text(pdf)
        images = [image_part(p) for p in a.image]
    except Bad as e:
        print(f"bcoc: {e}", file=sys.stderr)
        return finish("BAD_ARGS")

    out_path = Path(a.out) if a.out else state / "delegate" / (
        f"{_utc_stamp()}_{a.role}_{_slug(f'{pid}_{model}')}.md")

    used = ledger.cap_used_today()
    final_result, final_env, final_model = "ERROR", {"error": "no attempt was made"}, f"{pid}/{model}"
    for i, (apid, amodel) in enumerate(attempts):
        full = f"{apid}/{amodel}"
        cost = cost_of(cache, apid, amodel)
        if cost != dp.COST_FREE and not a.allow_paid:
            why = ("its cost is unknown — settle it with discover_providers.py --set-policy"
                   if cost == dp.COST_UNKNOWN else "it is not zero-cost")
            msg = f"{full} is blocked: {why}. Pass --allow-paid only with the user's explicit consent."
            ledger.append_row("PAID_BLOCKED", full, a.backend, a.role, False)
            if i > 0:
                # A fallback that cannot run must not erase what the primary actually returned.
                final_env = {**final_env, "error": f"{final_env.get('error') or ''} | fallback skipped: {msg}".strip(" |")}
            else:
                final_result, final_env, final_model = "PAID_BLOCKED", {"error": msg}, full
            break
        if used is None or used >= a.cap:
            final_result, final_env, final_model = "CAP", {
                "error": "local daily cap reached" if used is not None else "cannot read the ledger"}, full
            ledger.append_row("CAP", full, a.backend, a.role, cost == dp.COST_FREE)
            break
        rpm = a.rpm if a.rpm is not None else (_env_int("BCOPENCODE_FREE_RPM", 20)
                                               if apid == "openrouter" else 0)
        ledger.rpm_acquire(rpm)
        if a.backend == "or-api":
            result, env = run_or_api(apid, amodel, cache, a.role, user_text, images, history, a)
        else:
            result, env = run_opencode(apid, amodel, a.role, user_text, a)
        recorded = ledger.append_row(result, full, a.backend, a.role, cost == dp.COST_FREE, env.get("usage"))
        final_result, final_env, final_model = result, env, full
        if not recorded:
            # A spent request with no ledger row means the cap can never advance. Do not
            # keep spending into an unmetered ledger.
            print("bcoc: the ledger is not writable; stopping so spend stays metered.", file=sys.stderr)
            break
        if result not in ledger.NOT_BILLED:
            used = (used or 0) + 1
        if result == "OK":
            dp.record_ok(apid, amodel)
        retry = result not in NO_RETRY and not (result == "QUOTA" and env.get("http") == 402)
        if result == "ERROR" and env.get("http") not in (404, 405) and not str(env.get("http", "")).startswith("5"):
            retry = False       # an unusable answer from a live model is not a reason to spend again
        if not retry or i == len(attempts) - 1:
            break
        print(f"bcoc: {full} -> {result}; trying {attempts[i + 1][0]}/{attempts[i + 1][1]}", file=sys.stderr)

    write_result(out_path, a.role, final_model, a.backend, final_result, final_env, a.session)
    secret_scan(out_path)
    if a.json_out:
        side = out_path.with_name(out_path.name + ".json")
        side.write_text(json.dumps({"role": a.role, "model": final_model, "backend": a.backend,
                                    "result": final_result,
                                    "usage": final_env.get("usage"),
                                    "finish_reason": final_env.get("finish_reason"),
                                    "error": redact(str(final_env.get("error") or "")) or None,
                                    "out": str(out_path)}, indent=2), encoding="utf-8")
        os.chmod(side, 0o600)
        print(f"JSON={side}")
    if a.session and final_result in ("OK", "TRUNCATED") and a.backend == "or-api":
        # Stored REDACTED: the history is replayed to whatever model the next turn names,
        # possibly on another provider, so a credential-shaped string must not persist here.
        save_session(a.session, state, history + [
            {"role": "user", "content": redact(prompt)},
            {"role": "assistant", "content": redact(final_env.get("content") or "")}],
            model=final_model)
    return finish(final_result, out_path, final_model)


if __name__ == "__main__":
    sys.exit(main())
