import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import re
import time
import threading
import feedparser
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor
from telegram_news import get_telegram_news

FEED_TIMEOUT  = 7
IST           = timezone(timedelta(hours=5, minutes=30))
MAX_AGE_HOURS = 8
MAX_ITEMS_PER_SOURCE = 20
MAX_TOTAL     = 800

# Per-source MAX_AGE override. Primary-source feeds (Treasury, central banks)
# publish a few times per week, not hourly — an 8h global cutoff filters out
# yesterday's auction announcement even though it's still market-relevant.
# 48h gives these slow-cadence sources room to surface, while the fast
# newswires (Reuters, MarketWatch, CNBC) stay at the global 8h.
SLOW_CADENCE_MAX_AGE_HOURS = 48
SLOW_CADENCE_SOURCES: set = {
    "TreasuryDirect Auctions", "TreasuryDirect Results",
    "FRB San Francisco", "ECB Press",
    "Bank of England", "BoE Publications", "RBA Media",
    # Fed Reserve was already in the list — same cadence
    "Fed Reserve", "IMF News",
    # Blogs / trade press posting ~daily, not hourly — 8h cutoff leaves them empty
    "Calculated Risk", "SemiEngineering",
    # Market-hours wires: quiet on weekends, 8h cutoff empties them Sat/Sun
    "Kobeissi Letter", "CN Wire",
    # Monthly-release event wires: only fire around data dates, need wide window
    "SGE Withdrawals", "Korea Chip Exports", "TSMC Revenue", "SIA Billings",
    "Mine Supply Wire",
}

SOURCE_CATEGORY = {
    # Markets
    "Reuters Top":      "MARKETS",
    "Reuters Finance":  "MARKETS",
    "BBC Business":     "MARKETS",
    "MarketWatch":      "MARKETS",
    "CNBC Markets":     "MARKETS",
    "Yahoo Finance":    "MARKETS",
    "Benzinga":         "MARKETS",
    "Motley Fool":      "MARKETS",
    "Seeking Alpha":    "MARKETS",
    "SA Market":        "MARKETS",
    "SA Analysis":      "MARKETS",
    "SA Earnings":      "EARNINGS",
    "Bloomberg Mkts":   "MARKETS",
    "WSJ Mkt":          "MARKETS",
    "Barron's":         "MARKETS",
    # FX
    "ForexLive":        "FX",
    "DailyFX":          "FX",
    "FXStreet":         "FX",
    # Geopolitics
    "BBC World":        "GEOPOLITICS",
    "PBS NewsHour":     "GEOPOLITICS",
    "Al Jazeera":       "GEOPOLITICS",
    "AP News":          "GEOPOLITICS",
    "Foreign Policy":   "GEOPOLITICS",
    "Defense News":     "GEOPOLITICS",
    "Sky News World":   "GEOPOLITICS",
    # Bonds / Macro
    "FT Markets":       "BONDS",
    "ZeroHedge":        "MACRO",
    "BondBuyer":        "BONDS",
    "Calculated Risk":  "MACRO",
    "Mish Talk":        "MACRO",
    "Fed Reserve":      "MACRO",
    "IMF News":         "MACRO",
    "Econbrowser":      "MACRO",
    "Project Syndicate":"MACRO",
    # HNI / Institutional
    "FinancialJuice":   "HNI",
    "WalterBloomberg":  "HNI",
    "DreamCatcher":     "HNI",
    "Unusual Whales":   "HNI",
    "MarketCurrents":   "HNI",
    "ZeroHedgeTG":      "HNI",
    # Commodities
    "Kitco News":       "COMMODITIES",
    "Hellenic Ship":    "COMMODITIES",
    "OilPrice.com":     "COMMODITIES",
    "Mining.com":       "COMMODITIES",
    "GoldSeek":         "COMMODITIES",
    "Natural Gas Intel":"COMMODITIES",
    "Rigzone":          "COMMODITIES",
    "GoldTelegraph":    "COMMODITIES",
    # Tech / Semiconductors (topical breaking — captures supply-chain stories like
    # the China DRAM/NAND glut that our macro/forex sources don't cover)
    "Memory & Chips":   "TECH",
    "Semis Wire":       "TECH",
    "Tech Breaking":    "TECH",
    # India
    "Economic Times":   "INDIA",
    "ET Stocks":        "INDIA",
    "ET Global":        "INDIA",
    "Livemint":         "INDIA",
    "Mint Companies":   "INDIA",
    "Mint Economy":     "INDIA",
    "MoneyControl":     "INDIA",
    "MC Tech":          "INDIA",
    "Business Standard":"INDIA",
    "NDTV Profit":      "INDIA",
    "Hindu Business":   "INDIA",
    "The Hindu Biz":    "INDIA",
    "Finshots":         "INDIA",
    # Tech / AI / Chips
    "The Register":     "TECH",
    "Ars Technica":     "TECH",
    "SemiWiki":         "TECH",
    "SemiEngineering":  "TECH",
    "EE Times":         "TECH",
    "TechCrunch":       "TECH",
    "VentureBeat AI":   "TECH",
    "MIT Tech Rev":     "TECH",
    "Tom's Hardware":   "TECH",
    # Crypto
    "CoinDesk":         "CRYPTO",
    # Global / Asia
    "Nikkei Asia":      "GLOBAL",
    "Globe Mail":       "GLOBAL",
    "Asia Times":       "GLOBAL",
    "CNA Business":     "GLOBAL",
    "SCMP Markets":     "GLOBAL",
    "SCMP Economy":     "GLOBAL",
    "Yicai Global":     "GLOBAL",
    "CN Wire":          "GLOBAL",
    "China Macro":      "GLOBAL",
    "Japan Macro":      "GLOBAL",
    "Politico Europe":  "GEOPOLITICS",
    "Kobeissi Letter":  "MACRO",
    "SGE Withdrawals":  "COMMODITIES",
    "Korea Chip Exports": "TECH",
    "TSMC Revenue":     "TECH",
    "SIA Billings":     "TECH",
    "Mine Supply Wire": "COMMODITIES",
    # India fast
    "NDTV Business":    "INDIA",
    "Mint Opinion":     "INDIA",
    "Hindu Economy":    "INDIA",
    # Extra Markets
    "TheStreet":        "MARKETS",
    "Investing News":   "MARKETS",
    "Investing Stocks": "MARKETS",
    "FT Home":          "MARKETS",
    # Earnings wire
    "Globe Newswire":   "EARNINGS",
    # Primary-source feeds added 2026-05-27
    "TreasuryDirect Auctions": "BONDS",
    "TreasuryDirect Results":  "BONDS",
    "SEC EDGAR Latest":        "EARNINGS",
    "FRB San Francisco":       "MACRO",
    "ECB Press":               "MACRO",
    "Bank of England":         "MACRO",
    "BoE Publications":        "MACRO",
    "RBA Media":               "MACRO",
}

