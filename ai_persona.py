"""
ai_persona.py — Single source of truth for the "trader desk" AI voice.

Replaces the scattered short system prompts across ai_layer.py, explainer.py,
macro_analyst.py, and the HNI/morning/why-move endpoints in dashboard_api.py.

Goal: make AI tabs sound like a sharp institutional desk (Bloomberg First Word
style) — opinionated, specific, conviction-tiered — instead of a hedged
ChatGPT-default assistant.

Three components:
  1. SYSTEM_PROMPT          — the persona + hard rules. Use as ``system`` message.
  2. FEW_SHOTS              — concrete examples of good vs bad tone. Inject as
                              additional turns or append to the system message.
  3. Helpers                — context builders that pull from signal_memory,
                              regime, cb_calendar so callers don't reimplement.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# THE PERSONA — strict rules + Bloomberg-style voice
# ════════════════════════════════════════════════════════════════════════════
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
