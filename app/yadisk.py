"""
Яндекс.Диск REST API — хранилище итоговых xlsx и кеша фото товара (решение
от 20.10.2026, см. ПЛАН_ДОРАБОТКИ.md): живая SQLite-база (app.db — юзеры,
сессии, статистика, метаданные кеша) остаётся на локальном диске сервера —
частые быстрые запросы на каждый клик, HTTP-хранилище для такого не годится
(задержка, риск гонок при блокировках по сети). А вот большие редко
читаемые файлы (готовые xlsx, кеш фото) — хорошие кандидаты на вынос,
снимают растущую нагрузку с небольшого диска VPS.

Используется "папка приложения" (scope cloud_api:disk.app_folder,
path="app:/...") — приложение видит только свою папку на Диске
пользователя, не весь остальной Диск. Без токена (YADISK_TOKEN не задан)
всё тихо не работает (available() == False), вызывающий код (activity.py,
cache.py) в этом случае просто продолжает хранить файлы локально, как и до
этой интеграции — ничего не ломается, если токен не настроен или Диск
временно недоступен.
"""
import os
import json
import logging
import urllib.request
import urllib.error
from urllib.parse import quote

logger = logging.getLogger(__name__)

BASE_URL = "https://cloud-api.yandex.net/v1/disk"
TOKEN = os.getenv("YADISK_TOKEN", "").strip()


def available() -> bool:
    return bool(TOKEN)


def _headers():
    return {"Authorization": f"OAuth {TOKEN}", "Accept": "application/json"}


def _app_path(path: str) -> str:
    return quote(f"app:/{path}", safe="")


def _mkdir(path: str, timeout: int = 30):
    """Создаёт ОДНУ папку (без рекурсии) — Диск не создаёт промежуточные
    папки сам при загрузке файла в них (в отличие от обычной файловой
    системы), без этого PUT на вложенный путь падает 409 CONFLICT."""
    try:
        req = urllib.request.Request(
            f"{BASE_URL}/resources?path={_app_path(path)}",
            headers=_headers(), method="PUT")
        with urllib.request.urlopen(req, timeout=timeout):
            pass
    except urllib.error.HTTPError as e:
        if e.code != 409:  # 409 — папка уже есть, это ожидаемо и ОК
            raise


def _ensure_parent_dirs(path: str):
    """Создаёт все промежуточные папки пути (кроме самого файла), по
    очереди от корня — mkdir одного уровня за раз, как требует API."""
    parts = path.split("/")[:-1]
    cur = []
    for part in parts:
        cur.append(part)
        _mkdir("/".join(cur))


def upload(path: str, data: bytes, timeout: int = 60) -> bool:
    """path — относительный путь внутри папки приложения (без app:/ —
    добавляется сам). Двухшаговый процесс: получить ссылку для загрузки,
    затем PUT на неё (без токена на этом шаге — так требует API). True при
    успехе; любая проблема (нет токена, сеть, Диск недоступен) — тихо
    False, вызывающий код сохраняет файл локально вместо этого."""
    if not TOKEN:
        return False
    try:
        _ensure_parent_dirs(path)
        req = urllib.request.Request(
            f"{BASE_URL}/resources/upload?path={_app_path(path)}&overwrite=true",
            headers=_headers(), method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        put_req = urllib.request.Request(d["href"], data=data, method="PUT")
        with urllib.request.urlopen(put_req, timeout=timeout):
            pass
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("Яндекс.Диск: не удалось загрузить %s: %s", path, e)
        return False


def download(path: str, timeout: int = 60):
    """-> bytes или None (нет токена/файла нет/сеть недоступна)."""
    if not TOKEN:
        return None
    try:
        req = urllib.request.Request(
            f"{BASE_URL}/resources/download?path={_app_path(path)}",
            headers=_headers(), method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        dl_req = urllib.request.Request(d["href"], headers=_headers(), method="GET")
        with urllib.request.urlopen(dl_req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:  # noqa: BLE001
        logger.warning("Яндекс.Диск: не удалось скачать %s: %s", path, e)
        return None


def delete(path: str, timeout: int = 30) -> bool:
    if not TOKEN:
        return False
    try:
        req = urllib.request.Request(
            f"{BASE_URL}/resources?path={_app_path(path)}&permanently=true",
            headers=_headers(), method="DELETE")
        with urllib.request.urlopen(req, timeout=timeout):
            pass
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return True  # уже нет — для удаления это тоже успех
        logger.warning("Яндекс.Диск: не удалось удалить %s: %s", path, e)
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning("Яндекс.Диск: не удалось удалить %s: %s", path, e)
        return False
