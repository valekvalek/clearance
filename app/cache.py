"""
Кеш результатов веб-поиска и фото товара по нормализованному поисковому
запросу (та же строка, что уже строит `_search_query`/`_kw` в enrich.py/
sourcing.py) — чтобы не платить за повторный поиск/фото при повторяющейся
номенклатуре между разными обработками (решение от 20.10.2026, см.
ПЛАН_ДОРАБОТКИ.md: при 10-20 файлах в день номенклатура пересекается).

TTL — CACHE_TTL_DAYS (по умолчанию 7) — после истечения запись считается
отсутствующей и перезаписывается при следующем попадании тем же запросом;
старый файл фото при этом заменяется (не копится мусор на диске).
"""
import os
import time
import json
import hashlib
import logging

import db
import yadisk
import auth  # auth.DATA_DIR уже определён к моменту импорта этого модуля

logger = logging.getLogger(__name__)

DATA_DIR = auth.DATA_DIR
PHOTO_DIR = os.path.join(DATA_DIR, "photo_cache")
os.makedirs(PHOTO_DIR, exist_ok=True)

TTL_SECONDS = int(os.getenv("CACHE_TTL_DAYS", "7")) * 86400


def _key(query: str) -> str:
    return hashlib.sha1(query.strip().lower().encode("utf-8")).hexdigest()


def get_search(query: str):
    """-> {"answer":..., "results":[...]} из кеша, или None (нет записи/устарела)."""
    if not query:
        return None
    k = _key(query)
    con = db.connect()
    try:
        row = con.execute(
            "SELECT answer, results_json, created_at FROM search_cache WHERE key=?",
            (k,)).fetchone()
    finally:
        con.close()
    if not row or time.time() - row["created_at"] > TTL_SECONDS:
        return None
    try:
        results = json.loads(row["results_json"])
    except Exception:
        results = []
    return {"answer": row["answer"], "results": results}


def set_search(query: str, answer: str, results: list):
    if not query:
        return
    k = _key(query)
    with db.tx() as con:
        con.execute(
            "INSERT INTO search_cache (key, query, answer, results_json, created_at) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET answer=excluded.answer, "
            "results_json=excluded.results_json, created_at=excluded.created_at",
            (k, query, answer, json.dumps(results, ensure_ascii=False), int(time.time())))


_YADISK_PREFIX = "yadisk:"  # маркер в поле path — файл лежит на Яндекс.Диске,
# не на локальном диске сервера (путь после префикса — относительный, без app:/)


def get_photo(query: str):
    """-> bytes картинки из кеша, или None (нет записи/устарела/файл потерян)."""
    if not query:
        return None
    k = _key(query)
    con = db.connect()
    try:
        row = con.execute(
            "SELECT path, created_at FROM photo_cache WHERE key=?", (k,)).fetchone()
    finally:
        con.close()
    if not row or time.time() - row["created_at"] > TTL_SECONDS:
        return None
    path = row["path"]
    if path.startswith(_YADISK_PREFIX):
        return yadisk.download(path[len(_YADISK_PREFIX):])
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        return None


def set_photo(query: str, data: bytes):
    """Если настроен Яндекс.Диск (YADISK_TOKEN) — фото уходит ТУДА, локально
    не сохраняется (экономим диск сервера, см. решение от 20.10.2026). Если
    Диск не настроен или загрузка не удалась — как раньше, локальный файл в
    PHOTO_DIR на сервере."""
    if not query or not data:
        return
    k = _key(query)
    if yadisk.available():
        remote_path = f"photo_cache/{k}.bin"
        if yadisk.upload(remote_path, data):
            path = _YADISK_PREFIX + remote_path
        else:
            path = None
    else:
        path = None
    if path is None:
        local_path = os.path.join(PHOTO_DIR, k + ".bin")
        try:
            with open(local_path, "wb") as f:
                f.write(data)
        except Exception:
            logger.warning("не удалось сохранить фото в кеш (%s)", query[:60])
            return
        path = local_path
    with db.tx() as con:
        con.execute(
            "INSERT INTO photo_cache (key, query, path, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET path=excluded.path, created_at=excluded.created_at",
            (k, query, path, int(time.time())))
