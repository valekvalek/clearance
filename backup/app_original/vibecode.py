"""
Клиент VibeCode AI (Bitrix24) — OpenAI-совместимый чат + веб-поиск.
Только stdlib (urllib), чтобы держать образ лёгким.
"""
import os
import json
import time
import base64
import urllib.request
import urllib.error

BASE_URL = os.getenv("VIBE_BASE_URL", "https://vibecode.bitrix24.tech/v1")
SEARCH_URL = os.getenv("VIBE_SEARCH_URL", "https://vibecode.bitrix24.tech/v1/search")
# Ключ VibeCode (скоуп AI) задаётся только через env VIBE_API_KEY.
API_KEY = os.getenv("VIBE_API_KEY", "").strip()
# Модель обогащения/перевода. По умолчанию быстрая 5.5 (не-thinking) —
# в разы быстрее и дешевле при отличном качестве перевода.
# Можно вернуть 'bitrix/bitrixgpt-5.5-thinking' через env AI_MODEL.
MODEL = os.getenv("AI_MODEL", "bitrix/bitrixgpt-5.5")
SEARCH_PROVIDER = os.getenv("VIBE_SEARCH_PROVIDER", "bitrix-search")


def _post(url: str, payload: dict, timeout: int = 180) -> dict:
    if not API_KEY:
        raise RuntimeError("Не задан VIBE_API_KEY — укажите свой ключ VibeCode в переменных окружения")
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
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def chat(messages: list, temperature: float = 0.3, max_tokens: int = 12000,
         model: str = None, timeout: int = 240, max_retries: int = 3) -> str:
    """Вызов chat/completions. Возвращает текст ответа (content)."""
    model = model or MODEL
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    last_err = None
    for attempt in range(max_retries):
        try:
            d = _post(f"{BASE_URL}/chat/completions", payload, timeout=timeout)
            if "error" in d:
                last_err = d["error"]
                time.sleep(2)
                continue
            content = d["choices"][0]["message"].get("content") or ""
            if content.strip():
                return content
            last_err = "empty content"
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read().decode('utf-8')[:200]}"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        time.sleep(2 + attempt)
    raise RuntimeError(f"AI chat failed: {last_err}")


def chat_vision(system: str, user_text: str, images: list, model: str = None,
                temperature: float = 0.1, max_tokens: int = 16000,
                timeout: int = 300, max_retries: int = 3) -> str:
    """Vision-запрос: системный промпт + текст + список PNG-байтов. Возвращает content."""
    model = model or MODEL
    content = [{"type": "text", "text": user_text}]
    for img in images:
        b64 = base64.b64encode(img).decode()
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"}})
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": content}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    last_err = None
    for attempt in range(max_retries):
        try:
            d = _post(f"{BASE_URL}/chat/completions", payload, timeout=timeout)
            if "error" in d:
                last_err = d["error"]
                time.sleep(2)
                continue
            c = d["choices"][0]["message"].get("content") or ""
            if c.strip():
                return c
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read().decode('utf-8')[:200]}"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        time.sleep(2 + attempt)
    raise RuntimeError(f"AI vision failed: {last_err}")


def search(query: str, depth: str = "basic", timeout: int = 90) -> dict:
    """Веб-поиск через VibeCode. Возвращает {answer, results:[{url,title,content}]}."""
    payload = {"query": query, "provider": SEARCH_PROVIDER, "search_depth": depth}
    try:
        d = _post(SEARCH_URL, payload, timeout=timeout)
        return {
            "answer": d.get("answer") or "",
            "results": [
                {
                    "url": r.get("url", ""),
                    "title": r.get("title", ""),
                    "content": (r.get("content") or "")[:400],
                }
                for r in (d.get("results") or [])[:6]
            ],
        }
    except Exception as e:  # noqa: BLE001
        return {"answer": "", "results": [], "error": str(e)}


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
