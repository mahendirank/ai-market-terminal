"""
hni_news_store.py — Persistent, searchable archive for HNI / Telegram news.

The live news feed (news.get_all_news) is an in-memory rolling window: only the
newest few messages per Telegram channel survive, and they age out fast. That
means yesterday's pre-market institutional flow (e.g. an ARK/SpaceX buy posted
by WalterBloomberg before the US open) is gone by the time you look.

This module persists every Telegram/HNI item to SQLite as it's fetched, so the
feed becomes a *searchable history* instead of a snapshot. Dedup is by content
hash, so re-fetching the same post just bumps `last_seen` — no duplicates.

SQLite-only by design: an archive wants durability, not a TTL cache. Cheap,
no external deps, survives restarts. Set HNI_NEWS_DB to override the path.
"""
from __future__ import annotations

import os, re, time, sqlite3, hashlib, threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

IST     = timezone(timedelta(hours=5, minutes=30))
DB_PATH = os.environ.get(
    "HNI_NEWS_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "hni_news.db"),
)
RETENTION_DAYS = int(os.environ.get("HNI_NEWS_RETENTION_DAYS", "45"))

_lock        = threading.Lock()
_initialized = False
_last_prune  = 0.0
PRUNE_INTERVAL = 3600    # run retention prune at most once an hour


