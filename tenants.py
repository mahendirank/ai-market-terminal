"""
tenants.py — White-label / multi-tenant configuration.

One backend, many tenants. Each tenant has its own:
  - branding (name, logo text, primary/accent colors, tagline)
  - module visibility (which features are turned on)
  - enabled assets (macro bar tiles, FX pairs, charts, explainer tracked)
  - alert thresholds + telegram chat override
  - timezone + language + display preferences

Tenant detection (priority order):
  1. ?tenant=<id> in URL  → sets cookie, persists for session
  2. terminal_tenant cookie
  3. Per-user setting (in user_settings.tenant_id)
  4. "default"

Presets ship hardcoded. To add custom tenants, store in tenants.db or
extend TENANT_PRESETS at runtime.
"""
import os
import json
import sqlite3
import threading
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

# ─── Built-in tenant presets ──────────────────────────────────────────────────

# Module keys — every UI block we can toggle per tenant
ALL_MODULES = [
    "macro_bar",        # top macro tile strip
    "fx_strip",         # 6 FX major tiles below macro bar
    "news_terminal",    # main news column
    "macro_desk",       # 🔮 REGIME tab
    "cb_calendar",      # 📅 CALENDAR tab
    "macro_analyst",    # 🧠 MACRO ANALYST tab
    "explainer",        # 📡 WHY MOVE? tab
    "charts",           # 📈 CHARTS tab
    "ai_research",      # ⚡ AI RESEARCH tab
    "signals",          # 📊 SIGNALS tab
    "hni_ai",           # 🧠 HNI AI tab
    "morning_note",     # 🌅 MORNING NOTE tab
    "sectors",          # 📡 SECTORS tab
    "risk_tools",       # ⚖ RISK TOOLS tab
    "ai_perf",          # 📊 AI PERF tab
    "earnings_panel",   # earnings monitor
    "india_panels",     # NIFTY, BANKNIFTY, FII/DII, NSE earnings — India-specific
    "alerts_telegram",  # whether to send Telegram alerts at all for this tenant
]


