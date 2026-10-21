"""
Защита от CSRF: изменяющие запросы к /api/* принимаются только со своей страницы.
Не зависит от хостинга: свой домен за прокси добавляется через ALLOWED_ORIGINS.
"""
import os
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
# доп. разрешённые источники через запятую, напр. "https://invix.example.com"
ALLOWED_ORIGINS = {o.strip().rstrip("/").lower()
                   for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()}


def _own_hosts(request: Request) -> set:
    # X-Forwarded-Host со страницы атакующего не выставить: нестандартный
    # заголовок в кросс-доменном запросе требует CORS-preflight, а его мы не разрешаем
    hosts = {request.headers.get("host", "")}
    hosts |= {h.strip() for h in request.headers.get("x-forwarded-host", "").split(",")}
    return {h.lower() for h in hosts if h}


def csrf_error(request: Request):
    """Текст ошибки, если изменяющий запрос пришёл с чужой страницы, иначе None."""
    if request.method not in UNSAFE_METHODS or not request.url.path.startswith("/api/"):
        return None
    # 1) современные браузеры: заголовок ставит сам браузер, страница его не подделает
    site = request.headers.get("sec-fetch-site")
    if site:
        if site in ("same-origin", "none"):
            return None
        return "Запрос с чужого сайта отклонён"
    # 2) старые браузеры: сверяем Origin со своим хостом
    origin = request.headers.get("origin")
    if origin:
        if origin.rstrip("/").lower() in ALLOWED_ORIGINS:
            return None
        if urlsplit(origin).netloc.lower() in _own_hosts(request):
            return None
        return "Запрос с чужого сайта отклонён"
    # 3) не браузер (curl, скрипты): CSRF возможен только через браузер
    return None


async def read_json(request: Request) -> dict:
    """Тело запроса только как application/json-объект (HTML-форма так отправить не может)."""
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise HTTPException(415, "Ожидается Content-Type: application/json")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "Некорректный JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "Ожидается JSON-объект")
    return body
