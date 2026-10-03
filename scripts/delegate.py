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
import fcntl
import importlib.util
import json
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import threading
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
import opencode_v2 as ov2  # noqa: E402
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


def withheld_names(meta: Dict[str, Any]) -> List[str]:
    """Files the secret filter kept out of the model's view (by name or by content)."""
    names = list(meta.get("secrets_skipped_by_name") or []) + list(meta.get("secrets_skipped_by_content") or [])
    return sorted(set(str(n) for n in names))


def withheld_note(names: List[str]) -> str:
    """Said to the model AND recorded in the result. "Not reviewed" must never be
    indistinguishable from "reviewed and clean": the filter errs toward withholding, and
    ordinary prose that starts with a short word for a round of work, then a colon and a sentence, can look like a password assignment."""
    if not names:
        return ""
    shown = ", ".join(names[:30]) + (f" (+{len(names) - 30} more)" if len(names) > 30 else "")
    return ("\n\n[NOTE from the harness: the secret filter withheld these files from your view: "
            f"{shown}. They exist; do not conclude anything about their content.]")


def pack_scope(scope: str, max_input_tokens: int) -> Tuple[str, List[str]]:
    """Context goes through pack_context.py — the SAME secret filter every review uses."""
    if not Path(scope).is_dir():
        raise Bad(f"--scope is not a directory: {scope}")
    with tempfile.TemporaryDirectory(prefix="bcoc.pack.") as td:
        out, meta_f = Path(td) / "pack.txt", Path(td) / "meta.json"
        r = subprocess.run([sys.executable, str(SCRIPTS / "pack_context.py"), scope,
                            "--max-input-tokens", str(max_input_tokens), "--out", str(out),
                            "--meta-out", str(meta_f)],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            raise Bad("packing --scope failed or produced nothing: " + redact(r.stderr)[:300])
        try:
            meta = json.loads(meta_f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        return out.read_text(encoding="utf-8", errors="replace"), withheld_names(meta)


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


# One implementation of the 2.x gate, shared with opencode_review.sh (scripts/opencode_v2.py).
verify_v2 = ov2.verify_v2
parse_v2_events = ov2.parse_events


def _must_block(role: str, v2: bool) -> List[str]:
    if v2:
        return ov2.must_block(ROLES[role]["web"])
    base = ["edit", "bash", "external_directory", "task"]
    if not ROLES[role]["web"]:
        base.append("webfetch")
    return base


_CHILD_PGIDS: set = set()


def _kill_children() -> None:
    for pg in list(_CHILD_PGIDS):
        try:
            os.killpg(pg, signal.SIGKILL)
        except OSError:
            pass
    _CHILD_PGIDS.clear()


def _on_signal(signum: int, _frame: Any) -> None:
    """SIGTERM/SIGINT: the opencode child runs in its OWN session, so killing this process
    alone would orphan it (and its temp dir). Kill it, then unwind normally so `finally`
    blocks remove the run directory."""
    _kill_children()
    raise SystemExit(130)


def _run_stream(argv: List[str], env: Dict[str, str], cwd: str, total_timeout: int,
                stall_timeout: int) -> Dict[str, Any]:
    """Run `opencode run --format json`, reading its events AS THEY ARRIVE.

    Two clocks, because they catch different failures: a total deadline, and a STALL watchdog
    (no event for `stall_timeout` s). Measured: a task that normally takes ~2 min once sat
    for the whole 900 s total timeout because one call hung, and a total timeout alone loses
    all of that time. The child runs in its own process group so a kill takes the whole tree.
    Returns rc, the raw event text, stderr, why it was killed (None | "timeout" | "stall"),
    the opencode session id, the largest gap between events, and the elapsed time.
    """
    errf = tempfile.TemporaryFile("w+")
    p = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=errf,
                         stdin=subprocess.DEVNULL, text=True, bufsize=1, start_new_session=True)
    _CHILD_PGIDS.add(p.pid)
    start = time.monotonic()
    st = {"last": start, "sid": None, "gap": 0.0}
    lines: List[str] = []

    def reader() -> None:
        for line in p.stdout:                       # type: ignore[union-attr]
            now = time.monotonic()
            st["gap"] = max(st["gap"], now - st["last"])
            st["last"] = now
            lines.append(line)
            if st["sid"] is None:
                m = re.search(r'"sessionID"\s*:\s*"(ses_[A-Za-z0-9]+)"', line)
                if m:
                    st["sid"] = m.group(1)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    reason = None
    while p.poll() is None:
        now = time.monotonic()
        if total_timeout and now - start > total_timeout:
            reason = "timeout"
        elif stall_timeout and now - st["last"] > stall_timeout:
            reason = "stall"
        if reason:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            break
        time.sleep(0.2)
    p.wait()
    _CHILD_PGIDS.discard(p.pid)
    t.join(timeout=5)
    errf.seek(0)
    err = errf.read()
    errf.close()
    return {"rc": 124 if reason else p.returncode, "out": "".join(lines), "err": err,
            "reason": reason, "sid": st["sid"], "max_gap": round(st["gap"], 1),
            "elapsed": round(time.monotonic() - start, 1)}


FINALIZE_PROMPT = ("Stop researching now; no more tool calls. Using ONLY what you have already found "
                   "in this session, write your final answer in the format the task asked for. "
                   "State clearly what you had not finished or could not verify.")


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


def ensure_workdir(cwd: Path, agent: str, role: str, pid: str, scope: Optional[str],
                   major: int) -> Tuple[bool, str, List[str], Dict[str, str]]:
    """Make `cwd` a filtered, prepared and VERIFIED working directory for the restricted agent.

    -> (ok, reason, withheld files, env). On opencode 2.x the result is recorded in
    `.bcoc_ready.json` so that many units sharing one (scope, role) build and verify it ONCE:
    verification costs a few seconds per directory (a new directory answers empty the first
    time), which at hundreds of units dwarfs the work. The marker is keyed on opencode's
    version, the agent, the web flag and the scope, so a changed key rebuilds.
    """
    env = child_env(pid)
    v2 = major >= 2
    web = ROLES[role]["web"]
    marker = cwd / ".bcoc_ready.json"
    key = {"agent": agent, "web": web, "version": dp.opencode_version(),
           "scope": str(Path(scope).resolve()) if scope else None}
    if v2:
        env.pop("OPENCODE_DISABLE_PROJECT_CONFIG", None)
        env.pop("OPENCODE_PERMISSION", None)
        try:
            have = json.loads(marker.read_text())
            if have.get("key") == key and (cwd / "opencode.json").is_file():
                return True, "", list(have.get("withheld") or []), env
        except (OSError, ValueError):
            pass
    withheld: List[str] = []
    if cwd.exists():
        for child in cwd.iterdir():
            shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
    cwd.mkdir(parents=True, exist_ok=True)
    if scope:
        r = subprocess.run([sys.executable, str(SCRIPTS / "pack_context.py"), scope,
                            "--mirror-to", str(cwd)], capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            return False, "could not build a filtered mirror: " + redact(r.stderr)[:300], [], env
        try:
            withheld = withheld_names(json.loads(r.stdout))
        except ValueError:
            withheld = []
    if v2:
        # The agent is defined in an opencode.json THIS script writes into the mirror (repo-supplied
        # config is set aside), then verified with the SAME cwd and env the real run uses.
        try:
            ov2.prepare(cwd, agent, web)
        except (OSError, ValueError) as e:
            return False, f"could not prepare the mirror config: {e}", withheld, env
        ok, why = ov2.verify_v2(ov2.debug_agents(cwd, env), agent, _must_block(role, True))
        if ok:
            marker.write_text(json.dumps({"key": key, "withheld": withheld}))
        return ok, why, withheld, env
    cfgdir = Path(os.environ.get("BCOPENCODE_OPENCODE_CONFIG_DIR") or SKILL_DIR / "opencode-config")
    (cfgdir / "agents").mkdir(parents=True, exist_ok=True)
    src = SKILL_DIR / "agents" / f"{agent}.md"
    if src.is_file():
        shutil.copyfile(src, cfgdir / "agents" / f"{agent}.md")
    env["OPENCODE_CONFIG_DIR"] = str(cfgdir)
    deny = {"edit": "deny", "write": "deny", "patch": "deny", "bash": "deny",
            "task": "deny", "external_directory": "deny"}
    if not web:
        deny["webfetch"] = "deny"
    env["OPENCODE_PERMISSION"] = json.dumps(deny)
    dump = subprocess.run(["opencode", "agent", "list"], capture_output=True, text=True,
                          timeout=60, env=env, stdin=subprocess.DEVNULL, cwd=str(cwd)).stdout
    vr = subprocess.run([sys.executable, str(SCRIPTS / "verify_agent_permissions.py"),
                         agent, *_must_block(role, False)], input=dump, capture_output=True, text=True)
    return vr.returncode == 0, vr.stderr.strip(), withheld, env


def run_opencode(pid: str, model: str, role: str, prompt_text: str, a) -> Tuple[str, Dict[str, Any]]:
    if not shutil.which("opencode"):
        return "BAD_ARGS", {"error": "`opencode` is not on PATH"}
    major = opencode_major()
    if major is None:
        return "REFUSED", {"error": "cannot determine the opencode version, so its agent "
                           "permissions cannot be verified"}
    v2 = major >= 2
    if v2 and a.agent:
        return "BAD_ARGS", {"error": "--agent is not supported on opencode 2.x: the restricted agent "
                            "is defined (and verified) per run in the filtered mirror"}
    agent = (f"bcoc-{role}" if v2 else
             (a.agent or ("bcoc-research" if ROLES[role]["web"] else "bcoc-review")))

    run_dir = Path(tempfile.mkdtemp(prefix="bcoc.deleg."))
    os.chmod(run_dir, 0o700)
    try:
        # the agent runs in a filtered mirror of the scope, never the raw tree. --workdir lets
        # many units share one prepared+verified mirror (fanout.py does this); otherwise a
        # fresh one per call.
        cwd = Path(a.workdir) if a.workdir else run_dir / "mirror"
        if a.workdir:
            # Two processes meeting a not-yet-built workdir must not rebuild it over each other
            # (the rebuild clears the directory). Serialise on a lock file beside it.
            Path(a.workdir).parent.mkdir(parents=True, exist_ok=True)
            with open(str(a.workdir).rstrip("/") + ".lock", "w") as lk:
                try:
                    fcntl.flock(lk, fcntl.LOCK_EX)
                except OSError:
                    pass
                ok, why, withheld, env = ensure_workdir(cwd, agent, role, pid, a.scope, major)
        else:
            ok, why, withheld, env = ensure_workdir(cwd, agent, role, pid, a.scope, major)
        if not ok:
            if why.startswith("could not"):
                return "ERROR", {"error": why}
            return "REFUSED", {"error": f"could not verify agent {agent!r} is restricted "
                               f"(opencode {major}.x): {why}. Nothing was spent. Use --backend or-api."}
        if a.prepare_only:
            return "OK", {"content": f"prepared {cwd}", "withheld": withheld, "model": f"{pid}/{model}"}
        prompt_text += withheld_note(withheld)

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
        r = _run_stream(argv, env, str(cwd), a.timeout, a.stall_timeout)
        err = redact(r["err"])
        meta = {"model": f"{pid}/{model}", "session_id": r["sid"], "max_gap_s": r["max_gap"],
                "elapsed_s": r["elapsed"], "withheld": withheld}
        if "is a subagent, not a primary agent" in err:
            return "ERROR", {**meta, "error": "opencode refused the agent as a subagent and fell back to "
                             "its default agent. The request was spent; the result is NOT trusted."}
        if v2:
            text, tool_errs = parse_v2_events(r["out"])
        else:
            text, tool_errs = r["out"].strip(), []

        if r["reason"]:
            why_killed = ("no output for %ds (stalled)" % a.stall_timeout if r["reason"] == "stall"
                          else "no answer within %ds" % a.timeout)
            # SALVAGE: the research is already done inside the session; ask it to write the
            # report from what it has instead of throwing the whole run away.
            if v2 and r["sid"] and not a.no_salvage:
                sal = _run_stream(["opencode", "run", "--agent", agent, "-m", f"{pid}/{model}",
                                   "--format", "json", "-s", r["sid"], FINALIZE_PROMPT],
                                  env, str(cwd), a.salvage_timeout, min(a.stall_timeout, 120))
                stext, _ = parse_v2_events(sal["out"])
                if stext and not sal["reason"]:
                    return "TRUNCATED", {**meta, "content": redact(stext),
                                         "finish_reason": f"salvaged after {r['reason']}",
                                         "error": f"{why_killed}; the session was resumed and asked "
                                                  f"to finish from what it had", "usage": {},
                                         "denied_tool_calls": tool_errs, "salvaged": True}
            return "TIMEOUT", {**meta, "error": why_killed,
                               "content": redact(text) if text else "",
                               "finish_reason": f"killed: {r['reason']}"}
        perrs = ov2.parse_errors(r["out"]) if v2 else []
        if perrs and not text:
            # opencode reports a provider failure as an `error` event and still exits 0
            return ov2.classify_provider_error(perrs[0]), {
                **meta, "provider_status": perrs[0]["status"],
                "error": redact(f"provider error {perrs[0]['status']}: {perrs[0]['message']}")}
        if r["rc"] != 0 or not text:
            res = classify_cli_failure(r["rc"], err) if r["rc"] != 0 else "ERROR"
            return res, {**meta, "error": (err.strip() or "opencode returned no text")[:800]}
        return "OK", {**meta, "content": redact(text), "finish_reason": "stop", "usage": {},
                      "denied_tool_calls": tool_errs}
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
            f"withheld_files: {len(env.get('withheld') or [])}",
            f"salvaged: {bool(env.get('salvaged'))}", f"elapsed_s: {env.get('elapsed_s')}",
            f"max_event_gap_s: {env.get('max_gap_s')}", f"opencode_session: {env.get('session_id') or ''}",
            f"created: {_utc_stamp()}", "---", ""]
    body = env.get("content") or ""
    if env.get("withheld"):
        names = ", ".join(f"`{n}`" for n in env["withheld"][:30])
        body = ("> [!NOTE]\n> The secret filter withheld these files from the model, so they were "
                f"**not reviewed**: {names}.\n\n") + body
    if result not in ("OK", "TRUNCATED") and env.get("error"):
        body = f"> [!WARNING]\n> {redact(str(env['error']))[:1500]}\n\n{body}"
    elif result == "TRUNCATED" and env.get("salvaged"):
        body = ("> [!WARNING]\n> The session was cut off (" + redact(str(env.get("error") or "")) +
                "). It was resumed and asked to write its report from the work already done, so this "
                "is based on **partial research**.\n\n" + body)
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
    ap.add_argument("--prompt-file", help="the task (required unless --prepare-only)")
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
    ap.add_argument("--timeout", type=int, default=_env_int("BCOPENCODE_TIMEOUT", 600),
                    help="total seconds for one attempt")
    ap.add_argument("--stall-timeout", type=int, default=_env_int("BCOPENCODE_STALL_TIMEOUT", 300),
                    help="opencode backend: abort when no event arrives for this many seconds (0 = off)")
    ap.add_argument("--salvage-timeout", type=int, default=240,
                    help="after a timeout/stall, how long the resumed session gets to write its report")
    ap.add_argument("--no-salvage", action="store_true",
                    help="do not resume a killed session to ask for a report from what it had")
    ap.add_argument("--workdir", help="opencode backend: reuse this prepared+verified mirror directory "
                                      "(built on first use). Lets many units share one verification.")
    ap.add_argument("--prepare-only", action="store_true",
                    help="build and verify --workdir, spend nothing, and exit")
    ap.add_argument("--rpm", type=int, default=None,
                    help="shared per-minute budget across workers (default 20 for OpenRouter free, off otherwise)")
    ap.add_argument("--cap", type=int, default=_env_int("BCOPENCODE_CAP", 200))
    ap.add_argument("--agent", help="opencode agent override (opencode backend)")
    ap.add_argument("--allow-paid", action="store_true",
                    help="permit non-zero-cost models. ONLY with the user's explicit consent.")
    ap.add_argument("--offline", action="store_true", help="do not refresh the capabilities cache")
    ap.add_argument("--cache-only", action="store_true",
                    help="use the saved capabilities as they are; run no discovery at all")
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
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)
    state = ledger.state_dir()
    scope_withheld: List[str] = []
    try:
        if a.session is not None and not SESSION_RE.match(a.session):
            raise Bad("--session must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
        if a.max_attempts < 1 or a.max_tokens < 1 or a.timeout < 1:
            raise Bad("--max-attempts, --max-tokens and --timeout must be positive")
        if a.backend == "opencode" and (a.image or a.pdf or a.json_mode or a.fallback):
            raise Bad("--image/--pdf/--json-mode/--fallback are or-api features")
        if a.agent is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}", a.agent):
            raise Bad("--agent must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
        if a.prepare_only and (a.backend != "opencode" or not a.workdir):
            raise Bad("--prepare-only needs --backend opencode and --workdir")
        if not a.prompt_file and not a.prepare_only:
            raise Bad("--prompt-file is required")
        prompt = "" if a.prepare_only else read_prompt(a.prompt_file)
        try:
            # --cache-only: read the saved capabilities and touch nothing. A fan-out of hundreds
            # of units must not run three opencode CLI calls each just to re-check discovery.
            cache = dp.load_cache() if a.cache_only else dp.ensure(offline=a.offline)[0]
        except Exception as e:  # discovery must never be the reason a task fails to run
            print(f"bcoc: discovery skipped ({type(e).__name__}); using the saved cache", file=sys.stderr)
            cache = dp.load_cache()
        pid, model = resolve_model(a.model, cache, a.role)
        if a.prepare_only:
            res, penv = run_opencode(pid, model, a.role, "", a)
            if res != "OK":
                print(f"bcoc: {penv.get('error')}", file=sys.stderr)
            return finish(res)
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
            packed, scope_withheld = pack_scope(a.scope, a.max_input_tokens)
            user_text += "\n\n=== PROJECT CONTEXT ===\n" + packed + withheld_note(scope_withheld)
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
        try:
            if a.backend == "or-api":
                result, env = run_or_api(apid, amodel, cache, a.role, user_text, images, history, a)
            else:
                result, env = run_opencode(apid, amodel, a.role, user_text, a)
        except SystemExit:
            # interrupted mid-request: it may already have been charged, so it is counted
            ledger.append_row("INTERRUPTED", full, a.backend, a.role, cost == dp.COST_FREE)
            print("RESULT=INTERRUPTED")
            raise
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

    if scope_withheld and not final_env.get("withheld"):
        final_env = {**final_env, "withheld": scope_withheld}
    if final_env.get("withheld"):
        print(f"bcoc: NOTE — the secret filter withheld {len(final_env['withheld'])} file(s) from the "
              f"model: {', '.join(final_env['withheld'][:8])}"
              f"{' …' if len(final_env['withheld']) > 8 else ''}. They were NOT reviewed.", file=sys.stderr)
    write_result(out_path, a.role, final_model, a.backend, final_result, final_env, a.session)
    secret_scan(out_path)
    if a.json_out:
        side = out_path.with_name(out_path.name + ".json")
        side.write_text(json.dumps({"role": a.role, "model": final_model, "backend": a.backend,
                                    "result": final_result,
                                    "usage": final_env.get("usage"),
                                    "finish_reason": final_env.get("finish_reason"),
                                    "withheld": final_env.get("withheld") or [],
                                    "salvaged": bool(final_env.get("salvaged")),
                                    "elapsed_s": final_env.get("elapsed_s"),
                                    "max_gap_s": final_env.get("max_gap_s"),
                                    "opencode_session": final_env.get("session_id"),
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
