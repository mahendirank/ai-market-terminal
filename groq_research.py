"""
Groq-powered market research — analyzes your existing live news feed
per segment (GOLD/OIL/NIFTY etc.) and returns deep analysis, bullets,
sentiment, and key levels. Zero extra API cost — uses your free Groq key.
Results cached 15 minutes in SQLite.
"""
import os, re, json, time, sqlite3, threading, requests
from datetime import datetime, timezone, timedelta

GROQ_API_KEY    = os.environ.get("GROQ_API_KEY", "")
GROQ_URL        = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL      = "llama-3.1-8b-instant"
TAVILY_API_KEY  = os.environ.get("TAVILY_API_KEY") or os.environ.get("Tavily_API_KEY", "")
TAVILY_URL      = "https://api.tavily.com/search"
CACHE_TTL       = 900   # 15 minutes

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

TAVILY_QUERIES = {
    "GOLD":        "gold price today market news analysis",
    "OIL":         "crude oil WTI Brent price today news",
    "BTC":         "bitcoin price today crypto market news",
    "NIFTY":       "Nifty 50 India stock market news today",
    "SPX":         "S&P 500 US stock market news today",
    "BONDS":       "US treasury yield bond market news today",
    "FX":          "US dollar DXY currency forex news today",
    "CRYPTO":      "cryptocurrency bitcoin ethereum market news today",
    "COMMODITIES": "silver copper natural gas commodity prices today",
    "MACRO":       "global macro Fed interest rates economic news today",
}


