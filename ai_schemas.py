"""
ai_schemas.py — Layer 3 output schemas (single source of truth per tab).

Compact JSON-skeleton notation, NOT English-descriptive. Every required
field appears as a key with a tight type/constraint hint. The persona
(Layer 1) already says no hedge words / no disclaimers / cite levels —
this file does NOT repeat those instructions. The model gets one
non-duplicated answer about "what shape do you want?".

Conventions used in the skeletons:
  "key": "str"                  → free string
  "key": "str·NN-w"             → string with word cap
  "key": "str·NN-sent"          → string with sentence cap
  "key": "int 0-100"            → integer in range
  "key": "A|B|C"                → enum (pipe-separated)
  "key": ["..."]                → array of items matching template
  "key": "<custom hint>"        → free-form constraint

Why skeleton notation?
  English descriptions like "<integer 0-100>" or "<2-3 sentence opinionated
  desk read with specific levels>" cost 15-30 tokens each AND re-state
  rules the persona already covers. Skeleton notation is ~5 tokens per
  field and stays orthogonal to persona instructions.

Usage:
    from ai_schemas import schema_for
    schema_text = schema_for("hni")     # str ready to paste into prompt
"""
from __future__ import annotations


# ─── Tab → schema ────────────────────────────────────────────────────────────

SCHEMA_HNI = """\
{
  "macro_regime":     "BULL_MOMENTUM|BEAR_PRESSURE|RISK_OFF|RISK_ON|SIDEWAYS|BREAKOUT|DISTRIBUTION|ACCUMULATION",
  "trade_bias":       "BUY|SELL|WAIT",
  "confidence":       "int 0-100",
  "conviction_tier":  "HIGH|MEDIUM|LOW",
  "hni_view":         "str·2-3sent·cite levels",
  "historical_analog":"str | 'none — no clean analog'",
  "warnings":         ["str·specific risk", "str·event risk if any"],
  "instruments": [
    {"name":"str","signal":"BUY|SELL|WAIT","rationale":"str·≤20w·cite level"}
  ],
  "scalp_setup": {
    "bias":"BUY|SELL|WAIT",
    "instrument":"str",
    "entry_zone":"str·exact range",
    "stop_loss":"str·exact price",
    "tp1":"str·exact price",
    "tp2":"str·exact price",
    "trigger_condition":"str·what confirms entry"
  },
  "swing_setup": {
    "bias":"BUY|SELL|WAIT",
    "instrument":"str",
    "entry_zone":"str·exact range",
    "stop_loss":"str·exact price",
    "tp":"str·target + timeframe",
    "catalyst":"str·specific driver"
  }
}"""


SCHEMA_WHY_MOVE = """\
{
  "what_moved":         "str·1sent·asset + magnitude with numbers",
  "why_it_moved":       "str·2-3sent·link to macro drivers in state",
  "evidence":           ["str·bullet citing live data"],
  "historical_analog":  "str | 'none — no clean analog'",
  "risk_to_thesis":     "str·what invalidates this read",
  "forward_implication":"str·next session/week implications",
  "conviction_tier":    "HIGH|MEDIUM|LOW",
  "warnings":           ["str·specific risk"],
  "tags":               ["str·institutional term", "..."]
}"""


SCHEMA_MORNING_NOTE = """\
{
  "date":              "str·DD MMM YYYY",
  "headline":          "str·≤12w·bold market theme",
  "global_cues":       "str·2-3sent·overnight cues + India impact, cite levels",
  "key_levels": {
    "nifty":     {"support":"str","resistance":"str","bias":"BUY|SELL|WAIT"},
    "banknifty": {"support":"str","resistance":"str","bias":"BUY|SELL|WAIT"}
  },
  "top_3_ideas": [
    {"instrument":"str","direction":"BUY|SELL","rationale":"str·≤20w","entry":"str","sl":"str","target":"str"}
  ],
  "watch_out_for":     "str·1sent·specific event with time",
  "overall_bias":      "BULLISH|BEARISH|NEUTRAL",
  "conviction_tier":   "HIGH|MEDIUM|LOW",
  "historical_analog": "str | 'none — no clean analog'",
  "warnings":          ["str·specific risk"]
}"""


# Macro Analyst is a chat tab — free-form prose, NOT JSON. The "schema"
# here is structural guidance only, not a JSON template.
SCHEMA_MACRO_CHAT = """\
FORMAT
- 3-5 short paragraphs of plain prose. NOT JSON.
- Open with the strongest data point. End with what invalidates the view.
- Cite specific levels from the state block — never invent figures.
- Use institutional vocabulary; avoid retail terms (moon/rip/dump/pump).
"""


SCHEMA_RESEARCH = """\
FORMAT
- 5-8 tight bullets. NOT JSON.
- Each bullet cites a specific level / level / % / date from the state block.
- Lead with the strongest signal.
- Final bullet: what would invalidate the read.
"""


# News enrichment is per-item batch — different shape, kept compact for cost.
SCHEMA_NEWS_ENRICH = """\
JSON array, one object per input item:
[
  {
    "i":         "int·1-based input index",
    "summary":   "str·≤20w·market-focused",
    "sentiment": "BULL|BEAR|NEU",
    "impact":    "int 1-10",
    "assets":    ["str·affected asset codes"],
    "why":       "str·≤15w·trader relevance"
  }
]"""


# ─── Lookup ──────────────────────────────────────────────────────────────────
_SCHEMA_TABLE: dict[str, str] = {
    "hni":              SCHEMA_HNI,
    "why_move":         SCHEMA_WHY_MOVE,
    "morning_note":     SCHEMA_MORNING_NOTE,
    "macro_analyst":    SCHEMA_MACRO_CHAT,
    "macro_chat":       SCHEMA_MACRO_CHAT,        # alias
    "research":         SCHEMA_RESEARCH,
    "ai_research":      SCHEMA_RESEARCH,          # alias
    "news_enrich":      SCHEMA_NEWS_ENRICH,
}


def schema_for(task: str) -> str:
    """Return the schema block for a task, or empty string if none defined.

    Empty return is intentional — callers can detect "no schema = free-form"
    and skip the schema section of the prompt entirely.
    """
    return _SCHEMA_TABLE.get(task, "")


def list_tasks() -> list[str]:
    """Return the registered task names — useful for introspection."""
    return sorted(_SCHEMA_TABLE.keys())


def schema_size(task: str) -> int:
    """Char count of the schema string — useful for token budgeting."""
    return len(schema_for(task))
