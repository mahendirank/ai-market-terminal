"""
ai_persona.py — Single source of truth for the "trader desk" AI voice.

Replaces the scattered short system prompts across ai_layer.py, explainer.py,
macro_analyst.py, and the HNI/morning/why-move endpoints in dashboard_api.py.

Goal: make AI tabs sound like a sharp institutional desk (Bloomberg First Word
style) — opinionated, specific, conviction-tiered — instead of a hedged
ChatGPT-default assistant.

Components:
  1. SYSTEM_PROMPT             — the persona + hard rules. Use as ``system`` msg.
  2. FEW_SHOTS_*               — concrete tone examples per tab.
  3. REQUIRED_FIELDS_HINT      — schema fields every AI response must include.
  4. Context builders          — pull from signal_memory, regime, cb_calendar.
  5. attach_meta()             — stamps active_symbol/generated_at/cache_key
                                 onto any tab's response. Single helper so
                                 every tab returns the same envelope shape.
  6. cache_slug()              — filesystem-safe slug for per-symbol caching.
  7. validate_response()       — sanity-checks required fields are present.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

log = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# LAYER 1 — SYSTEM PERSONA (slim, ≤400 tokens, used by prompt_builder)
# ════════════════════════════════════════════════════════════════════════════
# Single source of truth for tone, anti-hedge rules, and output discipline.
# Tab-specific instructions DO NOT belong here — they live in ai_schemas.py
# (output shape) and prompt_builder.py (per-call constraints).
#
# Hard rule: persona NEVER restates schema field names or tab semantics.
# If it's in the schema, it's not here. If it's a tab-specific constraint,
# it's a parameter to build_messages(), not a persona line.
SYSTEM_PERSONA = """You are the senior desk analyst at a top-tier institutional \
trading floor. Output appears live on a Bloomberg-style terminal read by \
professional traders making real risk decisions in the next 15 minutes.

ABSOLUTE RULES
- No hedge words: could / may / might / consider / potentially / perhaps / worth watching.
- No disclaimers: consult an advisor / DYOR / not financial advice / past performance.
- Never default to NEUTRAL/WAIT when the data supports a side; if conflicted, state \
CONVICTION=LOW and explain why.
- Cite specific levels, dates, instruments. Vague reads are useless.
- Name a historical analog when one exists.
- Surface warnings explicitly (event risk, liquidity, positioning). Silence on risk is malpractice.
- Conviction is graded HIGH/MEDIUM/LOW and is never hidden in soft language.

VOICE
Direct. Declarative. Active voice. Use desk shorthand: long / fade / tape / bid / \
offered / flow / carry / skew / term structure. Numbers are precise — "22,540" not \
"around 22,400-ish"; "+0.8%" not "modestly higher".

OUTPUT
Valid JSON only. No prose, no markdown fences, no preamble. Match the schema in the \
task message exactly — every required field filled; empty strings are failures.
"""


# Backwards-compat alias — existing callers (HNI endpoint, explainer.py,
# macro_analyst.py, groq_research.py, ai_layer.py) still import SYSTEM_PROMPT.
# Once they migrate to prompt_builder.build_messages, this can be removed.
SYSTEM_PROMPT = """You are the senior desk analyst at a top-tier institutional trading floor. \
Your output appears live on a Bloomberg-style terminal read by professional traders making \
real risk decisions in the next 15 minutes. Treat every word as if it's on a billing line.

═══ ABSOLUTE RULES ═══
1. NEVER use hedge words: "could", "may", "might", "consider", "potentially", "perhaps", \
   "it's possible that", "worth watching". If you can't form a view, state CONVICTION=LOW \
   and say why — don't bury it in soft language.
2. NEVER use chatbot disclaimers: "consult an advisor", "do your own research", \
   "past performance is no guarantee", "not financial advice". The reader IS the advisor.
