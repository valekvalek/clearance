"""
История входов/действий и статистика использования — в SQLite (app.db,
см. db.py, шаг 3.5 плана доработки), хранилище последних 10 результатов на
учётную запись — как раньше, файлами на persistent-томе /data (сами xlsx —
бинарные, под SQLite не нужны, индекс на запись слишком мал, чтобы были
проблемы с конкурентной записью). Только stdlib + db.py.
"""
import os
import re
import json
import time
import logging
import hashlib
import threading

import auth  # используем auth.DATA_DIR, db уже инициализирован там
import db
import yadisk

logger = logging.getLogger(__name__)

DATA_DIR = auth.DATA_DIR
RES_DIR = os.path.join(DATA_DIR, "results")
os.makedirs(RES_DIR, exist_ok=True)

# Старые пути — только для разового переноса данных уже развёрнутого
# инстанса (см. _migrate_from_json ниже), новый код их не пишет.
_OLD_ACT_DIR = os.path.join(DATA_DIR, "activity")
_OLD_STATS_FILE = os.path.join(DATA_DIR, "stats.json")

_LOCK = threading.Lock()
MAX_EVENTS = 300        # хранимых событий на юзера
MAX_RESULTS = 10        # хранимых файлов-результатов на юзера


def _safe(username: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]", "", username or "")
    h = hashlib.md5((username or "").encode("utf-8")).hexdigest()[:8]
    return (s[:40] + "_" + h) if s else h


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ------------------------------------------------------------- события
def record(username: str, etype: str, detail: str = ""):
    """Записывает событие в историю (login/process/download/error...)."""
    if not username:
        return
    with _LOCK:
        with db.tx() as con:
            con.execute(
                "INSERT INTO activity (username, ts, type, detail) VALUES (?, ?, ?, ?)",
                (username, int(time.time()), etype, (detail or "")[:200]))
            # держим только последние MAX_EVENTS на пользователя, старые чистим
            con.execute(
                "DELETE FROM activity WHERE username = ? AND id NOT IN "
                "(SELECT id FROM activity WHERE username = ? ORDER BY id DESC LIMIT ?)",
                (username, username, MAX_EVENTS))


_STAT_COLUMNS = ("runs", "results", "last_login", "last_run")


def _bump(username: str, **inc):
    with _LOCK:
        with db.tx() as con:
            con.execute(
                "INSERT INTO stats (username) VALUES (?) "
                "ON CONFLICT(username) DO NOTHING", (username,))
            for k, v in inc.items():
                if k not in _STAT_COLUMNS:
                    raise ValueError(f"неизвестное поле статистики: {k}")
                if k in ("last_login", "last_run"):
                    con.execute(f"UPDATE stats SET {k} = ? WHERE username = ?",
                               (int(time.time()), username))
                else:
                    con.execute(f"UPDATE stats SET {k} = {k} + ? WHERE username = ?",
                               (v, username))


def on_login(username):
    _bump(username, last_login=1)
    record(username, "login")


def on_run_start(username, filename):
    _bump(username, runs=1, last_run=1)
    record(username, "process", filename)


def on_result(username, filename, items, data: bytes):
    _bump(username, results=1)
    record(username, "result", f"{filename} · {items} товаров")
    _store_result(username, filename, data)


def on_download(username, filename):
    record(username, "download", filename)


# ------------------------------------------------------- хранилище результатов
def _user_res_dir(username):
    d = os.path.join(RES_DIR, _safe(username))
    os.makedirs(d, exist_ok=True)
    return d


def _yadisk_result_path(username, rid):
    return f"results/{_safe(username)}/{rid}.xlsx"


def _store_result(username, filename, data: bytes):
    """Если настроен Яндекс.Диск (YADISK_TOKEN) — xlsx уходит ТУДА, локально
    не сохраняется (экономим диск сервера, см. решение от 20.10.2026). Если
    Диск не настроен или загрузка не удалась — как раньше, локальный файл."""
    with _LOCK:
        d = _user_res_dir(username)
        idx_path = os.path.join(d, "index.json")
        idx = _load(idx_path, [])
        rid = hashlib.md5((str(time.time()) + filename).encode()).hexdigest()[:12]
        store = "local"
        if yadisk.available() and yadisk.upload(_yadisk_result_path(username, rid), data):
            store = "yadisk"
        else:
            with open(os.path.join(d, rid + ".xlsx"), "wb") as f:
                f.write(data)
        idx.insert(0, {"id": rid, "name": filename,
                       "ts": int(time.time()), "size": len(data), "store": store})
        # держим только последние MAX_RESULTS, старые удаляем
        for old in idx[MAX_RESULTS:]:
            if old.get("store") == "yadisk":
                yadisk.delete(_yadisk_result_path(username, old["id"]))
            else:
                try:
                    os.remove(os.path.join(d, old["id"] + ".xlsx"))
                except Exception:
                    pass
        idx = idx[:MAX_RESULTS]
        _save(idx_path, idx)


def list_results(username):
    return _load(os.path.join(_user_res_dir(username), "index.json"), [])