RSS_SOURCES = {
    # ── Core Markets ─────────────────────────────────────────
    # feeds.reuters.com was discontinued (returns empty) — route via Google News.
    "Reuters Top":      "https://news.google.com/rss/search?q=site:reuters.com+when:1d&hl=en-US&gl=US&ceid=US:en",
    "Reuters Finance":  "https://news.google.com/rss/search?q=site:reuters.com+markets+OR+business+when:1d&hl=en-US&gl=US&ceid=US:en",
    "BBC Business":     "https://feeds.bbci.co.uk/news/business/rss.xml",
    "MarketWatch":      "https://feeds.marketwatch.com/marketwatch/topstories/",
    "CNBC Markets":     "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "Yahoo Finance":    "https://finance.yahoo.com/news/rssindex",
    "Benzinga":         "https://www.benzinga.com/feed",
    "Bloomberg Mkts":   "https://feeds.bloomberg.com/markets/news.rss",
    # feeds.a.dj.com stopped updating (items frozen ~2025) — route via Google News.
    "WSJ Mkt":          "https://news.google.com/rss/search?q=site:wsj.com+(markets+OR+stocks+OR+economy)+when:1d&hl=en-US&gl=US&ceid=US:en",
    "Motley Fool":      "https://www.fool.com/feeds/index.aspx",
    # valuewalk.com went dormant (zero items even over 30d) — replaced by Barron's via Google News.
    "Barron's":         "https://news.google.com/rss/search?q=site:barrons.com+when:1d&hl=en-US&gl=US&ceid=US:en",

    # ── Earnings-specific ────────────────────────────────────
    "SA Earnings":      "https://seekingalpha.com/tag/earnings/feed.xml",
    "SA Market":        "https://seekingalpha.com/market-outlook/rss.xml",
    "SA Analysis":      "https://seekingalpha.com/stock-ideas/rss.xml",

    # ── FX / Currencies ─────────────────────────────────────
    "ForexLive":        "https://www.forexlive.com/feed/news",
    "FXStreet":         "https://www.fxstreet.com/rss/news",
    # dailyfx.com feed is defunct — generic FX via Google News
    "DailyFX":          "https://news.google.com/rss/search?q=(forex+OR+%22currency+markets%22+OR+%22dollar+index%22)+when:1d&hl=en-US&gl=US&ceid=US:en",

    # ── Geopolitics / War ───────────────────────────────────
    "BBC World":        "https://feeds.bbci.co.uk/news/world/rss.xml",
    "Sky News World":   "https://feeds.skynews.com/feeds/rss/world.xml",
    "Al Jazeera":       "https://www.aljazeera.com/xml/rss/all.xml",
    "AP News":          "https://news.google.com/rss/search?q=site:apnews.com+(markets+OR+economy+OR+business)+when:1d&hl=en-US&gl=US&ceid=US:en",
    "PBS NewsHour":     "https://www.pbs.org/newshour/feeds/rss/world",
    "Foreign Policy":   "https://foreignpolicy.com/feed/",
    "Defense News":     "https://www.defensenews.com/arc/outboundfeeds/rss/?outputType=xml",

    # ── Bonds / Rates / Macro ───────────────────────────────
    "FT Markets":       "https://www.ft.com/rss/home/uk",
    "ZeroHedge":        "https://nitter.net/zerohedge/rss",
    "BondBuyer":        "https://news.google.com/rss/search?q=site:bondbuyer.com+when:3d&hl=en-US&gl=US&ceid=US:en",
    # calculatedriskblog.com feed abandoned — the blog moved to Substack.
    "Calculated Risk":  "https://calculatedrisk.substack.com/feed",
    "Mish Talk":        "https://mishtalk.com/feed",
    "Fed Reserve":      "https://www.federalreserve.gov/feeds/press_all.xml",
    "IMF News":         "https://news.google.com/rss/search?q=site:imf.org+when:7d&hl=en-US&gl=US&ceid=US:en",
    "Econbrowser":      "https://econbrowser.com/feed",
    "Project Syndicate":"https://www.project-syndicate.org/rss",

    # ── HNI / Institutional ─────────────────────────────────
    "FinancialJuice":   "https://nitter.net/financialjuice/rss",
    # @WalterBloomberg handle is stale on nitter (~3y-old items); the live
    # account is @DeItaone — same wire, keep the source name downstream relies on.
    "WalterBloomberg":  "https://nitter.net/DeItaone/rss",
    "Unusual Whales":   "https://nitter.net/unusual_whales/rss",

    # ── Tech / Semiconductors (Google News topical — captures supply-chain
    #    breaking like the China DRAM/NAND glut that macro/forex feeds miss) ──
    "Memory & Chips":   "https://news.google.com/rss/search?q=(DRAM+OR+NAND+OR+%22memory+chips%22+OR+CXMT+OR+YMTC+OR+Micron)+(glut+OR+oversupply+OR+prices+OR+shortage)+when:2d&hl=en-US&gl=US&ceid=US:en",
    "Semis Wire":       "https://news.google.com/rss/search?q=(semiconductor+OR+chipmaker+OR+foundry+OR+TSMC+OR+Nvidia)+when:1d&hl=en-US&gl=US&ceid=US:en",
    "Tech Breaking":    "https://news.google.com/rss/search?q=(site:reuters.com+OR+site:bloomberg.com+OR+site:cnbc.com)+technology+when:1d&hl=en-US&gl=US&ceid=US:en",

    # ── Commodities ─────────────────────────────────────────
    "Kitco News":       "https://news.google.com/rss/search?q=site:kitco.com+gold+silver&hl=en-US&gl=US&ceid=US:en",
    "Hellenic Ship":    "https://www.hellenicshippingnews.com/feed/",
    "OilPrice.com":     "https://oilprice.com/rss/main",
    "Rigzone":          "https://www.rigzone.com/news/rss/rigzone_latest.aspx",
    "Mining.com":       "https://www.mining.com/feed/",
    "Natural Gas Intel":"https://naturalgasintel.com/feed/",
    # goldseek.com is low-volume (recent items age out) — fresh gold/silver via Google News
    "GoldSeek":         "https://news.google.com/rss/search?q=(gold+OR+silver)+(price+OR+rally+OR+forecast+OR+outlook)+when:1d&hl=en-US&gl=US&ceid=US:en",

    # ── India — core ────────────────────────────────────────
    "Economic Times":   "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "ET Stocks":        "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    "ET Global":        "https://economictimes.indiatimes.com/news/international/rssfeeds/1715249553.cms",
    "Livemint":         "https://www.livemint.com/rss/markets",
    "Mint Companies":   "https://www.livemint.com/rss/companies",
    "Mint Economy":     "https://www.livemint.com/rss/economy",
    "MoneyControl":     "https://news.google.com/rss/search?q=site:moneycontrol.com+(markets+OR+stocks)+when:1d&hl=en-US&gl=US&ceid=US:en",
    "MC Tech":          "https://news.google.com/rss/search?q=site:moneycontrol.com+technology+when:2d&hl=en-US&gl=US&ceid=US:en",
    "Business Standard":"https://news.google.com/rss/search?q=site:business-standard.com+(markets+OR+economy)+when:1d&hl=en-US&gl=US&ceid=US:en",
    "NDTV Profit":      "https://news.google.com/rss/search?q=site:ndtvprofit.com+when:1d&hl=en-US&gl=US&ceid=US:en",
    "Hindu Business":   "https://www.thehindubusinessline.com/markets/feeder/default.rss",
    "The Hindu Biz":    "https://www.thehindu.com/business/feeder/default.rss",
    "Finshots":         "https://finshots.in/feed",

    # ── Tech / AI / Semiconductors ───────────────────────────
    "The Register":     "https://www.theregister.com/headlines.atom",
    "Ars Technica":     "https://feeds.arstechnica.com/arstechnica/technology-lab",
    "SemiWiki":         "https://semiwiki.com/feed/",
    # semiengineering.com/feed times out from the container — route via Google News.
    "SemiEngineering":  "https://news.google.com/rss/search?q=site:semiengineering.com+when:7d&hl=en-US&gl=US&ceid=US:en",
    "EE Times":         "https://news.google.com/rss/search?q=site:eetimes.com+when:3d&hl=en-US&gl=US&ceid=US:en",
    "TechCrunch":       "https://techcrunch.com/feed/",
    "VentureBeat AI":   "https://venturebeat.com/category/ai/feed/",
    "MIT Tech Rev":     "https://www.technologyreview.com/feed/",
    "Tom's Hardware":   "https://www.tomshardware.com/feeds/all",

    # ── Crypto ──────────────────────────────────────────────
    "CoinDesk":         "https://www.coindesk.com/arc/outboundfeeds/rss/",

    # ── Global / Asia ────────────────────────────────────────
    "Nikkei Asia":      "https://news.google.com/rss/search?q=site:asia.nikkei.com+when:1d&hl=en-US&gl=US&ceid=US:en",
    "CNA Business":     "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=6311",
    "SCMP Markets":     "https://www.scmp.com/rss/91/feed",
    "Globe Mail":       "https://news.google.com/rss/search?q=site:theglobeandmail.com+business+when:1d&hl=en-US&gl=US&ceid=US:en",
    "Asia Times":       "https://asiatimes.com/feed/",
    # ── China / Japan / Europe depth added 2026-07-19 (all probed live) ──
    "SCMP Economy":     "https://www.scmp.com/rss/318421/feed",
    "Yicai Global":     "https://news.google.com/rss/search?q=site:yicaiglobal.com+when:2d&hl=en-US&gl=US&ceid=US:en",
    # Real-time China markets wire (@Sino_Market) — quiet on weekends, fine.
    "CN Wire":          "https://nitter.net/Sino_Market/rss",
    # Always-on China coverage (works weekends/holidays, no nitter dependency)
    "China Macro":      "https://news.google.com/rss/search?q=(China+economy+OR+PBOC+OR+yuan+OR+%22China+stimulus%22+OR+%22China+markets%22)+when:1d&hl=en-US&gl=US&ceid=US:en",
    "Japan Macro":      "https://news.google.com/rss/search?q=(BOJ+OR+%22Bank+of+Japan%22+OR+yen+OR+Nikkei)+when:1d&hl=en-US&gl=US&ceid=US:en",
    "Politico Europe":  "https://www.politico.eu/feed/",
    # US macro commentary wire — t.me channel is abandoned (last post May 2024),
    # the live outlet is their X account.
    "Kobeissi Letter":  "https://nitter.net/KobeissiLetter/rss",

    # ── Physical / supply-chain event wires added 2026-07-20 (monthly data
    #    releases: SGE offtake, Korea 20-day exports, TSMC revenue, SIA billings) ──
    "SGE Withdrawals":  "https://news.google.com/rss/search?q=%22Shanghai+Gold+Exchange%22+(withdrawals+OR+deliveries+OR+premium)+when:30d&hl=en-US&gl=US&ceid=US:en",
    "Korea Chip Exports": "https://news.google.com/rss/search?q=Korea+exports+(semiconductor+OR+chips)+(%22first+20+days%22+OR+monthly)+when:30d&hl=en-US&gl=US&ceid=US:en",
    "TSMC Revenue":     "https://news.google.com/rss/search?q=TSMC+(%22monthly+revenue%22+OR+%22monthly+sales%22)+when:45d&hl=en-US&gl=US&ceid=US:en",
    "SIA Billings":     "https://news.google.com/rss/search?q=%22Semiconductor+Industry+Association%22+(billings+OR+sales)+when:60d&hl=en-US&gl=US&ceid=US:en",
    # Mine supply shocks — the live counterpart to the annual USGS production table
    "Mine Supply Wire": "https://news.google.com/rss/search?q=(gold+OR+silver)+mine+(output+OR+production+OR+strike+OR+halt+OR+suspended+OR+guidance)+when:7d&hl=en-US&gl=US&ceid=US:en",

    # ── India Fast (verified working) ────────────────────────
    "NDTV Business":    "https://feeds.feedburner.com/ndtvprofit-latest",
    "Mint Opinion":     "https://www.livemint.com/rss/opinion",
    "Hindu Economy":    "https://www.thehindubusinessline.com/economy/feeder/default.rss",

    # ── More Markets (verified working) ──────────────────────
    "TheStreet":        "https://www.thestreet.com/.rss/full/",
    "Investing News":   "https://www.investing.com/rss/news.rss",
    "Investing Stocks": "https://www.investing.com/rss/stock_stock_picks.rss",
    "FT Home":          "https://www.ft.com/rss/home",

    # ── Earnings Wire (verified working) ─────────────────────
    "Globe Newswire":   "https://www.globenewswire.com/RssFeed/subjectcode/17-Earnings",

    # ── Primary-source feeds added 2026-05-27 ────────────────
    # These come straight from the issuing authority (Treasury, SEC,
    # central banks) — no scraping, no Cloudflare risk, no aggregator
    # in between. Each URL verified responsive at add time.
    # SEC EDGAR requires a contact-bearing User-Agent (see HEADERS above);
    # the global UA includes admin@zyvoratech.co for compliance.
    "TreasuryDirect Auctions":  "https://www.treasurydirect.gov/TA_WS/securities/announced?format=rss",
    "TreasuryDirect Results":   "https://www.treasurydirect.gov/TA_WS/securities/auctioned?format=rss",
    "SEC EDGAR Latest":         "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&output=atom",
    "FRB San Francisco":        "https://www.frbsf.org/feed/",
    "ECB Press":                "https://www.ecb.europa.eu/rss/press.html",
    "Bank of England":          "https://www.bankofengland.co.uk/rss/news",
    "BoE Publications":         "https://www.bankofengland.co.uk/rss/publications",
    "RBA Media":                "https://news.google.com/rss/search?q=%22Reserve+Bank+of+Australia%22+when:14d&hl=en-US&gl=US&ceid=US:en",
}