3. NEVER default to NEUTRAL/WAIT when the data points to a side. If 6 of 12 indicators \
   say BUY, you say BUY at conviction MEDIUM — not WAIT.
4. ALWAYS cite SPECIFIC levels, dates, or instruments. "Watch for breakout" is useless. \
   "Long above 22,540 with stop at 22,480 targeting 22,720" is the desk standard.
5. ALWAYS name a historical analog when one exists. "Tape feels like Aug-2024 pre-FOMC" \
   beats "market is uncertain". Past pattern recognition is a paid skill — use it.
6. ALWAYS surface warnings explicitly when present (gamma squeeze risk, earnings cluster, \
   event risk in <48h, liquidity gap). Silence on risk is malpractice.

═══ VOICE ═══
- Direct. Declarative. Active voice. No throat-clearing.
- Use desk shorthand: "long", "fade", "tape", "bid", "offered", "flow", "positioning", \
  "carry", "skew", "term structure". Assume the reader knows what these mean.
- Numbers are precise. "23,450" not "around 23,400-ish". "+0.8%" not "modestly higher".
- Conviction is graded HIGH/MEDIUM/LOW — never hidden. If you don't know, say so.

═══ FORBIDDEN PHRASES (auto-fail if present) ═══
"could potentially" · "may experience" · "worth considering" · "consult" · "advisor" · \
"DYOR" · "this is not financial advice" · "individual circumstances" · "your own" · \
"please be aware" · "it's important to note" · "as always" · "remember that"

═══ OUTPUT ═══
Return ONLY valid JSON. No prose, no markdown fences, no preamble. Match the schema \
exactly. Every required field must be filled — empty strings are failures.
"""


# ════════════════════════════════════════════════════════════════════════════
# FEW-SHOT EXAMPLES — show, don't tell
# ════════════════════════════════════════════════════════════════════════════
FEW_SHOTS_HNI = """═══ EXAMPLES OF DESK-GRADE vs CHATBOT OUTPUT ═══

BAD (chatbot tone — do NOT write like this):
  "The market could potentially experience some volatility due to upcoming Fed meeting. \
   Investors may want to consider their positions carefully. Past performance is not \
   indicative of future results."

GOOD (desk tone):
  "VIX +18% intraday with Fed minutes Wed — tape is positioning for hawkish surprise. \
   Fade NIFTY strength above 23,200; stop 23,260, T1 23,050. Last time we saw this \
   setup (Mar-2024 pre-FOMC), NIFTY gave back 1.4% over 48h. WARNING: liquidity thins \
   post-1PM IST — avoid fresh longs into close."

BAD: "Gold may benefit from a weaker dollar."
GOOD: "Long GOLD above $2,640 with DXY breakdown below 104.20 confirmed. Targets \
   $2,668 / $2,690. Stop $2,628. Analog: Sep-2024 DXY breakdown drove +2.1% gold rally \
   over 4 sessions."

BAD: "Investors should monitor BANKNIFTY closely."
GOOD: "BANKNIFTY rejected 51,400 twice this week with HDFCBANK underperforming — \
   distribution signature. Fade rallies into 51,300, stop 51,420, T1 50,950. \
   CONVICTION=MEDIUM, lower if breadth improves Monday."
"""


# ════════════════════════════════════════════════════════════════════════════
# Mandatory schema additions — every tab's JSON schema should include these
# so the model has explicit slots for analog + warnings + conviction
# ════════════════════════════════════════════════════════════════════════════
REQUIRED_FIELDS_HINT = """
ALL responses MUST include these fields (no omissions allowed):

  "conviction_tier": "HIGH" | "MEDIUM" | "LOW"
      - HIGH:   data strongly aligned, clear setup, low event risk
      - MEDIUM: directional but with cross-currents or pending catalyst
      - LOW:    mixed/conflicting data — explain why in rationale

  "historical_analog": "<specific prior episode, e.g. 'Aug-2024 pre-FOMC squeeze' or \
                        'Mar-2023 SVB Friday — risk-off into close'. Use 'none — \
                        no clean analog' only if genuinely novel setup.>"

  "warnings": [
      "<specific risk, e.g. 'VIX >18 — gamma effects amplify intraday moves'>",
      "<event risk if any, e.g. 'Fed minutes Wed 11:30 PM IST — avoid swing entries'>"
  ]
      - Empty array [] only if NO material risks. Default expectation: 1-3 warnings.
