"""
Perplexity AI — real-time market research using live web search.
Model: sonar-pro (searches the web, returns citations + analysis).
Each asset panel calls this with a specific market research prompt.
Results cached in SQLite for 15 minutes to control API costs.
"""
import os, re, json, time, sqlite3, threading, requests
from datetime import datetime, timezone, timedelta

PERPLEXITY_API_KEY = os.environ.get("PERPLEXITY_API_KEY", "")
PERPLEXITY_URL     = "https://api.perplexity.ai/chat/completions"
PERPLEXITY_MODEL   = "sonar-pro"       # live web search + citations
CACHE_TTL          = 900               # 15 minutes
MAX_TOKENS         = 800

IST = timezone(timedelta(hours=5, minutes=30))

DB_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db", "perplexity_cache.db")
_db_lock = threading.Lock()


# ── SQLite cache ──────────────────────────────────────────────
def _db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS research (
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
            row  = conn.execute("SELECT data,ts FROM research WHERE key=?", (key,)).fetchone()
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
                "INSERT OR REPLACE INTO research(key,data,ts) VALUES(?,?,?)",
                (key, json.dumps(data), time.time())
            )
            conn.commit()
            conn.close()
    except: pass


# ── Prompt templates per asset ────────────────────────────────
ASSET_PROMPTS = {
    "GOLD": (
        "What is driving Gold (XAUUSD) prices RIGHT NOW today? "
        "Give: (1) current price direction, (2) top 3 news catalysts, "
        "(3) key support/resistance levels, (4) trader sentiment. "
        "Focus on actionable insights. Be concise."
    ),
    "OIL": (
        "What is moving Crude Oil (WTI/Brent) prices TODAY? "
        "Give: (1) current trend, (2) top 3 drivers (OPEC/inventory/geopolitics), "
        "(3) key price levels, (4) short-term outlook for traders."
    ),
    "BTC": (
        "What is happening with Bitcoin (BTC) RIGHT NOW? "
        "Give: (1) price direction and momentum, (2) top 3 news catalysts today, "
        "(3) on-chain or sentiment signals, (4) key levels to watch."
    ),
    "NIFTY": (
        "What is driving Nifty 50 and Indian stock markets TODAY? "
        "Give: (1) market direction, (2) top FII/DII flows, (3) key sectoral moves, "
        "(4) RBI/macro factors, (5) levels to watch. India-focused analysis."
    ),
    "SPX": (
        "What is moving S&P 500 and US markets TODAY? "
        "Give: (1) index direction, (2) top 3 macro/earnings drivers, "
        "(3) sector rotation happening now, (4) key levels and sentiment."
    ),
    "BONDS": (
        "What is happening with US Treasury yields and bonds TODAY? "
        "Give: (1) 10Y/2Y yield direction, (2) key drivers (Fed/inflation/data), "
        "(3) yield curve signal, (4) impact on equities and gold."
    ),
    "FX": (
        "What is driving major currency pairs (DXY, EUR/USD, USD/INR) TODAY? "
        "Give: (1) Dollar index trend, (2) top macro drivers, "
        "(3) key levels for DXY and USD/INR, (4) central bank signals."
    ),
    "CRYPTO": (
        "What is happening across the crypto market TODAY? "
        "Give: (1) Bitcoin and Ethereum direction, (2) top altcoin moves, "
        "(3) key catalysts (ETF/regulation/on-chain), (4) risk sentiment."
    ),
    "COMMODITIES": (
        "What is happening across commodity markets (Silver, Copper, Natural Gas, Wheat) TODAY? "
        "Give: (1) top movers and why, (2) macro drivers, "
        "(3) China demand signals, (4) key levels."
    ),
    "MACRO": (
        "What are the top global macro themes driving markets RIGHT NOW TODAY? "
        "Give: (1) Fed/central bank signals, (2) inflation/growth data, "
        "(3) geopolitical risks, (4) risk-on vs risk-off signal."
    ),
}

SEGMENT_LABELS = {
    "GOLD":       "Gold (XAUUSD)",
    "OIL":        "Crude Oil",
    "BTC":        "Bitcoin",
    "NIFTY":      "Nifty 50 / India",
    "SPX":        "S&P 500 / US Equities",
    "BONDS":      "US Bonds / Yields",
    "FX":         "Forex / DXY / INR",
    "CRYPTO":     "Crypto Markets",
    "COMMODITIES":"Commodities",
    "MACRO":      "Global Macro",
}


# ── Core API call ─────────────────────────────────────────────
def _call_perplexity(prompt, custom=False):
    if not PERPLEXITY_API_KEY:
        return None
    system = (
        "You are a professional financial analyst and market researcher. "
        "Be precise, concise, and trader-focused. Use bullet points. "
        "Always mention specific price levels and % moves when available. "
        "Today's date context: always refer to the most current data available."
    )
    try:
        resp = requests.post(
            PERPLEXITY_URL,
            headers={
                "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model":      PERPLEXITY_MODEL,
                "messages":   [
                    {"role": "system",  "content": system},
                    {"role": "user",    "content": prompt},
                ],
                "max_tokens":          MAX_TOKENS,
                "temperature":         0.2,
                "search_recency_filter": "day",   # only today's news
                "return_citations":    True,
            },
            timeout=20,
        )
        if resp.status_code == 200:
            data    = resp.json()
            content = data["choices"][0]["message"]["content"]
            # Citations come as list of URL strings
            citations = data.get("citations", [])
            return {"content": content, "citations": citations[:5]}
        elif resp.status_code == 429:
            time.sleep(3)
    except Exception:
        pass
    return None