# ── Stock ticker detection ────────────────────────────────────
# Maps keyword → ticker for inline tagging on news items
TICKER_MAP = {
    # India
    "hdfc bank": "HDFCBANK", "hdfcbank": "HDFCBANK",
    "icici bank": "ICICIBANK", "icicibank": "ICICIBANK",
    "axis bank": "AXISBANK", "kotak bank": "KOTAKBANK",
    "state bank of india": "SBIN", " sbi ": "SBIN",
    "infosys": "INFY", "tata consultancy": "TCS", " tcs ": "TCS",
    "wipro": "WIPRO", "hcl tech": "HCLTECH",
    "reliance industries": "RELIANCE", " ril ": "RELIANCE",
    "bajaj finance": "BAJFIN", "sun pharma": "SUNPHARMA",
    "maruti suzuki": "MARUTI", "hindustan unilever": "HUL",
    "adani": "ADANI", "larsen & toubro": "L&T", "l&t ": "L&T",
    "tata motors": "TATAMOTORS", "bajaj auto": "BAJAJ-AUTO",
    "power grid": "POWERGRID", "ntpc": "NTPC",
    "tech mahindra": "TECHM", "indusind bank": "INDUSINDBK",
    # USA
    "apple inc": "AAPL", "apple iphone": "AAPL", "apple's": "AAPL", " aapl": "AAPL",
    "microsoft": "MSFT", " msft": "MSFT",
    "nvidia": "NVDA", " nvda": "NVDA",
    "alphabet": "GOOGL", "google llc": "GOOGL",
    "amazon.com": "AMZN", " amzn": "AMZN",
    "meta platforms": "META", "facebook": "META",
    "tesla inc": "TSLA", " tsla": "TSLA",
    "jpmorgan": "JPM", "jp morgan": "JPM",
    "goldman sachs": "GS",
    "bank of america": "BAC",
    "morgan stanley": "MS",
    "netflix": "NFLX", " nflx": "NFLX",
    "visa inc": "VISA",
    "mastercard": "MA",
    "walmart": "WMT",
    "berkshire": "BRK",
    "exxon mobil": "XOM", "chevron": "CVX",
    "johnson & johnson": "JNJ",
    "pfizer": "PFE",
    "eli lilly": "LLY",
    # Global
    "tsmc": "TSM", "taiwan semi": "TSM",
    "samsung": "SMSN",
    "toyota": "TM",
    "sony": "SONY",
    "siemens": "SIEGY",
    "sap se": "SAP", "sap ag": "SAP",
    "asml": "ASML",
    "shell": "SHEL",
    "hsbc": "HSBC",
    # Indices / macro
    "nifty": "NIFTY", "sensex": "SENSEX",
    "s&p 500": "SPX", "s&p500": "SPX",
    "nasdaq": "NDX",
    "dow jones": "DJIA",
    "dax": "DAX",
    "ftse": "FTSE",
    "nikkei": "NKY",
    # Commodities
    "gold": "GOLD", "silver": "SILVER",
    "crude oil": "OIL", "brent": "BRENT", "wti": "WTI",
    "natural gas": "NATGAS",
    "copper": "COPPER",
    "bitcoin": "BTC",
    # Macro
    "fed": "FED", "fomc": "FOMC",
    "rbi": "RBI",
    "ecb": "ECB",
    "boj": "BOJ",
}


_CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,6})(?=[^A-Za-z]|$)")


def _detect_tickers(text):
    """Return up to 3 ticker tags: explicit $CASHTAGs first, then keyword map.

    Cashtag extraction means a brand-new or unmapped listing (e.g. $SPCX from a
    WalterBloomberg post) still gets tagged, even though it's not in TICKER_MAP.
    """
    found = []
    seen  = set()
    # 1) Explicit cashtags — catches new/unmapped tickers verbatim.
    for sym in _CASHTAG_RE.findall(text or ""):
        sym = sym.upper()
        if sym not in seen:
            found.append(sym)
            seen.add(sym)
        if len(found) >= 3:
            return found
    # 2) Company/asset name keywords → canonical ticker.
    t = " " + (text or "").lower() + " "
    for keyword, ticker in TICKER_MAP.items():
        if keyword in t and ticker not in seen:
            found.append(ticker)
            seen.add(ticker)
        if len(found) >= 3:
            break
    return found


def _to_ist(dt):
    try:
        return dt.astimezone(IST).strftime("%H:%M IST")
    except:
        return ""


# UA carries a contact email so SEC EDGAR (which requires it under their
# fair-use policy) doesn't IP-ban us. Other sources are happy with any UA;
# using a single contact-bearing string for all of them is the simpler
# robust approach vs. per-source UA overrides.
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ZyvoraTerminal/1.0; +admin@zyvoratech.co)"}


