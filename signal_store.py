"""
signal_store.py — Redis-first, SQLite-fallback signal storage.

Every AI signal is pushed here immediately with 30-day TTL.
Redis → fast access, 30-day TTL, accessible across services.
SQLite → fallback when Redis unavailable / offline mode.
Both stores are written simultaneously when Redis is up.
"""
import os, json, time, uuid, threading, sqlite3
from datetime import datetime, timezone, timedelta

IST             = timezone(timedelta(hours=5, minutes=30))
SIGNAL_TTL      = 86400 * 30   # 30 days in seconds
MAX_INDEX_SIZE  = 500           # keep newest N in Redis sorted set

_redis_client  = None
_redis_ok      = False
_redis_lock    = threading.Lock()

# ── Redis init ─────────────────────────────────────────────────────────────────

def _init_redis():
    global _redis_client, _redis_ok
    url = os.environ.get("REDIS_URL", "")
    if not url:
        print("[signal_store] REDIS_URL not set — using SQLite only", flush=True)
        return
    try:
        import redis
        client = redis.from_url(
            url,
            socket_connect_timeout=4,
            socket_timeout=4,
            decode_responses=True,
        )
        client.ping()
        _redis_client = client
        _redis_ok     = True
        print(f"[signal_store] Redis connected: {url[:30]}...", flush=True)
    except Exception as e:
        _redis_ok = False
        print(f"[signal_store] Redis unavailable ({e}) — SQLite fallback active", flush=True)


_init_redis()

# ── SQLite fallback ────────────────────────────────────────────────────────────

_SQ_PATH = os.path.join(os.path.dirname(__file__), "db", "signal_store.db")
_sq_lock = threading.Lock()


def _sq_init():
    os.makedirs(os.path.dirname(_SQ_PATH), exist_ok=True)
    conn = sqlite3.connect(_SQ_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS signal_store (
            id   TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            ts   REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ss_ts ON signal_store(ts);
    """)
    conn.commit()
    conn.close()


_sq_init()


def _sq_push(sig_id: str, data: dict):
    with _sq_lock:
        conn = sqlite3.connect(_SQ_PATH, check_same_thread=False)
        conn.execute(
            "INSERT OR REPLACE INTO signal_store VALUES (?,?,?)",
            (sig_id, json.dumps(data), time.time())
        )
        # Prune expired (>30 days)
        conn.execute("DELETE FROM signal_store WHERE ts < ?", (time.time() - SIGNAL_TTL,))
        conn.commit()
        conn.close()


def _sq_get_recent(limit: int = 50) -> list:
    with _sq_lock:
        conn = sqlite3.connect(_SQ_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT data FROM signal_store ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
    result = []
    for r in rows:
        try:
            result.append(json.loads(r["data"]))
        except Exception:
            pass
    return result


def _sq_get_by_id(sig_id: str) -> dict | None:
    with _sq_lock:
        conn = sqlite3.connect(_SQ_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT data FROM signal_store WHERE id=?", (sig_id,)
        ).fetchone()
        conn.close()
    if row:
        try:
            return json.loads(row["data"])
        except Exception:
            pass
    return None


# ── Redis helpers ──────────────────────────────────────────────────────────────

def _redis_push(sig_id: str, data: dict):
    key = f"signal:{sig_id}"
    ts  = data.get("ts", time.time())
    with _redis_lock:
        pipe = _redis_client.pipeline()
        pipe.setex(key, SIGNAL_TTL, json.dumps(data))
        pipe.zadd("signals:index", {sig_id: ts})
        # Trim sorted set to newest MAX_INDEX_SIZE
        pipe.zremrangebyrank("signals:index", 0, -(MAX_INDEX_SIZE + 2))
        pipe.execute()


def _redis_get_recent(limit: int = 50) -> list:
    with _redis_lock:
        ids = _redis_client.zrevrange("signals:index", 0, limit - 1)
        if not ids:
            return []
        pipe = _redis_client.pipeline()
        for sid in ids:
            pipe.get(f"signal:{sid}")
        raw_list = pipe.execute()

    result = []
    for raw in raw_list:
        if raw:
            try:
                result.append(json.loads(raw))
            except Exception:
                pass
    return result


def _redis_get_by_id(sig_id: str) -> dict | None:
    with _redis_lock:
        raw = _redis_client.get(f"signal:{sig_id}")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return None


# ── Public API ─────────────────────────────────────────────────────────────────

def push_signal(signal_data: dict) -> str:
    """
    Store a signal in Redis (primary) + SQLite (always).
    Returns the signal ID.
    signal_data should be the full enriched signal dict.
    """
    sig_id = signal_data.get("id") or str(uuid.uuid4())
    signal_data["id"]  = sig_id
    signal_data["ts"]  = signal_data.get("ts", time.time())
    signal_data["stored_at"] = datetime.now(IST).strftime("%d-%b-%Y %H:%M IST")

    # SQLite always (durability)
    try:
        _sq_push(sig_id, signal_data)
    except Exception as e:
        print(f"[signal_store] SQLite push error: {e}", flush=True)

    # Redis when available
    if _redis_ok:
        try:
            _redis_push(sig_id, signal_data)
        except Exception as e:
            print(f"[signal_store] Redis push error: {e}", flush=True)

    return sig_id


def get_recent_signals(limit: int = 50) -> list:
    """
    Return recent signals, newest first.
    Tries Redis first; falls back to SQLite.
    """
    if _redis_ok:
        try:
            result = _redis_get_recent(limit)
            if result:
                return result
        except Exception as e:
            print(f"[signal_store] Redis read error: {e}", flush=True)
    return _sq_get_recent(limit)


def get_signal_by_id(sig_id: str) -> dict | None:
    """Retrieve a single signal by ID."""
    if _redis_ok:
        try:
            result = _redis_get_by_id(sig_id)
            if result:
                return result
        except Exception:
            pass
    return _sq_get_by_id(sig_id)


def storage_status() -> dict:
    """Health check for storage backends."""
    redis_ping = False
    if _redis_ok:
        try:
            with _redis_lock:
                _redis_client.ping()
            redis_ping = True
        except Exception:
            pass
    sq_count = 0
    try:
        with _sq_lock:
            conn = sqlite3.connect(_SQ_PATH, check_same_thread=False)
            sq_count = conn.execute("SELECT COUNT(*) FROM signal_store").fetchone()[0]
            conn.close()
    except Exception:
        pass
    return {
        "redis":         redis_ping,
        "redis_url_set": bool(os.environ.get("REDIS_URL")),
        "sqlite":        True,
        "sqlite_count":  sq_count,
        "primary":       "redis" if redis_ping else "sqlite",
    }
