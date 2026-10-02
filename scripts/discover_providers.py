#!/usr/bin/env python3
"""discover_providers.py — learn which AI providers opencode can use, and what they can do.

Provider-agnostic. Nothing here is specific to one vendor: it asks opencode what is
connected, then asks the provider (and a public catalog) what each model can do, and saves
the answer so the next call is a ~1 s check instead of a re-probe.

Sources, cheapest first (every field records which one produced it):
  1. opencode itself      `opencode models`, `opencode auth list` (v2) or
                          `opencode providers list` (v1), plus its merged config for base URLs
  2. the provider         GET {baseURL}/models, when the provider serves one (OpenRouter does
                          and includes pricing, context and supported parameters)
  3. models.dev           the public catalog opencode itself uses: context, output cap,
                          tool_call, reasoning, modalities, cost — for any provider it knows
  Nothing answers -> the field stays null and the model is "capabilities: unknown". A value
  is never invented.

Cache: $BCOPENCODE_STATE_DIR/capabilities.json (default ~/.bettercallopencode), mode 0600,
written atomically. It never contains a credential: keys are read from the environment or
opencode's auth store only to make the /models request and are not stored or printed.

Quick check (every call): fingerprint = hash(opencode version + `models` + `auth list`).
Same fingerprint and younger than the TTL -> use the cache. Otherwise only the providers
whose own fingerprint changed (or went stale) are re-probed.

Usage:
  discover_providers.py                       quick check, refresh only if needed
  discover_providers.py --refresh             re-probe everything
  discover_providers.py --offline             never touch the network (cache + opencode only)
  discover_providers.py --json                print the cache as JSON on stdout
  discover_providers.py --set-policy ID free|paid
        Providers that publish no pricing cannot be classified. Say once whether that
        provider's models are zero-cost; the answer is remembered.

Exit: 0 ok · 2 usage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA = 1
DEFAULT_TTL = 24 * 3600
MODELS_DEV_URL = "https://models.dev/api.json"
CLI_TIMEOUT = 30
HTTP_TIMEOUT = 20

COST_FREE, COST_PAID, COST_UNKNOWN = "free", "paid", "unknown"


# --------------------------------------------------------------------------- paths / io
def state_dir() -> Path:
    return Path(os.environ.get("BCOPENCODE_STATE_DIR") or (Path.home() / ".bettercallopencode"))


def cache_path(state: Optional[Path] = None) -> Path:
    return (state or state_dir()) / "capabilities.json"


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ttl_seconds() -> int:
    try:
        return max(0, int(os.environ.get("BCOPENCODE_CAPS_TTL", DEFAULT_TTL)))
    except ValueError:
        return DEFAULT_TTL


def load_cache(state: Optional[Path] = None) -> Dict[str, Any]:
    """The saved capabilities, or an empty skeleton. A corrupt file is treated as absent."""
    p = cache_path(state)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("schema") == SCHEMA and isinstance(
            data.get("providers"), dict
        ):
            return data
    except (OSError, ValueError):
        pass
    return {"schema": SCHEMA, "fetched_at": None, "fingerprint": None,
            "opencode_version": None, "providers": {}}


def save_cache(cache: Dict[str, Any], state: Optional[Path] = None) -> None:
    d = state or state_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    p = cache_path(d)
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, p)
        os.chmod(p, 0o600)
    finally:
        if tmp.exists():
            tmp.unlink()


# --------------------------------------------------------------------------- opencode CLI
def _run(argv: List[str]) -> Optional[str]:
    """stdout of a CLI call, or None if it is missing, fails or times out.

    Runs from an EMPTY directory with project config disabled. opencode reads project
    config (and, measured on 1.x, executes project plugins) from the working directory, so
    probing from inside an untrusted repository would let that repository choose the base
    URLs and env-var names discovery then sends credentials to.
    """
    if not shutil.which(argv[0]):
        return None
    # A STABLE empty directory, not a fresh one per call: opencode treats a new directory as
    # a new project and the first call there comes back empty while it registers it
    # (measured, 2.0.22). A stable one pays that once.
    cwd = state_dir() / "probe_cwd"
    try:
        cwd.mkdir(parents=True, exist_ok=True)
        os.chmod(cwd, 0o700)
    except OSError:
        cwd = Path(tempfile.gettempdir())
    for attempt in (0, 1):
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=CLI_TIMEOUT,
                               stdin=subprocess.DEVNULL, cwd=str(cwd),
                               env={**os.environ, "OPENCODE_DISABLE_PROJECT_CONFIG": "1"})
        except (OSError, subprocess.TimeoutExpired):
            return None
        if p.returncode != 0:
            return None
        if p.stdout.strip() or attempt:
            return p.stdout
        time.sleep(2)           # warm-up: one retry on an empty answer
    return ""


_MODEL_LINE = re.compile(r"^([A-Za-z0-9_.\-]+)/(\S+)$")


def parse_models(text: Optional[str]) -> Dict[str, List[str]]:
    """`provider/model` lines -> {provider: [model, ...]}. Anything else is ignored."""
    out: Dict[str, List[str]] = {}
    for line in (text or "").splitlines():
        m = _MODEL_LINE.match(line.strip())
        if m:
            out.setdefault(m.group(1), []).append(m.group(2))
    return out


def parse_auth_list(text: Optional[str]) -> List[Dict[str, str]]:
    """`opencode auth list` rows: columns separated by 2+ spaces -> name / env / source.

    Only names and where the credential comes from are kept; the output of this command
    does not contain secrets, and nothing but those three columns is retained anyway.
    """
    rows = []
    for line in (text or "").splitlines():
        parts = [p.strip() for p in re.split(r"\s{2,}", line.strip()) if p.strip()]
        if len(parts) >= 2:
            rows.append({"name": parts[0],
                         "env": parts[1] if len(parts) == 3 else "",
                         "source": parts[-1]})
    return rows


def opencode_version() -> Optional[str]:
    out = _run(["opencode", "--version"])
    return out.strip() if out else None


def opencode_snapshot() -> Dict[str, Any]:
    """Everything cheap that opencode knows. Works on v2 and falls back to v1 commands."""
    models_txt = _run(["opencode", "models"])
    auth_txt = _run(["opencode", "auth", "list"])
    if auth_txt is None:
        auth_txt = _run(["opencode", "providers", "list"])
    return {
        "version": opencode_version(),
        "models_raw": models_txt or "",
        "auth_raw": auth_txt or "",
        "models": parse_models(models_txt),
        "auth": parse_auth_list(auth_txt),
        "config": opencode_provider_config(),
    }


def opencode_provider_config() -> Dict[str, Dict[str, Any]]:
    """{provider id: {baseURL, env: [...]}} from opencode's merged config.

    v2: `opencode debug config` prints a JSON array of documents. Older versions (and
    failures) fall back to reading ~/.config/opencode/opencode.json directly. Only
    non-secret routing fields are returned.
    """
    docs: List[Dict[str, Any]] = []
    out = _run(["opencode", "debug", "config"])
    if out:
        try:
            data = json.loads(out)
            for d in data if isinstance(data, list) else [data]:
                info = d.get("info") if isinstance(d, dict) else None
                docs.append(info if isinstance(info, dict) else d)
        except (ValueError, AttributeError):
            pass
    if not docs:
        for name in ("opencode.json", "opencode.jsonc"):
            p = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "opencode" / name
            try:
                docs.append(json.loads(p.read_text(encoding="utf-8")))
                break
            except (OSError, ValueError):
                continue
    res: Dict[str, Dict[str, Any]] = {}
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        for key in ("providers", "provider"):
            section = doc.get(key)
            if not isinstance(section, dict):
                continue
            for pid, pv in section.items():
                if not isinstance(pv, dict):
                    continue
                settings = pv.get("settings") or pv.get("options") or {}
                if not isinstance(settings, dict):
                    settings = {}
                base = settings.get("baseURL") or settings.get("baseUrl") or pv.get("api") or ""
                env = pv.get("env") or []
                entry = res.setdefault(pid, {"baseURL": "", "env": []})
                if isinstance(base, str) and safe_base_url(base):
                    entry["baseURL"] = base.rstrip("/")
                if isinstance(env, list):
                    entry["env"] = [e for e in env if isinstance(e, str)]
    return res


def safe_base_url(url: str) -> bool:
    """A credential is sent to this URL, so: https only (plain http only to loopback)."""
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
    except ValueError:
        return False
    if not u.hostname:
        return False
    if u.scheme == "https":
        return True
    return u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1")


# --------------------------------------------------------------------------- HTTP sources
def _http_get_json(url: str, headers: Optional[Dict[str, str]] = None,
                   timeout: int = HTTP_TIMEOUT) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "BetterCallOpenCode/1.1",
                                               **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _provider_key(pid: str, env_names: List[str]) -> str:
    """A credential for the /models call, from the environment or opencode's auth store.

    Held only in memory for one request. Never returned to the caller's output or cache.
    """
    for name in env_names:
        v = os.environ.get(name, "").strip()
        if v:
            return v
    if pid == "openrouter":
        for name in ("OPENROUTER_API_KEY", "BCOPENCODE_API_KEY"):
            v = os.environ.get(name, "").strip()
            if v:
                return v
    auth = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    try:
        entry = json.loads(auth.read_text(encoding="utf-8")).get(pid) or {}
        return str(entry.get("key") or "").strip() if isinstance(entry, dict) else ""
    except (OSError, ValueError):
        return ""


def _num(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _int(x: Any) -> Optional[int]:
    v = _num(x)
    return int(v) if v is not None else None


def cost_class_from_prices(prompt: Any, completion: Any) -> str:
    p, c = _num(prompt), _num(completion)
    if p is None or c is None:
        return COST_UNKNOWN
    return COST_FREE if p == 0 and c == 0 else COST_PAID


def from_provider_models(payload: Any) -> Dict[str, Dict[str, Any]]:
    """Parse an OpenAI-style `/models` payload. OpenRouter's carries rich metadata; a plain
    OpenAI-compatible gateway often carries only ids — missing fields stay None."""
    out: Dict[str, Dict[str, Any]] = {}
    items = payload.get("data") if isinstance(payload, dict) else payload
    for m in items if isinstance(items, list) else []:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        mid = str(m["id"])
        pricing = m.get("pricing") if isinstance(m.get("pricing"), dict) else None
        arch = m.get("architecture") if isinstance(m.get("architecture"), dict) else {}
        sp = m.get("supported_parameters")
        sp = [str(s) for s in sp] if isinstance(sp, list) else None
        mods = arch.get("input_modalities")
        if not isinstance(mods, list):
            mod = arch.get("modality")
            mods = re.split(r"[+>]", str(mod).split("->")[0]) if isinstance(mod, str) else None
        cost = COST_UNKNOWN
        if pricing is not None:
            cost = cost_class_from_prices(pricing.get("prompt"), pricing.get("completion"))
            # `prompt`/`completion` at 0 is not enough: a per-request, image or web-search
            # fee still bills. Any other positive number makes it paid.
            if cost == COST_FREE and any((_num(v) or 0) > 0 for v in pricing.values()):
                cost = COST_PAID
        if cost == COST_UNKNOWN and mid.endswith(":free"):
            cost = COST_FREE
        top = m.get("top_provider") if isinstance(m.get("top_provider"), dict) else {}
        out[mid] = {
            "name": m.get("name"),
            "ctx": _int(m.get("context_length")),
            "max_out": _int(top.get("max_completion_tokens")),
            "tools": ("tools" in sp) if sp is not None else None,
            "reasoning": (("reasoning" in sp) or ("include_reasoning" in sp)) if sp is not None else None,
            "json_mode": (("response_format" in sp) or ("structured_outputs" in sp)) if sp is not None else None,
            "modalities": [str(x).strip() for x in mods if str(x).strip()] if mods else None,
            "cost_class": cost,
            "source": "provider",
        }
    return out


def from_models_dev(catalog: Any, pid: str) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    prov = catalog.get(pid) if isinstance(catalog, dict) else None
    models = prov.get("models") if isinstance(prov, dict) else None
    for mid, m in (models or {}).items():
        if not isinstance(m, dict):
            continue
        lim = m.get("limit") if isinstance(m.get("limit"), dict) else {}
        cost = m.get("cost") if isinstance(m.get("cost"), dict) else None
        mods = (m.get("modalities") or {}).get("input") if isinstance(m.get("modalities"), dict) else None
        out[str(mid)] = {
            "name": m.get("name"),
            "ctx": _int(lim.get("context")),
            "max_out": _int(lim.get("output")),
            "tools": m.get("tool_call") if isinstance(m.get("tool_call"), bool) else None,
            "reasoning": m.get("reasoning") if isinstance(m.get("reasoning"), bool) else None,
            "json_mode": m.get("structured_output") if isinstance(m.get("structured_output"), bool) else None,
            "modalities": [str(x) for x in mods] if isinstance(mods, list) else None,
            "cost_class": cost_class_from_prices(cost.get("input"), cost.get("output"))
            if cost is not None else COST_UNKNOWN,
            "source": "models.dev",
        }
    return out


def merge_caps(*layers: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """First layer wins per field; later layers only fill what is still None/unknown."""
    out: Dict[str, Dict[str, Any]] = {}
    for layer in layers:
        for mid, caps in layer.items():
            cur = out.setdefault(mid, {})
            srcs = cur.setdefault("sources", [])
            used = False
            for k, v in caps.items():
                if k == "source":
                    continue
                if k not in cur or cur[k] is None or (k == "cost_class" and cur[k] == COST_UNKNOWN):
                    if v is not None and not (k == "cost_class" and v == COST_UNKNOWN):
                        cur[k] = v
                        used = True
                    else:
                        cur.setdefault(k, v)
            if used and caps.get("source") not in srcs:
                srcs.append(caps.get("source"))
    return out


# --------------------------------------------------------------------------- probing
def _fp(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()[:16]


def probe_provider(pid: str, model_ids: List[str], cfg: Dict[str, Any],
                   catalog: Any, offline: bool) -> Dict[str, Any]:
    layers: List[Dict[str, Dict[str, Any]]] = []
    notes: List[str] = []
    base = cfg.get("baseURL") or ("https://openrouter.ai/api/v1" if pid == "openrouter" else "")
    if base and not offline:
        key = _provider_key(pid, cfg.get("env") or [])
        hdrs = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            layers.append(from_provider_models(_http_get_json(f"{base}/models", hdrs)))
        except Exception as e:  # network, auth, shape: all just "this source gave nothing"
            notes.append(f"provider /models unavailable ({type(e).__name__})")
    if catalog is not None:
        layers.append(from_models_dev(catalog, pid))
    merged = merge_caps(*layers)
    models: Dict[str, Dict[str, Any]] = {}
    for mid in model_ids:
        caps = dict(merged.get(mid) or {})
        caps.setdefault("name", None)
        for k in ("ctx", "max_out", "tools", "reasoning", "json_mode", "modalities"):
            caps.setdefault(k, None)
        caps.setdefault("cost_class", COST_UNKNOWN)
        caps["sources"] = caps.get("sources") or []
        caps["capabilities"] = "known" if caps["sources"] else "unknown"
        models[mid] = caps
    return {"base_url": base or None, "models": models, "notes": notes}


def _unknown_models(entry: Dict[str, Any]) -> List[str]:
    return [m for m, c in entry["models"].items() if c.get("cost_class") not in (COST_FREE, COST_PAID)]


def _apply_status(entry: Dict[str, Any]) -> None:
    covered = set(entry.get("policy_covers") or [])
    unknown = [m for m in _unknown_models(entry) if m not in covered]
    if unknown:
        entry["status"] = "needs_policy"
    else:
        entry["status"] = "ok"


def ensure(refresh: bool = False, offline: bool = False,
           state: Optional[Path] = None, snapshot: Optional[Dict[str, Any]] = None
           ) -> Tuple[Dict[str, Any], str]:
    """Return (cache, verdict). verdict: 'fresh' | 'updated' | 'offline-stale' | 'empty'."""
    cache = load_cache(state)
    snap = snapshot if snapshot is not None else opencode_snapshot()
    if snapshot is None and not snap["models"] and snap.get("version"):
        # opencode starts a background service on first use; the very first `models` call
        # can come back empty while it comes up. One retry, then trust the answer.
        time.sleep(2)
        snap = opencode_snapshot()
    models_by_provider: Dict[str, List[str]] = snap["models"]
    if not models_by_provider:
        # Never overwrite a good cache with "nothing": that would look like every
        # provider vanished.
        return cache, ("empty" if not cache["providers"] else "offline-stale")

    fp = _fp(snap.get("version") or "", snap.get("models_raw") or "", snap.get("auth_raw") or "")
    age = _now() - (cache.get("_ts") or 0)
    if (not refresh and cache.get("fingerprint") == fp and age < ttl_seconds()):
        return cache, "fresh"

    catalog = None
    if not offline:
        try:
            catalog = _http_get_json(MODELS_DEV_URL, timeout=30)
        except Exception:
            catalog = None

    providers = cache["providers"]
    changed = 0
    for pid, mids in models_by_provider.items():
        cfg = snap["config"].get(pid, {})
        pfp = _fp(pid, ",".join(sorted(mids)), cfg.get("baseURL", ""))
        old = providers.get(pid)
        stale = old is None or _now() - (old.get("_ts") or 0) >= ttl_seconds()
        if not (refresh or stale or old.get("fp") != pfp):
            continue
        res = probe_provider(pid, mids, cfg, catalog, offline)
        entry = {"fp": pfp, "_ts": _now(), "fetched_at": _iso(_now()),
                 "zero_cost_policy": (old or {}).get("zero_cost_policy"),
                 "policy_covers": (old or {}).get("policy_covers") or [],
                 **res}
        # keep the reliability history a previous run recorded
        for mid, caps in entry["models"].items():
            prev = ((old or {}).get("models") or {}).get(mid) or {}
            if prev.get("last_ok"):
                caps["last_ok"] = prev["last_ok"]
            if offline and prev:
                for k, v in prev.items():
                    if caps.get(k) is None and v is not None:
                        caps[k] = v
        _apply_status(entry)
        providers[pid] = entry
        changed += 1
    for pid in list(providers):
        if pid not in models_by_provider:
            del providers[pid]
            changed += 1

    cache.update({"fingerprint": fp, "opencode_version": snap.get("version"),
                  "_ts": _now(), "fetched_at": _iso(_now())})
    save_cache(cache, state)
    return cache, "updated"


# --------------------------------------------------------------------------- policy / cost
def effective_cost(cache: Dict[str, Any], provider: str, model: str) -> str:
    """free | paid | unknown. A provider-level policy only resolves 'unknown'; it never
    overrides a price the provider or catalog actually published."""
    prov = (cache.get("providers") or {}).get(provider) or {}
    cls = ((prov.get("models") or {}).get(model) or {}).get("cost_class", COST_UNKNOWN)
    if cls not in (COST_FREE, COST_PAID):
        cls = COST_UNKNOWN
    if cls == COST_UNKNOWN:
        pol = prov.get("zero_cost_policy")
        # The policy covers the models that existed when the user decided. A model a
        # later refresh adds is a new money decision, not an inherited one.
        if pol in (COST_FREE, COST_PAID) and model in (prov.get("policy_covers") or []):
            return pol
    return cls


def set_policy(provider: str, policy: str, state: Optional[Path] = None) -> bool:
    if policy not in (COST_FREE, COST_PAID):
        raise ValueError("policy must be 'free' or 'paid'")
    cache = load_cache(state)
    prov = cache["providers"].get(provider)
    if prov is None:
        return False
    prov["zero_cost_policy"] = policy
    prov["policy_covers"] = sorted(set(prov.get("policy_covers") or []) | set(_unknown_models(prov)))
    _apply_status(prov)
    save_cache(cache, state)
    return True


def record_ok(provider: str, model: str, state: Optional[Path] = None) -> None:
    """Stamp a model that just answered. Advisory reliability data for select_models."""
    cache = load_cache(state)
    caps = ((cache["providers"].get(provider) or {}).get("models") or {}).get(model)
    if caps is None:
        return
    caps["last_ok"] = _iso(_now())
    save_cache(cache, state)


def summary_line(cache: Dict[str, Any], verdict: str) -> str:
    provs = cache.get("providers") or {}
    n = sum(len(p.get("models") or {}) for p in provs.values())
    need = [pid for pid, p in provs.items() if p.get("status") == "needs_policy"]
    s = f"providers: {len(provs)}, models: {n}, cache {verdict}"
    if need:
        s += f"; NEEDS_POLICY: {','.join(need)} (publish no pricing — say whether free or paid)"
    return s


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--set-policy", nargs=2, metavar=("PROVIDER", "free|paid"))
    args = ap.parse_args()

    if args.set_policy:
        pid, pol = args.set_policy
        if pol not in (COST_FREE, COST_PAID):
            print("policy must be 'free' or 'paid'", file=sys.stderr)
            return 2
        if not set_policy(pid, pol):
            print(f"unknown provider {pid!r}; run discovery first", file=sys.stderr)
            return 2
        print(f"{pid}: zero_cost_policy={pol}", file=sys.stderr)
        return 0

    cache, verdict = ensure(refresh=args.refresh, offline=args.offline)
    print(summary_line(cache, verdict), file=sys.stderr)
    if verdict == "empty":
        print("no providers found: is opencode installed and a provider connected?",
              file=sys.stderr)
    if args.json:
        print(json.dumps(cache, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