def _parse_sentiment(text):
    """Quick sentiment from response text."""
    t = text.lower()
    bull = sum(1 for w in ["bullish","buying","rally","surge","up","gains","strong","positive"] if w in t)
    bear = sum(1 for w in ["bearish","selling","drop","fall","decline","weak","negative","risk-off"] if w in t)
    if bull > bear + 1:   return "BULL"
    if bear > bull + 1:   return "BEAR"
    return "NEU"


def _parse_bullets(text):
    """Extract bullet points from markdown text."""
    lines  = text.split("\n")
    bullets = []
    for line in lines:
        line = line.strip()
        if line.startswith(("- ", "• ", "* ", "· ")):
            bullets.append(line[2:].strip())
        elif re.match(r"^\d+[\.\)]\s", line):
            bullets.append(re.sub(r"^\d+[\.\)]\s+", "", line).strip())
        elif line and not line.startswith("#") and len(line) > 20:
            # Non-bullet paragraph — include as-is
            bullets.append(line)
    return [b for b in bullets if b][:8]


# ── Public functions ──────────────────────────────────────────
def research_asset(asset):
    """
    Research a specific asset. Returns cached result if fresh.
    asset: one of ASSET_PROMPTS keys
    """
    cache_key = f"asset_{asset}"
    cached = _cache_get(cache_key)
    if cached:
        cached["cached"] = True
        return cached

    prompt = ASSET_PROMPTS.get(asset)
    if not prompt:
        return {"error": f"Unknown asset: {asset}"}

    if not PERPLEXITY_API_KEY:
        return {
            "asset":     asset,
            "label":     SEGMENT_LABELS.get(asset, asset),
            "content":   "Add PERPLEXITY_API_KEY to Railway environment variables to enable live research.",
            "bullets":   ["Set PERPLEXITY_API_KEY in Railway → Variables tab"],
            "sentiment": "NEU",
            "citations": [],
            "cached":    False,
            "timestamp": datetime.now(IST).strftime("%H:%M IST"),
            "no_key":    True,
        }

    raw = _call_perplexity(prompt)
    if not raw:
        return {"error": "Perplexity API call failed", "asset": asset}

    result = {
        "asset":     asset,
        "label":     SEGMENT_LABELS.get(asset, asset),
        "content":   raw["content"],
        "bullets":   _parse_bullets(raw["content"]),
        "sentiment": _parse_sentiment(raw["content"]),
        "citations": raw["citations"],
        "cached":    False,
        "timestamp": datetime.now(IST).strftime("%H:%M IST"),
    }
    _cache_set(cache_key, result)
    return result


def research_all():
    """Research all 10 asset panels. Runs sequentially with small gaps."""
    assets  = list(ASSET_PROMPTS.keys())
    results = {}
    for asset in assets:
        results[asset] = research_asset(asset)
        time.sleep(0.3)   # gentle rate limit
    return results


def research_query(query):
    """
    Free-form market research query.
    e.g. "Why is Nifty falling today?" or "Impact of Fed decision on gold"
    NOT cached (custom queries are unique).
    """
    if not PERPLEXITY_API_KEY:
        return {
            "content": "Add PERPLEXITY_API_KEY to Railway environment variables.",
            "bullets": [],
            "citations": [],
            "no_key": True,
        }

    prompt = (
        f"{query}\n\n"
        "Answer as a professional trader/analyst. Be specific with price levels and % moves. "
        "Use bullet points for key findings. Cite sources."
    )
    raw = _call_perplexity(prompt, custom=True)
    if not raw:
        return {"error": "API call failed", "content": "", "bullets": [], "citations": []}

    return {
        "content":   raw["content"],
        "bullets":   _parse_bullets(raw["content"]),
        "sentiment": _parse_sentiment(raw["content"]),
        "citations": raw["citations"],
        "timestamp": datetime.now(IST).strftime("%H:%M IST"),
    }


def get_cache_status():
    """Returns age of each cached panel (for UI freshness indicator)."""
    status = {}
    try:
        with _db_lock:
            conn = _db()
            rows = conn.execute("SELECT key, ts FROM research").fetchall()
            conn.close()
        now = time.time()
        for key, ts in rows:
            asset = key.replace("asset_", "")
            age_min = round((now - ts) / 60, 1)
            status[asset] = {
                "age_min": age_min,
                "fresh": age_min < 15,
                "expires_in": max(0, round((CACHE_TTL - (now - ts)) / 60, 1)),
            }
    except: pass
    return status
