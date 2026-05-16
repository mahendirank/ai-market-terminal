"""
prompt_builder.py — Layer-1 + Layer-2 + Layer-3 composer.

Single entry point for assembling AI tab prompts. Replaces ad-hoc f-string
construction scattered across dashboard_api / explainer / macro_analyst.

Three-layer composition (see ARCHITECTURE.md):

  L1  SYSTEM PERSONA  — ai_persona.SYSTEM_PERSONA (constant ~400 tok)
  L2  STATE BLOCK     — market_intel.format_state_compact(snap) (~250 tok)
  L3  TASK BLOCK      — schema (from ai_schemas) + per-call constraints

Hard guarantee: no layer ever repeats another layer's content. The composer
knows where each instruction lives and assembles them exactly once.

Usage:
    from prompt_builder import build_messages
    msgs = build_messages(
        task="hni",
        snap=intel_snapshot,
        symbol="GOLD",
        focus_display="Gold (XAU/USD)",
        focus_ticker="GC=F",
    )
    response = ai_router.chat(task="hni", messages=msgs, ...)

The returned ``msgs`` is the standard 2-message OpenAI format that
ai_router (or any provider client) understands directly.
"""
from __future__ import annotations

from typing import Optional

from ai_persona import SYSTEM_PERSONA, few_shots_for
from ai_schemas import schema_for


# ─── Reasoning payload rendering (Phase 6) ──────────────────────────────────
# Renders the macro_reasoning_engine Stage-5 output into a compact text block
# AI tabs can read alongside the state block. Pure string concatenation —
# no LLM, no summarization, deterministic.
#
# Reasoning payload represents DIRECTIONAL INTELLIGENCE, never trade orders.
# The "INTEL · not_for_execution · directional_intelligence" footer is
# rendered literally so downstream consumers (humans + LLMs) see the marker.

REASONING_MODES = ("compact", "verbose", "hidden")
DEFAULT_REASONING_MODE = "compact"


def format_reasoning_compact(reasoning: dict) -> str:
    """Render Stage-5 reasoning as a compact macro-read block.

    Target: <120 tokens. Pure rendering, no LLM, no summarization.
    Returns empty string if reasoning is None or not a dict.

    Block sections (each one line):
      REGIME · DRIVER · POSTURE · PREFERRED · WEAK · CONFLICTS · VOL · CATALYST
      + intent marker footer
    """
    if not reasoning or not isinstance(reasoning, dict):
        return ""

    lines: list[str] = ["=== MACRO READ ==="]

    scenario = reasoning.get("scenario_name") or "UNKNOWN"
    conf     = reasoning.get("overall_confidence") or 0
    lines.append(f"REGIME: {scenario}  CONF: {conf}/100")

    driver = reasoning.get("dominant_driver") or "ambiguous"
    lines.append(f"DRIVER: {driver}")

    scalp_bias = (reasoning.get("scalp")    or {}).get("bias", "?")
    intra_bias = (reasoning.get("intraday") or {}).get("bias", "?")
    swing_bias = (reasoning.get("swing")    or {}).get("bias", "?")
    lines.append(f"POSTURE: scalp={scalp_bias} · intraday={intra_bias} · swing={swing_bias}")

    preferred = reasoning.get("preferred_assets") or []
    weak      = reasoning.get("weak_assets") or []
    if preferred:
        lines.append("PREFERRED: " + ", ".join(str(a) for a in preferred[:5]))
    if weak:
        lines.append("WEAK: " + ", ".join(str(a) for a in weak[:5]))

    conflicts = reasoning.get("conflicts") or []
    if conflicts:
        types = [str(c.get("type", "?")) for c in conflicts]
        sample = ", ".join(types[:2])
        if len(conflicts) > 2:
            sample += f" (+{len(conflicts) - 2} more)"
        lines.append(f"CONFLICTS({len(conflicts)}): {sample}")

    # Cap warnings at 78 chars so worst-case compact (with conflicts + both
    # warnings) stays under the 120-token target. Truncation is informational
    # only — full text is reachable in verbose mode or the raw reasoning dict.
    vw = reasoning.get("volatility_warning")
    if vw:
        lines.append(f"VOL: {str(vw)[:78]}")
    cr = reasoning.get("catalyst_risk")
    if cr:
        lines.append(f"CATALYST: {str(cr)[:78]}")

    # Intent footer — survives prompt rendering deliberately so downstream
    # consumers (AI tabs reading their own prompt, debugging tools, telemetry)
    # see it. NOT cosmetic — preserves the not_for_execution contract.
    lines.append("INTEL · not_for_execution · directional_intelligence")

    return "\n".join(lines)