# ── Per-source health monitor ───────────────────────────────────────────────
# Catches silently-dead feeds (e.g. the discontinued feeds.reuters.com, or a
# user-agent-blocked source) that return 0 items without raising. A source is
# "dead/suspect" when the request fails OR the RAW feed has zero entries for
# _DEAD_STREAK consecutive fetches — distinct from a healthy-but-quiet source
# whose entries exist but were filtered out by the age cutoff.
_FEED_HEALTH: dict = {}
_DEAD_STREAK = 6


def _record_health(source, ok, raw, kept, status=0):
    # Transient throttling/unavailability (429/503) isn't a dead feed — leave the
    # streak untouched so we don't false-flag a live source that's just rate-limited
    # (the new Google-News feeds can hit this when several fire at once).
    if status in (429, 503):
        return
    prev   = _FEED_HEALTH.get(source, {})
    dead   = (not ok) or raw == 0
    streak = (prev.get("empty_streak", 0) + 1) if dead else 0
    _FEED_HEALTH[source] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ok": ok, "raw": raw, "kept": kept, "empty_streak": streak,
    }
    if dead and streak == _DEAD_STREAK:   # warn once on crossing the threshold
        print(f"[news] source '{source}' looks DEAD — {streak} consecutive "
              f"empty/failed fetches (ok={ok}, raw_entries={raw})", flush=True)