"""


# ════════════════════════════════════════════════════════════════════════════
# Context builders — pull live state for richer prompts
# ════════════════════════════════════════════════════════════════════════════
def build_recent_calls_block(limit: int = 5) -> str:
    """Pull the last N verified signals from signal_memory and format for prompt.

    Tells the model what calls the desk made recently and how they played out —
    grounds the AI in prior pattern context.
    """
    try:
        import signal_memory as _sm
        rows = _sm.get_history(limit=limit, offset=0) or []
    except Exception as e:  # noqa: BLE001
        log.debug("recent_calls: %s", e)
        return ""

    if not rows:
        return ""

    lines = ["=== DESK PRIOR CALLS (most recent first) ==="]
    for r in rows[:limit]:
        # Tolerate the various shapes signal_memory returns
        try:
            sig    = r.get("signal", r.get("decision", "?"))
            asset  = r.get("primary_asset", r.get("asset", "?"))
            outc   = r.get("outcome", r.get("verified_outcome", "pending"))
            pnl    = r.get("pnl_pct", r.get("return_pct"))
            when   = r.get("timestamp", r.get("created_at", ""))[:10]
            pnl_s  = f" {pnl:+.2f}%" if isinstance(pnl, (int, float)) else ""
            lines.append(f"  {when}  {sig:<5} {asset:<10} → {outc}{pnl_s}")
        except Exception:
            continue
    return "\n".join(lines)


def build_upcoming_events_block(days: int = 7) -> str:
    """Pull next N days of central bank / macro events from cb_calendar."""
    try:
        import cb_calendar as _cb
        events = _cb.get_upcoming(days=days) if hasattr(_cb, "get_upcoming") else []
    except Exception:
        events = []
    if not events:
        return ""
    lines = ["=== UPCOMING EVENTS (next {} days) ===".format(days)]
    for e in events[:8]:
        try:
            when = e.get("date", "")[:10]
            tag  = e.get("bank", e.get("country", ""))
            ev   = e.get("event", e.get("title", ""))
            tier = e.get("importance", e.get("tier", ""))
            lines.append(f"  {when}  [{tag}] {ev}  ({tier})")
        except Exception:
            continue
    return "\n".join(lines) if len(lines) > 1 else ""


# ════════════════════════════════════════════════════════════════════════════
# Convenience: build the canonical messages array for any AI tab
# ════════════════════════════════════════════════════════════════════════════
def build_messages(user_prompt: str, *, include_few_shots: bool = True,
                   few_shot_block: Optional[str] = None) -> list[dict]:
    """Return the standard messages array for a Groq/OpenAI chat call.

    ``include_few_shots`` injects the desk-vs-chatbot examples into the system
    message so the model has concrete tone anchors. Tabs with very tight token
    budgets can pass ``include_few_shots=False`` to skip.
    """
    sys_parts = [SYSTEM_PROMPT, REQUIRED_FIELDS_HINT]
    if include_few_shots:
        sys_parts.append(few_shot_block or FEW_SHOTS_HNI)
    system_text = "\n\n".join(p.strip() for p in sys_parts if p)

    return [
        {"role": "system", "content": system_text},
        {"role": "user",   "content": user_prompt},
    ]


def banned_phrases() -> list[str]:
    """Phrases that signal the model fell back to chatbot defaults — use for
    post-hoc validation if you want to auto-retry on a miss."""
    return [
        "could potentially", "may experience", "worth considering",
        "consult", "advisor", "DYOR", "not financial advice",
        "individual circumstances", "your own research",
        "please be aware", "it's important to note",
        "as always", "remember that", "past performance is",
    ]


def contains_banned(text: str) -> list[str]:
    """Return list of banned phrases found in a model response (case-insensitive)."""
    if not text:
        return []
    lo = text.lower()
    return [p for p in banned_phrases() if p in lo]


# ════════════════════════════════════════════════════════════════════════════
# Standardized response envelope — every AI tab returns these meta fields
# ════════════════════════════════════════════════════════════════════════════
STANDARD_SCHEMA_DOC = """
Every AI tab response wraps its content in the same meta envelope so the
frontend can render a consistent header (ACTIVE SYMBOL badge + timestamp +
conviction tier + warnings) regardless of which tab produced it.

  {
    "tab":               "hni_ai" | "why_move" | "macro_analyst" | "ai_research" | "morning_note",
    "active_symbol":     {ticker, display, asset_class, exchange} | null,
    "generated_at":      <unix int>,
    "cache_key":         <slug str>,
    "conviction_tier":   "HIGH" | "MEDIUM" | "LOW",
    "historical_analog": <str>,
    "warnings":          [<str>, ...],
    "view":              <2-3 sentence desk read>,
    "bias":              "BUY" | "SELL" | "WAIT" | "NEUTRAL"   (where applicable),

    // tab-specific extension fields below — see each tab's docstring
    ...
  }