TENANT_PRESETS = {

    # ─────────────────────────────────────────────────────────────────────────
    "default": {
        "id":      "default",
        "name":    "AI Market Terminal",
        "tagline": "Institutional intelligence — built for serious traders",
        "branding": {
            "logo_text":     "AI TERMINAL",
            "primary_color": "#22d3ee",   # cyan
            "accent_color":  "#f59e0b",   # amber
            "primary_rgb":   "34, 211, 238",
            "accent_rgb":    "245, 158, 11",
        },
        "modules": {m: True for m in ALL_MODULES},   # everything ON by default
        "assets": {
            "macro_bar":         ["GOLD", "DXY", "NASDAQ", "US10Y", "OIL", "BTC", "FG_US", "FG_CRYPTO"],
            "fx_pairs":          ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD", "USD/CHF"],
            "explainer_tracked": ["GOLD", "DXY", "EURUSD", "USDJPY", "NASDAQ", "OIL", "BTC"],
            "chart_assets":      ["GOLD", "DXY", "EURUSD", "USDJPY", "NASDAQ", "BTC", "OIL"],
        },
        "alerts": {
            "vix_spike_pct": 15.0, "vix_abs_level": 25.0, "gold_pct": 0.8,
            "yield_shock_pct": 0.5, "dxy_pct": 0.4, "min_conf": 80,
        },
        "telegram_chat_id": None,
        "timezone": "Asia/Kolkata",
        "language": "en",
    },

    # ─────────────────────────────────────────────────────────────────────────
    "uae_forex": {
        "id":      "uae_forex",
        "name":    "Zyvora MENA — Forex Intelligence Desk",
        "tagline": "Institutional FX desk intelligence for the Gulf",
        "branding": {
            "logo_text":     "ZYVORA MENA",
            "primary_color": "#d4af37",   # gold
            "accent_color":  "#22d3ee",   # cyan accent
            "primary_rgb":   "212, 175, 55",
            "accent_rgb":    "34, 211, 238",
        },
        "modules": {
            **{m: True for m in ALL_MODULES},
            "india_panels":   False,   # hide NIFTY/BANKNIFTY/FII-DII
            "earnings_panel": False,   # hide earnings monitor
            "ai_research":    True,
            "hni_ai":         True,
            "morning_note":   True,
        },
        "assets": {
            "macro_bar":         ["GOLD", "DXY", "NASDAQ", "US10Y", "OIL", "BTC", "FG_US", "FG_CRYPTO"],
            "fx_pairs":          ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD", "USD/CHF"],
            "explainer_tracked": ["GOLD", "DXY", "EURUSD", "USDJPY", "OIL", "BTC"],   # no NDX
            "chart_assets":      ["GOLD", "DXY", "EURUSD", "USDJPY", "BTC", "OIL"],
        },
        "alerts": {
            "vix_spike_pct": 12.0, "vix_abs_level": 22.0, "gold_pct": 0.5,
            "yield_shock_pct": 0.4, "dxy_pct": 0.3, "min_conf": 75,
        },
        "telegram_chat_id": None,
        "timezone": "Asia/Dubai",
        "language": "en",
    },

    # ─────────────────────────────────────────────────────────────────────────
    "gold_desk": {
        "id":      "gold_desk",
        "name":    "Aurum Pro — Precious Metals Desk",
        "tagline": "Gold-focused macro intelligence for precious-metals traders",
        "branding": {
            "logo_text":     "AURUM PRO",
            "primary_color": "#d4af37",   # gold
            "accent_color":  "#fbbf24",   # amber-gold
            "primary_rgb":   "212, 175, 55",
            "accent_rgb":    "251, 191, 36",
        },
        "modules": {
            **{m: True for m in ALL_MODULES},
            "india_panels":   False,
            "earnings_panel": False,
            "fx_strip":       True,        # FX still useful for Gold (DXY-related)
            "ai_research":    True,
            "hni_ai":         False,       # gold is more focused
        },
        "assets": {
            # Gold-centric macro bar
            "macro_bar":         ["GOLD", "DXY", "US10Y", "VIX", "OIL", "BTC", "FG_US"],
            "fx_pairs":          ["EUR/USD", "USD/JPY", "USD/CHF"],   # gold-relevant pairs only
            "explainer_tracked": ["GOLD", "DXY", "OIL"],                # gold + drivers
            "chart_assets":      ["GOLD", "DXY", "OIL", "USDJPY"],
        },
        "alerts": {
            "vix_spike_pct": 15.0, "vix_abs_level": 25.0, "gold_pct": 0.4,   # tighter Gold trigger
            "yield_shock_pct": 0.3, "dxy_pct": 0.3, "min_conf": 70,
        },
        "telegram_chat_id": None,
        "timezone": "Europe/London",
        "language": "en",
    },

    # ─────────────────────────────────────────────────────────────────────────
    "prop_firm": {
        "id":      "prop_firm",
        "name":    "Apex Trader Prop — Risk-Managed Edge",
        "tagline": "All-asset macro + signal performance for proprietary trading desks",
        "branding": {
            "logo_text":     "APEX PROP",
            "primary_color": "#22d3ee",
            "accent_color":  "#a78bfa",   # purple — institutional/quant feel
            "primary_rgb":   "34, 211, 238",
            "accent_rgb":    "167, 139, 250",
        },
        "modules": {
            **{m: True for m in ALL_MODULES},
            "india_panels":   False,
            "morning_note":   False,
            "earnings_panel": True,
            "risk_tools":     True,    # CRITICAL for prop firms
            "ai_perf":        True,    # signal track record
            "signals":        True,
        },
        "assets": {
            "macro_bar":         ["GOLD", "DXY", "NASDAQ", "US10Y", "OIL", "BTC", "VIX", "FG_US"],
            "fx_pairs":          ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD", "USD/CHF"],
            "explainer_tracked": ["GOLD", "DXY", "EURUSD", "USDJPY", "NASDAQ", "OIL", "BTC"],
            "chart_assets":      ["GOLD", "DXY", "EURUSD", "USDJPY", "NASDAQ", "BTC", "OIL"],
        },
        "alerts": {
            "vix_spike_pct": 10.0, "vix_abs_level": 20.0, "gold_pct": 0.6,
            "yield_shock_pct": 0.4, "dxy_pct": 0.3, "min_conf": 75,
        },
        "telegram_chat_id": None,
        "timezone": "America/New_York",
        "language": "en",
    },

    # ─────────────────────────────────────────────────────────────────────────
    "crypto_macro": {
        "id":      "crypto_macro",
        "name":    "BlockMacro — Crypto Macro Desk",
        "tagline": "Macro context for crypto-native traders",
        "branding": {
            "logo_text":     "BLOCKMACRO",
            "primary_color": "#a78bfa",   # purple
            "accent_color":  "#22d3ee",
            "primary_rgb":   "167, 139, 250",
            "accent_rgb":    "34, 211, 238",
        },
        "modules": {
            **{m: True for m in ALL_MODULES},
            "india_panels":   False,
            "earnings_panel": False,
            "morning_note":   False,
            "fx_strip":       True,        # DXY-related FX still useful
        },
        "assets": {
            # BTC-centric: BTC, DXY, US10Y, Gold, NDX
            "macro_bar":         ["BTC", "DXY", "US10Y", "GOLD", "NASDAQ", "OIL", "FG_CRYPTO", "FG_US"],
            "fx_pairs":          ["EUR/USD", "USD/JPY"],   # minimal FX
            "explainer_tracked": ["BTC", "DXY", "GOLD", "NASDAQ"],
            "chart_assets":      ["BTC", "DXY", "GOLD", "NASDAQ", "USDJPY"],
        },
        "alerts": {
            "vix_spike_pct": 15.0, "vix_abs_level": 25.0, "gold_pct": 1.0,
            "yield_shock_pct": 0.5, "dxy_pct": 0.4, "min_conf": 75,
        },
        "telegram_chat_id": None,
        "timezone": "UTC",
        "language": "en",
    },
}


