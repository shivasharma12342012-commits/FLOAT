"""Float — the database.

SQLite, WAL, one connection per operation. That last choice is deliberate: the
server is threaded, SQLite connections are not safely shared across threads, and
a fresh connection on a WAL database costs microseconds. It means no connection
pool to get wrong and no ``check_same_thread=False`` lie.

Nothing here knows about HTTP and nothing here calls a model. It is a store.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import config, security

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name          TEXT NOT NULL,
    password_hash TEXT NOT NULL DEFAULT '',
    pin_hash      TEXT NOT NULL DEFAULT '',
    google_sub    TEXT DEFAULT NULL,
    role          TEXT NOT NULL DEFAULT 'teacher' CHECK(role IN ('teacher','admin')),
    active        INTEGER NOT NULL DEFAULT 1,
    must_change   INTEGER NOT NULL DEFAULT 0,
    school        TEXT NOT NULL DEFAULT '',
    board         TEXT NOT NULL DEFAULT 'CBSE',
    subjects      TEXT NOT NULL DEFAULT '[]',
    grades        TEXT NOT NULL DEFAULT '[]',
    language      TEXT NOT NULL DEFAULT 'en',
    assist_level  TEXT NOT NULL DEFAULT 'draft',
    accent        TEXT NOT NULL DEFAULT '#E8A33D',
    appearance    TEXT NOT NULL DEFAULT 'system',
    created_at    REAL NOT NULL,
    last_seen     REAL NOT NULL DEFAULT 0,
    sign_ins      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_users_google ON users(google_sub);

CREATE TABLE IF NOT EXISTS sessions (
    token_fp   TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    boot_nonce TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    last_seen  REAL NOT NULL,
    ip         TEXT NOT NULL DEFAULT '',
    agent      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS api_keys (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope      TEXT NOT NULL CHECK(scope IN ('org','user')),
    user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
    provider   TEXT NOT NULL,
    secret     TEXT NOT NULL,
    hint       TEXT NOT NULL DEFAULT '',
    label      TEXT NOT NULL DEFAULT '',
    added_by   INTEGER,
    created_at REAL NOT NULL,
    UNIQUE(scope, user_id, provider)
);

CREATE TABLE IF NOT EXISTS conversations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title      TEXT NOT NULL DEFAULT 'New conversation',
    tool       TEXT NOT NULL DEFAULT 'chat',
    pinned     INTEGER NOT NULL DEFAULT 0,
    archived   INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    conv_id    INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    meta       TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conv_id, id);

CREATE TABLE IF NOT EXISTS artifacts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conv_id    INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    rel_path   TEXT NOT NULL,
    size       INTEGER NOT NULL DEFAULT 0,
    saved_to   TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifact_user ON artifacts(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS usage (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider   TEXT NOT NULL,
    model      TEXT NOT NULL,
    tokens_in  INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    paise      REAL NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    ok         INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_user ON usage(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    actor      TEXT NOT NULL DEFAULT '',
    action     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    ip         TEXT NOT NULL DEFAULT '',
    level      TEXT NOT NULL DEFAULT 'info',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit(created_at DESC);

CREATE TABLE IF NOT EXISTS klasses (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    grade      TEXT NOT NULL DEFAULT '',
    section    TEXT NOT NULL DEFAULT '',
    subject    TEXT NOT NULL DEFAULT '',
    board      TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS students (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    class_id INTEGER NOT NULL REFERENCES klasses(id) ON DELETE CASCADE,
    roll     TEXT NOT NULL DEFAULT '',
    name     TEXT NOT NULL,
    notes    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_student_class ON students(class_id);

CREATE TABLE IF NOT EXISTS marks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    assessment TEXT NOT NULL,
    score      REAL NOT NULL DEFAULT 0,
    max_score  REAL NOT NULL DEFAULT 100,
    topic      TEXT NOT NULL DEFAULT '',
    on_date    TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_marks_student ON marks(student_id);

CREATE TABLE IF NOT EXISTS attendance (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    class_id INTEGER NOT NULL REFERENCES klasses(id) ON DELETE CASCADE,
    on_date  TEXT NOT NULL,
    present  TEXT NOT NULL DEFAULT '[]',
    absent   TEXT NOT NULL DEFAULT '[]',
    UNIQUE(class_id, on_date)
);

CREATE TABLE IF NOT EXISTS periods (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    weekday  INTEGER NOT NULL,
    slot     INTEGER NOT NULL DEFAULT 1,
    starts   TEXT NOT NULL,
    ends     TEXT NOT NULL,
    label    TEXT NOT NULL DEFAULT '',
    room     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_period_user ON periods(user_id, weekday, starts);

CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title      TEXT NOT NULL,
    due        TEXT NOT NULL DEFAULT '',
    done       INTEGER NOT NULL DEFAULT 0,
    kind       TEXT NOT NULL DEFAULT 'todo',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_user ON tasks(user_id, done, due);

CREATE TABLE IF NOT EXISTS org (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notices (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    level      TEXT NOT NULL DEFAULT 'info',
    active     INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS own_work (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    artifact   TEXT NOT NULL DEFAULT '',
    ai_chars   INTEGER NOT NULL DEFAULT 0,
    own_chars  INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
"""

