"""
Клиент Yandex Cloud AI Studio — генерация текста/vision через OpenAI-совместимый
эндпоинт (chat/completions) и веб-поиск через Responses API. Только stdlib
(urllib), чтобы держать образ лёгким.

search() (текстовый веб-поиск, для перевода/описания) и image_search() (поиск
картинки товара, Яндекс.Картинки) — РАЗНЫЕ продукты Yandex Cloud с разными
base URL и форматом ответа:
  - search() -> {BASE_URL}/responses (тот же хост, что chat/completions),
    JSON-ответ с "output" (см. search() ниже).
  - image_search() -> отдельный сервис "Yandex Search API"
    (searchapi.api.cloud.yandex.net), ответ — JSON-обёртка с полем "rawData":
    base64 XML классического формата Яндекс.XML (см. image_search() ниже).
Оба проверены реальным ключом пользователя 20.10.2026.
"""
import os
import json
import time
import base64
import logging
import threading
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

BASE_URL = os.getenv("YANDEX_BASE_URL", "https://ai.api.cloud.yandex.net/v1")
# Ключ и ID каталога (folder) Yandex Cloud AI Studio — задаются только через env.
API_KEY = os.getenv("YANDEX_API_KEY", "").strip()
FOLDER_ID = os.getenv("YANDEX_FOLDER_ID", "").strip()

# Модель распознавания/перевода (без префикса gpt://<folder_id>/ — он
# добавляется в _model_uri). yandexgpt/latest — стандартный алиас на
# актуальную версию YandexGPT.
MODEL = os.getenv("AI_MODEL", "yandexgpt/latest")

# yandexgpt (текстовая модель) изображения не принимает — для vision/скан-PDF
# нужна отдельная мультимодальная модель. qwen3.6-35b-a3b — проверена реальным
# ключом 20.10.2026 (верно описала тестовое изображение); если в вашем
# каталоге (Model Gallery) её вдруг не будет — заменить через env без правки кода.
VISION_MODEL = os.getenv("YANDEX_VISION_MODEL", "qwen3.6-35b-a3b")

# Отдельный сервис Yandex Search API (не Foundation Models/AI Studio) — для
# image_search(). Та же пара ключ+folder работает и здесь (проверено реальным
# запросом), доп. включения в консоли не требует.
SEARCH_API_BASE_URL = os.getenv("YANDEX_SEARCH_API_BASE_URL",
                                "https://searchapi.api.cloud.yandex.net/v2")

# Запасные модели — пробуются по очереди, если основная упёрлась в лимит/квоту
# (HTTP 429) или недоступна (HTTP 404). Как и у Gemini (см. gemini.py) —
# список НЕ проверен реальным ключом, это лучшее предположение по документации
# Yandex; состав стоит пересмотреть после первого реального прогона.
MODEL_FALLBACKS = [m.strip() for m in os.getenv(
    "YANDEX_MODEL_FALLBACKS", "yandexgpt-lite/latest"
).split(",") if m.strip()]

# Общий лимит одновременных запросов к Yandex — на весь процесс (как
# GEMINI_MAX_CONCURRENT у gemini.py, см. комментарий там).
YANDEX_MAX_CONCURRENT = max(1, int(os.getenv("YANDEX_MAX_CONCURRENT", "10")))
_YANDEX_SEM = threading.Semaphore(YANDEX_MAX_CONCURRENT)


def _model_uri(model: str) -> str:
    return f"gpt://{FOLDER_ID}/{model}"


def _post(model: str, payload: dict, timeout: int = 180, path: str = "chat/completions") -> dict:
    if not API_KEY:
        raise RuntimeError("Не задан YANDEX_API_KEY — укажите свой ключ Yandex AI Studio в переменных окружения")
    if not FOLDER_ID:
        raise RuntimeError("Не задан YANDEX_FOLDER_ID — укажите ID каталога Yandex Cloud в переменных окружения")
    url = f"{BASE_URL}/{path}"
    body = dict(payload)
    body["model"] = _model_uri(model)
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Api-Key {API_KEY}",
            "OpenAI-Project": FOLDER_ID,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with _YANDEX_SEM, urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _client_error_no_retry(e: "urllib.error.HTTPError") -> bool:
    """4xx, кроме 429 и 404 — см. gemini._client_error_no_retry, та же логика."""
    return 400 <= e.code < 500 and e.code not in (404, 429)


def _model_chain(model: str) -> list:
    chain = [model]
    for m in MODEL_FALLBACKS:
        if m not in chain:
            chain.append(m)
    return chain


def _retry_delay(attempt: int, e=None) -> float:
    if isinstance(e, urllib.error.HTTPError) and e.code == 429:
        header = e.headers.get("Retry-After") if e.headers else None
        if header:
            try:
                return min(max(float(header), 1), 60)
            except ValueError:
                pass
    return 2 + attempt


