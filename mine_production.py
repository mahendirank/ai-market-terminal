"""
mine_production.py — Gold & silver mine supply by country (USGS primary source).

Parses the World Mine Production and Reserves table straight out of the USGS
Mineral Commodity Summaries PDFs — the authoritative free dataset — so the
numbers are sourced and re-derivable rather than hand-typed. Gold is reported
in metric tons, silver in metric tons (USGS uses tons for both).

MCS is published each January, so the data is annual: refreshed monthly here,
cached in sqlite, and clearly stamped with its vintage in the UI. The live
counterpart is the "Mine Supply Wire" news feed (strikes, halts, guidance
cuts) — structural supply here, supply *shocks* there.

Trading relevance: concentration is the story. Gold supply is diffuse (top
producer ~11%), so single-country disruptions barely move it; silver is
concentrated (Mexico ~25%, Peru ~12%), so a Mexican mine strike is a genuine
supply event. The panel surfaces that asymmetry via the top-3 share.
"""
import io, os, re, sqlite3, threading, time
from datetime import datetime, timezone

import requests

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db", "production_cache.db")
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}
MCS_YEAR = 2025          # bump each January when USGS publishes the next edition
SOURCES = {
    "GOLD":   f"https://pubs.usgs.gov/periodicals/mcs{MCS_YEAR}/mcs{MCS_YEAR}-gold.pdf",
    "SILVER": f"https://pubs.usgs.gov/periodicals/mcs{MCS_YEAR}/mcs{MCS_YEAR}-silver.pdf",
}
REFRESH_SECS = 30 * 86400
_lock = threading.Lock()

# Table rows look like:  "Mexico 6,290 6,300 37,000"  →  country, prev-yr, est, reserves.
# "World total (rounded)" needs the parens in the country class.
_ROW = re.compile(r"^([A-Za-z][A-Za-z .,'()\-]{2,40}?)\s+((?:e|\d)[\d,]*)\s+([\d,]+)\s+([\d,]+|NA)\s*$")


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS production (
        metal    TEXT NOT NULL,
        country  TEXT NOT NULL,
        tonnes   REAL NOT NULL,
        reserves REAL,
        year     INTEGER NOT NULL,
        fetched  INTEGER NOT NULL,
        PRIMARY KEY (metal, country))""")
    return conn


def _num(s):
    """Parse a USGS table figure, rejecting footnote-contaminated values.

    USGS glues footnote markers onto the number ("e100", "1112,000" = footnote
    11 + 12,000). A clean figure always has 1-3 digits before its first comma,
    so anything wider is a marker we cannot split unambiguously — return None
    rather than publish a wrong number (this mainly affects reserves).
    """
    if not s or s == "NA":
        return None
    s = s.replace("e", "").strip()
    if "," in s and len(s.split(",")[0]) > 3:
        return None
    if not re.fullmatch(r"[\d,]+", s):
        return None
    return float(s.replace(",", ""))


def _parse(metal):
    from pypdf import PdfReader
    r = requests.get(SOURCES[metal], timeout=40, headers=HEADERS)
    r.raise_for_status()
    text = PdfReader(io.BytesIO(r.content)).pages[-1].extract_text()
    start = text.find("Mine production")
    if start < 0:
        raise ValueError("production table not found")
    rows = []
    for line in text[start:].splitlines():
        m = _ROW.match(line.strip())
        if not m:
            continue
        country, _prev, est, reserves = m.groups()
        country = country.strip()
        if country.lower().startswith(("world resources", "substitutes")):
            break
        val = _num(est)
        if val is None:
            continue
        rows.append((country, val, _num(reserves)))
        if country.lower().startswith("world total"):
            break
    if len(rows) < 5:
        raise ValueError(f"only parsed {len(rows)} rows")
    return rows


def _refresh(conn, metal, force=False):
    row = conn.execute("SELECT MAX(fetched) FROM production WHERE metal=?", (metal,)).fetchone()
    if not force and row and row[0] and time.time() - row[0] < REFRESH_SECS:
        return
    try:
        rows = _parse(metal)
    except Exception as e:
        print(f"[production] {metal} parse failed: {type(e).__name__}: {e}", flush=True)
        return
    now = int(time.time())
    conn.execute("DELETE FROM production WHERE metal=?", (metal,))
    conn.executemany(
        "INSERT OR REPLACE INTO production VALUES (?,?,?,?,?,?)",
        [(metal, c, t, res, MCS_YEAR - 1, now) for c, t, res in rows])
    conn.commit()


def _block(conn, metal):
    rows = conn.execute(
        "SELECT country, tonnes, reserves FROM production WHERE metal=? ORDER BY tonnes DESC",
        (metal,)).fetchall()
    if not rows:
        return {"status": "no-data"}
    total = next((t for c, t, _ in rows if c.lower().startswith("world total")), None)
    countries = [{"country": c, "tonnes": t, "reserves": r} for c, t, r in rows
                 if not c.lower().startswith(("world total", "other countries"))]
    other = next((t for c, t, _ in rows if c.lower().startswith("other countries")), None)
    top = countries[:8]
    if total:
        for c in top:
            c["share"] = round(c["tonnes"] / total * 100, 1)
    top3 = round(sum(c["tonnes"] for c in countries[:3]) / total * 100, 1) if total else None
    return {
        "status": "ok", "year": MCS_YEAR - 1, "source": f"USGS MCS {MCS_YEAR}",
        "world_total": total, "other_countries": other,
        "top": top, "top3_share": top3,
        "concentration": ("CONCENTRATED" if top3 and top3 >= 40 else
                          "MODERATE" if top3 and top3 >= 25 else "DIFFUSE"),
    }


def get_mine_production() -> dict:
    with _lock:
        conn = _conn()
        try:
            for metal in SOURCES:
                _refresh(conn, metal)
            return {"gold": _block(conn, "GOLD"), "silver": _block(conn, "SILVER"),
                    "updated": datetime.now(timezone.utc).isoformat()}
        finally:
            conn.close()


if __name__ == "__main__":
    import json
    print(json.dumps(get_mine_production(), indent=2))