_init_lock = threading.Lock()
_initialised = False


def _connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


@contextmanager
def cursor() -> Iterator[sqlite3.Connection]:
    init()
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def init() -> None:
    global _initialised
    if _initialised:
        return
    with _init_lock:
        if _initialised:
            return
        conn = _connect()
        try:
            conn.executescript(SCHEMA)
            _seed_org(conn)
        finally:
            conn.close()
        _initialised = True


DEFAULT_ORG = {
    "school_name": config.settings.school_name,
    "allowed_providers": json.dumps(["anthropic", "openai", "google"]),
    "computer_control": "1",
    "teacher_own_keys": "1",
    "monthly_paise_budget": "0",
    "self_signup": "0",
    "require_pin": "0",
}


def _seed_org(conn: sqlite3.Connection) -> None:
    for key, value in DEFAULT_ORG.items():
        conn.execute("INSERT OR IGNORE INTO org(key, value) VALUES(?,?)", (key, value))


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Org settings
# ---------------------------------------------------------------------------
def org_get(key: str, default: str = "") -> str:
    with cursor() as db:
        row = db.execute("SELECT value FROM org WHERE key=?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_ORG.get(key, default)


def org_set(key: str, value: str) -> None:
    with cursor() as db:
        db.execute(
            "INSERT INTO org(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def org_all() -> dict[str, str]:
    with cursor() as db:
        rows = db.execute("SELECT key, value FROM org").fetchall()
    data = dict(DEFAULT_ORG)
    data.update({r["key"]: r["value"] for r in rows})
    return data


def allowed_providers() -> list[str]:
    try:
        value = json.loads(org_get("allowed_providers"))
        return [p for p in value if p in config.PROVIDERS]
    except (ValueError, TypeError):
        return list(config.PROVIDER_ORDER)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
def count_users() -> int:
    with cursor() as db:
        return int(db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"])


def create_user(
    email: str,
    name: str,
    password: str = "",
    role: str = "teacher",
    google_sub: str | None = None,
    must_change: bool = False,
    school: str = "",
) -> dict[str, Any]:
    now = time.time()
    with cursor() as db:
        db.execute(
            """INSERT INTO users(email,name,password_hash,role,google_sub,must_change,school,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                email.strip().lower(),
                name.strip() or email.split("@")[0].title(),
                security.hash_password(password) if password else "",
                role,
                google_sub,
                1 if must_change else 0,
                school or config.settings.school_name,
                now,
            ),
        )
        row = db.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    return dict(row)


def get_user(user_id: int) -> dict[str, Any] | None:
    with cursor() as db:
        return row_to_dict(db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())


def get_user_by_email(email: str) -> dict[str, Any] | None:
    with cursor() as db:
        return row_to_dict(
            db.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
        )


def get_user_by_google(sub: str) -> dict[str, Any] | None:
    with cursor() as db:
        return row_to_dict(db.execute("SELECT * FROM users WHERE google_sub=?", (sub,)).fetchone())


def list_users(include_inactive: bool = True) -> list[dict[str, Any]]:
    sql = "SELECT * FROM users"
    if not include_inactive:
        sql += " WHERE active=1"
    sql += " ORDER BY role='admin' DESC, name COLLATE NOCASE"
    with cursor() as db:
        return [dict(r) for r in db.execute(sql).fetchall()]


_USER_FIELDS = {
    "name", "school", "board", "subjects", "grades", "language",
    "assist_level", "accent", "appearance", "role", "active", "must_change",
    "google_sub",
}


def update_user(user_id: int, **fields: Any) -> None:
    updates = {k: v for k, v in fields.items() if k in _USER_FIELDS}
    if not updates:
        return
    sets = ", ".join(f"{k}=?" for k in updates)
    with cursor() as db:
        db.execute(f"UPDATE users SET {sets} WHERE id=?", (*updates.values(), user_id))


def set_password(user_id: int, password: str) -> None:
    with cursor() as db:
        db.execute(
            "UPDATE users SET password_hash=?, must_change=0 WHERE id=?",
            (security.hash_password(password), user_id),
        )


def set_pin(user_id: int, pin: str) -> None:
    value = security.hash_password(pin) if pin else ""
    with cursor() as db:
        db.execute("UPDATE users SET pin_hash=? WHERE id=?", (value, user_id))


def touch_user(user_id: int, count_sign_in: bool = False) -> None:
    with cursor() as db:
        if count_sign_in:
            db.execute(
                "UPDATE users SET last_seen=?, sign_ins=sign_ins+1 WHERE id=?",
                (time.time(), user_id),
            )
        else:
            db.execute("UPDATE users SET last_seen=? WHERE id=?", (time.time(), user_id))


def delete_user(user_id: int) -> None:
    with cursor() as db:
        db.execute("DELETE FROM users WHERE id=?", (user_id,))


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def create_session(user_id: int, ip: str = "", agent: str = "") -> str:
    token = security.new_token()
    now = time.time()
    with cursor() as db:
        db.execute(
            """INSERT INTO sessions(token_fp,user_id,boot_nonce,created_at,expires_at,last_seen,ip,agent)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                security.token_fingerprint(token),
                user_id,
                security.BOOT_NONCE,
                now,
                now + config.settings.session_hours * 3600,
                now,
                ip,
                agent[:200],
            ),
        )
    return token


def resolve_session(token: str) -> dict[str, Any] | None:
    """Return the signed-in user, or None. Enforces boot nonce, expiry and idle."""
    if not token:
        return None
    fp = security.token_fingerprint(token)
    now = time.time()
    with cursor() as db:
        row = db.execute(
            """SELECT s.*, u.id AS uid FROM sessions s
               JOIN users u ON u.id = s.user_id
               WHERE s.token_fp=? AND u.active=1""",
            (fp,),
        ).fetchone()
        if row is None:
            return None
        if row["boot_nonce"] != security.BOOT_NONCE:
            db.execute("DELETE FROM sessions WHERE token_fp=?", (fp,))
            return None
        if now > row["expires_at"] or now - row["last_seen"] > config.settings.idle_minutes * 60:
            db.execute("DELETE FROM sessions WHERE token_fp=?", (fp,))
            return None
        db.execute("UPDATE sessions SET last_seen=? WHERE token_fp=?", (now, fp))
        user = db.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
    return dict(user) if user else None


def end_session(token: str) -> None:
    with cursor() as db:
        db.execute("DELETE FROM sessions WHERE token_fp=?", (security.token_fingerprint(token),))


def end_all_sessions(user_id: int) -> int:
    with cursor() as db:
        cur = db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        return cur.rowcount or 0


def purge_stale_sessions() -> None:
    """Called at boot. Everything from a previous run is dead by definition."""
    with cursor() as db:
        db.execute("DELETE FROM sessions WHERE boot_nonce<>?", (security.BOOT_NONCE,))


def active_sessions() -> list[dict[str, Any]]:
    with cursor() as db:
        rows = db.execute(
            """SELECT s.user_id, s.created_at, s.last_seen, s.ip, s.agent, u.name, u.email, u.role
               FROM sessions s JOIN users u ON u.id=s.user_id
               WHERE s.boot_nonce=? ORDER BY s.last_seen DESC""",
            (security.BOOT_NONCE,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------
def put_key(provider: str, secret: str, scope: str = "org", user_id: int | None = None,
            added_by: int | None = None, label: str = "") -> None:
    blob = security.encrypt_secret(secret)
    with cursor() as db:
        db.execute(
            """INSERT INTO api_keys(scope,user_id,provider,secret,hint,label,added_by,created_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(scope,user_id,provider) DO UPDATE SET
                 secret=excluded.secret, hint=excluded.hint,
                 label=excluded.label, created_at=excluded.created_at""",
            (scope, user_id, provider, blob, security.mask_key(secret), label, added_by, time.time()),
        )


def drop_key(provider: str, scope: str = "org", user_id: int | None = None) -> None:
    with cursor() as db:
        if scope == "org":
            db.execute("DELETE FROM api_keys WHERE scope='org' AND provider=?", (provider,))
        else:
            db.execute(
                "DELETE FROM api_keys WHERE scope='user' AND user_id=? AND provider=?",
                (user_id, provider),
            )


def get_key(provider: str, user_id: int | None = None) -> str:
    """The teacher's own key if they have one, otherwise the school's."""
    with cursor() as db:
        if user_id is not None:
            row = db.execute(
                "SELECT secret FROM api_keys WHERE scope='user' AND user_id=? AND provider=?",
                (user_id, provider),
            ).fetchone()
            if row:
                return security.decrypt_secret(row["secret"])
        row = db.execute(
            "SELECT secret FROM api_keys WHERE scope='org' AND provider=?", (provider,)
        ).fetchone()
    return security.decrypt_secret(row["secret"]) if row else ""


def key_status(user_id: int | None = None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    with cursor() as db:
        rows = db.execute(
            "SELECT scope,user_id,provider,hint,created_at FROM api_keys "
            "WHERE scope='org' OR user_id=?",
            (user_id,),
        ).fetchall()
    for row in rows:
        entry = out.setdefault(row["provider"], {"org": None, "mine": None})
        entry["org" if row["scope"] == "org" else "mine"] = {
            "hint": row["hint"],
            "added": row["created_at"],
        }
    return out


# ---------------------------------------------------------------------------
# Conversations and messages
# ---------------------------------------------------------------------------
def new_conversation(user_id: int, title: str = "New conversation", tool: str = "chat") -> int:
    now = time.time()
    with cursor() as db:
        cur = db.execute(
            "INSERT INTO conversations(user_id,title,tool,created_at,updated_at) VALUES(?,?,?,?,?)",
            (user_id, title[:120], tool, now, now),
        )
        return int(cur.lastrowid)


def list_conversations(user_id: int, limit: int = 60) -> list[dict[str, Any]]:
    with cursor() as db:
        rows = db.execute(
            """SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conv_id=c.id) AS n
               FROM conversations c WHERE c.user_id=? AND c.archived=0
               ORDER BY c.pinned DESC, c.updated_at DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_conversation(conv_id: int, user_id: int) -> dict[str, Any] | None:
    with cursor() as db:
        return row_to_dict(
            db.execute(
                "SELECT * FROM conversations WHERE id=? AND user_id=?", (conv_id, user_id)
            ).fetchone()
        )


def rename_conversation(conv_id: int, user_id: int, title: str) -> None:
    with cursor() as db:
        db.execute(
            "UPDATE conversations SET title=? WHERE id=? AND user_id=?",
            (title[:120], conv_id, user_id),
        )


def set_conversation_flag(conv_id: int, user_id: int, field: str, value: int) -> None:
    if field not in {"pinned", "archived"}:
        return
    with cursor() as db:
        db.execute(
            f"UPDATE conversations SET {field}=? WHERE id=? AND user_id=?",
            (value, conv_id, user_id),
        )


def delete_conversation(conv_id: int, user_id: int) -> None:
    with cursor() as db:
        db.execute("DELETE FROM conversations WHERE id=? AND user_id=?", (conv_id, user_id))


def add_message(conv_id: int, role: str, content: str, meta: dict[str, Any] | None = None) -> int:
    now = time.time()
    with cursor() as db:
        cur = db.execute(
            "INSERT INTO messages(conv_id,role,content,meta,created_at) VALUES(?,?,?,?,?)",
            (conv_id, role, content, json.dumps(meta or {}), now),
        )
        db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
        return int(cur.lastrowid)


def get_messages(conv_id: int, limit: int = 200) -> list[dict[str, Any]]:
    with cursor() as db:
        rows = db.execute(
            "SELECT * FROM messages WHERE conv_id=? ORDER BY id LIMIT ?", (conv_id, limit)
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        try:
            item["meta"] = json.loads(item["meta"])
        except ValueError:
            item["meta"] = {}
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------
def add_artifact(user_id: int, conv_id: int | None, name: str, kind: str,
                 rel_path: str, size: int) -> int:
    with cursor() as db:
        cur = db.execute(
            """INSERT INTO artifacts(user_id,conv_id,name,kind,rel_path,size,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (user_id, conv_id, name, kind, rel_path, size, time.time()),
        )
        return int(cur.lastrowid)


def get_artifact(artifact_id: int, user_id: int) -> dict[str, Any] | None:
    with cursor() as db:
        return row_to_dict(
            db.execute(
                "SELECT * FROM artifacts WHERE id=? AND user_id=?", (artifact_id, user_id)
            ).fetchone()
        )


def list_artifacts(user_id: int, limit: int = 100) -> list[dict[str, Any]]:
    with cursor() as db:
        rows = db.execute(
            "SELECT * FROM artifacts WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_artifact_saved(artifact_id: int, destination: str) -> None:
    with cursor() as db:
        db.execute("UPDATE artifacts SET saved_to=? WHERE id=?", (destination, artifact_id))


# ---------------------------------------------------------------------------
# Usage and audit
# ---------------------------------------------------------------------------
def record_usage(user_id: int, provider: str, model: str, tokens_in: int, tokens_out: int,
                 paise: float, latency_ms: int, ok: bool = True) -> None:
    with cursor() as db:
        db.execute(
            """INSERT INTO usage(user_id,provider,model,tokens_in,tokens_out,paise,latency_ms,ok,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (user_id, provider, model, tokens_in, tokens_out, paise, latency_ms, 1 if ok else 0, time.time()),
        )


def usage_summary(days: int = 30) -> list[dict[str, Any]]:
    since = time.time() - days * 86400
    with cursor() as db:
        rows = db.execute(
            """SELECT u.id, u.name, u.email, u.role, u.active, u.last_seen, u.sign_ins,
                      COALESCE(SUM(g.tokens_in),0)  AS tin,
                      COALESCE(SUM(g.tokens_out),0) AS tout,
                      COALESCE(SUM(g.paise),0)      AS paise,
                      COUNT(g.id)                   AS calls,
                      COALESCE(AVG(g.latency_ms),0) AS latency
               FROM users u LEFT JOIN usage g ON g.user_id=u.id AND g.created_at>=?
               GROUP BY u.id ORDER BY paise DESC, u.name COLLATE NOCASE""",
            (since,),
        ).fetchall()
    return [dict(r) for r in rows]


def usage_for_user(user_id: int, days: int = 30) -> dict[str, Any]:
    since = time.time() - days * 86400
    with cursor() as db:
        row = db.execute(
            """SELECT COALESCE(SUM(tokens_in),0) tin, COALESCE(SUM(tokens_out),0) tout,
                      COALESCE(SUM(paise),0) paise, COUNT(*) calls
               FROM usage WHERE user_id=? AND created_at>=?""",
            (user_id, since),
        ).fetchone()
    return dict(row)


def usage_by_day(days: int = 14) -> list[dict[str, Any]]:
    since = time.time() - days * 86400
    with cursor() as db:
        rows = db.execute(
            """SELECT date(created_at,'unixepoch','localtime') AS day,
                      COUNT(*) calls, COALESCE(SUM(paise),0) paise
               FROM usage WHERE created_at>=? GROUP BY day ORDER BY day""",
            (since,),
        ).fetchall()
    return [dict(r) for r in rows]


def audit(action: str, user_id: int | None = None, actor: str = "", detail: str = "",
          ip: str = "", level: str = "info") -> None:
    with cursor() as db:
        db.execute(
            "INSERT INTO audit(user_id,actor,action,detail,ip,level,created_at) VALUES(?,?,?,?,?,?,?)",
            (user_id, actor, action, detail[:600], ip, level, time.time()),
        )


def audit_tail(limit: int = 200, level: str = "", user_id: int | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM audit WHERE 1=1"
    args: list[Any] = []
    if level:
        sql += " AND level=?"
        args.append(level)
    if user_id:
        sql += " AND user_id=?"
        args.append(user_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with cursor() as db:
        return [dict(r) for r in db.execute(sql, args).fetchall()]


# ---------------------------------------------------------------------------
# Classes, students, marks, attendance
# ---------------------------------------------------------------------------
def add_class(user_id: int, name: str, grade: str = "", section: str = "",
              subject: str = "", board: str = "") -> int:
    with cursor() as db:
        cur = db.execute(
            "INSERT INTO klasses(user_id,name,grade,section,subject,board,created_at) VALUES(?,?,?,?,?,?,?)",
            (user_id, name, grade, section, subject, board, time.time()),
        )
        return int(cur.lastrowid)


def list_classes(user_id: int) -> list[dict[str, Any]]:
    with cursor() as db:
        rows = db.execute(
            """SELECT k.*, (SELECT COUNT(*) FROM students s WHERE s.class_id=k.id) AS students
               FROM klasses k WHERE k.user_id=? ORDER BY k.grade, k.section, k.name""",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_class(class_id: int, user_id: int) -> None:
    with cursor() as db:
        db.execute("DELETE FROM klasses WHERE id=? AND user_id=?", (class_id, user_id))


def owns_class(class_id: int, user_id: int) -> bool:
    with cursor() as db:
        row = db.execute(
            "SELECT 1 FROM klasses WHERE id=? AND user_id=?", (class_id, user_id)
        ).fetchone()
    return row is not None


def add_students(class_id: int, entries: list[dict[str, str]]) -> int:
    rows = [
        (class_id, str(e.get("roll", "")).strip(), str(e.get("name", "")).strip(),
         str(e.get("notes", "")).strip())
        for e in entries
        if str(e.get("name", "")).strip()
    ]
    if not rows:
        return 0
    with cursor() as db:
        db.executemany("INSERT INTO students(class_id,roll,name,notes) VALUES(?,?,?,?)", rows)
    return len(rows)


def list_students(class_id: int) -> list[dict[str, Any]]:
    with cursor() as db:
        rows = db.execute(
            "SELECT * FROM students WHERE class_id=? ORDER BY CAST(roll AS INTEGER), name",
            (class_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_student(student_id: int) -> None:
    with cursor() as db:
        db.execute("DELETE FROM students WHERE id=?", (student_id,))


def record_marks(rows: list[dict[str, Any]]) -> int:
    now = time.time()
    payload = [
        (int(r["student_id"]), str(r.get("assessment", "Assessment")), float(r.get("score", 0)),
         float(r.get("max_score", 100)), str(r.get("topic", "")), str(r.get("on_date", "")), now)
        for r in rows
    ]
    if not payload:
        return 0
    with cursor() as db:
        db.executemany(
            """INSERT INTO marks(student_id,assessment,score,max_score,topic,on_date,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            payload,
        )
    return len(payload)


def class_marks(class_id: int) -> list[dict[str, Any]]:
    with cursor() as db:
        rows = db.execute(
            """SELECT m.*, s.name, s.roll FROM marks m JOIN students s ON s.id=m.student_id
               WHERE s.class_id=? ORDER BY m.created_at DESC LIMIT 2000""",
            (class_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def save_attendance(class_id: int, on_date: str, present: list[int], absent: list[int]) -> None:
    with cursor() as db:
        db.execute(
            """INSERT INTO attendance(class_id,on_date,present,absent) VALUES(?,?,?,?)
               ON CONFLICT(class_id,on_date) DO UPDATE SET
                 present=excluded.present, absent=excluded.absent""",
            (class_id, on_date, json.dumps(present), json.dumps(absent)),
        )


def attendance_range(class_id: int, limit: int = 60) -> list[dict[str, Any]]:
    with cursor() as db:
        rows = db.execute(
            "SELECT * FROM attendance WHERE class_id=? ORDER BY on_date DESC LIMIT ?",
            (class_id, limit),
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["present"] = json.loads(item["present"] or "[]")
        item["absent"] = json.loads(item["absent"] or "[]")
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Timetable and tasks
# ---------------------------------------------------------------------------
def set_periods(user_id: int, rows: list[dict[str, Any]]) -> None:
    with cursor() as db:
        db.execute("DELETE FROM periods WHERE user_id=?", (user_id,))
        db.executemany(
            "INSERT INTO periods(user_id,weekday,slot,starts,ends,label,room) VALUES(?,?,?,?,?,?,?)",
            [
                (user_id, int(r.get("weekday", 0)), int(r.get("slot", 1)), str(r.get("starts", "")),
                 str(r.get("ends", "")), str(r.get("label", "")), str(r.get("room", "")))
                for r in rows
            ],
        )


def list_periods(user_id: int, weekday: int | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM periods WHERE user_id=?"
    args: list[Any] = [user_id]
    if weekday is not None:
        sql += " AND weekday=?"
        args.append(weekday)
    sql += " ORDER BY weekday, starts"
    with cursor() as db:
        return [dict(r) for r in db.execute(sql, args).fetchall()]


def add_task(user_id: int, title: str, due: str = "", kind: str = "todo") -> int:
    with cursor() as db:
        cur = db.execute(
            "INSERT INTO tasks(user_id,title,due,kind,created_at) VALUES(?,?,?,?,?)",
            (user_id, title[:300], due, kind, time.time()),
        )
        return int(cur.lastrowid)


def list_tasks(user_id: int, include_done: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM tasks WHERE user_id=?"
    if not include_done:
        sql += " AND done=0"
    sql += " ORDER BY done, CASE WHEN due='' THEN 1 ELSE 0 END, due, id DESC LIMIT 200"
    with cursor() as db:
        return [dict(r) for r in db.execute(sql, (user_id,)).fetchall()]


def set_task_done(task_id: int, user_id: int, done: bool) -> None:
    with cursor() as db:
        db.execute(
            "UPDATE tasks SET done=? WHERE id=? AND user_id=?", (1 if done else 0, task_id, user_id)
        )


def delete_task(task_id: int, user_id: int) -> None:
    with cursor() as db:
        db.execute("DELETE FROM tasks WHERE id=? AND user_id=?", (task_id, user_id))


# ---------------------------------------------------------------------------
# Notices and the own-work ledger
# ---------------------------------------------------------------------------
def add_notice(title: str, body: str, level: str, created_by: int) -> int:
    with cursor() as db:
        cur = db.execute(
            "INSERT INTO notices(title,body,level,created_by,created_at) VALUES(?,?,?,?,?)",
            (title[:160], body[:2000], level, created_by, time.time()),
        )
        return int(cur.lastrowid)


def active_notices() -> list[dict[str, Any]]:
    with cursor() as db:
        rows = db.execute(
            "SELECT * FROM notices WHERE active=1 ORDER BY created_at DESC LIMIT 5"
        ).fetchall()
    return [dict(r) for r in rows]


def retire_notice(notice_id: int) -> None:
    with cursor() as db:
        db.execute("UPDATE notices SET active=0 WHERE id=?", (notice_id,))


def log_own_work(user_id: int, artifact: str, ai_chars: int, own_chars: int) -> None:
    with cursor() as db:
        db.execute(
            "INSERT INTO own_work(user_id,artifact,ai_chars,own_chars,created_at) VALUES(?,?,?,?,?)",
            (user_id, artifact[:160], ai_chars, own_chars, time.time()),
        )


def own_work_summary(user_id: int, days: int = 7) -> dict[str, Any]:
    since = time.time() - days * 86400
    with cursor() as db:
        row = db.execute(
            """SELECT COALESCE(SUM(ai_chars),0) ai, COALESCE(SUM(own_chars),0) own, COUNT(*) n
               FROM own_work WHERE user_id=? AND created_at>=?""",
            (user_id, since),
        ).fetchone()
    data = dict(row)
    total = data["ai"] + data["own"]
    data["own_share"] = round(100 * data["own"] / total) if total else 0
    return data