def format_reasoning_verbose(reasoning: dict) -> str:
    """Verbose macro-read block — compact + breakdown + thesis text.

    Target: <300 tokens. Use for debugging, audit trails, or high-stakes
    calls where the full reasoning matters. Default tabs should use compact.
    """
    if not reasoning or not isinstance(reasoning, dict):
        return ""

    base = format_reasoning_compact(reasoning)
    if not base:
        return ""

    extras: list[str] = []

    breakdown = reasoning.get("confidence_breakdown") or {}
    if breakdown:
        bits = " · ".join(f"{k}={v}" for k, v in breakdown.items())
        extras.append(f"CONF_BREAKDOWN: {bits}")

    # Per-horizon thesis invalidator — the macro-thesis-breaks condition,
    # NOT a stop-loss. Preserves the directional_intelligence semantics.
    for hz in ("scalp", "intraday", "swing"):
        h = reasoning.get(hz) or {}
        inv = h.get("thesis_invalidator")
        if inv:
            extras.append(f"{hz.upper()}_THESIS_BREAKS_IF: {str(inv)[:100]}")

    conflicts = reasoning.get("conflicts") or []
    if conflicts:
        extras.append("CONFLICT_DETAILS:")
        for c in conflicts[:4]:
            desc    = str(c.get("description", "?"))[:110]
            penalty = c.get("penalty", 0)
            extras.append(f"  · {desc} ({penalty:+d})")

    if not extras:
        return base
    return base + "\n" + "\n".join(extras)


def _render_reasoning_block(reasoning: Optional[dict],
                              mode: str) -> str:
    """Internal dispatch for build_messages — returns the rendered block
    (or empty string for ``hidden`` / unknown modes)."""
    if reasoning is None:
        return ""
    mode = mode if mode in REASONING_MODES else DEFAULT_REASONING_MODE
    if mode == "hidden":
        return ""
    if mode == "verbose":
        return format_reasoning_verbose(reasoning)
    return format_reasoning_compact(reasoning)


