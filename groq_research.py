"""
Groq-powered market research — analyzes your existing live news feed
per segment (GOLD/OIL/NIFTY etc.) and returns deep analysis, bullets,
sentiment, and key levels. Zero extra API cost — uses your free Groq key.
Results cached 15 minutes in SQLite.
"""
import os, re, json, time, sqlite3, threading, requests
from datetime import datetime, timezone, timedelta

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL   = "llama-3.1-8b-instant"
CACHE_TTL    = 900   # 15 minutes

IST = timezone(timedelta(hours=5, minutes=30))

DB_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db", "groq_research.db")
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


# ── Segment keyword filters ───────────────────────────────────
SEGMENT_KEYWORDS = {
    "GOLD":        ["gold","xauusd","xau","bullion","precious metal","safe haven"],
    "OIL":         ["oil","crude","brent","wti","opec","petroleum","energy prices","barrel"],
    "BTC":         ["bitcoin","btc","crypto","ethereum","eth","blockchain","digital asset","coinbase","binance"],
    "NIFTY":       ["nifty","sensex","india","bse","nse","sebi","rbi","rupee","inr","indian market","dalal"],
    "SPX":         ["s&p","spx","dow jones","djia","nasdaq","wall street","us stock","nyse","fed","powell","fomc"],
    "BONDS":       ["treasury","yield","bond","10-year","2-year","gilt","debt","fixed income","rate hike","rate cut","fed funds"],
    "FX":          ["dollar","dxy","usd","eur/usd","gbp","yen","jpy","currency","forex","fx","exchange rate","inr"],
    "CRYPTO":      ["bitcoin","ethereum","crypto","altcoin","defi","nft","web3","binance","coinbase","solana","xrp","blockchain"],
    "COMMODITIES": ["silver","copper","aluminum","wheat","corn","soy","natural gas","lng","iron ore","lithium","commodity"],
    "MACRO":       ["fed","fomc","powell","ecb","rbi","inflation","cpi","gdp","recession","rate","central bank","fiscal","tariff","trade war","sanctions"],
}

SEGMENT_LABELS = {
    "GOLD":        "Gold (XAUUSD)",
    "OIL":         "Crude Oil (WTI/Brent)",
    "BTC":         "Bitcoin & Crypto",
    "NIFTY":       "Nifty 50 / India Markets",
    "SPX":         "S&P 500 / US Equities",
    "BONDS":       "US Bonds / Treasury Yields",
    "FX":          "Forex — DXY / USD-INR",
    "CRYPTO":      "Crypto Markets",
    "COMMODITIES": "Commodities (Silver/Copper/Gas)",
    "MACRO":       "Global Macro / Central Banks",
}

SEGMENT_CONTEXT = {
    "GOLD":        "Gold (XAUUSD) prices, safe haven demand, central bank buying, dollar correlation",
    "OIL":         "Crude Oil (WTI/Brent) prices, OPEC decisions, US inventory data, geopolitical supply risk",
    "BTC":         "Bitcoin price, crypto market sentiment, ETF flows, on-chain activity, regulatory news",
    "NIFTY":       "Nifty 50 index, Indian stock market, FII/DII flows, RBI policy, INR, Budget/SEBI news",
    "SPX":         "S&P 500, US equities, Fed policy, earnings season, sector rotation, recession risk",
    "BONDS":       "US Treasury yields, Fed rate decisions, yield curve, inflation data, bond market flows",
    "FX":          "Dollar Index (DXY), EUR/USD, USD/INR, central bank policy divergence, risk sentiment",
    "CRYPTO":      "Bitcoin, Ethereum, altcoins, crypto market structure, ETF flows, regulatory developments",
    "COMMODITIES": "Silver, Copper, Natural Gas, Agricultural commodities, China demand, supply chains",
    "MACRO":       "Global macro themes, Fed/ECB/RBI policy, inflation, GDP growth, trade policy, geopolitics",
}


