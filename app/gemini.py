"""
Клиент Google AI Studio (Gemini) — генерация текста/vision + веб-поиск через
встроенный Google Search grounding. Только stdlib (urllib), чтобы держать
образ лёгким.
"""
import os
import json
import time
import base64
import logging
import threading
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

BASE_URL = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")
# Ключ Google AI Studio задаётся только через env GEMINI_API_KEY.
API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
# Модель распознавания/перевода. Быстрая и дешёвая по умолчанию; для более
# точного, но медленного и дорогого разбора сложных инвойсов можно поставить
# gemini-2.5-pro через env AI_MODEL.
MODEL = os.getenv("AI_MODEL", "gemini-3.8-flash")

# Запасные модели — пробуются по очереди, если основная (MODEL/model=...)
# упёрлась в лимит/квоту (HTTP 429) или снята с поддержки (HTTP 404, как уже
# случилось один раз с gemini-2.5-flash, см. ПЛАН_ДОРАБОТКИ.md). Первой стоит
# псевдоним gemini-flash-latest — Google сам держит его указывающим на
# актуальную flash-модель, поэтому он переживёт будущие снятия версий с
# поддержки без правки кода. Порядок настраивается через
# GEMINI_MODEL_FALLBACKS (через запятую), запасные модели по умолчанию:
MODEL_FALLBACKS = [m.strip() for m in os.getenv(
    "GEMINI_MODEL_FALLBACKS",
    "gemini-flash-latest,gemini-3.1-flash-lite,gemini-2.5-flash"
).split(",") if m.strip()]

# Общий лимит ОДНОВРЕМЕННЫХ запросов к Gemini — на весь процесс, а не на одну
# задачу. Каждая задача сама по себе уже параллелит распознавание/перевод/поиск
# в несколько потоков (см. extract.py/enrich.py); без общего лимита при нескольких
# задачах сразу (см. jobs.py, JOB_WORKERS) число одновременных запросов к
# Gemini ничем не ограничено. Требует пересоздания при смене переменной
# окружения в уже запущенном процессе — GEMINI_MAX_CONCURRENT читается один раз.
GEMINI_MAX_CONCURRENT = max(1, int(os.getenv("GEMINI_MAX_CONCURRENT", "10")))
_GEMINI_SEM = threading.Semaphore(GEMINI_MAX_CONCURRENT)


def _post(model: str, payload: dict, timeout: int = 180) -> dict:
    if not API_KEY:
        raise RuntimeError("Не задан GEMINI_API_KEY — укажите свой ключ Google AI Studio в переменных окружения")
    url = f"{BASE_URL}/models/{model}:generateContent"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "x-goog-api-key": API_KEY,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with _GEMINI_SEM, urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _client_error_no_retry(e: "urllib.error.HTTPError") -> bool:
    """4xx, кроме 429 и 404, — запрос сам по себе плохой (битые данные, неверный
    ключ); повтор/смена модели не изменит результат, только тратит время и
    вызовы. 429 — временное превышение лимита/квоты, 404 — модель снята с
    поддержки (как случилось с gemini-2.5-flash); оба лечатся переключением
    на следующую модель в MODEL_FALLBACKS, см. _request."""
    return 400 <= e.code < 500 and e.code not in (404, 429)


def _model_chain(model: str) -> list:
    """Основная модель первой, затем запасные из MODEL_FALLBACKS (без дублей)."""
    chain = [model]
    for m in MODEL_FALLBACKS:
        if m not in chain:
            chain.append(m)
    return chain


def _retry_delay(attempt: int, e=None) -> float:
    """Задержка перед повтором. Для 429 — по заголовку Retry-After, если он есть."""
    if isinstance(e, urllib.error.HTTPError) and e.code == 429:
        header = e.headers.get("Retry-After") if e.headers else None
        if header:
            try:
                return min(max(float(header), 1), 60)
            except ValueError:
                pass
    return 2 + attempt


def _text_from_response(d: dict) -> str:
    """Склеивает текстовые части первого кандидата ответа."""
    candidates = d.get("candidates") or []
    if not candidates:
        return ""
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


