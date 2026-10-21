"""
История входов/действий, статистика использования и хранилище последних
10 результатов на учётную запись. Всё на persistent-томе /data. Только stdlib.
"""
import os
import io
import re
import json
import time
import hashlib
import threading

import auth  # используем auth.DATA_DIR

DATA_DIR = auth.DATA_DIR
ACT_DIR = os.path.join(DATA_DIR, "activity")
RES_DIR = os.path.join(DATA_DIR, "results")
STATS_FILE = os.path.join(DATA_DIR, "stats.json")
os.makedirs(ACT_DIR, exist_ok=True)
os.makedirs(RES_DIR, exist_ok=True)

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
        path = os.path.join(ACT_DIR, _safe(username) + ".jsonl")
        events = _load(path, [])
        events.append({"ts": int(time.time()), "type": etype, "detail": detail[:200]})
        if len(events) > MAX_EVENTS:
            events = events[-MAX_EVENTS:]
        _save(path, events)


def _bump(username: str, **inc):
    with _LOCK:
        stats = _load(STATS_FILE, {})
        u = stats.setdefault(username, {"runs": 0, "results": 0,
                                        "last_login": 0, "last_run": 0})
        for k, v in inc.items():
            if k in ("last_login", "last_run"):
                u[k] = int(time.time())
            else:
                u[k] = u.get(k, 0) + v
        _save(STATS_FILE, stats)


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


def _store_result(username, filename, data: bytes):
    with _LOCK:
        d = _user_res_dir(username)
        idx_path = os.path.join(d, "index.json")
        idx = _load(idx_path, [])
        rid = hashlib.md5((str(time.time()) + filename).encode()).hexdigest()[:12]
        with open(os.path.join(d, rid + ".xlsx"), "wb") as f:
            f.write(data)
        idx.insert(0, {"id": rid, "name": filename,
                       "ts": int(time.time()), "size": len(data)})
        # держим только последние MAX_RESULTS, старые удаляем
        for old in idx[MAX_RESULTS:]:
            try:
                os.remove(os.path.join(d, old["id"] + ".xlsx"))
            except Exception:
                pass
        idx = idx[:MAX_RESULTS]
        _save(idx_path, idx)


def list_results(username):
    return _load(os.path.join(_user_res_dir(username), "index.json"), [])


def read_result(username, rid):
    if not re.fullmatch(r"[0-9a-f]{6,32}", rid or ""):
        return None, None
    d = _user_res_dir(username)
    idx = _load(os.path.join(d, "index.json"), [])
    meta = next((r for r in idx if r["id"] == rid), None)
    if not meta:
        return None, None
    try:
        with open(os.path.join(d, rid + ".xlsx"), "rb") as f:
            return f.read(), meta["name"]
    except Exception:
        return None, None


# ------------------------------------------------------------- сводки
def summary(username):
    stats = _load(STATS_FILE, {}).get(username, {})
    events = _load(os.path.join(ACT_DIR, _safe(username) + ".jsonl"), [])
    return {
        "username": username,
        "runs": stats.get("runs", 0),
        "results": stats.get("results", 0),
        "last_login": stats.get("last_login", 0),
        "last_run": stats.get("last_run", 0),
        "events": list(reversed(events[-40:])),   # последние сверху
        "results_list": list_results(username),
    }


def all_stats():
    stats = _load(STATS_FILE, {})
    return stats