def _text_from_response(d: dict) -> str:
    choices = d.get("choices") or []
    if not choices:
        return ""
    return ((choices[0].get("message") or {}).get("content") or "")


# Суммарный расход токенов за время жизни процесса (для тестового замера —
# "сколько токенов уходит на обработку одного файла"). В памяти, сбрасывается
# при перезапуске контейнера — для разовых тестов этого достаточно, отдельной
# персистентной статистики по токенам сейчас не ведётся.
_TOKENS_TOTAL = {"prompt": 0, "completion": 0, "total": 0, "calls": 0}
_TOKENS_LOCK = threading.Lock()


def _log_usage(d: dict, label: str):
    """Логирует расход токенов одного запроса — /chat/completions отдаёт
    prompt_tokens/completion_tokens (OpenAI-совместимый формат), /responses
    (используется в search()) — input_tokens/output_tokens. Заодно копит
    суммарный счётчик за время жизни процесса."""
    u = d.get("usage") or {}
    total = u.get("total_tokens")
    if total is None:
        return
    prompt = u.get("prompt_tokens", u.get("input_tokens", 0)) or 0
    completion = u.get("completion_tokens", u.get("output_tokens", 0)) or 0
    with _TOKENS_LOCK:
        _TOKENS_TOTAL["prompt"] += prompt
        _TOKENS_TOTAL["completion"] += completion
        _TOKENS_TOTAL["total"] += total
        _TOKENS_TOTAL["calls"] += 1
        running = dict(_TOKENS_TOTAL)
    logger.info("Yandex %s: токены — запрос %s / ответ %s / всего %s "
               "(с начала процесса: %s вызовов, %s токенов суммарно)",
               label, prompt, completion, total, running["calls"], running["total"])


def chat(messages: list, temperature: float = 0.3, max_tokens: int = 12000,
         model: str = None, timeout: int = 240, max_retries: int = 3) -> str:
    """Вызов chat/completions. messages — role/content как в OpenAI, передаются
    как есть (эндпоинт OpenAI-совместимый, своего формата конвертировать не
    нужно, в отличие от Gemini)."""
    payload = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    d = _request(model or MODEL, payload, timeout, max_retries, "AI chat failed")
    _log_usage(d, "chat")
    content = _text_from_response(d)
    if not content.strip():
        raise RuntimeError("AI chat failed: empty content")
    return content


def chat_vision(system: str, user_text: str, images: list, model: str = None,
                temperature: float = 0.1, max_tokens: int = 16000,
                timeout: int = 300, max_retries: int = 3) -> str:
    """Vision-запрос: системный промпт + текст + список PNG-байтов.
    В отличие от chat() всегда использует мультимодальную модель (VISION_MODEL),
    а не основную MODEL/model= — обычная yandexgpt изображения не принимает,
    поэтому явный model= здесь (если вызывающий код его передаёт — например,
    extract.EXTRACT_MODEL для текстового разбора) игнорируется намеренно."""
    content = [{"type": "text", "text": user_text}]
    for img in images:
        b64 = base64.b64encode(img).decode()
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"}})
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": content})
    payload = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    d = _request(VISION_MODEL, payload, timeout, max_retries, "AI vision failed")
    _log_usage(d, "chat_vision")
    content_text = _text_from_response(d)
    if not content_text.strip():
        raise RuntimeError("AI vision failed: empty content")
    return content_text


def _request(model: str, payload: dict, timeout: int, max_retries: int, err_prefix: str,
             path: str = "chat/completions") -> dict:
    """Запрос с повторами и автопереключением модели — см. gemini._request,
    та же логика (429/404/таймаут -> следующая модель из MODEL_FALLBACKS,
    остальные 4xx поднимаются сразу)."""
    last_err = None
    chain = _model_chain(model)
    for idx, model_name in enumerate(chain):
        switchable = False
        for attempt in range(max_retries):
            try:
                d = _post(model_name, payload, timeout=timeout, path=path)
                # ВАЖНО: /responses всегда включает ключ "error" (null при успехе) —
                # в отличие от /chat/completions, где он есть ТОЛЬКО при ошибке.
                # Проверяем значение, а не просто наличие ключа.
                if d.get("error"):
                    last_err = d["error"]
                    time.sleep(2)
                    continue
                return d
            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code}: {e.read().decode('utf-8')[:200]}"
                if e.code == 404:
                    switchable = True
                    break
                if _client_error_no_retry(e):
                    raise RuntimeError(f"{err_prefix}: {last_err}") from e
                switchable = e.code == 429
                time.sleep(_retry_delay(attempt, e))
                continue
            except TimeoutError as e:
                last_err = f"таймаут: {e}"
                switchable = True
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                switchable = False
            time.sleep(2 + attempt)
        if not switchable:
            break
        if idx + 1 < len(chain):
            logger.warning("модель %s недоступна (%s) — переключаюсь на %s",
                           model_name, last_err, chain[idx + 1])
    raise RuntimeError(f"{err_prefix}: {last_err}")


