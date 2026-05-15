"""
ai_models.py — Centralized model registry.

Single source of truth for every model the system can call. Each entry
captures provider, context window, pricing, capability tags, and fallback
chain. Changing the model for a task is a one-line edit here — no caller
needs to know.

Pricing notes:
  - Groq free tier is currently $0/token but with rate limits. We track
    prices as if paid (Groq's published per-token rates) so cost tracking
    is meaningful from day one and survives a future paid-tier upgrade.
  - Local Ollama models are $0/token but consume local RAM/CPU/disk.

Mixtral note:
  - Mixtral-8x7B was removed from Groq in early 2025. Their fast-tier
    replacement is llama-3.1-8b-instant. Mixtral is registered here as
    an Ollama-only entry for anyone who wants to run it locally.
"""
from __future__ import annotations

from typing import Optional


# ─── Provider definitions ────────────────────────────────────────────────────
PROVIDER_GROQ   = "groq"
PROVIDER_OLLAMA = "ollama"
PROVIDER_OPENAI = "openai"        # placeholder for future
PROVIDER_VLLM   = "vllm"          # placeholder for future local-GPU


# ─── Capability tags ─────────────────────────────────────────────────────────
# Tasks ask for capabilities; the router picks a model that has them.
CAP_REASONING    = "reasoning"      # complex multi-step inference, long context
CAP_FAST         = "fast"           # short prompts, sub-second is the goal
CAP_JSON         = "json"           # reliable structured output
CAP_EXTRACTION   = "extraction"     # pull fields out of text deterministically
CAP_CHAT         = "chat"           # multi-turn conversation
CAP_LOCAL        = "local"          # no network round-trip needed
CAP_LARGE_CTX    = "large_ctx"      # 32k+ context window


# ─── Model registry ──────────────────────────────────────────────────────────
# Schema per entry:
#   provider, model_id, context_window, max_output,
#   price_in_per_mtok (USD per 1M input tokens — 0 for local/free),
#   price_out_per_mtok,
#   capabilities (set of CAP_*),
#   fallback (ordered list of model keys to try if this one fails)
MODELS: dict[str, dict] = {
    # ── Groq cloud models ───────────────────────────────────────────────────
    "groq:llama-3.3-70b": {
        "provider": PROVIDER_GROQ,
        "model_id": "llama-3.3-70b-versatile",
        "context_window": 131072,
        "max_output": 8192,
        "price_in_per_mtok":  0.59,   # USD/1M input tokens (Groq paid tier)
        "price_out_per_mtok": 0.79,
        "capabilities": {CAP_REASONING, CAP_CHAT, CAP_JSON, CAP_LARGE_CTX},
        "fallback": ["groq:llama-3.1-8b", "ollama:llama3.2"],
    },
    "groq:llama-3.1-8b": {
        "provider": PROVIDER_GROQ,
        "model_id": "llama-3.1-8b-instant",
        "context_window": 131072,
        "max_output": 8192,
        "price_in_per_mtok":  0.05,
        "price_out_per_mtok": 0.08,
        "capabilities": {CAP_FAST, CAP_CHAT, CAP_JSON, CAP_LARGE_CTX},
        "fallback": ["ollama:llama3.2"],
    },
    "groq:gemma2-9b": {
        "provider": PROVIDER_GROQ,
        "model_id": "gemma2-9b-it",
        "context_window": 8192,
        "max_output": 8192,
        "price_in_per_mtok":  0.20,
        "price_out_per_mtok": 0.20,
        "capabilities": {CAP_FAST, CAP_CHAT, CAP_JSON},
        "fallback": ["groq:llama-3.1-8b", "ollama:llama3.2"],
    },

    # ── Local Ollama models ────────────────────────────────────────────────
    # User runs `ollama pull <model>` once before first use. Router detects
    # unavailability via a probe and falls back gracefully.
    "ollama:qwen2.5:7b": {
        "provider": PROVIDER_OLLAMA,
        "model_id": "qwen2.5:7b",
        "context_window": 32768,
        "max_output": 4096,
        "price_in_per_mtok":  0.0,
        "price_out_per_mtok": 0.0,
        "capabilities": {CAP_LOCAL, CAP_EXTRACTION, CAP_JSON, CAP_FAST},
        "fallback": ["groq:llama-3.1-8b"],
    },
    "ollama:llama3.2": {
        "provider": PROVIDER_OLLAMA,
        "model_id": "llama3.2:latest",
        "context_window": 131072,
        "max_output": 4096,
        "price_in_per_mtok":  0.0,
        "price_out_per_mtok": 0.0,
        "capabilities": {CAP_LOCAL, CAP_CHAT, CAP_FAST},
        "fallback": ["groq:llama-3.1-8b"],
    },
    "ollama:mixtral": {
        # Optional — pull via `ollama pull mixtral`. Heavy (~26GB) so most
        # VPS deployments will skip; M4 desktop can run it for power users.
        "provider": PROVIDER_OLLAMA,
        "model_id": "mixtral:latest",
        "context_window": 32768,
        "max_output": 4096,
        "price_in_per_mtok":  0.0,
        "price_out_per_mtok": 0.0,
        "capabilities": {CAP_LOCAL, CAP_REASONING, CAP_CHAT, CAP_LARGE_CTX},
        "fallback": ["groq:llama-3.3-70b", "groq:llama-3.1-8b"],
    },
}


# ─── Task → preferred model ──────────────────────────────────────────────────
# This is the "router map". Changing where a task runs is a one-line edit.
# Every task name here is canonical — callers reference by task, not by model.
TASK_ROUTES: dict[str, str] = {
    # Heavy reasoning tasks (HNI, Why Move, Macro Analyst, Research)
    "reasoning":     "groq:llama-3.3-70b",
    "research":      "groq:llama-3.3-70b",
    "chat":          "groq:llama-3.3-70b",
    "hni":           "groq:llama-3.3-70b",
    "why_move":      "groq:llama-3.3-70b",
    "morning_note":  "groq:llama-3.3-70b",

    # Fast/cheap tasks (news enrichment, short summaries)
    "fast_summary":  "groq:llama-3.1-8b",
    "news_enrich":   "groq:llama-3.1-8b",
    "classify":      "groq:llama-3.1-8b",

    # Extraction tasks — JSON-shaped, deterministic. Prefer local Qwen if
    # available; router falls back to Groq 8b if Ollama not reachable.
    "extraction":    "ollama:qwen2.5:7b",
    "json_extract":  "ollama:qwen2.5:7b",
    "symbol_parse":  "ollama:qwen2.5:7b",
}


def get_model(key: str) -> Optional[dict]:
    """Return a registry entry by key, or None if unknown."""
    return MODELS.get(key)


def get_task_route(task: str) -> Optional[str]:
    """Return the model key configured for a task, or None."""
    return TASK_ROUTES.get(task)


def estimate_cost(model_key: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Estimate USD cost for a single call. Returns 0.0 for local models."""
    m = MODELS.get(model_key)
    if not m:
        return 0.0
    cost_in  = (prompt_tokens / 1_000_000) * m.get("price_in_per_mtok", 0.0)
    cost_out = (completion_tokens / 1_000_000) * m.get("price_out_per_mtok", 0.0)
    return round(cost_in + cost_out, 6)


def list_models_by_capability(cap: str) -> list[str]:
    """Return model keys that advertise a given capability — useful for
    auto-discovery when adding new tasks."""
    return [k for k, v in MODELS.items() if cap in v.get("capabilities", set())]
