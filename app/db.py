"""
SQLite-хранилище для пользователей, сессий, статистики и активности (шаг 3.5
плана доработки) — раньше это были отдельные JSON-файлы на /data
(users.json, sessions.json, stats.json, activity/<юзер>.jsonl), каждый со
своей ручной защитой от порчи (temp-файл + os.replace, .bak-копия). SQLite
сам атомарен на запись, поэтому ручная защита больше не нужна; заодно это
шаг к тому, чтобы uvicorn мог запускаться с несколькими воркерами (сейчас
этому ещё мешает память процесса в auth.py/jobs.py — лимит попыток входа и
очередь задач, это не входит в 3.5).

Один файл app.db, WAL-режим (параллельные чтения не блокируют запись) и
busy_timeout (запись ждёт освобождения файла вместо падения с "database is
locked"). Прочие persistent-файлы (rates.json, jobs.json, xlsx-результаты,
secret.key, .reset_token, initial_admin_password.txt) остаются как есть —
3.5 касается только этих четырёх хранилищ.
"""
import os
import sqlite3
import threading
from contextlib import contextmanager

_PATH = None
_INIT_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    salt TEXT NOT NULL,
    hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    groups_json TEXT NOT NULL DEFAULT '[]',
    tokens_valid_after INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    sid_hash TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    created INTEGER NOT NULL,
    exp INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_exp ON sessions(exp);
CREATE TABLE IF NOT EXISTS stats (
    username TEXT PRIMARY KEY,
    runs INTEGER NOT NULL DEFAULT 0,
    results INTEGER NOT NULL DEFAULT 0,
    last_login INTEGER NOT NULL DEFAULT 0,
    last_run INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    ts INTEGER NOT NULL,
    type TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_activity_user ON activity(username, id);
CREATE TABLE IF NOT EXISTS search_cache (
    key TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    answer TEXT NOT NULL DEFAULT '',
    results_json TEXT NOT NULL DEFAULT '[]',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS photo_cache (
    key TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
"""


def init(data_dir: str):
    """Вызывается один раз (из auth.py при импорте, там уже известен
    DATA_DIR): путь к файлу и таблицы, если их ещё нет. Безопасно вызывать
    повторно — вторая попытка ничего не делает."""
    global _PATH
    with _INIT_LOCK:
        if _PATH is not None:
            return
        _PATH = os.path.join(data_dir, "app.db")
        con = connect()
        try:
            con.executescript(_SCHEMA)
            con.commit()
        finally:
            con.close()


def path() -> str:
    return _PATH


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.row_factory = sqlite3.Row
    return con


@contextmanager
def tx():
    """Соединение на одну операцию записи: commit при успехе, rollback и
    закрытие при исключении — вызывающему коду не нужно думать об этом на
    каждом месте (раньше про это заботился temp-файл + os.replace)."""
    con = connect()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
