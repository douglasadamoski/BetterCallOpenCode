#!/usr/bin/env python3
"""or_client.py — OpenRouter HTTP client for BetterCallOpenCode (stdlib only).

Resolves API key from OPENROUTER_API_KEY or OpenCode auth.json.
Enforces free-model gate unless --allow-paid.

Exit codes:
  0 OK
  2 usage / config
  3 AUTH
  4 QUOTA
  5 TIMEOUT
  6 UNREACHABLE
  7 ERROR
  8 TRUNCATED
  9 PAID_BLOCKED

Stdout: JSON envelope (or content with --print-content).
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_TIMEOUT = 600
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
APP_TITLE = "BetterCallOpenCode"
APP_REFERER = "https://github.com/douglasadamoski/BetterCallOpenCode"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def resolve_api_key(explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit.strip()
    k = _env("OPENROUTER_API_KEY") or _env("BCOPENCODE_API_KEY")
    if k:
        return k
    # OpenCode stores credentials here
    auth = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    if auth.is_file():
        try:
            data = json.loads(auth.read_text(encoding="utf-8"))
            or_entry = data.get("openrouter") or {}
            if isinstance(or_entry, dict) and or_entry.get("key"):
                return str(or_entry["key"]).strip()
        except Exception:
            pass
    return ""


def normalize_model(model: str) -> Tuple[str, str]:
    """Return (opencode_form, openrouter_id)."""
    m = (model or "").strip()
    while m.startswith("openrouter/"):
        m = m[len("openrouter/") :]
    while m.startswith("openrouter/"):
        m = m[len("openrouter/") :]
    return f"openrouter/{m}", m


def is_free_model(model: str) -> bool:
    _, mid = normalize_model(model)
    if mid.endswith(":free"):
        return True
    if mid in ("free", "openrouter/free"):
        return True
    return False


def http_json(
    method: str,
    url: str,
    headers: Dict[str, str],
    body: Optional[dict],
    timeout: int,
) -> Tuple[int, str, Any]:
    data = None
    hdrs = dict(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            text = raw.decode("utf-8", errors="replace")
            code = getattr(resp, "status", 200) or 200
            return code, text, _try_parse(text)
    except urllib.error.HTTPError as e:
        raw = e.read() if hasattr(e, "read") else b""
        text = raw.decode("utf-8", errors="replace") if raw else str(e.reason)
        return int(e.code), text, _try_parse(text)
    except urllib.error.URLError as e:
        reason = str(getattr(e, "reason", e))
        if "timed out" in reason.lower() or "timeout" in reason.lower():
            raise TimeoutError(reason) from e
        raise ConnectionError(reason) from e
    except TimeoutError:
        raise
    except Exception as e:
        raise ConnectionError(str(e)) from e


def _try_parse(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    if text[0] in "{[":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
    return None


def auth_headers(api_key: str) -> Dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": APP_REFERER,
        "X-Title": APP_TITLE,
        "User-Agent": "BetterCallOpenCode/1.0",
    }


def classify_http(code: int, text: str, parsed: Any) -> str:
    if code in (401, 403):
        return "AUTH"
    if code == 402:
        return "QUOTA"
    if code == 429:
        return "QUOTA"
    if code in (404, 405, 502, 503, 504):
        return "UNREACHABLE"
    if 200 <= code < 300:
        return "OK"
    if code >= 500:
        return "ERROR"
    if code >= 400:
        low = (text or "").lower()
        if "rate" in low or "quota" in low or "credit" in low or "too many" in low:
            return "QUOTA"
        if "auth" in low or "api key" in low or "unauthorized" in low:
            return "AUTH"
        return "ERROR"
    return "OK"


def key_info(api_key: str, timeout: int = 30) -> Tuple[str, Dict[str, Any]]:
    try:
        code, text, parsed = http_json(
            "GET", f"{OPENROUTER_BASE}/key", auth_headers(api_key), None, timeout
        )
    except TimeoutError as e:
        return "TIMEOUT", {"error": str(e)}
    except ConnectionError as e:
        return "UNREACHABLE", {"error": str(e)}
    result = classify_http(code, text, parsed)
    if result != "OK" or not isinstance(parsed, dict):
        return result, {"http": code, "body": (text or "")[:800]}
    data = parsed.get("data") if isinstance(parsed.get("data"), dict) else parsed
    credits = {}
    try:
        ccode, ctext, cparsed = http_json(
            "GET", f"{OPENROUTER_BASE}/credits", auth_headers(api_key), None, timeout
        )
        if 200 <= ccode < 300 and isinstance(cparsed, dict):
            credits = cparsed.get("data") or cparsed
    except Exception:
        pass
    is_free_tier = bool(data.get("is_free_tier", True))
    # Official table: is_free_tier true → 50 RPD; false (purchased ≥$10) → 1000 RPD
    free_rpd = 50 if is_free_tier else 1000
    out = {
        "is_free_tier": is_free_tier,
        "free_rpd_bucket": free_rpd,
        "free_rpm": 20,
        "usage": data.get("usage"),
        "usage_daily": data.get("usage_daily"),
        "limit": data.get("limit"),
        "limit_remaining": data.get("limit_remaining"),
        "credits": credits,
        "label": data.get("label"),
    }
    return "OK", out


def chat(
    api_key: str,
    model: str,
    messages: List[Dict[str, str]],
    max_tokens: int = 8192,
    temperature: float = 0.2,
    timeout: int = DEFAULT_TIMEOUT,
    allow_paid: bool = False,
) -> Tuple[str, Dict[str, Any]]:
    oc_form, or_id = normalize_model(model)
    free = is_free_model(model)
    if not free and not allow_paid:
        return "PAID_BLOCKED", {
            "error": f"Model {or_id!r} is not a free variant (:free). Pass --allow-paid only after explicit user consent.",
            "model": oc_form,
            "free": False,
        }
    body = {
        "model": or_id,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "usage": {"include": True},
    }
    try:
        code, text, parsed = http_json(
            "POST",
            f"{OPENROUTER_BASE}/chat/completions",
            auth_headers(api_key),
            body,
            timeout,
        )
    except TimeoutError as e:
        return "TIMEOUT", {"error": str(e), "model": oc_form, "free": free}
    except ConnectionError as e:
        return "UNREACHABLE", {"error": str(e), "model": oc_form, "free": free}

    result = classify_http(code, text, parsed)
    if result != "OK":
        err_msg = text[:1200]
        if isinstance(parsed, dict):
            err = parsed.get("error")
            if isinstance(err, dict):
                err_msg = err.get("message") or err_msg
            elif err:
                err_msg = str(err)
        return result, {
            "http": code,
            "error": err_msg,
            "model": oc_form,
            "free": free,
            "raw": (text or "")[:2000],
        }

    if not isinstance(parsed, dict):
        return "ERROR", {"error": "non-JSON response", "raw": (text or "")[:500]}

    choices = parsed.get("choices") or []
    content = ""
    finish = None
    if choices:
        ch0 = choices[0] or {}
        finish = ch0.get("finish_reason") or ch0.get("native_finish_reason")
        msg = ch0.get("message") or {}
        content = msg.get("content") or ""
        if not content and msg.get("reasoning"):
            content = f"(reasoning only)\n{msg.get('reasoning')}"
    usage = parsed.get("usage") or {}
    env = {
        "model": oc_form,
        "openrouter_model": parsed.get("model") or or_id,
        "free": free,
        "content": content or "",
        "finish_reason": finish,
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "cost": usage.get("cost"),
        },
        "id": parsed.get("id"),
    }
    if finish in ("length", "max_tokens") or (
        content and len(content) < 20 and finish == "length"
    ):
        return "TRUNCATED", env
    if not (content or "").strip():
        return "ERROR", {**env, "error": "empty content", "raw": parsed}
    return "OK", env


def main() -> int:
    p = argparse.ArgumentParser(description="BetterCallOpenCode OpenRouter client")
    p.add_argument("--model", default=_env("BCOPENCODE_MODEL") or "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free")
    p.add_argument("--prompt-file", help="User message file")
    p.add_argument("--system-file", help="Optional system message file")
    p.add_argument("--max-tokens", type=int, default=int(_env("BCOPENCODE_MAX_TOKENS") or "8192"))
    p.add_argument("--temperature", type=float, default=float(_env("BCOPENCODE_TEMPERATURE") or "0.2"))
    p.add_argument("--timeout", type=int, default=int(_env("BCOPENCODE_TIMEOUT") or str(DEFAULT_TIMEOUT)))
    p.add_argument("--allow-paid", action="store_true")
    p.add_argument("--api-key", default=None)
    p.add_argument("--preflight", action="store_true")
    p.add_argument("--print-content", action="store_true")
    p.add_argument("--check-free", action="store_true", help="Exit 9 if model not free and not --allow-paid")
    args = p.parse_args()

    key = resolve_api_key(args.api_key)
    if not key:
        print(json.dumps({"result": "AUTH", "error": "No OPENROUTER_API_KEY and no OpenCode openrouter auth"}))
        print("RESULT=AUTH", file=sys.stderr)
        return 3

    if args.preflight:
        result, info = key_info(key, min(args.timeout, 30))
        free_ok = is_free_model(args.model)
        out = {
            "result": result,
            "model": normalize_model(args.model)[0],
            "model_is_free": free_ok,
            "allow_paid": bool(args.allow_paid),
            "key": info,
        }
        if result == "OK" and not free_ok and not args.allow_paid:
            out["result"] = "PAID_BLOCKED"
            out["error"] = "Default free-only gate: model is not :free"
            print(json.dumps(out, ensure_ascii=False, indent=2))
            print("RESULT=PAID_BLOCKED", file=sys.stderr)
            return 9
        print(json.dumps(out, ensure_ascii=False, indent=2))
        print(f"RESULT={out['result']}", file=sys.stderr)
        return 0 if out["result"] == "OK" else {"AUTH": 3, "QUOTA": 4, "TIMEOUT": 5, "UNREACHABLE": 6}.get(out["result"], 7)

    if args.check_free and not is_free_model(args.model) and not args.allow_paid:
        print(json.dumps({"result": "PAID_BLOCKED", "model": args.model}))
        print("RESULT=PAID_BLOCKED", file=sys.stderr)
        return 9

    if not args.prompt_file:
        print("Missing --prompt-file (or use --preflight)", file=sys.stderr)
        return 2
    user = Path(args.prompt_file).read_text(encoding="utf-8")
    messages: List[Dict[str, str]] = []
    if args.system_file:
        messages.append({"role": "system", "content": Path(args.system_file).read_text(encoding="utf-8")})
    messages.append({"role": "user", "content": user})

    result, env = chat(
        key,
        args.model,
        messages,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        timeout=args.timeout,
        allow_paid=args.allow_paid or _env("BCOPENCODE_ALLOW_PAID") in ("1", "true", "yes"),
    )
    env["result"] = result
    if args.print_content and result in ("OK", "TRUNCATED"):
        sys.stdout.write(env.get("content") or "")
        if not (env.get("content") or "").endswith("\n"):
            sys.stdout.write("\n")
    else:
        print(json.dumps(env, ensure_ascii=False))
    print(f"RESULT={result}", file=sys.stderr)
    return {
        "OK": 0,
        "AUTH": 3,
        "QUOTA": 4,
        "TIMEOUT": 5,
        "UNREACHABLE": 6,
        "ERROR": 7,
        "TRUNCATED": 8,
        "PAID_BLOCKED": 9,
    }.get(result, 7)


if __name__ == "__main__":
    sys.exit(main())
