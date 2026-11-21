"""Постоянная база бота: пользователи, переписка, бизнес-подключения.

Хранится в SQLite (по умолчанию data/bot.db). База переживает перезапуски
облака: снимок шифруется и уезжает в репозиторий, а при старте
расшифровывается обратно (см. sync.py).
"""

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.getenv("DB_PATH", "data/bot.db"))

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    chat_id INTEGER,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    messages INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'normal',
    context_cleared_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    ts TEXT NOT NULL,
    connection_id TEXT,
    chat_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_peer ON messages(connection_id, chat_id, id);

CREATE TABLE IF NOT EXISTS peers (
    connection_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    username TEXT,
    name TEXT,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (connection_id, chat_id)
);

CREATE TABLE IF NOT EXISTS connections (
    connection_id TEXT PRIMARY KEY,
    info TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def init() -> None:
    with _lock:
        connect()


def _write(sql: str, params: tuple = ()) -> None:
    with _lock:
        conn = connect()
        conn.execute(sql, params)
        conn.commit()


def _rows(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return connect().execute(sql, params).fetchall()


def touch_user(user_id: int, chat_id: int | None, username: str = "", first_name: str = "", last_name: str = "") -> None:
    stamp = now_iso()
    with _lock:
        conn = connect()
        row = conn.execute("SELECT first_seen FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (user_id, chat_id, username, first_name, last_name, first_seen, last_seen, messages)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (user_id, chat_id, username, first_name, last_name, stamp, stamp),
            )
        else:
            conn.execute(
                "UPDATE users SET chat_id = COALESCE(?, chat_id),"
                " username = CASE WHEN ? <> '' THEN ? ELSE username END,"
                " first_name = CASE WHEN ? <> '' THEN ? ELSE first_name END,"
                " last_name = CASE WHEN ? <> '' THEN ? ELSE last_name END,"
                " last_seen = ?, messages = messages + 1 WHERE user_id = ?",
                (
                    chat_id,
                    username, username,
                    first_name, first_name,
                    last_name, last_name,
                    stamp, user_id,
                ),
            )
        conn.commit()


def get_mode(user_id: int) -> str:
    rows = _rows("SELECT mode FROM users WHERE user_id = ?", (user_id,))
    return rows[0]["mode"] if rows else "normal"


def set_mode(user_id: int, mode: str) -> None:
    _write("UPDATE users SET mode = ? WHERE user_id = ?", (mode, user_id))


def add_message(
    role: str,
    text: str,
    user_id: int | None = None,
    connection_id: str | None = None,
    chat_id: int | None = None,
) -> None:
    _write(
        "INSERT INTO messages (user_id, role, text, ts, connection_id, chat_id) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, role, text[:4000], now_iso(), connection_id, chat_id),
    )


def context(user_id: int, limit: int) -> list[tuple[str, str]]:
    cleared = _rows("SELECT context_cleared_at FROM users WHERE user_id = ?", (user_id,))
    cleared_at = cleared[0]["context_cleared_at"] if cleared else None
    if cleared_at:
        rows = _rows(
            "SELECT role, text FROM messages WHERE user_id = ? AND connection_id IS NULL AND ts > ?"
            " ORDER BY id DESC LIMIT ?",
            (user_id, cleared_at, limit * 2),
        )
    else:
        rows = _rows(
            "SELECT role, text FROM messages WHERE user_id = ? AND connection_id IS NULL"
            " ORDER BY id DESC LIMIT ?",
            (user_id, limit * 2),
        )
    return [(r["role"], r["text"]) for r in reversed(rows)]


def archive(user_id: int, limit: int = 200) -> list[tuple[str, str]]:
    rows = _rows(
        "SELECT role, text FROM messages WHERE user_id = ? AND connection_id IS NULL ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )
    return [(r["role"], r["text"]) for r in reversed(rows)]


def clear_context(user_id: int) -> None:
    _write("UPDATE users SET context_cleared_at = ? WHERE user_id = ?", (now_iso(), user_id))


def forget_user(user_id: int) -> None:
    with _lock:
        conn = connect()
        conn.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        conn.commit()


def users(limit: int = 200) -> list[sqlite3.Row]:
    return _rows("SELECT * FROM users ORDER BY messages DESC, last_seen DESC LIMIT ?", (limit,))


def user_count_message(user_id: int) -> int:
    rows = _rows("SELECT COUNT(*) AS c FROM messages WHERE user_id = ? AND connection_id IS NULL", (user_id,))
    return int(rows[0]["c"]) if rows else 0


def save_connection(connection_id: str, info: dict) -> None:
    import json

    _write(
        "INSERT INTO connections (connection_id, info, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(connection_id) DO UPDATE SET info = excluded.info, updated_at = excluded.updated_at",
        (connection_id, json.dumps(info, ensure_ascii=False), now_iso()),
    )


def connections() -> dict[str, dict]:
    import json

    result: dict[str, dict] = {}
    for row in _rows("SELECT connection_id, info FROM connections"):
        try:
            result[row["connection_id"]] = json.loads(row["info"])
        except Exception:
            continue
    return result


def add_peer(connection_id: str, chat_id: int, username: str = "", name: str = "") -> None:
    _write(
        "INSERT INTO peers (connection_id, chat_id, username, name, last_seen) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(connection_id, chat_id) DO UPDATE SET"
        " username = CASE WHEN excluded.username <> '' THEN excluded.username ELSE peers.username END,"
        " name = CASE WHEN excluded.name <> '' THEN excluded.name ELSE peers.name END,"
        " last_seen = excluded.last_seen",
        (connection_id, chat_id, username, name, now_iso()),
    )


def peers(connection_id: str) -> dict[int, dict]:
    return {
        int(r["chat_id"]): {"username": r["username"] or "", "name": r["name"] or "", "last_seen": r["last_seen"]}
        for r in _rows("SELECT * FROM peers WHERE connection_id = ?", (connection_id,))
    }


def peer_by_username(username: str) -> tuple[str, int] | None:
    low = username.lower()
    for row in _rows("SELECT connection_id, chat_id, username FROM peers WHERE lower(username) = ?", (low,)):
        return row["connection_id"], int(row["chat_id"])
    return None


def peer_messages(connection_id: str, chat_id: int, limit: int = 60) -> list[dict]:
    rows = _rows(
        "SELECT role, text, ts FROM messages WHERE connection_id = ? AND chat_id = ? ORDER BY id DESC LIMIT ?",
        (connection_id, chat_id, limit),
    )
    return [{"role": r["role"], "text": r["text"], "ts": r["ts"]} for r in reversed(rows)]


def save_touch() -> None:
    with _lock:
        conn = connect()
        conn.commit()