def get_feed_health(only_suspect: bool = False) -> dict:
    """Snapshot of per-source health. suspect=True means it's been empty/failing
    for >= _DEAD_STREAK fetches (likely a dead feed needing replacement)."""
    out = {}
    for src, h in list(_FEED_HEALTH.items()):   # copy: workers mutate the dict concurrently
        suspect = h.get("empty_streak", 0) >= _DEAD_STREAK
        if only_suspect and not suspect:
            continue
        out[src] = {**h, "suspect": suspect}
    return out


def _fetch_one(source, url):
    items  = []
    ok     = False
    raw    = 0
    status = 0
    age_h  = SLOW_CADENCE_MAX_AGE_HOURS if source in SLOW_CADENCE_SOURCES else MAX_AGE_HOURS
    cutoff = datetime.now(timezone.utc) - timedelta(hours=age_h)
    try:
        resp = requests.get(url, timeout=FEED_TIMEOUT, headers=HEADERS)
        status = resp.status_code
        ok   = status == 200
        feed = feedparser.parse(resp.content)
        raw  = len(feed.entries)
        for entry in feed.entries[:MAX_ITEMS_PER_SOURCE]:
            try:
                title = entry.get("title", "").strip()
                if not title or len(title) > 400:
                    continue
                pub     = entry.get("published", "") or entry.get("updated", "")
                ts_ist  = ""
                pub_utc = ""
                if pub:
                    try:
                        dt_utc  = parsedate_to_datetime(pub).astimezone(timezone.utc)
                        if dt_utc < cutoff:
                            continue
                        ts_ist  = _to_ist(dt_utc)
                        pub_utc = dt_utc.isoformat()
                    except:
                        pass
                cat     = SOURCE_CATEGORY.get(source, "MARKETS")
                tickers = _detect_tickers(title)
                item_url = entry.get("link", "") or entry.get("id", "")
                items.append({
                    "text":     title,
                    "source":   source,
                    "time":     ts_ist,
                    "pub_utc":  pub_utc,
                    "category": cat,
                    "tickers":  tickers,
                    "url":      item_url,
                })
            except:
                pass
    except:
        ok = False
    try:
        _record_health(source, ok, raw, len(items), status)
    except Exception:
        pass                       # health bookkeeping must never drop a feed's items
    return items


def get_rss_news(allowed_sources=None):
    sources = {k: v for k, v in RSS_SOURCES.items()
               if allowed_sources is None or k in allowed_sources}
    all_items = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(_fetch_one, src, url)
                   for src, url in sources.items()]
    # executor context exit waits for all futures; each request has FEED_TIMEOUT=6s
    for fut in futures:
        try:
            all_items.extend(fut.result(timeout=0.01))
        except Exception:
            pass
    return all_items


def get_alpaca_news():
    """Free Alpaca markets news API — no key required, Reuters/Benzinga wire quality."""
    items = []
    try:
        url  = "https://data.alpaca.markets/v1beta1/news?limit=50&sort=desc"
        resp = requests.get(url, timeout=8, headers={"Accept": "application/json"})
        if resp.status_code != 200:
            return items
        data = resp.json().get("news", [])
        now  = datetime.now(timezone.utc)
        cut  = now - timedelta(hours=MAX_AGE_HOURS)
        for art in data:
            try:
                headline = (art.get("headline") or art.get("summary", "")).strip()
                if not headline or len(headline) > 400:
                    continue
                created  = art.get("created_at", "")
                pub_utc  = ""
                ts_ist   = ""
                if created:
                    dt_utc  = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    if dt_utc < cut:
                        continue
                    pub_utc = dt_utc.isoformat()
                    ts_ist  = _to_ist(dt_utc)
                syms    = art.get("symbols", [])
                tickers = syms[:3] if syms else _detect_tickers(headline)
                items.append({
                    "text":     headline,
                    "source":   art.get("source", "Alpaca"),
                    "time":     ts_ist,
                    "pub_utc":  pub_utc,
                    "category": "MARKETS",
                    "tickers":  tickers,
                    "url":      art.get("url", ""),
                })
            except:
                pass
    except:
        pass
    return items