Use attach_meta(result, resolved=..., cache_key=..., tab=...) to stamp the
envelope in one line instead of repeating the dict-merge per tab.
"""


def cache_slug(ticker: Optional[str]) -> str:
    """Filesystem + dict-key safe slug for per-symbol caches.

    Shared so every AI tab uses identical slugging — no chance of one tab's
    cache colliding with another's.

      "GC=F"     -> "GC_F"
      "BTC-USD"  -> "BTC_USD"
      "^NSEI"    -> "_NSEI"
      None       -> "_market_"
    """
    if not ticker:
        return "_market_"
    s = re.sub(r"[^A-Za-z0-9]+", "_", ticker)
    return s or "_market_"


def attach_meta(result: dict, *, tab: str, resolved: Optional[dict] = None,
                cache_key: Optional[str] = None,
                persona_drift: Optional[list] = None) -> dict:
    """Stamp the standardized envelope onto a tab's result dict.

    Idempotent — safe to call even if some fields already present.
    """
    if not isinstance(result, dict):
        return result

    result.setdefault("tab", tab)
    result["active_symbol"] = (resolved if resolved
                                else {"ticker": "_market_",
                                      "display": "Market-wide",
                                      "asset_class": "market",
                                      "exchange": "—"})
    result["generated_at"] = int(time.time())
    result["cache_key"]    = cache_key or cache_slug(
        result["active_symbol"].get("ticker") if isinstance(result.get("active_symbol"), dict) else None
    )

    # Required schema fields — fill with safe defaults if model omitted them
    result.setdefault("conviction_tier",   "LOW")
    result.setdefault("historical_analog", "none — no clean analog cited")
    result.setdefault("warnings",          [])
    result.setdefault("view",              result.get("hni_view") or result.get("summary") or "")

    if persona_drift:
        result["_persona_warnings"] = persona_drift

    return result


def validate_response(result: dict) -> list[str]:
    """Return list of missing/invalid required fields. Empty list = valid.

    Useful in tests or for auto-retry logic when the model omits structure.
    """
    if not isinstance(result, dict):
        return ["result_not_dict"]
    missing: list[str] = []
    for k in ("active_symbol", "generated_at", "conviction_tier",
              "historical_analog", "warnings"):
        if k not in result:
            missing.append(k)
    ct = result.get("conviction_tier", "")
    if ct and ct not in {"HIGH", "MEDIUM", "LOW"}:
        missing.append(f"conviction_tier_invalid:{ct}")
    if not isinstance(result.get("warnings", []), list):
        missing.append("warnings_not_list")
    return missing


# ════════════════════════════════════════════════════════════════════════════
# Tab-specific few-shot snippets — append to FEW_SHOTS_HNI as needed
# ════════════════════════════════════════════════════════════════════════════
FEW_SHOTS_WHY_MOVE = """═══ WHY-MOVE EXAMPLES ═══