def delete_result(username, rid) -> bool:
    """Удаляет результат из истории (кнопка «×» в ленте/списке). True, если
    запись была и удалена; False — нет такой записи (чужая/уже удалена)."""
    if not re.fullmatch(r"[0-9a-f]{6,32}", rid or ""):
        return False
    with _LOCK:
        d = _user_res_dir(username)
        idx_path = os.path.join(d, "index.json")
        idx = _load(idx_path, [])
        meta = next((r for r in idx if r["id"] == rid), None)
        new_idx = [r for r in idx if r["id"] != rid]
        if len(new_idx) == len(idx):
            return False
        _save(idx_path, new_idx)
    if meta and meta.get("store") == "yadisk":
        yadisk.delete(_yadisk_result_path(username, rid))
    else:
        try:
            os.remove(os.path.join(d, rid + ".xlsx"))
        except Exception:
            pass
    return True


def read_result(username, rid):
    if not re.fullmatch(r"[0-9a-f]{6,32}", rid or ""):
        return None, None
    d = _user_res_dir(username)
    idx = _load(os.path.join(d, "index.json"), [])
    meta = next((r for r in idx if r["id"] == rid), None)
    if not meta:
        return None, None
    if meta.get("store") == "yadisk":
        data = yadisk.download(_yadisk_result_path(username, rid))
        return (data, meta["name"]) if data else (None, None)
    try:
        with open(os.path.join(d, rid + ".xlsx"), "rb") as f:
            return f.read(), meta["name"]
    except Exception:
        return None, None


# ------------------------------------------------------------- сводки
def summary(username):
    con = db.connect()
    try:
        row = con.execute(
            "SELECT runs, results, last_login, last_run FROM stats "
            "WHERE username = ?", (username,)).fetchone()
        events = con.execute(
            "SELECT ts, type, detail FROM activity WHERE username = ? "
            "ORDER BY id DESC LIMIT 40", (username,)).fetchall()
    finally:
        con.close()
    stats = dict(row) if row else {}
    return {
        "username": username,
        "runs": stats.get("runs", 0),
        "results": stats.get("results", 0),
        "last_login": stats.get("last_login", 0),
        "last_run": stats.get("last_run", 0),
        "events": [dict(e) for e in events],   # последние сверху
        "results_list": list_results(username),
    }


def all_stats():
    con = db.connect()
    try:
        rows = con.execute(
            "SELECT username, runs, results, last_login, last_run FROM stats").fetchall()
    finally:
        con.close()
    return {r["username"]: {"runs": r["runs"], "results": r["results"],
                            "last_login": r["last_login"], "last_run": r["last_run"]}
            for r in rows}


def _migrate_from_json():
    """Разовый перенос данных уже развёрнутого инстанса из старых
    stats.json/activity/<юзер>.jsonl в SQLite — только если туда ещё
    ничего не перенесено. Файлы переименовываются в *.migrated, а не
    удаляются: откат на предыдущую версию кода не должен терять данные.
    История активности мигрирует только для существующих сейчас
    пользователей — имя восстанавливается из списка auth.list_users(),
    а не из хэшированного имени файла (см. _safe)."""
    if not all_stats() and os.path.exists(_OLD_STATS_FILE):
        try:
            old_stats = _load(_OLD_STATS_FILE, {})
            if isinstance(old_stats, dict) and old_stats:
                with db.tx() as con:
                    con.executemany(
                        "INSERT INTO stats (username, runs, results, last_login, "
                        "last_run) VALUES (?, ?, ?, ?, ?)",
                        [(u, s.get("runs", 0), s.get("results", 0),
                          s.get("last_login", 0), s.get("last_run", 0))
                         for u, s in old_stats.items()])
                logger.info("stats.json перенесён в SQLite (%d пользователей)",
                           len(old_stats))
            os.rename(_OLD_STATS_FILE, _OLD_STATS_FILE + ".migrated")
        except Exception as e:  # noqa: BLE001
            logger.error("не удалось перенести stats.json в SQLite: %s", e)

    if not os.path.isdir(_OLD_ACT_DIR):
        return
    try:
        usernames = [u["username"] for u in auth.list_users()]
    except Exception as e:  # noqa: BLE001
        logger.error("не удалось получить список пользователей для переноса "
                    "активности: %s", e)
        return
    for username in usernames:
        path = os.path.join(_OLD_ACT_DIR, _safe(username) + ".jsonl")
        if not os.path.exists(path):
            continue
        try:
            con = db.connect()
            try:
                already = con.execute(
                    "SELECT 1 FROM activity WHERE username = ? LIMIT 1",
                    (username,)).fetchone()
            finally:
                con.close()
            if not already:
                events = _load(path, [])
                rows = [(username, e.get("ts", 0), e.get("type", ""),
                        e.get("detail", "")) for e in events if isinstance(e, dict)]
                if rows:
                    with db.tx() as con:
                        con.executemany(
                            "INSERT INTO activity (username, ts, type, detail) "
                            "VALUES (?, ?, ?, ?)", rows)
            os.rename(path, path + ".migrated")
        except Exception as e:  # noqa: BLE001
            logger.error("не удалось перенести активность '%s' в SQLite: %s",
                        username, e)
    logger.info("перенос истории активности в SQLite завершён")


_migrate_from_json()
