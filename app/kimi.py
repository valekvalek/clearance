"""
Клиент Kimi API (Moonshot AI) — OpenAI-совместимый Chat Completions API.
Только stdlib (urllib), чтобы держать образ лёгким — как gemini.py/yandex.py.

ВАЖНО про поиск: у Kimi/Moonshot НЕТ собственного веб-поиска или
картиночного поиска в API (проверено по официальной документации
platform.kimi.ai/docs — там только Chat/Responses/Messages API, без
web-search/image-search инструмента). Поэтому search()/image_search() тут —
заглушки без результатов, как и у gemini.image_search для Google AI Studio:
enrich.py при этом провайдере переводит/описывает товар по своим знаниям
модели и данным из инвойса, БЕЗ грунтинга реальными ссылками/фото из сети.
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

BASE_URL = os.getenv("KIMI_BASE_URL", "https://api.moonshot.ai/v1")
# Ключ Kimi API задаётся только через env MOONSHOT_API_KEY (как в офиц. доках).
API_KEY = os.getenv("MOONSHOT_API_KEY", "").strip()
MODEL = os.getenv("AI_MODEL", "kimi-k2.6")

# Запасные модели — пробуются по очереди при HTTP 429/404/таймауте.
MODEL_FALLBACKS = [m.strip() for m in os.getenv(
    "KIMI_MODEL_FALLBACKS", "kimi-k2.6,kimi-k2.5"
).split(",") if m.strip()]

KIMI_MAX_CONCURRENT = max(1, int(os.getenv("KIMI_MAX_CONCURRENT", "10")))
_KIMI_SEM = threading.Semaphore(KIMI_MAX_CONCURRENT)


def _post(payload: dict, timeout: int = 180) -> dict:
    if not API_KEY:
        raise RuntimeError("Не задан MOONSHOT_API_KEY — укажите свой ключ Kimi API в переменных окружения")
    url = f"{BASE_URL}/chat/completions"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with _KIMI_SEM, urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _client_error_no_retry(e: "urllib.error.HTTPError") -> bool:
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
    msg = choices[0].get("message") or {}
    return msg.get("content") or ""


def _base_payload(model: str, max_tokens: int) -> dict:
    """temperature/top_p и т.п. на моделях kimi-* зафиксированы платформой и
    часть моделей (напр. k2.6) даже отвечает HTTP 400 на непредустановленное
    значение — поэтому не отправляем их вовсе, вместо подгонки под каждую
    модель. Для k2.6 (думающая модель по умолчанию) явно выключаем режим
    рассуждений ("thinking": disabled) — иначе весь max_tokens уходит в
    reasoning_content, а не в content, и обычные задачи (перевод/JSON) кратно
    дорожают и долго выполняются без выгоды для качества. k3/k2.7-code либо
    не принимают этот параметр, либо не поддерживают отключение — thinking
    не передаём для них."""
    payload = {"max_tokens": max_tokens}
    if "k2.6" in model:
        payload["thinking"] = {"type": "disabled"}
    return payload


def _request(model: str, payload: dict, timeout: int, max_retries: int, err_prefix: str) -> dict:
    last_err = None
    chain = _model_chain(model)
    for idx, model_name in enumerate(chain):
        payload = {**payload, "model": model_name}
        if "k2.6" in model_name:
            payload["thinking"] = {"type": "disabled"}
        else:
            payload.pop("thinking", None)
        switchable = False
        for attempt in range(max_retries):
            try:
                d = _post(payload, timeout=timeout)
                if "error" in d:
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


def chat(messages: list, temperature: float = 0.3, max_tokens: int = 12000,
         model: str = None, timeout: int = 240, max_retries: int = 3) -> str:
    """temperature принимается только для единого интерфейса с yandex.py/gemini.py
    (extract.py/enrich.py вызывают с этим аргументом) — на моделях kimi-* она
    зафиксирована платформой и не передаётся в запрос, см. _base_payload."""
    m = model or MODEL
    payload = {**_base_payload(m, max_tokens), "messages": messages}
    d = _request(m, payload, timeout, max_retries, "AI chat failed")
    content = _text_from_response(d)
    if not content.strip():
        raise RuntimeError("AI chat failed: empty content")
    return content


def chat_vision(system: str, user_text: str, images: list, model: str = None,
                temperature: float = 0.1, max_tokens: int = 16000,
                timeout: int = 300, max_retries: int = 3) -> str:
    """Vision-запрос: системный промпт + текст + список PNG-байтов, через
    OpenAI-совместимый content-массив с image_url (data URI), который Kimi
    K2.6/K3 поддерживают (мультимодальный вход)."""
    content = [{"type": "text", "text": user_text}]
    for img in images:
        b64 = base64.b64encode(img).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": content})
    m = model or MODEL
    payload = {**_base_payload(m, max_tokens), "messages": messages}
    d = _request(m, payload, timeout, max_retries, "AI vision failed")
    text = _text_from_response(d)
    if not text.strip():
        raise RuntimeError("AI vision failed: empty content")
    return text


def search(query: str, depth: str = "basic", timeout: int = 90) -> dict:
    """Нет эквивалента у Kimi API (см. докстринг модуля) — enrich.py получает
    пустой результат и работает без веб-грунтинга для этого провайдера."""
    return {"answer": "", "results": [],
            "error": "Kimi API не предоставляет веб-поиска — работа без грунтинга из интернета"}


def image_search(query: str, count: int = 5, timeout: int = 30) -> list:
    """Нет эквивалента у Kimi API — как и у gemini.image_search."""
    return []


# extract_json — идентичная логика gemini.py/yandex.py (извлечение JSON из
# ответа модели, включая срез ```-ограждений и спасение обрезанного массива).
def extract_json(text: str):
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