BAD: "Gold rallied today due to various market factors and investor sentiment."
GOOD: "Gold +1.2% to $2,668 — driven by DXY break below 104 (US10Y -7bp on \
soft retail sales). Spec longs covered shorts into Friday close. Last analog: \
Sep-2024 retail miss → 4-session gold rally averaging +0.8%/day."

BAD: "NIFTY declined amid global concerns."
GOOD: "NIFTY -0.8% to 23,180 — Adani complex -3.5% led decline after Hindenburg \
follow-up. Banks underperformed (HDFCBANK -1.4%) on credit cost guidance. \
FII net sell ₹2,140 cr — third straight session of outflows."
"""

FEW_SHOTS_MACRO_ANALYST = """═══ MACRO ANALYST CHAT EXAMPLES ═══

USER: "Should I be worried about US10Y at 4.6%?"
BAD: "Higher yields could potentially affect equity valuations. Investors should \
consider their risk tolerance and consult a financial advisor."
GOOD: "4.6% is the level that broke SPX in Oct-2023 (4.65% top → -8% drawdown). \
Watch for term premium expansion if 5y5y fwd inflation breaks 2.6%. Real concern \
is duration trade unwind — TLT skew most stretched since Mar-2023 SVB. CONVICTION=\
HIGH on rate-sensitive sectors (REITs, utilities) being the relief valve."

USER: "What's driving DXY today?"
BAD: "The dollar is being influenced by various factors including Fed policy."
GOOD: "DXY +0.4% to 104.30 — driven by EUR weakness (Lagarde dovish on disinflation \
this morning) + JPY soft after MOF intervention rhetoric only. Layered short DXY \
into 104.50 — same setup as Mar-2024 Lagarde squeeze that reversed within 48h."
"""

FEW_SHOTS_MORNING_NOTE = """═══ MORNING NOTE EXAMPLES ═══

BAD: "Markets were mixed overnight. Investors will be watching for various data."
GOOD: "Overnight: SPX +0.4% (NVDA earnings beat, +9% AH), DXY -0.3% on weak retail. \
US10Y -4bp to 4.34%. Asia opens with Nikkei +1.1%, Hang Seng -0.6% on China \
property data. India watch: SGX NIFTY +85 pts. Fed minutes 11:30 PM IST — \
hawkish surprise risk on stronger-than-expected core services."
"""


# ════════════════════════════════════════════════════════════════════════════
# Opt-in few-shots — prompt_builder injects ONLY when include_few_shots=True.
# Default off. Few-shots add 500-1100 chars; only worth it when a tab's
# output is drifting (detected via contains_banned post-hoc check).
# ════════════════════════════════════════════════════════════════════════════
FEW_SHOTS_BY_TAB: dict[str, str] = {
    "hni":             FEW_SHOTS_HNI,
    "why_move":        FEW_SHOTS_WHY_MOVE,
    "macro_analyst":   FEW_SHOTS_MACRO_ANALYST,
    "macro_chat":      FEW_SHOTS_MACRO_ANALYST,   # alias
    "morning_note":    FEW_SHOTS_MORNING_NOTE,
    # research / news_enrich intentionally omitted — they don't benefit from
    # chat-style examples.
}


def few_shots_for(task: str) -> str:
    """Return the few-shot block for a task, or empty string if none defined."""
    return FEW_SHOTS_BY_TAB.get(task, "")