# ─── Active tenant resolution ────────────────────────────────────────────────

def get_tenant(tenant_id: str | None) -> dict:
    """Return tenant config. Falls back to 'default' if id is unknown."""
    if not tenant_id:
        return TENANT_PRESETS["default"]
    return TENANT_PRESETS.get(tenant_id, TENANT_PRESETS["default"])


def list_tenants() -> list:
    """Return id + name + tagline for each preset (for switcher UI)."""
    return [{
        "id":      t["id"],
        "name":    t["name"],
        "tagline": t["tagline"],
        "logo":    t["branding"]["logo_text"],
        "primary_color": t["branding"]["primary_color"],
    } for t in TENANT_PRESETS.values()]


# ─── Custom-tenant persistence (DB-backed override / extension) ───────────────

_DB_DIR  = os.path.join(os.path.dirname(__file__), "db")
os.makedirs(_DB_DIR, exist_ok=True)
_DB_PATH = os.path.join(_DB_DIR, "tenants.db")
_db_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(_DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _init_db():
    with _db_lock, _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS custom_tenants (
                id           TEXT PRIMARY KEY,
                config_json  TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            )
        """)
        c.commit()
_init_db()


def upsert_custom_tenant(tenant_id: str, config: dict) -> dict:
    """Save / update a custom tenant config (admin only).
    The config may extend or override a preset by setting 'extends': 'uae_forex'."""
    if not tenant_id or not isinstance(config, dict):
        return {"ok": False, "error": "invalid input"}
    base = TENANT_PRESETS.get(config.get("extends"), TENANT_PRESETS["default"])
    # Deep-merge config over base
    final = json.loads(json.dumps(base))
    for k, v in config.items():
        if isinstance(v, dict) and isinstance(final.get(k), dict):
            final[k].update(v)
        else:
            final[k] = v
    final["id"] = tenant_id
    now = datetime.now(IST).strftime("%d-%b-%Y %H:%M:%S IST")
    try:
        with _db_lock, _conn() as c:
            c.execute("""
                INSERT INTO custom_tenants (id, config_json, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET config_json=excluded.config_json, updated_at=excluded.updated_at
            """, (tenant_id, json.dumps(final), now, now))
            c.commit()
        # Hot-load into PRESETS so it takes effect immediately
        TENANT_PRESETS[tenant_id] = final
        return {"ok": True, "tenant": final}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def load_custom_tenants_into_memory():
    """Called at startup to register any DB-stored tenants."""
    try:
        with _db_lock, _conn() as c:
            rows = c.execute("SELECT id, config_json FROM custom_tenants").fetchall()
        for r in rows:
            try:
                TENANT_PRESETS[r["id"]] = json.loads(r["config_json"])
            except Exception:
                continue
    except Exception:
        pass

# Load any DB-stored custom tenants on import
load_custom_tenants_into_memory()
