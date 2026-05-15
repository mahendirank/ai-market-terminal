"""
AI Intelligence Layer — Groq (cloud, free) + Ollama (local fallback)
Enriches top news with: summary, sentiment, impact score, assets, why_matters.
Results cached in SQLite for 30 minutes to respect rate limits.
"""
import os, re, json, time, sqlite3, threading, requests
from datetime import datetime, timezone

GROQ_API_KEY  = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL    = "llama-3.3-70b-versatile"   # upgraded from 8b-instant for nuanced news scoring
GROQ_URL      = "https://api.groq.com/openai/v1/chat/completions"
OLLAMA_URL    = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL  = "llama3.2:latest"
CACHE_TTL     = 1800   # 30 min
MAX_BATCH     = 8      # news items per Groq call (saves rate limit)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db", "ai_cache.db")
_db_lock = threading.Lock()


# ── SQLite cache ──────────────────────────────────────────────
def _db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS ai_news (
        key  TEXT PRIMARY KEY,
        data TEXT NOT NULL,
        ts   REAL NOT NULL
    )""")
    conn.commit()
    return conn

def _cache_get(key):
    try:
        with _db_lock:
            conn = _db()
            row = conn.execute(
                "SELECT data, ts FROM ai_news WHERE key=?", (key,)
            ).fetchone()
            conn.close()
        if row and (time.time() - row[1]) < CACHE_TTL:
            return json.loads(row[0])
    except: pass
    return None

def _cache_set(key, data):
    try:
        with _db_lock:
            conn = _db()
            conn.execute(
                "INSERT OR REPLACE INTO ai_news(key,data,ts) VALUES(?,?,?)",
                (key, json.dumps(data), time.time())
            )
            conn.commit()
            conn.close()
    except: pass


# ── Routed AI call via 3-layer prompt_builder ─────────────────────────
# Persona (L1), state-not-applicable, schema (L3 = SCHEMA_NEWS_ENRICH) all
# composed by prompt_builder. No inline persona text — single source of truth.
def _format_items_block(news_items: list) -> str:
    """The only news-enrichment-specific content: the numbered headline list."""
    items_txt = "\n".join(
        f"{i+1}. [{item.get('source','')}] {item.get('text','')}"
        for i, item in enumerate(news_items)
    )
    return f"Analyze these {len(news_items)} financial news items for traders:\n\n{items_txt}"


def _call_router(news_items):
    try:
        from ai_router import chat
        from prompt_builder import build_messages
    except Exception as e:
        print(f"[ai_layer] composer import failed ({e}) — keyword fallback only", flush=True)
        return []
    messages = build_messages(
        task="news_enrich",
        snap=None,                          # batched per-item, no market state needed
        extra_context=_format_items_block(news_items),
        include_few_shots=False,
    )
    result = chat(
        task="news_enrich",
        messages=messages,
        temperature=0.1,
        max_tokens=600,
        timeout=15,
        extra={"response_format": {"type": "json_object"}},
    )
    if not result.ok or not result.content:
        return []
    raw = result.content
    try:
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        obj = json.loads(raw)
        for v in obj.values():
            if isinstance(v, list):
                return v
    except Exception:
        pass
    return []


# Legacy alias — preserved for any code that still imports _make_prompt or _call_groq
def _make_prompt(news_items):
    """Deprecated: use _format_items_block + prompt_builder.build_messages."""
    return _format_items_block(news_items)


# Legacy alias — preserved for any code that still imports _call_groq directly
_call_groq = _call_router


# ── Ollama fallback (local only) ──────────────────────────────
def _call_ollama(news_items):
    try:
        prompt = _make_prompt(news_items)
        resp = requests.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
                  "options": {"temperature": 0.1}},
            timeout=120
        )
        if resp.status_code == 200:
            raw = resp.json().get("response", "")
            m = re.search(r'\[.*\]', raw, re.DOTALL)
            if m:
                return json.loads(m.group(0))
    except: pass
    return []


# ── Keyword fallback (no API needed) ─────────────────────────
_BULL_WORDS = {"cut","easing","stimulus","rally","surge","gains","beats","beat",
               "strong","growth","hiring","rise","rises","rose","soars","bull"}
_BEAR_WORDS = {"hike","tightening","recession","crash","drop","falls","fell",
               "miss","weak","layoffs","decline","slump","fear","sell","bear",
               "warns","warning","tariff","sanctions","war","attack"}
_ASSET_MAP  = {
    "gold":"GOLD","xauusd":"GOLD","silver":"SILVER",
    "oil":"OIL","crude":"OIL","brent":"OIL","wti":"OIL",
    "bitcoin":"BTC","crypto":"BTC","btc":"BTC","ethereum":"ETH",
    "nasdaq":"NDX","s&p":"SPX","spx":"SPX","dow":"DJIA","us30":"DJIA",
    "nifty":"NIFTY","sensex":"SENSEX","india":"NIFTY",
    "dollar":"DXY","dxy":"DXY","usd":"DXY",
    "fed":"FED","fomc":"FED","powell":"FED","rate":"RATES",
    "bond":"BONDS","yield":"BONDS","treasury":"BONDS",
    "rupee":"INR","inr":"INR",
}
_HIGH_WORDS = {"fed","fomc","cpi","gdp","nfp","war","crisis","collapse",
               "recession","hike","cut","powell","nuclear","sanctions"}

def _keyword_enrich(item):
    text  = item.get("text", "").lower()
    words = set(re.findall(r'\b\w+\b', text))
    bull  = len(words & _BULL_WORDS)
    bear  = len(words & _BEAR_WORDS)
    if bull > bear:    sentiment = "BULL"
    elif bear > bull:  sentiment = "BEAR"
    else:              sentiment = "NEU"
    impact = 5
    if words & _HIGH_WORDS: impact = 8
    assets = list({v for k, v in _ASSET_MAP.items() if k in text})[:4]
    return {
        "summary": item["text"][:120],
        "sentiment": sentiment,
        "impact": impact,
        "assets": assets,
        "why": "",
        "source": "keywords"
    }


# ── Public API ────────────────────────────────────────────────
def enrich_news(news_items, max_items=40):
    """
    Enrich top news items with AI analysis.
    Returns list of dicts with original fields + ai_* fields added.
    Uses cache, batches API calls, falls back gracefully.
    """
    results = []
    to_fetch = []   # items not in cache

    for item in news_items[:max_items]:
        key = re.sub(r'\s+', ' ', item.get("text",""))[:80].lower()
        cached = _cache_get(key)
        if cached:
            results.append({**item, **cached, "ai_source": "cache"})
        else:
            to_fetch.append((key, item))

    if not to_fetch:
        return results

    # Batch API calls
    for i in range(0, len(to_fetch), MAX_BATCH):
        batch_keys  = [x[0] for x in to_fetch[i:i+MAX_BATCH]]
        batch_items = [x[1] for x in to_fetch[i:i+MAX_BATCH]]

        ai_results = []
        if GROQ_API_KEY:
            ai_results = _call_groq(batch_items)
        if not ai_results:
            try:
                ai_results = _call_ollama(batch_items)
            except: pass

        for j, item in enumerate(batch_items):
            # Find matching AI result by index
            ai = next((r for r in ai_results if r.get("i") == j+1), None)
            if ai:
                enriched = {
                    "summary":   ai.get("summary", item["text"][:120]),
                    "sentiment": ai.get("sentiment", "NEU"),
                    "impact":    int(ai.get("impact", 5)),
                    "assets":    ai.get("assets", []),
                    "why":       ai.get("why", ""),
                    "ai_source": "groq" if GROQ_API_KEY else "ollama"
                }
            else:
                enriched = {**_keyword_enrich(item), "ai_source": "keywords"}

            _cache_set(batch_keys[j], enriched)
            results.append({**item, **enriched})

        if i + MAX_BATCH < len(to_fetch):
            time.sleep(0.5)   # respect Groq rate limit

    return results


def get_market_sentiment(enriched_news):
    """
    Aggregate sentiment from enriched news into overall market bias.
    Returns dict with bias, confidence, asset breakdown.
    """
    if not enriched_news:
        return {"bias": "NEU", "confidence": 50, "bull_pct": 50, "bear_pct": 50, "assets": {}}

    bull = sum(n.get("impact",5) for n in enriched_news if n.get("sentiment")=="BULL")
    bear = sum(n.get("impact",5) for n in enriched_news if n.get("sentiment")=="BEAR")
    neu  = sum(n.get("impact",5) for n in enriched_news if n.get("sentiment")=="NEU")
    total = bull + bear + neu or 1

    bull_pct = round(bull / total * 100)
    bear_pct = round(bear / total * 100)

    if bull_pct > 55:   bias = "BULL"
    elif bear_pct > 55: bias = "BEAR"
    else:               bias = "NEU"

    confidence = max(bull_pct, bear_pct)

    # Per-asset sentiment
    asset_scores = {}
    for n in enriched_news:
        s = 1 if n.get("sentiment")=="BULL" else (-1 if n.get("sentiment")=="BEAR" else 0)
        w = n.get("impact", 5)
        for asset in n.get("assets", []):
            asset_scores.setdefault(asset, {"bull":0,"bear":0})
            if s > 0: asset_scores[asset]["bull"] += w
            elif s < 0: asset_scores[asset]["bear"] += w

    asset_bias = {}
    for asset, scores in asset_scores.items():
        b, r = scores["bull"], scores["bear"]
        if b > r:   asset_bias[asset] = "BULL"
        elif r > b: asset_bias[asset] = "BEAR"
        else:       asset_bias[asset] = "NEU"

    return {
        "bias":       bias,
        "confidence": confidence,
        "bull_pct":   bull_pct,
        "bear_pct":   bear_pct,
        "assets":     asset_bias,
    }