def get_finviz_news():
    """Scrape Finviz market news page — headlines from Bloomberg, Reuters, WSJ etc."""
    from urllib.parse import urlparse
    items = []
    try:
        resp = requests.get(
            "https://finviz.com/news.ashx?v=2",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Referer": "https://finviz.com/",
            },
            timeout=12,
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        for tr in soup.select("table tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            a = tds[1].find("a", href=True)
            if not a:
                continue
            headline = a.get_text(strip=True)
            if len(headline) < 20:
                continue
            url  = a.get("href", "")
            date = tds[0].get_text(strip=True)
            # Derive source from domain: bloomberg.com → Bloomberg
            try:
                domain = urlparse(url).netloc.replace("www.", "").split(".")[0].capitalize()
            except Exception:
                domain = "Finviz"
            items.append({
                "text":     headline,
                "source":   f"Finviz/{domain}",
                "time":     date,
                "pub_utc":  "",
                "category": "MARKETS",
                "tickers":  _detect_tickers(headline),
                "url":      url,
            })
            if len(items) >= 60:
                break
    except Exception:
        pass
    return items


def _norm(text):
    """Normalise headline for dedup — lowercase, strip punctuation, collapse spaces."""
    t = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    return re.sub(r"\s+", " ", t).strip()


# ─── Content-based category tags ────────────────────────────────────────────
# Source-based SOURCE_CATEGORY misses headlines whose CONTENT is about
# bonds/yields but came from a markets/world-news source (a Reuters story
# saying "Japan 10Y rises, Nikkei falls" lands in MARKETS, never BONDS).
# These keyword sets tag items by content so the BONDS/RATES tab actually
# fills up. ``tags`` is a multi-category list — primary category stays.

_BOND_NEWS_KEYWORDS = (
    # Yields + curve
    "yield curve", "yields", "yield ", "10-year", "10 year", "10y ",
    "2-year", "2 year", "2y ", "30-year", "30y ", "5-year",
    "basis points", " bps ", " bp ",
    # Bonds + treasury
    "treasury", "treasuries", "bond market", "bond rout", "bond sell",
    "sovereign debt", "auction tail", "issuance",
    # Non-US sovereigns
    " jgb", "japan 10", "bund", "gilt", "btp", "oat", "boj",
    # Rates policy
    "rate hike", "rate cut", "rate decision", "rate path",
    "fed funds", "dot plot", "fomc", "ecb policy", "rbi repo",
    "monetary policy", "tightening", "easing cycle",
)


def _tag_content_categories(items: list) -> None:
    """Add content-based category tags. Currently tags BONDS by keyword
    match. ``tags`` is a list so other content categories can be added
    later without touching the source-based ``category`` field."""
    for n in items:
        if not isinstance(n, dict):
            continue
        text = (n.get("text") or n.get("headline") or "").lower()
        if not text:
            continue
        tags = n.get("tags") or []
        if "BONDS" not in tags and any(kw in text for kw in _BOND_NEWS_KEYWORDS):
            tags.append("BONDS")
        if tags:
            n["tags"] = tags


# Module-level cache for get_all_news — 16 callers across the codebase hit this
# function. Without this, each call does ~28s of RSS fetches (79 feeds × 7s timeout
# / 20 workers). Cache TTL matches the dashboard's _cached("news", 15) refresh.
_ALL_NEWS_TTL    = 30  # seconds
_all_news_cache  = {"data": None, "ts": 0.0}
_all_news_lock   = threading.Lock()


def get_all_news():
    """Aggregated news from all sources. Cached 30s, single-flight: only one
    thread fetches at a time; concurrent callers wait for the same result."""
    now = time.time()
    cached = _all_news_cache["data"]
    if cached is not None and (now - _all_news_cache["ts"]) < _ALL_NEWS_TTL:
        return cached
    with _all_news_lock:
        # Re-check under lock — another thread may have just refreshed
        now    = time.time()
        cached = _all_news_cache["data"]
        if cached is not None and (now - _all_news_cache["ts"]) < _ALL_NEWS_TTL:
            return cached
        result = _get_all_news_uncached()
        _all_news_cache["data"] = result
        _all_news_cache["ts"]   = time.time()
        return result


# ── Cross-confirmation gate (authentic-only) ────────────────────────────────────
# Sentiment-tier items (Reddit, and any Tier-B social source) are surfaced ONLY when
# a mainstream headline corroborates them — a shared ticker, or >=3 shared significant
# words. This keeps raw social chatter out of the feed while still catching the cases
# where Reddit is echoing a real story.
_STOP = {"about","after","again","amid","another","says","said","with","that","this",
         "from","into","over","under","their","there","these","those","which","while",
         "would","could","should","being","been","have","has","will","new","up","down"}


def _sig_words(text):
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) > 4 and w not in _STOP}


