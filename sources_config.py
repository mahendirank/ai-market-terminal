import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sources_config.json")

# All known sources with their type and URL
ALL_SOURCES = {
    # Telegram
    "WalterBloomberg":  {"type": "telegram", "url": "https://t.me/s/WalterBloomberg",      "category": "HNI"},
    "DreamCatcher":     {"type": "telegram", "url": "https://t.me/s/thedreamcatcher0",     "category": "HNI"},
    "FinancialJuice":   {"type": "telegram", "url": "https://t.me/s/financialjuice",       "category": "HNI"},
    "Unusual Whales":   {"type": "telegram", "url": "https://t.me/s/unusual_whales",       "category": "HNI"},
    "MarketCurrents":   {"type": "telegram", "url": "https://t.me/s/marketcurrents",       "category": "MARKETS"},
    "ZeroHedge":        {"type": "telegram", "url": "https://t.me/s/zerohedge",            "category": "MACRO"},
    "ForexLive":        {"type": "telegram", "url": "https://t.me/s/forexlive",            "category": "FX"},
    "GoldTelegraph":    {"type": "telegram", "url": "https://t.me/s/GoldTelegraph",        "category": "COMMODITIES"},
    "KitcoNews":        {"type": "telegram", "url": "https://t.me/s/KitcoNews",            "category": "COMMODITIES"},
    "OilPrice":         {"type": "telegram", "url": "https://t.me/s/oilpricedotcom",       "category": "COMMODITIES"},
    "BusinessInsider":  {"type": "telegram", "url": "https://t.me/s/Business_Insider",     "category": "MARKETS"},
    # RSS
    "Reuters Markets":  {"type": "rss", "url": "https://feeds.reuters.com/reuters/businessNews", "category": "MARKETS"},
    "BBC Business":     {"type": "rss", "url": "https://feeds.bbci.co.uk/news/business/rss.xml", "category": "MARKETS"},
    "CNBC Markets":     {"type": "rss", "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html", "category": "MARKETS"},
    "MarketWatch":      {"type": "rss", "url": "https://feeds.marketwatch.com/marketwatch/topstories/", "category": "MARKETS"},
    "Yahoo Finance":    {"type": "rss", "url": "https://finance.yahoo.com/news/rssindex", "category": "MARKETS"},
    "Investing.com":    {"type": "rss", "url": "https://www.investing.com/rss/news.rss", "category": "MARKETS"},
    "FXStreet":         {"type": "rss", "url": "https://www.fxstreet.com/rss/news", "category": "FX"},
    "DailyFX":          {"type": "rss", "url": "https://www.dailyfx.com/feeds/all", "category": "FX"},
    "Reuters World":    {"type": "rss", "url": "https://feeds.reuters.com/Reuters/worldNews", "category": "GEOPOLITICS"},
    "BBC World":        {"type": "rss", "url": "https://feeds.bbci.co.uk/news/world/rss.xml", "category": "GEOPOLITICS"},
    "Al Jazeera":       {"type": "rss", "url": "https://www.aljazeera.com/xml/rss/all.xml", "category": "GEOPOLITICS"},
    "Sky News World":   {"type": "rss", "url": "https://feeds.skynews.com/feeds/rss/world.xml", "category": "GEOPOLITICS"},
    "FT Markets":       {"type": "rss", "url": "https://www.ft.com/rss/home/uk", "category": "BONDS"},
    "WSJ Markets":      {"type": "rss", "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml", "category": "BONDS"},
    "BondBuyer":        {"type": "rss", "url": "https://www.bondbuyer.com/feed", "category": "BONDS"},
    "Kitco Gold":       {"type": "rss", "url": "https://www.kitco.com/rss/news.xml", "category": "COMMODITIES"},
    "Kitco Silver":     {"type": "rss", "url": "https://www.kitco.com/rss/silver_news.xml", "category": "COMMODITIES"},
    "OilPrice.com":     {"type": "rss", "url": "https://oilprice.com/rss/main", "category": "COMMODITIES"},
    "Mining.com":       {"type": "rss", "url": "https://www.mining.com/feed/", "category": "COMMODITIES"},
    "GoldSeek":         {"type": "rss", "url": "https://news.goldseek.com/goldseek/rss.php", "category": "COMMODITIES"},
    "Natural Gas Intel":{"type": "rss", "url": "https://naturalgasintel.com/feed/", "category": "COMMODITIES"},
    "Rigzone":          {"type": "rss", "url": "https://www.rigzone.com/news/rss/rigzone_latest.aspx", "category": "COMMODITIES"},
    "Economic Times":   {"type": "rss", "url": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms", "category": "INDIA"},
    "Livemint":         {"type": "rss", "url": "https://www.livemint.com/rss/markets", "category": "INDIA"},
    "MoneyControl":     {"type": "rss", "url": "https://www.moneycontrol.com/rss/marketreports.xml", "category": "INDIA"},
    "Business Standard":{"type": "rss", "url": "https://www.business-standard.com/rss/markets-106.rss", "category": "INDIA"},
    "NDTV Profit":      {"type": "rss", "url": "https://www.ndtvprofit.com/rss", "category": "INDIA"},
    "Hindu Business":   {"type": "rss", "url": "https://www.thehindubusinessline.com/markets/feeder/default.rss", "category": "INDIA"},
    "The Register":     {"type": "rss", "url": "https://www.theregister.com/headlines.atom", "category": "TECH"},
    "Ars Technica":     {"type": "rss", "url": "https://feeds.arstechnica.com/arstechnica/technology-lab", "category": "TECH"},
    "SemiWiki":         {"type": "rss", "url": "https://semiwiki.com/feed/", "category": "TECH"},
    "SemiEngineering":  {"type": "rss", "url": "https://semiengineering.com/feed/", "category": "TECH"},
    "EE Times":         {"type": "rss", "url": "https://www.eetimes.com/rss/", "category": "TECH"},
    "Tom's Hardware":   {"type": "rss", "url": "https://www.tomshardware.com/feeds/all", "category": "TECH"},
}


def _load():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except:
        return {"approved": list(ALL_SOURCES.keys()), "rejected": [], "pending": []}


def _save(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def get_all_sources():
    cfg = _load()
    result = []
    for name, meta in ALL_SOURCES.items():
        if name in cfg.get("rejected", []):
            status = "rejected"
        elif name in cfg.get("pending", []):
            status = "pending"
        else:
            status = "approved"
        result.append({
            "name":     name,
            "type":     meta["type"],
            "category": meta["category"],
            "url":      meta["url"],
            "status":   status,
        })
    # Add any custom pending sources not in ALL_SOURCES
    for name in cfg.get("pending", []):
        if name not in ALL_SOURCES:
            result.append({"name": name, "type": "custom", "category": "?", "url": "", "status": "pending"})
    return result


def is_active(source_name):
    cfg = _load()
    return source_name not in cfg.get("rejected", [])


def approve(source_name):
    cfg = _load()
    cfg.setdefault("approved", [])
    cfg.setdefault("rejected", [])
    cfg.setdefault("pending", [])
    if source_name in cfg["rejected"]:
        cfg["rejected"].remove(source_name)
    if source_name in cfg["pending"]:
        cfg["pending"].remove(source_name)
    if source_name not in cfg["approved"]:
        cfg["approved"].append(source_name)
    _save(cfg)


def reject(source_name):
    cfg = _load()
    cfg.setdefault("approved", [])
    cfg.setdefault("rejected", [])
    cfg.setdefault("pending", [])
    if source_name in cfg["approved"]:
        cfg["approved"].remove(source_name)
    if source_name in cfg["pending"]:
        cfg["pending"].remove(source_name)
    if source_name not in cfg["rejected"]:
        cfg["rejected"].append(source_name)
    _save(cfg)


def add_pending(source_name, url, category="MARKETS", src_type="telegram"):
    """Add a new source for user approval."""
    cfg = _load()
    cfg.setdefault("pending", [])
    if source_name not in cfg["pending"]:
        cfg["pending"].append(source_name)
    _save(cfg)
    # Also register in ALL_SOURCES at runtime
    ALL_SOURCES[source_name] = {"type": src_type, "url": url, "category": category}