# ─── Builder ────────────────────────────────────────────────────────────────
def build_messages(
    task: str,
    *,
    snap: Optional[dict] = None,
    reasoning: Optional[dict] = None,
    reasoning_mode: str = DEFAULT_REASONING_MODE,
    symbol: Optional[str] = None,
    focus_display: Optional[str] = None,
    focus_ticker: Optional[str] = None,
    constraints: Optional[list[str]] = None,
    extra_context: Optional[str] = None,
    schema_override: Optional[str] = None,
    include_few_shots: bool = False,
    state_max_clusters: int = 8,
) -> list[dict]:
    """Compose a 2-message prompt (system + user) for an AI tab call.

    Parameters
    ----------
    task : str
        Canonical task name (matches ai_schemas / ai_router routing).
        Examples: "hni", "why_move", "morning_note", "macro_analyst",
                  "research", "news_enrich".
    snap : dict, optional
        Output of market_intel.get_intel_snapshot(). Rendered into the L2
        state block via market_intel.format_state_compact(). Omit for tabs
        that don't need market state.
    reasoning : dict, optional
        Output of macro_reasoning_engine.generate_trades() or analyze_stage5().
        When provided, a MACRO READ block is prepended to L2 above the raw
        STATE block. Carries directional_intelligence — NOT trade orders.
        The ``not_for_execution`` marker is preserved in the rendered text.
        Default None (no MACRO READ block).
    reasoning_mode : str
        One of: ``compact`` (~80-110 tok, default), ``verbose`` (~180-260 tok,
        adds breakdown + thesis text), ``hidden`` (skip block entirely).
        Invalid modes fall back to compact.
    symbol : str, optional
        Active ticker (already resolved via symbol_resolver). When given,
        adds a FOCUS line to the task block and forces the model toward
        single-instrument analysis.
    focus_display : str, optional
        Human-friendly display name for the symbol (e.g. "Gold (XAU/USD)").
    focus_ticker : str, optional
        yfinance ticker (e.g. "GC=F") — appears in task block so the model
        can reference it in scalp/swing setups.
    constraints : list[str], optional
        Per-call task constraints. Free-form strings appended to the task
        block. Tab-specific logic (e.g. "scalp.instrument must equal X")
        belongs here, NOT in the persona.
    extra_context : str, optional
        Additional state-like content beyond the snapshot (e.g. perf_block
        from signal_memory.format_performance_for_prompt()). Inserted at
        the end of the L2 state block.
    schema_override : str, optional
        Use a custom schema instead of looking up ai_schemas.schema_for(task).
        Useful for ad-hoc tasks that don't have a registered schema.
    include_few_shots : bool
        Inject the few-shot block from ai_persona.FEW_SHOTS_BY_TAB into the
        system message. Default False — use only when output is drifting.
    state_max_clusters : int
        Cap on news clusters rendered in the L2 state block. Lower for
        cost-sensitive tabs.

    Returns
    -------
    list[dict]
        Two-message OpenAI/Groq chat format:
        ``[{"role": "system", "content": ...}, {"role": "user", "content": ...}]``
    """
    # ── Layer 1: SYSTEM ─────────────────────────────────────────────────────
    system_parts: list[str] = [SYSTEM_PERSONA]
    if include_few_shots:
        fs = few_shots_for(task)
        if fs:
            system_parts.append(fs)
    system_msg = "\n\n".join(p.strip() for p in system_parts if p)

    # ── Layer 2: STATE ──────────────────────────────────────────────────────
    state_parts: list[str] = []

    # OPTIONAL reasoning block — directional_intelligence, NOT entry signals.
    # Rendered BEFORE the raw state so the conclusion sits above the evidence.
    # When reasoning_mode == "hidden" or reasoning is None, this is a no-op.
    reasoning_block = _render_reasoning_block(reasoning, reasoning_mode)
    if reasoning_block:
        state_parts.append(reasoning_block)

    if snap is not None:
        from market_intel import format_state_compact
        state_block = format_state_compact(snap, max_clusters=state_max_clusters)
        if state_block:
            state_parts.append("=== STATE ===")
            state_parts.append(state_block)
    if extra_context:
        state_parts.append(extra_context.strip())

    # ── Layer 3: TASK ───────────────────────────────────────────────────────
    task_parts: list[str] = ["=== TASK ==="]

    if symbol or focus_display or focus_ticker:
        focus_bits = [b for b in (
            focus_display,
            f"({focus_ticker})" if focus_ticker else None,
        ) if b]
        if focus_bits:
            task_parts.append(f"FOCUS: {' '.join(focus_bits)}")
        if symbol and focus_ticker:
            task_parts.append(
                f"All entry/stop/target levels must be valid for {focus_display or symbol}. "
                f"scalp_setup.instrument and swing_setup.instrument MUST equal {focus_ticker!r}."
            )

    if constraints:
        task_parts.extend(constraints)

    # Schema — single source from ai_schemas
    schema_text = schema_override if schema_override is not None else schema_for(task)
    if schema_text:
        task_parts.append("Return JSON matching this schema (every required field filled):")
        task_parts.append(schema_text)

    task_msg = "\n\n".join(p.strip() for p in task_parts if p)

    # ── Combine state + task into one user message (single user turn) ───────
    user_msg = "\n\n".join(p for p in (
        "\n".join(state_parts) if state_parts else "",
        task_msg,
    ) if p).strip()

    return [
        {"role": "system", "content": system_msg},
        {"role": "user",   "content": user_msg},
    ]


# ─── Token estimation ───────────────────────────────────────────────────────
def estimate_tokens(text: str) -> int:
    """Rough token estimate (chars / 4). Good enough for budget tracking
    without adding tiktoken as a dependency."""
    return max(0, len(text or "") // 4)


def estimate_messages(messages: list[dict]) -> dict:
    """Per-message + total token estimate for a built prompt. Used in tests
    and the debug payload."""
    breakdown = []
    total = 0
    for m in messages:
        content = m.get("content", "") or ""
        tok = estimate_tokens(content)
        breakdown.append({
            "role":   m.get("role", "?"),
            "chars":  len(content),
            "tokens": tok,
        })
        total += tok
    return {"total_tokens": total, "messages": breakdown}


# ─── Introspection ──────────────────────────────────────────────────────────
def layer_sizes(task: str = "hni", *, with_few_shots: bool = False) -> dict:
    """Show the chars/tokens cost per layer for a given task.

    Use to confirm budget targets before a migration:
        >>> from prompt_builder import layer_sizes
        >>> layer_sizes("hni")
        {'system_persona': {'chars': ..., 'tokens': ...},
         'few_shots': ..., 'schema': ..., ...}
    """
    sys_chars = len(SYSTEM_PERSONA)
    fs_chars  = len(few_shots_for(task)) if with_few_shots else 0
    sch_chars = len(schema_for(task))
    return {
        "system_persona": {"chars": sys_chars,
                            "tokens": estimate_tokens(SYSTEM_PERSONA)},
        "few_shots":      {"chars": fs_chars,
                            "tokens": estimate_tokens(few_shots_for(task)) if with_few_shots else 0,
                            "included": with_few_shots},
        "schema":         {"chars": sch_chars,
                            "tokens": estimate_tokens(schema_for(task))},
        "task":           task,
    }