def _cross_confirm(candidates, mainstream):
    """Keep only candidate items corroborated by a mainstream headline."""
    if not candidates:                 # cold start / fully-throttled round — skip the work
        return []
    main_tickers  = set()
    main_wordsets = []
    for m in mainstream:
        if not isinstance(m, dict):
            continue
        for t in (m.get("tickers") or []):
            main_tickers.add(t)
        main_wordsets.append(_sig_words(m.get("text", "")))
    out = []
    for c in candidates:
        ok = bool(set(c.get("tickers") or []) & main_tickers)
        if not ok:
            cw = _sig_words(c.get("text", ""))
            if len(cw) >= 3:
                ok = any(len(cw & mw) >= 3 for mw in main_wordsets)
        if ok:
            c["confirmed"] = True
            out.append(c)
    return out


# Telegram channels treated as Tier-B (lower-trust / sensational / trading opinion):
# routed through the cross-confirm gate just like Reddit instead of flowing straight
# into the feed — only items a mainstream headline corroborates surface.
_TIER_B_TG = {
    "Disclose.tv",
    # Trader flow / options accounts — opinion + promo, not wire news.
    # (Kobeissi dropped 2026-07-19: its t.me channel is dead; now a Tier-A
    # RSS source "Kobeissi Letter" via nitter.)
    "SpotGamma", "Cheddar Flow", "WallSt Jesus", "TheoTrade", "Tradytics",
}


def _get_all_news_uncached():
    news = []
    social = []          # Tier-B social (Reddit + flagged Telegram) — gated below

    try:
        tg = get_telegram_news()
        for item in tg:
            if isinstance(item, dict):
                if "category" not in item:
                    item["category"] = "HNI"
                if "tickers" not in item:
                    item["tickers"] = _detect_tickers(item.get("text", ""))
                if "pub_utc" not in item:
                    item["pub_utc"] = ""
                if item.get("source") in _TIER_B_TG:   # gate sensational TG channels
                    item["tier"] = "B"; item["platform"] = "telegram"
                    social.append(item)
                    continue
            news.append(item)
        # Persist Telegram/HNI items to the searchable archive so pre-market
        # institutional flow survives past the in-memory rolling window.
        try:
            from hni_news_store import store_items
            store_items(tg)
        except Exception:
            pass
    except:
        pass

    try:
        news += get_rss_news()
    except:
        pass

    try:
        news += get_alpaca_news()
    except:
        pass

    try:
        news += get_finviz_news()
    except:
        pass

    # Reddit (sentiment / early-signal) — cached + rate-limit-safe. Copy items so we
    # don't mutate the cache, then add to the Tier-B social pool.
    try:
        from reddit_news import get_reddit_news
        for it in get_reddit_news():
            c = dict(it)
            c["tickers"] = _detect_tickers(c.get("text", ""))
            social.append(c)
    except Exception:
        pass

    # Gate ALL Tier-B social (Reddit + flagged Telegram): only items cross-confirmed by
    # a mainstream headline enter the feed, so raw chatter never surfaces as "news".
    news += _cross_confirm(social, news)

    # Smart dedup: same story from multiple sources → keep first, merge source list
    norm_map = {}   # norm_key → index in unique
    unique   = []
    for n in news:
        if not isinstance(n, dict):
            continue
        key = _norm(n.get("text", ""))[:80]
        if not key:
            continue
        if key in norm_map:
            # Merge: append source name so UI can show "Reuters +2"
            existing = unique[norm_map[key]]
            prev_src = existing.get("source", "")
            new_src  = n.get("source", "")
            if new_src and new_src not in prev_src:
                existing["source"] = f"{prev_src} +1" if "+" not in prev_src else re.sub(r"\+\d+", lambda m: f"+{int(m.group()[1:])+1}", prev_src)
            # Keep highest-quality tickers
            if not existing.get("tickers") and n.get("tickers"):
                existing["tickers"] = n["tickers"]
        else:
            norm_map[key] = len(unique)
            unique.append(n)

    unique = unique[:MAX_TOTAL]
    _tag_content_categories(unique)

    # Also archive country-relevant institutional comments from non-HNI (RSS)
    # sources — India/Asia/global desks — so the HNI Comments country filter
    # isn't US-only. Only items that BOTH classify as a comment (analyst call,
    # stake, earnings, M&A, tracked name) AND tag to a country are kept, so the
    # archive stays focused and small.
    try:
        from hni_news_store import store_items
        from hni_watch import classify, detect_countries
        extra = []
        for n in unique:
            if not isinstance(n, dict) or n.get("category") == "HNI":
                continue
            _, prio = classify(n)
            if prio and detect_countries(n):
                extra.append(n)
        if extra:
            store_items(extra)
    except Exception:
        pass

    return unique


def format_news(news_list):
    lines = []
    for n in news_list:
        if isinstance(n, dict):
            lines.append(f"- [{n['source']} | {n.get('time','')}] {n['text']}")
        else:
            lines.append(f"- {n}")
    return "\n".join(lines)