# ── News filter: extract segment-relevant headlines ───────────
def _filter_news(all_news, segment, max_items=20):
    """Filter news cache for segment-relevant items. Returns list of headline strings."""
    keywords = SEGMENT_KEYWORDS.get(segment, [])
    scored   = []

    for item in all_news:
        headline = ""
        score    = 0
        # Handle both (score, item) tuples and plain dicts
        if isinstance(item, (list, tuple)) and len(item) == 2:
            score, item = item
        if not isinstance(item, dict):
            continue
        headline = item.get("text", "") or item.get("headline", "")
        if not headline:
            continue
        txt = headline.lower()
        hits = sum(1 for kw in keywords if kw in txt)
        if hits > 0:
            scored.append((hits * 10 + score, headline, item.get("source",""), item.get("time","")))

    scored.sort(key=lambda x: -x[0])
    return scored[:max_items]


# ── Groq analysis prompt ──────────────────────────────────────
def _make_research_prompt(segment, news_items):
    context = SEGMENT_CONTEXT.get(segment, segment)
    if not news_items:
        return f"""You are a senior market analyst. Based on your general knowledge, provide a brief analysis of {context} RIGHT NOW.
Give:
1. Current market direction (bullish/bearish/neutral)
2. Key drivers to watch
3. Important price levels
4. Short-term outlook for traders

Be concise, use bullet points, trader-focused."""

    headlines = "\n".join(
        f"• [{src}] {headline} ({t})"
        for _, headline, src, t in news_items[:15]
    )
    return f"""You are a senior market analyst. Analyze these recent news headlines about {context}:

{headlines}

Based on these headlines, provide a comprehensive market analysis:
1. DIRECTION: What is the current market direction/sentiment? (Bullish/Bearish/Neutral)
2. KEY DRIVERS: Top 3 factors moving this market right now
3. PRICE IMPACT: Specific price levels, % moves, or targets mentioned
4. RISK FACTORS: What could reverse this move?
5. TRADER ACTION: What should traders watch in the next 24 hours?

Be concise. Use bullet points. Mention specific numbers when available. Trader-focused analysis only."""


def _call_groq_research(prompt):
    if not GROQ_API_KEY:
        return None
    try:
        resp = requests.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model":       GROQ_MODEL,
                "messages":    [
                    {"role": "system", "content": "You are a professional financial analyst. Be concise and trader-focused. Use bullet points."},
                    {"role": "user",   "content": prompt},
                ],
                "max_tokens":  700,
                "temperature": 0.3,
            },
            timeout=20,
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        elif resp.status_code == 429:
            time.sleep(3)
    except Exception:
        pass
    return None


# ── Parse Groq response ───────────────────────────────────────
def _parse_bullets(text):
    lines   = text.split("\n")
    bullets = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Remove markdown bullets/numbers
        clean = re.sub(r"^[-•*·]\s+", "", line)
        clean = re.sub(r"^\d+[\.\)]\s+", "", clean)
        clean = re.sub(r"^\*\*(.+?)\*\*:?\s*", r"\1: ", clean)  # bold headers
        clean = re.sub(r"\*\*(.+?)\*\*", r"\1", clean)           # inline bold
        if clean and len(clean) > 15:
            bullets.append(clean)
    return bullets[:8]

def _parse_sentiment(text):
    t    = text.lower()
    bull = sum(1 for w in ["bullish","buying","rally","surge","upside","gains","positive","strong demand","risk-on"] if w in t)
    bear = sum(1 for w in ["bearish","selling","drop","fall","decline","downside","weak","risk-off","pressure"] if w in t)
    if bull > bear + 1:  return "BULL"
    if bear > bull + 1:  return "BEAR"
    return "NEU"

def _parse_sources(news_items):
    """Return unique source names from filtered news."""
    seen = set()
    sources = []
    for _, _, src, _ in news_items:
        if src and src not in seen:
            seen.add(src)
            sources.append(src)
    return sources[:6]


