"""
chokepoints.py — Live chokepoint monitor (supply-chain Phase 2).

Two independent gauges over the world's critical trade arteries:

1. AIRSPACE DENSITY (live now, no key) — OpenSky Network ADS-B aircraft
   counts inside each chokepoint bounding box. The trading signal is
   *avoidance*: when airlines reroute around a region (Iran strikes, war
   risk), the count collapses vs its own hour-of-day baseline. Anonymous
   OpenSky allows ~400 credits/day; 4 boxes at a 45-min cadence stays
   inside that, and stale data is served from sqlite on 429s.

2. HORMUZ TANKER COUNT (needs a free aisstream.io key) — 45-second AIS
   websocket sample counting unique tankers (ship type 80-89) inside the
   Strait of Hormuz box. Gated on AISSTREAM_API_KEY; the panel shows how
   to enable it when the key is absent.

Baselines are same-UTC-hour averages over the trailing 7 days, because
air traffic is strongly diurnal — comparing 3am to 3pm would false-alarm.
"""
import json, os, sqlite3, threading, time
from datetime import datetime, timezone

import requests

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db", "chokepoint_cache.db")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ZyvoraTerminal/1.0; +admin@zyvoratech.co)"}

# (lamin, lomin, lamax, lomax)
BOXES = {
    "HORMUZ":        (24.5, 54.5, 27.5, 58.5),
    "TAIWAN STRAIT": (22.5, 117.5, 26.0, 122.5),
    "SUEZ":          (29.0, 31.5, 32.0, 34.0),
    "MALACCA":       (1.0, 97.0, 6.5, 104.5),
}
NOTES = {
    "HORMUZ":        "20% of world oil transits below",
    "TAIWAN STRAIT": "semiconductor supply artery",
    "SUEZ":          "Asia-Europe container route",
    "MALACCA":       "busiest shipping lane on earth",
}

REFRESH_SECS = 45 * 60
_lock = threading.Lock()
_last_refresh = 0.0


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS air_samples (
        box   TEXT NOT NULL,
        ts    INTEGER NOT NULL,        -- unix seconds
        hour  INTEGER NOT NULL,        -- UTC hour bucket 0-23
        count INTEGER NOT NULL,
        PRIMARY KEY (box, ts))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS tanker_samples (
        ts    INTEGER PRIMARY KEY,
        count INTEGER NOT NULL)""")
    return conn


def _sample_airspace(conn):
    now = int(time.time())
    hour = datetime.now(timezone.utc).hour
    for box, (lamin, lomin, lamax, lomax) in BOXES.items():
        try:
            r = requests.get(
                "https://opensky-network.org/api/states/all",
                params={"lamin": lamin, "lomin": lomin, "lamax": lamax, "lomax": lomax},
                timeout=20, headers=HEADERS)
            if r.status_code == 429:      # anonymous credit limit — keep stale data
                print("[chokepoints] OpenSky 429 — serving stale", flush=True)
                return
            r.raise_for_status()
            states = r.json().get("states") or []
            airborne = sum(1 for s in states if not s[8])   # s[8] = on_ground
            conn.execute("INSERT OR REPLACE INTO air_samples VALUES (?,?,?,?)",
                         (box, now, hour, airborne))
            conn.commit()
        except Exception as e:
            print(f"[chokepoints] {box}: {type(e).__name__}: {e}", flush=True)
        time.sleep(1.5)                   # be gentle with the free API


def _sample_tankers(conn):
    """45s AIS sample of unique tankers in the Hormuz box (needs free key)."""
    key = os.environ.get("AISSTREAM_API_KEY", "").strip()
    if not key:
        return
    try:
        import websocket
        lamin, lomin, lamax, lomax = BOXES["HORMUZ"]
        seen = set()
        ws = websocket.create_connection("wss://stream.aisstream.io/v0/stream", timeout=10)
        ws.send(json.dumps({
            "APIKey": key,
            "BoundingBoxes": [[[lamin, lomin], [lamax, lomax]]],
            "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
        }))
        deadline = time.time() + 45
        types = {}                        # mmsi -> ship type
        while time.time() < deadline:
            ws.settimeout(max(1, deadline - time.time()))
            try:
                msg = json.loads(ws.recv())
            except Exception:
                break
            meta = msg.get("MetaData", {})
            mmsi = meta.get("MMSI")
            if not mmsi:
                continue
            static = msg.get("Message", {}).get("ShipStaticData")
            if static and static.get("Type") is not None:
                types[mmsi] = static["Type"]
            seen.add(mmsi)
        ws.close()
        tankers = [m for m in seen if 80 <= types.get(m, 0) <= 89]
        # If static data was sparse in the sample window, fall back to all vessels
        count = len(tankers) if tankers else len(seen)
        conn.execute("INSERT OR REPLACE INTO tanker_samples VALUES (?,?)",
                     (int(time.time()), count))
        conn.commit()
    except Exception as e:
        print(f"[chokepoints] tanker sample: {type(e).__name__}: {e}", flush=True)


def _refresh_if_due():
    global _last_refresh
    with _lock:
        if time.time() - _last_refresh < REFRESH_SECS:
            return
        _last_refresh = time.time()
    conn = _conn()
    _sample_airspace(conn)
    _sample_tankers(conn)
    conn.close()


def _box_block(conn, box):
    row = conn.execute(
        "SELECT ts, hour, count FROM air_samples WHERE box=? ORDER BY ts DESC LIMIT 1",
        (box,)).fetchone()
    if not row:
        return {"status": "no-data", "note": NOTES[box]}
    ts, hour, count = row
    week_ago = ts - 7 * 86400
    base = conn.execute(
        """SELECT AVG(count), COUNT(*) FROM air_samples
           WHERE box=? AND ts>=? AND ts<? AND hour BETWEEN ? AND ?""",
        (box, week_ago, ts, (hour - 1) % 24, (hour + 1) % 24)).fetchone()
    baseline, n = (base[0], base[1]) if base and base[0] else (None, 0)
    if n < 3:
        label, ratio = "CALIBRATING", None
    else:
        ratio = round(count / baseline, 2)
        label = ("AVOIDANCE" if ratio < 0.5 else
                 "REDUCED" if ratio < 0.8 else
                 "ELEVATED" if ratio > 1.6 else "NORMAL")
    return {
        "status": "ok", "aircraft": count, "note": NOTES[box],
        "baseline": round(baseline, 1) if baseline else None,
        "baseline_n": n, "ratio": ratio, "label": label,
        "sampled": datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M UTC"),
    }


def get_chokepoints() -> dict:
    _refresh_if_due()
    conn = _conn()
    try:
        out = {"boxes": {b: _box_block(conn, b) for b in BOXES},
               "updated": datetime.now(timezone.utc).isoformat()}
        t = conn.execute(
            "SELECT ts, count FROM tanker_samples ORDER BY ts DESC LIMIT 1").fetchone()
        if t:
            out["hormuz_tankers"] = {
                "status": "ok", "count": t[1],
                "sampled": datetime.fromtimestamp(t[0], timezone.utc).strftime("%H:%M UTC")}
        elif not os.environ.get("AISSTREAM_API_KEY", "").strip():
            out["hormuz_tankers"] = {"status": "no-key"}
        else:
            out["hormuz_tankers"] = {"status": "no-data"}
        return out
    finally:
        conn.close()


if __name__ == "__main__":
    print(json.dumps(get_chokepoints(), indent=2))
