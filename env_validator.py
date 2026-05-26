"""
env_validator.py — Startup environment-variable sanity check.

Called once from run.py before uvicorn binds the port. Prints a clear
human-readable status block listing which envs are present, which are
missing, and what each missing env will degrade. Fails hard ONLY when
a true-required var is missing — the rest are warnings the operator
should see but that don't stop the boot.

The set of required vars deliberately reflects what the *code actually
reads* (audited 2026-05-26), not a wishlist. OPENAI_API_KEY,
CLAUDE_API_KEY, DATABASE_URL are NOT validated here because no module
in this repo uses them.
"""
from __future__ import annotations

import os
import sys
from typing import Iterable


# Required vars — boot fails clearly if any are missing.
# Each entry: (env_name, human_explanation_of_what_breaks_without_it)
_REQUIRED: tuple[tuple[str, str], ...] = (
    ("GROQ_API_KEY",
     "every LLM call (explainer, narratives, signal commentary, AI sidebar) "
     "will fail. The terminal becomes a numbers-only display."),
)

# Recommended vars — boot continues with a warning. Each entry says what
# specifically degrades when the var is missing.
_RECOMMENDED: tuple[tuple[str, str], ...] = (
    ("REDIS_URL",
     "no event-bus, no cross-process cache. Falls back to in-process dicts: "
     "each worker has its own state, breaking-news invalidation stops working."),
    ("TELEGRAM_BOT_TOKEN",
     "no Telegram alerts. notify.py and alert_engine continue to call the "
     "Telegram API with a hard-coded dev token (security smell — rotate "
     "and set a real token via .env)."),
    ("TELEGRAM_CHAT_ID",
     "even with a token, alerts have nowhere to go."),
    ("FRED_API_KEY",
     "global liquidity panel falls back to yfinance proxies for US yields. "
     "Bonds/rates dashboard quality drops."),
    ("TAVILY_API_KEY",
     "groq_research deep-web lookups disabled. /api/research returns no "
     "external evidence."),
    ("TV_USERNAME",
     "tvdata logs in as guest — TradingView rate-limits aggressively, "
     "NSE indices and non-US 10Y yields fail intermittently with 429."),
    ("TV_PASSWORD",
     "(paired with TV_USERNAME)"),
)


def _check(envs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(name, why) for name, why in envs if not os.environ.get(name, "").strip()]


def validate_env(*, strict: bool = True, fd=None) -> bool:
    """Print env status and (when strict=True) sys.exit on missing required.

    Returns True when all required vars are present, False otherwise.
    The strict=False variant is useful for tests and for the diagnostic
    /api/env-check endpoint that may be added later.
    """
    out = fd or sys.stdout
    missing_required = _check(_REQUIRED)
    missing_recommended = _check(_RECOMMENDED)

    out.write("\n=== Environment validation ===\n")

    if missing_required:
        out.write(" ✗ MISSING REQUIRED:\n")
        for name, why in missing_required:
            out.write(f"     {name:<22} — {why}\n")
    else:
        out.write(" ✓ Required vars present: " +
                  ", ".join(n for n, _ in _REQUIRED) + "\n")

    if missing_recommended:
        out.write(" ⚠ recommended vars not set (degraded features):\n")
        for name, why in missing_recommended:
            out.write(f"     {name:<22} — {why}\n")
    else:
        out.write(" ✓ Recommended vars all present\n")

    out.write("===============================\n\n")
    out.flush()

    if missing_required and strict:
        sys.stderr.write(
            "FATAL: missing required environment variables. Set them in .env "
            "or as docker environment entries and retry. Boot aborted.\n"
        )
        sys.exit(78)  # EX_CONFIG — distinguishes config error from code crash

    return not missing_required