# ── Public API ────────────────────────────────────────────────
def research_asset(asset, all_news=None):
    """
    Deep analysis for one asset segment using Groq + live news feed.
    all_news: the full news cache list (score, item) tuples.
    """
    asset = asset.upper()
    cache_key = f"asset_{asset}"
    cached = _cache_get(cache_key)
    if cached:
        cached["cached"] = True
        return cached

    if not GROQ_API_KEY:
        return {
            "asset":     asset,
            "label":     SEGMENT_LABELS.get(asset, asset),
            "content":   "Add GROQ_API_KEY to Railway environment variables to enable AI research.",
            "bullets":   ["Set GROQ_API_KEY in Railway → Variables tab (free at console.groq.com)"],
            "sentiment": "NEU",
            "sources":   [],
            "news_count": 0,
            "cached":    False,
            "timestamp": datetime.now(IST).strftime("%H:%M IST"),
            "no_key":    True,
        }

    # Filter relevant news
    filtered = _filter_news(all_news or [], asset)
    prompt   = _make_research_prompt(asset, filtered)
    content  = _call_groq_research(prompt)

    if not content:
        return {
            "asset": asset, "label": SEGMENT_LABELS.get(asset, asset),
            "error": "Groq API call failed — rate limited or timeout",
            "bullets": [], "sentiment": "NEU", "sources": [], "news_count": 0,
            "cached": False, "timestamp": datetime.now(IST).strftime("%H:%M IST"),
        }

    result = {
        "asset":      asset,
        "label":      SEGMENT_LABELS.get(asset, asset),
        "content":    content,
        "bullets":    _parse_bullets(content),
        "sentiment":  _parse_sentiment(content),
        "sources":    _parse_sources(filtered),
        "news_count": len(filtered),
        "cached":     False,
        "timestamp":  datetime.now(IST).strftime("%H:%M IST"),
    }
    _cache_set(cache_key, result)
    return result


def research_query(query, all_news=None):
    """Free-form market research query analyzed by Groq."""
    if not GROQ_API_KEY:
        return {
            "content": "Add GROQ_API_KEY to Railway environment variables.",
            "bullets": [], "sources": [], "no_key": True,
        }

    # Find relevant news for this query
    query_lower = query.lower()
    relevant = []
    for item in (all_news or []):
        if isinstance(item, (list, tuple)) and len(item) == 2:
            score, item = item
        if not isinstance(item, dict):
            continue
        headline = item.get("text", "")
        # Simple relevance: check if any query word appears in headline
        words = set(re.findall(r'\b\w{4,}\b', query_lower))
        if any(w in headline.lower() for w in words):
            relevant.append(headline)

    news_ctx = ""
    if relevant:
        news_ctx = "\n\nRecent relevant headlines:\n" + "\n".join(f"• {h}" for h in relevant[:12])

    prompt = (
        f"Market research question: {query}{news_ctx}\n\n"
        "Answer as a professional trader/analyst. Be specific with price levels and % moves. "
        "Use bullet points for key findings. Focus on actionable insights."
    )

    content = _call_groq_research(prompt)
    if not content:
        return {"error": "API call failed", "content": "", "bullets": [], "sources": []}

    return {
        "content":   content,
        "bullets":   _parse_bullets(content),
        "sentiment": _parse_sentiment(content),
        "sources":   [],
        "timestamp": datetime.now(IST).strftime("%H:%M IST"),
    }


def get_cache_status():
    status = {}
    try:
        with _db_lock:
            conn = _db()
            rows = conn.execute("SELECT key,ts FROM research").fetchall()
            conn.close()
        now = time.time()
        for key, ts in rows:
            asset = key.replace("asset_", "")
            age_min = round((now - ts) / 60, 1)
            status[asset] = {
                "age_min":    age_min,
                "fresh":      age_min < 15,
                "expires_in": max(0, round((CACHE_TTL - (now - ts)) / 60, 1)),
            }
    except: pass
    return status