# ── Tavily web search ─────────────────────────────────────────
def _fetch_tavily(segment, max_results=6):
    """
    Fetch live web search results from Tavily for a segment.
    Returns list of {title, url, content, score} dicts.
    Free tier: 1000 searches/month — safe with 15-min cache.
    """
    if not TAVILY_API_KEY:
        return []
    query = TAVILY_QUERIES.get(segment, f"{segment} market news today")
    try:
        resp = requests.post(
            TAVILY_URL,
            json={
                "api_key":              TAVILY_API_KEY,
                "query":                query,
                "search_depth":         "basic",   # faster, uses fewer credits
                "max_results":          max_results,
                "include_answer":       False,
                "include_raw_content":  False,
                "topic":                "finance",
            },
            timeout=12,
        )
        if resp.status_code == 200:
            data    = resp.json()
            results = data.get("results", [])
            return [
                {
                    "title":   r.get("title", ""),
                    "url":     r.get("url", ""),
                    "content": r.get("content", "")[:300],
                    "score":   round(r.get("score", 0), 2),
                }
                for r in results if r.get("title")
            ]
    except Exception:
        pass
    return []


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
def _make_research_prompt(segment, news_items, tavily_results=None):
    context  = SEGMENT_CONTEXT.get(segment, segment)
    sections = []

    if news_items:
        headlines = "\n".join(
            f"• [{src}] {headline} ({t})"
            for _, headline, src, t in news_items[:15]
        )
        sections.append(f"YOUR LIVE NEWS FEED ({len(news_items)} headlines):\n{headlines}")

    if tavily_results:
        web_items = "\n".join(
            f"• [{r['url'].split('/')[2] if r.get('url') else 'web'}] {r['title']} — {r['content'][:150]}"
            for r in tavily_results[:6]
        )
        sections.append(f"LIVE WEB SEARCH RESULTS (Tavily):\n{web_items}")

    if not sections:
        return (
            f"You are a senior market analyst. Based on your general knowledge, "
            f"provide a brief analysis of {context} RIGHT NOW.\n"
            "Give: (1) current direction, (2) key drivers, (3) price levels, (4) short-term outlook.\n"
            "Be concise, use bullet points, trader-focused."
        )

    combined = "\n\n".join(sections)
    source_note = " + Tavily live web search" if tavily_results else ""
    return f"""You are a senior market analyst. Analyze this data about {context}:

{combined}

Provide comprehensive market analysis:
1. DIRECTION: Current market direction/sentiment (Bullish/Bearish/Neutral)
2. KEY DRIVERS: Top 3 factors moving this market right now
3. PRICE IMPACT: Specific price levels, % moves, targets mentioned
4. RISK FACTORS: What could reverse this move?
5. TRADER ACTION: What to watch in next 24 hours

Be concise. Bullet points. Specific numbers when available. Trader-focused.
Data sources: live news feed{source_note}."""


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

    # Filter relevant news from internal feed
    filtered = _filter_news(all_news or [], asset)

    # Tavily live web search (runs if key is set, adds real-time web context)
    tavily  = _fetch_tavily(asset) if TAVILY_API_KEY else []

    prompt  = _make_research_prompt(asset, filtered, tavily_results=tavily)
    content = _call_groq_research(prompt)

    if not content:
        return {
            "asset": asset, "label": SEGMENT_LABELS.get(asset, asset),
            "error": "Groq API call failed — rate limited or timeout",
            "bullets": [], "sentiment": "NEU", "sources": [], "news_count": 0,
            "tavily_count": 0, "cached": False,
            "timestamp": datetime.now(IST).strftime("%H:%M IST"),
        }

    # Collect sources: internal feed names + Tavily domains
    feed_sources = _parse_sources(filtered)
    web_sources  = []
    web_urls     = []
    for r in tavily:
        try:
            domain = r["url"].split("/")[2].replace("www.", "")
            if domain not in web_sources:
                web_sources.append(domain)
                web_urls.append({"name": domain, "url": r["url"], "title": r["title"]})
        except Exception:
            pass

    result = {
        "asset":        asset,
        "label":        SEGMENT_LABELS.get(asset, asset),
        "content":      content,
        "bullets":      _parse_bullets(content),
        "sentiment":    _parse_sentiment(content),
        "sources":      feed_sources,
        "web_sources":  web_urls[:5],
        "news_count":   len(filtered),
        "tavily_count": len(tavily),
        "has_tavily":   bool(tavily),
        "cached":       False,
        "timestamp":    datetime.now(IST).strftime("%H:%M IST"),
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
        news_ctx = "\n\nRelevant headlines from your live feed:\n" + "\n".join(f"• {h}" for h in relevant[:12])

    # Tavily web search for this custom query
    tavily_ctx  = ""
    web_sources = []
    if TAVILY_API_KEY:
        try:
            resp = requests.post(
                TAVILY_URL,
                json={
                    "api_key":             TAVILY_API_KEY,
                    "query":               query,
                    "search_depth":        "basic",
                    "max_results":         5,
                    "include_answer":      False,
                    "include_raw_content": False,
                    "topic":               "finance",
                },
                timeout=12,
            )
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                if results:
                    tavily_ctx = "\n\nLIVE WEB SEARCH RESULTS (Tavily):\n" + "\n".join(
                        f"• [{r['url'].split('/')[2] if r.get('url') else 'web'}] {r.get('title','')} — {r.get('content','')[:200]}"
                        for r in results[:5]
                    )
                    for r in results:
                        try:
                            d = r["url"].split("/")[2].replace("www.","")
                            web_sources.append({"name": d, "url": r["url"], "title": r.get("title","")})
                        except Exception:
                            pass
        except Exception:
            pass

    prompt = (
        f"Market research question: {query}{news_ctx}{tavily_ctx}\n\n"
        "Answer as a professional trader/analyst. Be specific with price levels and % moves. "
        "Use bullet points for key findings. Focus on actionable insights."
    )

    content = _call_groq_research(prompt)
    if not content:
        return {"error": "API call failed", "content": "", "bullets": [], "web_sources": []}

    return {
        "content":     content,
        "bullets":     _parse_bullets(content),
        "sentiment":   _parse_sentiment(content),
        "web_sources": web_sources[:5],
        "has_tavily":  bool(web_sources),
        "timestamp":   datetime.now(IST).strftime("%H:%M IST"),
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