def search(query: str, depth: str = "basic", timeout: int = 90) -> dict:
    """Веб-поиск через встроенный инструмент web_search Yandex Responses API
    (тот же BASE_URL, путь /responses вместо /chat/completions; формат
    запроса/ответа другой — не OpenAI chat-like, а output: [...] со списком
    шагов). Проверено реальным ключом 20.10.2026: модель сама решает,
    искать ли, добавляет шаг {"type":"web_search_call"} и затем
    {"type":"message", "content":[{"type":"output_text","text":...,
    "annotations":[{"type":"url_citation","url":...,"title":...}]}]}.
    Как и у Gemini (gemini.search()), контент самих страниц (сниппет) не
    возвращается — только url/title; сама выжимка — в answer."""
    payload = {
        "input": query,
        "tools": [{"type": "web_search"}],
        "temperature": 0.3,
        "max_output_tokens": 1500,
    }
    try:
        d = _request(MODEL, payload, timeout, max_retries=2,
                     err_prefix="AI search failed", path="responses")
        _log_usage(d, "search")
        answer = ""
        results = []
        for item in d.get("output") or []:
            if item.get("type") != "message":
                continue
            for c in item.get("content") or []:
                if c.get("type") != "output_text":
                    continue
                answer = c.get("text") or answer
                for a in c.get("annotations") or []:
                    if a.get("type") == "url_citation" and a.get("url"):
                        results.append({"url": a["url"], "title": a.get("title", ""), "content": ""})
        return {"answer": answer, "results": results[:6]}
    except Exception as e:  # noqa: BLE001
        return {"answer": "", "results": [], "error": str(e)}


def image_search(query: str, count: int = 5, timeout: int = 30) -> list:
    """Поиск картинки товара через Yandex Search API, раздел «Картинки»
    (v2/image/search) — ОТДЕЛЬНЫЙ сервис от Foundation Models/Responses выше
    (другой хост, SEARCH_API_BASE_URL), но та же пара ключ+folder работает без
    доп. включения в консоли (проверено реальным запросом 20.10.2026). Ответ —
    JSON {"rawData": "<base64 XML>"}, классический формат Яндекс.XML: каждый
    <doc><image-properties> содержит прямую ссылку на оригинал (image-link) и
    её размеры (original-width/height) — проверено, отдаёт реальные ссылки на
    страницы карточек товара (ozon, alibaba, joom и т.п.), не на сами страницы.
    Возвращает список {url, width, height}, пустой при любой проблеме (нет
    доступа к продукту, лимит, битый XML, сеть) — не поднимает исключение,
    вызывающий код (enrich.py) просто не находит фото для этого товара."""
    if not API_KEY or not FOLDER_ID:
        return []
    url = f"{SEARCH_API_BASE_URL}/image/search"
    body = {
        "query": {
            "searchType": "SEARCH_TYPE_COM",
            "queryText": query[:400],
            "familyMode": "FAMILY_MODE_MODERATE",
            "page": "0",
        },
        "docsOnPage": str(max(1, min(count, 50))),
        "folderId": FOLDER_ID,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Authorization": f"Api-Key {API_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        xml_bytes = base64.b64decode(d.get("rawData", ""))
        root = ET.fromstring(xml_bytes)
        out = []
        for props in root.iter("image-properties"):
            link = props.findtext("image-link")
            if not link:
                continue
            try:
                w = int(props.findtext("original-width") or 0)
                h = int(props.findtext("original-height") or 0)
            except ValueError:
                w = h = 0
            out.append({"url": link, "width": w, "height": h})
        return out[:count]
    except Exception as e:  # noqa: BLE001
        logger.warning("Yandex image_search failed: %s", e)
        return []


def extract_json(text: str):
    """Достаёт JSON (массив или объект) из ответа модели — идентично gemini.extract_json."""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)
        t = t[1] if len(t) > 1 else text
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    t = t.strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    import re
    m = re.search(r"\[.*\]", t, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    m = re.search(r"\{.*\}", t, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    objs = _salvage_objects(t)
    if objs:
        return objs
    return None


def _salvage_objects(t: str):
    out = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(t):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    frag = t[start:i + 1]
                    try:
                        o = json.loads(frag)
                        if isinstance(o, dict):
                            out.append(o)
                    except Exception:
                        pass
                    start = -1
    return out
