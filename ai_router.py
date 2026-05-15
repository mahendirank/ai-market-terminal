"""
ai_router.py — Provider-agnostic AI dispatcher.

Single entry point for every AI call in the system. Hides provider details
(Groq REST vs Ollama REST vs whatever comes next) behind a stable interface:

    from ai_router import chat
    result = chat(task="hni", messages=[...], temperature=0.15)

What it gives you:
  - Task-based routing (ai_models.TASK_ROUTES)
  - Automatic fallback when a model errors, times out, or is unavailable
  - Per-call latency + token + cost logging to SQLite (db/ai_calls.db)
  - Structured ``CallResult`` so callers don't repeat error handling
  - One-line provider swap — point ``groq:llama-3.3-70b`` at vLLM or LMStudio
    in ai_models.py and the rest of the app is unchanged

Backwards-compat:
  Existing callers using requests.post(...groq...) still work. Migration is
  per-tab and incremental. New code should always go through chat().
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

import requests

from ai_models import (
    MODELS, TASK_ROUTES, PROVIDER_GROQ, PROVIDER_OLLAMA,
    get_model, get_task_route, estimate_cost,
)

log = logging.getLogger(__name__)


# ─── Config ──────────────────────────────────────────────────────────────────
# GROQ_API_KEY read lazily so .env loaded by other modules at import time is
# visible. Module-load capture would miss it.
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def _get_groq_key() -> str:
    return os.environ.get("GROQ_API_KEY", "").strip()


def _get_ollama_base() -> str:
    """Return Ollama base URL, stripping any /api/* suffix that some deploy
    configs include (e.g. ``http://ollama:11434/api/generate``).

    Callers append ``/api/chat`` themselves to hit the chat-completion path.
    """
    import re as _re
    raw = os.environ.get("OLLAMA_URL", "http://localhost:11434").strip()
    base = _re.sub(r"/api/[^/]+/?$", "", raw).rstrip("/")
    return base or "http://localhost:11434"


DEFAULT_TIMEOUT = 30
DEFAULT_TEMP    = 0.2
DEFAULT_MAX_OUT = 1200

# Probe cache — avoid hitting Ollama for every call to check availability
_OLLAMA_AVAILABLE: Optional[bool] = None
_OLLAMA_PROBE_TS  = 0
_OLLAMA_PROBE_TTL = 60   # re-probe every 60s


# ─── Database (latency + cost tracking) ──────────────────────────────────────
_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db", "ai_calls.db")
_db_lock = threading.Lock()


def _db_conn():
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS ai_calls (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ts              REAL    NOT NULL,
        task            TEXT    NOT NULL,
        model_key       TEXT    NOT NULL,
        provider        TEXT    NOT NULL,
        ok              INTEGER NOT NULL,           -- 1 success, 0 failure
        latency_ms      INTEGER,
        prompt_tokens   INTEGER,
        completion_tokens INTEGER,
        estimated_cost_usd REAL,
        fallback_depth  INTEGER DEFAULT 0,          -- 0 primary, 1 first fallback...
        error           TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_calls_ts ON ai_calls(ts DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_calls_model ON ai_calls(model_key)")
    conn.commit()
    return conn


def _log_call(task: str, model_key: str, *, ok: bool, latency_ms: int,
              prompt_tokens: int = 0, completion_tokens: int = 0,
              fallback_depth: int = 0, error: Optional[str] = None) -> None:
    """Persist a single call's metrics. Failures here are swallowed — we never
    want logging to break the AI path."""
    try:
        m = MODELS.get(model_key, {})
        cost = estimate_cost(model_key, prompt_tokens, completion_tokens) if ok else 0.0
        with _db_lock:
            conn = _db_conn()
            conn.execute(
                """INSERT INTO ai_calls
                   (ts, task, model_key, provider, ok, latency_ms,
                    prompt_tokens, completion_tokens, estimated_cost_usd,
                    fallback_depth, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (time.time(), task, model_key, m.get("provider", "?"),
                 1 if ok else 0, latency_ms,
                 prompt_tokens, completion_tokens, cost,
                 fallback_depth, (error or "")[:500])
            )
            conn.commit()
            conn.close()
    except Exception as e:
        log.debug("ai_calls log failed: %s", e)


# ─── Result envelope ─────────────────────────────────────────────────────────
@dataclass
class CallResult:
    """Standard envelope returned by chat(). Callers never have to peek at
    provider-specific response shapes again."""
    ok: bool
    content: str = ""               # raw text from the model (assistant message)
    task: str = ""
    model_key: str = ""             # the model that actually answered
    requested_model: str = ""       # the originally requested model (for fallback visibility)
    provider: str = ""
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_usd: float = 0.0
    fallback_depth: int = 0         # 0 = primary, 1 = first fallback, ...
    error: Optional[str] = None
    raw: dict = field(default_factory=dict)   # original provider response (for debugging)


# ─── Provider clients ────────────────────────────────────────────────────────
def _ollama_available() -> bool:
    """Cheap probe of the local Ollama server. Cached for ``_OLLAMA_PROBE_TTL``."""
    global _OLLAMA_AVAILABLE, _OLLAMA_PROBE_TS
    if _OLLAMA_AVAILABLE is not None and (time.time() - _OLLAMA_PROBE_TS) < _OLLAMA_PROBE_TTL:
        return _OLLAMA_AVAILABLE
    try:
        r = requests.get(f"{_get_ollama_base()}/api/tags", timeout=2)
        _OLLAMA_AVAILABLE = (r.status_code == 200)
    except Exception:
        _OLLAMA_AVAILABLE = False
    _OLLAMA_PROBE_TS = time.time()
    return _OLLAMA_AVAILABLE


def _call_groq(model_id: str, messages: list, *, temperature: float,
               max_tokens: int, timeout: int, extra: dict | None = None) -> dict:
    """POST to Groq. Returns the parsed response dict. Raises on non-200."""
    key = _get_groq_key()
    if not key:
        raise RuntimeError("GROQ_API_KEY not set")
    body = {
        "model":       model_id,
        "messages":    messages,
        "temperature": temperature,
        "max_tokens":  max_tokens,
    }
    if extra:
        body.update(extra)
    r = requests.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=body,
        timeout=timeout,
    )
    if r.status_code != 200:
        raise RuntimeError(f"groq {r.status_code}: {r.text[:200]}")
    return r.json()


def _call_ollama(model_id: str, messages: list, *, temperature: float,
                 max_tokens: int, timeout: int, extra: dict | None = None) -> dict:
    """POST to local Ollama. Returns a response shaped to look like Groq's
    so the router can read both uniformly."""
    if not _ollama_available():
        raise RuntimeError("ollama unavailable")
    # Ollama's /api/chat supports messages array directly
    body = {
        "model":    model_id,
        "messages": messages,
        "stream":   False,
        "options":  {"temperature": temperature, "num_predict": max_tokens},
    }
    if extra:
        body["options"].update(extra)
    r = requests.post(f"{_get_ollama_base()}/api/chat", json=body, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"ollama {r.status_code}: {r.text[:200]}")
    js = r.json()
    # Reshape to Groq-compatible
    content = js.get("message", {}).get("content", "")
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {
            "prompt_tokens":     js.get("prompt_eval_count", 0),
            "completion_tokens": js.get("eval_count", 0),
            "total_tokens":      js.get("prompt_eval_count", 0) + js.get("eval_count", 0),
        },
        "_provider": "ollama",
        "_raw": js,
    }


def _execute(model_key: str, messages: list, *, temperature: float,
             max_tokens: int, timeout: int, extra: dict | None) -> tuple[str, dict, int, int]:
    """Run a single model call. Returns (content, raw, prompt_toks, completion_toks).
    Raises on failure — caller decides whether to fall back."""
    m = MODELS.get(model_key)
    if not m:
        raise RuntimeError(f"unknown model key: {model_key}")

    provider = m["provider"]
    if provider == PROVIDER_GROQ:
        js = _call_groq(m["model_id"], messages, temperature=temperature,
                        max_tokens=max_tokens, timeout=timeout, extra=extra)
    elif provider == PROVIDER_OLLAMA:
        js = _call_ollama(m["model_id"], messages, temperature=temperature,
                          max_tokens=max_tokens, timeout=timeout, extra=extra)
    else:
        raise RuntimeError(f"provider {provider!r} not implemented")

    content = js["choices"][0]["message"]["content"]
    usage   = js.get("usage", {})
    return content, js, int(usage.get("prompt_tokens", 0) or 0), int(usage.get("completion_tokens", 0) or 0)


# ─── Public API ──────────────────────────────────────────────────────────────
def chat(task: str, messages: list, *,
         temperature: float = DEFAULT_TEMP,
         max_tokens: int = DEFAULT_MAX_OUT,
         timeout: int = DEFAULT_TIMEOUT,
         model: Optional[str] = None,
         extra: Optional[dict] = None,
         allow_fallback: bool = True) -> CallResult:
    """Route a chat call to the right model for the given task.

    Parameters
    ----------
    task : str
        Canonical task name (matches a key in ai_models.TASK_ROUTES).
    messages : list[dict]
        OpenAI-style messages array.
    model : str, optional
        Force a specific model key, bypassing TASK_ROUTES.
    extra : dict, optional
        Provider-specific overrides (e.g. ``{"response_format": {"type": "json_object"}}``).
    allow_fallback : bool
        When True (default), failed calls cascade through MODELS[key]['fallback'].

    Returns
    -------
    CallResult
        Always returned — ``ok=False`` if every model in the chain failed.
    """
    requested = model or get_task_route(task)
    if not requested:
        return CallResult(ok=False, task=task, error=f"no route for task={task!r}")
    if requested not in MODELS:
        return CallResult(ok=False, task=task, requested_model=requested,
                          error=f"unknown model key: {requested}")

    # Build candidate chain: primary + fallbacks (if allowed)
    chain: list[str] = [requested]
    if allow_fallback:
        chain.extend(MODELS[requested].get("fallback", []))

    last_error = None
    for depth, mkey in enumerate(chain):
        m = MODELS.get(mkey)
        if not m:
            last_error = f"unknown model in chain: {mkey}"
            continue

        # Skip ollama models if the daemon isn't reachable — avoids a long
        # timeout cascade when Ollama is down.
        if m["provider"] == PROVIDER_OLLAMA and not _ollama_available():
            log.debug("router: skipping %s (ollama not reachable)", mkey)
            _log_call(task, mkey, ok=False, latency_ms=0,
                      fallback_depth=depth, error="ollama_unavailable")
            last_error = "ollama_unavailable"
            continue

        start = time.time()
        try:
            content, raw, ptok, ctok = _execute(
                mkey, messages,
                temperature=temperature, max_tokens=max_tokens,
                timeout=timeout, extra=extra,
            )
            latency_ms = int((time.time() - start) * 1000)
            _log_call(task, mkey, ok=True, latency_ms=latency_ms,
                      prompt_tokens=ptok, completion_tokens=ctok,
                      fallback_depth=depth)
            cost = estimate_cost(mkey, ptok, ctok)
            print(f"[ai_router] task={task} model={mkey} ok elapsed_ms={latency_ms} "
                  f"tok={ptok}/{ctok} cost=${cost:.6f} depth={depth}", flush=True)
            return CallResult(
                ok=True, content=content,
                task=task, model_key=mkey, requested_model=requested,
                provider=m["provider"], latency_ms=latency_ms,
                prompt_tokens=ptok, completion_tokens=ctok,
                estimated_cost_usd=cost, fallback_depth=depth, raw=raw,
            )
        except Exception as e:
            latency_ms = int((time.time() - start) * 1000)
            err = str(e)[:200]
            last_error = err
            _log_call(task, mkey, ok=False, latency_ms=latency_ms,
                      fallback_depth=depth, error=err)
            print(f"[ai_router] task={task} model={mkey} FAIL elapsed_ms={latency_ms} "
                  f"err={err!r} depth={depth} (will{' ' if depth<len(chain)-1 else ' NOT '}fallback)", flush=True)
            continue

    return CallResult(
        ok=False, task=task, requested_model=requested,
        error=last_error or "all models in chain failed",
        fallback_depth=len(chain) - 1,
    )


# ─── Stats / introspection ───────────────────────────────────────────────────
def stats(hours: int = 24) -> dict:
    """Aggregate call metrics for the last N hours.

    Returns: {since_ts, total_calls, total_cost_usd, by_model: {...},
              by_task: {...}, recent_errors: [...]}
    """
    since = time.time() - hours * 3600
    try:
        with _db_lock:
            conn = _db_conn()
            cur = conn.cursor()

            # Aggregate per model
            cur.execute("""
                SELECT model_key, provider,
                       COUNT(*) AS calls,
                       SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END) AS ok_calls,
                       AVG(latency_ms) AS avg_ms,
                       MAX(latency_ms) AS max_ms,
                       SUM(prompt_tokens) AS in_tok,
                       SUM(completion_tokens) AS out_tok,
                       SUM(estimated_cost_usd) AS cost
                FROM ai_calls WHERE ts >= ?
                GROUP BY model_key, provider
                ORDER BY calls DESC
            """, (since,))
            by_model = []
            for r in cur.fetchall():
                avg = int(r[4]) if r[4] is not None else 0
                by_model.append({
                    "model_key": r[0], "provider": r[1],
                    "calls": r[2], "ok_calls": r[3],
                    "success_rate": round(r[3] / r[2], 3) if r[2] else 0,
                    "avg_latency_ms": avg, "max_latency_ms": r[5] or 0,
                    "prompt_tokens":  r[6] or 0,
                    "completion_tokens": r[7] or 0,
                    "cost_usd": round(r[8] or 0.0, 6),
                })

            # Aggregate per task
            cur.execute("""
                SELECT task, COUNT(*), AVG(latency_ms), SUM(estimated_cost_usd)
                FROM ai_calls WHERE ts >= ?
                GROUP BY task ORDER BY 2 DESC
            """, (since,))
            by_task = [
                {"task": r[0], "calls": r[1],
                 "avg_latency_ms": int(r[2]) if r[2] else 0,
                 "cost_usd": round(r[3] or 0.0, 6)}
                for r in cur.fetchall()
            ]

            # Recent errors
            cur.execute("""
                SELECT ts, task, model_key, error FROM ai_calls
                WHERE ok=0 AND ts >= ? ORDER BY ts DESC LIMIT 20
            """, (since,))
            recent_errors = [
                {"ts": int(r[0]), "task": r[1], "model_key": r[2], "error": r[3]}
                for r in cur.fetchall()
            ]

            # Totals
            cur.execute("SELECT COUNT(*), SUM(estimated_cost_usd) FROM ai_calls WHERE ts >= ?", (since,))
            tot = cur.fetchone()
            total_calls = tot[0] or 0
            total_cost  = round(tot[1] or 0.0, 6)

            conn.close()
        return {
            "since_ts": int(since),
            "hours": hours,
            "total_calls": total_calls,
            "total_cost_usd": total_cost,
            "by_model": by_model,
            "by_task": by_task,
            "recent_errors": recent_errors,
        }
    except Exception as e:
        return {"error": str(e)}


def healthcheck() -> dict:
    """Quick view of which providers are reachable right now."""
    return {
        "groq_configured": bool(_get_groq_key()),
        "ollama_reachable": _ollama_available(),
        "ollama_url": _get_ollama_base(),
        "registered_models": list(MODELS.keys()),
        "task_routes": dict(TASK_ROUTES),
    }