@contextmanager
def _conn():
    # contextmanager so the connection is ALWAYS closed — `with sqlite3.connect(...)`
    # only commits/rolls-back the transaction, it does NOT close, which leaked a
    # connection (and FD) on every store/search/prune call.
    c = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _init():
    global _initialized
    if _initialized:
        return
    with _lock:
        if _initialized:
            return
        try:
            with _conn() as c:
                c.execute("""
                    CREATE TABLE IF NOT EXISTS hni_news (
                        id         TEXT PRIMARY KEY,
                        source     TEXT,
                        category   TEXT,
                        text       TEXT,
                        tickers    TEXT,
                        url        TEXT,
                        time_str   TEXT,
                        pub_utc    TEXT,
                        first_seen REAL,
                        last_seen  REAL
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_hni_first_seen ON hni_news(first_seen)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_hni_source     ON hni_news(source)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_hni_category    ON hni_news(category)")
            _initialized = True
        except Exception as e:
            print(f"[hni_news_store] init failed: {e}", flush=True)


def _id(source: str, text: str) -> str:
    """Stable content hash — same post from same source dedups to one row."""
    basis = f"{source}|{(text or '').strip()[:200]}"
    return hashlib.sha1(basis.encode("utf-8", "ignore")).hexdigest()


def store_items(items) -> int:
    """Persist a list of news dicts. Returns count of NEW rows inserted.

    Existing rows just get `last_seen` bumped (so we can tell how long a story
    stayed live). Safe to call on every fetch cycle — it's an upsert.
    """
    if not items:
        return 0
    _init()
    if not _initialized:
        return 0
    now = time.time()
    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        text = (it.get("text") or "").strip()
        if not text:
            continue
        src = it.get("source", "") or ""
        tickers = it.get("tickers") or []
        if isinstance(tickers, (list, tuple)):
            tickers = ",".join(str(t) for t in tickers)
        rows.append((
            _id(src, text), src, it.get("category", "HNI"), text,
            tickers, it.get("url", "") or "",
            it.get("time", "") or "", it.get("pub_utc", "") or "",
            now, now,
        ))
    if not rows:
        return 0
    new_count = 0
    try:
        with _lock, _conn() as c:
            for r in rows:
                cur = c.execute(
                    "INSERT INTO hni_news "
                    "(id,source,category,text,tickers,url,time_str,pub_utc,first_seen,last_seen) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen",
                    r,
                )
                # rowcount is 1 for insert; for the update path it's also 1, so
                # detect novelty by checking whether last_seen==first_seen.
                if cur.rowcount:
                    pass
            # Count genuinely-new rows (first_seen == this cycle's now)
            new_count = c.execute(
                "SELECT COUNT(*) FROM hni_news WHERE first_seen=?", (now,)
            ).fetchone()[0]
    except Exception as e:
        print(f"[hni_news_store] store failed: {e}", flush=True)
        return 0
    # Time-based prune so retention actually runs (the old `int(now) % 100 == 0`
    # gate almost never fired on coarse cycles → archive grew unbounded).
    global _last_prune
    if now - _last_prune > PRUNE_INTERVAL:
        _last_prune = now
        prune()
    return new_count


def search(q: str = None, source: str = None, ticker: str = None,
           category: str = None, since_hours: float = None,
           limit: int = 100) -> list:
    """Searchable history. All filters optional; newest first.

    q        — substring match on headline text (case-insensitive)
    source   — exact source name (e.g. "WalterBloomberg")
    ticker   — substring match on detected tickers (e.g. "SPCX")
    category — exact category (e.g. "HNI")
    since_hours — only items first seen within the last N hours
    """
    _init()
    if not _initialized:
        return []
    where, params = [], []
    if q:
        where.append("text LIKE ?");      params.append(f"%{q}%")
    if source:
        where.append("source = ?");       params.append(source)
    if ticker:
        where.append("tickers LIKE ?");   params.append(f"%{ticker.upper()}%")
    if category:
        where.append("category = ?");     params.append(category)
    if since_hours:
        where.append("first_seen >= ?");  params.append(time.time() - since_hours * 3600)
    sql = "SELECT * FROM hni_news"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY first_seen DESC LIMIT ?"
    params.append(int(limit))
    try:
        with _conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception as e:
        print(f"[hni_news_store] search failed: {e}", flush=True)
        return []


def recent(limit: int = 100) -> list:
    return search(limit=limit)


def _row_to_dict(r) -> dict:
    fs = r["first_seen"] or 0
    return {
        "source":    r["source"],
        "category":  r["category"],
        "text":      r["text"],
        "tickers":   [t for t in (r["tickers"] or "").split(",") if t],
        "url":       r["url"],
        "time":      r["time_str"],
        "pub_utc":   r["pub_utc"],
        "first_seen_ist": datetime.fromtimestamp(fs, IST).strftime("%d %b %H:%M IST") if fs else "",
        "first_seen_epoch": fs,
    }


def prune(days: int = None) -> int:
    """Delete rows older than retention. Returns rows removed."""
    _init()
    if not _initialized:
        return 0
    days = days if days is not None else RETENTION_DAYS
    cutoff = time.time() - days * 86400
    try:
        with _lock, _conn() as c:
            cur = c.execute("DELETE FROM hni_news WHERE first_seen < ?", (cutoff,))
            return cur.rowcount or 0
    except Exception as e:
        print(f"[hni_news_store] prune failed: {e}", flush=True)
        return 0


def stats() -> dict:
    _init()
    if not _initialized:
        return {"total": 0}
    try:
        with _conn() as c:
            total = c.execute("SELECT COUNT(*) FROM hni_news").fetchone()[0]
            oldest = c.execute("SELECT MIN(first_seen) FROM hni_news").fetchone()[0]
            newest = c.execute("SELECT MAX(first_seen) FROM hni_news").fetchone()[0]
            by_src = c.execute(
                "SELECT source, COUNT(*) n FROM hni_news GROUP BY source ORDER BY n DESC LIMIT 15"
            ).fetchall()
        return {
            "total": total,
            "oldest_ist": datetime.fromtimestamp(oldest, IST).strftime("%d %b %H:%M IST") if oldest else "",
            "newest_ist": datetime.fromtimestamp(newest, IST).strftime("%d %b %H:%M IST") if newest else "",
            "by_source": {r["source"]: r["n"] for r in by_src},
        }
    except Exception as e:
        return {"total": 0, "error": str(e)}


if __name__ == "__main__":
    import json
    print("DB:", DB_PATH)
    print(json.dumps(stats(), indent=2))
    print("\nLatest 5:")
    for n in recent(5):
        print(f"  [{n['first_seen_ist']}] {n['source']}: {n['text'][:80]}")