def chat(messages: list, temperature: float = 0.3, max_tokens: int = 12000,
         model: str = None, timeout: int = 240, max_retries: int = 3) -> str:
    """Вызов generateContent. messages — как в OpenAI (system/user), но у нас
    всегда ровно одна системная и одна пользовательская реплика (без истории
    диалога) — поэтому просто разносим их по systemInstruction/contents, а не
    тащим полноценный chat-формат, который тут не нужен. Возвращает текст
    ответа."""
    system_text = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
    user_text = "\n\n".join(m["content"] for m in messages if m.get("role") != "system")
    payload = {
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    if system_text:
        payload["systemInstruction"] = {"parts": [{"text": system_text}]}
    d = _request(model or MODEL, payload, timeout, max_retries, "AI chat failed")
    content = _text_from_response(d)
    if not content.strip():
        raise RuntimeError("AI chat failed: empty content")
    return content


def chat_vision(system: str, user_text: str, images: list, model: str = None,
                temperature: float = 0.1, max_tokens: int = 16000,
                timeout: int = 300, max_retries: int = 3) -> str:
    """Vision-запрос: системный промпт + текст + список PNG-байтов. Возвращает content."""
    parts = [{"text": user_text}]
    for img in images:
        parts.append({"inline_data": {"mime_type": "image/png",
                                      "data": base64.b64encode(img).decode()}})
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    d = _request(model or MODEL, payload, timeout, max_retries, "AI vision failed")
    content = _text_from_response(d)
    if not content.strip():
        raise RuntimeError("AI vision failed: empty content")
    return content


def _request(model: str, payload: dict, timeout: int, max_retries: int, err_prefix: str) -> dict:
    """Запрос с повторами и автопереключением модели: 429 (лимит/квота
    запросов или токенов), 404 (модель снята с поддержки) или таймаут
    (модель перегружена и не успевает ответить) после исчерпания попыток на
    текущей модели — пробуем следующую из MODEL_FALLBACKS, а не сразу
    поднимаем ошибку. Остальные 4xx (битый запрос, неверный ключ) сменой
    модели не лечатся — поднимаются сразу."""
    last_err = None
    chain = _model_chain(model)
    for idx, model_name in enumerate(chain):
        switchable = False
        for attempt in range(max_retries):
            try:
                d = _post(model_name, payload, timeout=timeout)
                if "error" in d:
                    last_err = d["error"]
                    time.sleep(2)
                    continue
                return d
            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code}: {e.read().decode('utf-8')[:200]}"
                if e.code == 404:
                    switchable = True
                    break  # модель недоступна — повторять на ней же бессмысленно
                if _client_error_no_retry(e):
                    raise RuntimeError(f"{err_prefix}: {last_err}") from e
                switchable = e.code == 429
                time.sleep(_retry_delay(attempt, e))
                continue
            except TimeoutError as e:
                # модель перегружена и не укладывается в timeout — та же
                # природа проблемы, что и 429, стоит попробовать другую модель
                last_err = f"таймаут: {e}"
                switchable = True
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                switchable = False
            time.sleep(2 + attempt)
        if not switchable:
            break  # обычная ошибка — сменой модели не лечится, дальше по цепочке не идём
        if idx + 1 < len(chain):
            logger.warning("модель %s недоступна (%s) — переключаюсь на %s",
                           model_name, last_err, chain[idx + 1])
    raise RuntimeError(f"{err_prefix}: {last_err}")


def search(query: str, depth: str = "basic", timeout: int = 90) -> dict:
    """Веб-поиск через встроенный Google Search grounding модели (tools:
    [{"google_search": {}}]) — отдельного поискового API у Google AI Studio
    нет, поиск встроен прямо в generateContent. Возвращает
    {answer, results:[{url,title,content}]}; content у Gemini-поиска пустой —
    сниппетов страниц grounding не отдаёт, только заголовок и ссылку (сама
    выжимка — в answer, она уже используется отдельно в enrich.py)."""
    payload = {
        "contents": [{"role": "user", "parts": [{"text": query}]}],
        "tools": [{"google_search": {}}],
    }
    try:
        d = _request(MODEL, payload, timeout, max_retries=2, err_prefix="AI search failed")
        answer = _text_from_response(d)
        candidates = d.get("candidates") or []
        chunks = ((candidates[0].get("groundingMetadata") or {}).get("groundingChunks") or
                  []) if candidates else []
        results = []
        for c in chunks[:6]:
            web = c.get("web") or {}
            if web.get("uri"):
                results.append({"url": web["uri"], "title": web.get("title", ""), "content": ""})
        return {"answer": answer, "results": results}
    except Exception as e:  # noqa: BLE001
        return {"answer": "", "results": [], "error": str(e)}


def image_search(query: str, count: int = 5, timeout: int = 30) -> list:
    """Нет эквивалента у Google AI Studio (картиночный поиск товара реализован
    только для Yandex, см. yandex.image_search) — пустой список, enrich.py
    просто не находит фото для этого провайдера, как и раньше до этой функции."""
    return []


def extract_json(text: str):
    """Достаёт JSON (массив или объект) из ответа модели, срезая ```-ограждения."""
    if not text:
        return None
    t = text.strip()
    # срезаем markdown-ограждение ```json ... ```
    if t.startswith("```"):
        t = t.split("```", 2)
        t = t[1] if len(t) > 1 else text
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    t = t.strip()
    # прямой разбор
    try:
        return json.loads(t)
    except Exception:
        pass
    # поиск массива
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
    # СПАСЕНИЕ обрезанного массива: собираем все ЦЕЛЫЕ объекты верхнего уровня
    objs = _salvage_objects(t)
    if objs:
        return objs
    return None


def _salvage_objects(t: str):
    """Из (возможно обрезанного) текста вытаскивает список целых JSON-объектов."""
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
