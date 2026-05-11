"""
auth.py — Session-based authentication for AI Market Terminal.

Users stored in SQLite. Sessions via secure cookie (30-day TTL).
Admin role: full access + user management.
Subscriber role: dashboard access only.
"""
import os, hashlib, secrets, sqlite3, time, threading
from datetime import datetime, timezone, timedelta

IST          = timezone(timedelta(hours=5, minutes=30))
DB_PATH      = os.path.join(os.path.dirname(__file__), "db", "auth.db")
SESSION_TTL  = 86400 * 30   # 30 days
COOKIE_NAME  = "ai_terminal_session"
_db_lock     = threading.Lock()


# ── DB ─────────────────────────────────────────────────────────────────────────

def _get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_auth_db():
    with _db_lock:
        conn = _get_conn()
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT    UNIQUE NOT NULL,
            email       TEXT    DEFAULT '',
            password_h  TEXT    NOT NULL,
            role        TEXT    DEFAULT 'subscriber',   -- admin / subscriber
            active      INTEGER DEFAULT 1,
            plan        TEXT    DEFAULT 'monthly',
            created_at  TEXT,
            expires_at  TEXT,
            last_login  TEXT,
            notes       TEXT    DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS sessions (
            token       TEXT    PRIMARY KEY,
            user_id     INTEGER NOT NULL,
            username    TEXT    NOT NULL,
            role        TEXT    NOT NULL,
            created_at  REAL    NOT NULL,
            expires_at  REAL    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
        """)
        conn.commit()

        # Create default admin if no users exist
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count == 0:
            env_pw   = os.environ.get("ADMIN_PASSWORD")
            admin_pw = env_pw if env_pw else secrets.token_urlsafe(16)
            _create_user_internal(conn, "admin", admin_pw, "admin@terminal.local", "admin", days=36500)
            if env_pw:
                print(f"[auth] Default admin created from ADMIN_PASSWORD env var", flush=True)
            else:
                print(f"[auth] Default admin created. Password: {admin_pw}", flush=True)
                print(f"[auth] ⚠ This password is only shown ONCE. Save it, or set ADMIN_PASSWORD env var.", flush=True)
            print(f"[auth] CHANGE THIS via Admin Panel → Settings", flush=True)

        conn.close()


# ── Password ───────────────────────────────────────────────────────────────────

def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    key  = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260000)
    return f"{salt}${key.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt, key_hex = stored.split("$", 1)
        key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260000)
        return secrets.compare_digest(key.hex(), key_hex)
    except Exception:
        return False


# ── User management ────────────────────────────────────────────────────────────

def _create_user_internal(conn, username, password, email, role, days=30):
    now   = datetime.now(IST).strftime("%d-%b-%Y %H:%M IST")
    exp   = (datetime.now(IST) + timedelta(days=days)).strftime("%d-%b-%Y")
    pw_h  = _hash_password(password)
    conn.execute("""
        INSERT INTO users (username, email, password_h, role, active, created_at, expires_at)
        VALUES (?,?,?,?,1,?,?)
    """, (username.lower().strip(), email.strip(), pw_h, role, now, exp))
    conn.commit()


def create_user(username: str, password: str, email: str = "",
                role: str = "subscriber", days: int = 30) -> bool:
    """Create a new user. Returns True on success."""
    try:
        with _db_lock:
            conn = _get_conn()
            _create_user_internal(conn, username, password, email, role, days)
            conn.close()
        print(f"[auth] Created user: {username} ({role}, {days}d)", flush=True)
        return True
    except sqlite3.IntegrityError:
        print(f"[auth] User already exists: {username}", flush=True)
        return False
    except Exception as e:
        print(f"[auth] create_user error: {e}", flush=True)
        return False


def get_user(username: str) -> dict | None:
    try:
        with _db_lock:
            conn = _get_conn()
            row  = conn.execute(
                "SELECT * FROM users WHERE username=?", (username.lower().strip(),)
            ).fetchone()
            conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def list_users() -> list:
    try:
        with _db_lock:
            conn  = _get_conn()
            rows  = conn.execute(
                "SELECT id,username,email,role,active,plan,created_at,expires_at,last_login,notes FROM users ORDER BY id"
            ).fetchall()
            conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def update_user(username: str, **kwargs) -> bool:
    """Update any user field. Allowed keys: active, email, plan, expires_at, notes, role."""
    allowed = {"active", "email", "plan", "expires_at", "notes", "role"}
    fields  = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return False
    try:
        set_clause = ", ".join(f"{k}=?" for k in fields)
        values     = list(fields.values()) + [username.lower()]
        with _db_lock:
            conn = _get_conn()
            conn.execute(f"UPDATE users SET {set_clause} WHERE username=?", values)
            conn.commit()
            conn.close()
        return True
    except Exception as e:
        print(f"[auth] update_user error: {e}", flush=True)
        return False


def change_password(username: str, new_password: str) -> bool:
    try:
        pw_h = _hash_password(new_password)
        with _db_lock:
            conn = _get_conn()
            conn.execute("UPDATE users SET password_h=? WHERE username=?",
                         (pw_h, username.lower()))
            conn.commit()
            conn.close()
        return True
    except Exception as e:
        print(f"[auth] change_password error: {e}", flush=True)
        return False


def delete_user(username: str) -> bool:
    try:
        with _db_lock:
            conn = _get_conn()
            uid  = conn.execute("SELECT id FROM users WHERE username=?",
                                (username.lower(),)).fetchone()
            if uid:
                conn.execute("DELETE FROM sessions WHERE user_id=?", (uid["id"],))
            conn.execute("DELETE FROM users WHERE username=?", (username.lower(),))
            conn.commit()
            conn.close()
        return True
    except Exception as e:
        print(f"[auth] delete_user error: {e}", flush=True)
        return False


# ── Sessions ───────────────────────────────────────────────────────────────────

def create_session(username: str) -> str | None:
    """Create a 30-day session. Returns token."""
    user = get_user(username)
    if not user:
        return None
    token = secrets.token_urlsafe(40)
    now   = time.time()
    exp   = now + SESSION_TTL
    try:
        # Update last_login
        with _db_lock:
            conn = _get_conn()
            conn.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
                (token, user["id"], user["username"], user["role"], now, exp)
            )
            conn.execute(
                "UPDATE users SET last_login=? WHERE id=?",
                (datetime.now(IST).strftime("%d-%b-%Y %H:%M IST"), user["id"])
            )
            # Prune old sessions for this user (keep last 3)
            conn.execute("""
                DELETE FROM sessions WHERE user_id=? AND token NOT IN (
                    SELECT token FROM sessions WHERE user_id=? ORDER BY created_at DESC LIMIT 3
                )
            """, (user["id"], user["id"]))
            conn.commit()
            conn.close()
        return token
    except Exception as e:
        print(f"[auth] create_session error: {e}", flush=True)
        return None


def verify_session(token: str) -> dict | None:
    """
    Verify a session token.
    Returns user dict {username, role, user_id} or None if invalid/expired.
    """
    if not token:
        return None
    try:
        with _db_lock:
            conn = _get_conn()
            row  = conn.execute(
                "SELECT * FROM sessions WHERE token=? AND expires_at > ?",
                (token, time.time())
            ).fetchone()
            conn.close()
        if not row:
            return None

        # Also check user is still active
        user = get_user(row["username"])
        if not user or not user["active"]:
            return None

        # Check subscription not expired
        try:
            exp_str = user.get("expires_at", "")
            if exp_str:
                exp_dt = datetime.strptime(exp_str, "%d-%b-%Y").replace(
                    tzinfo=IST
                )
                if datetime.now(IST) > exp_dt and user["role"] != "admin":
                    return None
        except Exception:
            pass

        return {
            "user_id":  row["user_id"],
            "username": row["username"],
            "role":     row["role"],
        }
    except Exception as e:
        print(f"[auth] verify_session error: {e}", flush=True)
        return None


def delete_session(token: str):
    try:
        with _db_lock:
            conn = _get_conn()
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))
            conn.commit()
            conn.close()
    except Exception:
        pass


def login(username: str, password: str) -> str | None:
    """
    Verify credentials and return session token, or None on failure.
    """
    user = get_user(username)
    if not user:
        return None
    if not user["active"]:
        return None
    if not _verify_password(password, user["password_h"]):
        return None
    return create_session(username)


def get_stats() -> dict:
    """Quick stats for admin dashboard."""
    try:
        with _db_lock:
            conn = _get_conn()
            total    = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            active   = conn.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]
            subs     = conn.execute("SELECT COUNT(*) FROM users WHERE role='subscriber' AND active=1").fetchone()[0]
            sessions = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE expires_at > ?", (time.time(),)
            ).fetchone()[0]
            conn.close()
        return {"total": total, "active": active, "subscribers": subs, "sessions": sessions}
    except Exception:
        return {}


# Init on import
init_auth_db()
