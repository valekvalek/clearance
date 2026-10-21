"""
Определение кода ТН ВЭД ЕАЭС для товара: гибридная схема FTS5 + LLM.

Шаг 1. FTS5-поиск по app/tnved.db (встроенная токенизация unicode61 —
        регистронезависимый поиск, снятие диакритики) -> топ-15 кандидатов.
Шаг 2. LLM (ai_client, тот же провайдер, что и остальной проект) выбирает
        РОВНО ОДИН код из списка кандидатов или отвечает "не определён".
Шаг 3. Валидация: выбранный код должен существовать в tnved_codes. Если нет —
        код не принимается, needs_review=true, confidence=low.
Угадывать код при отсутствии кандидатов или ответе "не определён" запрещено —
ячейка остаётся пустой с пометкой на ручную проверку.

Результаты кэшируются в tnved.db (таблица classify_cache) по нормализованному
названию товара — повторный classify_batch() не делает новых вызовов LLM.
"""
import json
import logging
import os
import re
import sqlite3
import threading

import ai_client

logger = logging.getLogger("tnved_classifier")

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "tnved.db")
TOP_K = 15
# Товаров на один вызов LLM. При 15 кандидатах на товар (+ контекст позиции)
# батч из 10 товаров (~47K символов промпта) на практике иногда вызывает
# отказ модели или обрезание ответа по max_tokens; 5 товаров (~21K символов)
# работает стабильно в тестах на реальных данных.
BATCH_SIZE = 5

_LOCK = threading.Lock()
_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS classify_cache (
    norm_key TEXT PRIMARY KEY,
    code TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    confidence TEXT NOT NULL DEFAULT 'low',
    reason TEXT NOT NULL DEFAULT '',
    needs_review INTEGER NOT NULL DEFAULT 1
);
"""


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(_CACHE_SCHEMA)
    return conn


def normalize(text: str) -> str:
    """Нормализация названия товара для ключа кэша: нижний регистр, схлопнутые
    пробелы, без пунктуации. Лемматизации/стемминга в проекте нет — не
    добавляем новую зависимость без согласования."""
    text = (text or "").lower().strip()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _stem(word: str) -> str:
    """Грубая эвристика без словаря: обрезаем 1-2 последних символа, чтобы
    запрос в ЕДИНСТВЕННОМ числе (как в названии товара из инвойса, напр.
    "куртка") находил записи справочника во МНОЖЕСТВЕННОМ ("куртки") или с
    другим падежным окончанием через FTS5-префикс. Настоящего стемминга/
    лемматизации в проекте нет (см. docstring нормализации) — это заведомо
    приблизительная замена, часть кандидатов она не найдёт и переизбыточных
    совпадений не исключает; LLM-шаг ниже всё равно выбирает только из
    реально найденных кандидатов, так что ложные совпадения не попадают в
    итоговый код, а только риск — не найти верный кандидат вовсе."""
    if len(word) > 5:
        return word[:-2]
    if len(word) > 3:
        return word[:-1]
    return word


def _fts_candidates(conn, query: str, limit: int = TOP_K) -> list[dict]:
    q = normalize(query)
    if not q:
        return []
    # OR, а не AND: названия из инвойса часто содержат смесь русских слов и
    # иностранных брендов/моделей ("светильник rexin brick") — в русском
    # справочнике бренда нет, и AND-запрос по ВСЕМ словам не даст ни одного
    # совпадения. OR + ранжирование по bm25 находит лучшее совпадение по
    # смысловым (русским) словам, игнорируя "шумовые" непонятные токены.
    terms = [f'"{_stem(w)}"*' for w in q.split() if w]
    if not terms:
        return []
    match = " OR ".join(terms)
    try:
        rows = conn.execute(
            "SELECT code, name, group_name, heading_name, bm25(tnved_fts) AS rank "
            "FROM tnved_fts WHERE tnved_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        # запрос с одними стоп-словами/символами может не распарситься FTS5 — не кандидат
        return []
    return [
        {"code": r[0], "name": r[1], "group_name": r[2], "heading_name": r[3]}
        for r in rows
    ]


def _code_exists(conn, code: str) -> str | None:
    row = conn.execute("SELECT name FROM tnved_codes WHERE code = ?", (code,)).fetchone()
    return row[0] if row else None


_SYSTEM_PROMPT = (
    "Ты — классификатор товаров по ТН ВЭД ЕАЭС (10-значные коды). Для каждого "
    "товара тебе дан список кандидатов (реальные коды из официального "
    "классификатора) с наименованиями. Выбери РОВНО ОДИН код из списка "
    "кандидатов для этого товара, или верни null, если ни один кандидат явно "
    "не подходит. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО придумывать код, которого нет в "
    "списке кандидатов для данного товара. Ответь строго JSON-массивом вида "
    '[{"id": 0, "code": "0101210000", "confidence": "high", "reason": "..."}, '
    '{"id": 1, "code": null, "confidence": "low", "reason": "нет подходящего кандидата"}]. '
    "id — порядковый номер товара из запроса (с нуля). confidence — одно из "
    "high/medium/low. reason — одна короткая фраза на русском. Без текста "
    "вне JSON."
)


def _build_user_prompt(batch: list[dict]) -> str:
    lines = []
    for i, item in enumerate(batch):
        lines.append(f"### Товар {i}")
        lines.append(f"Название: {item['product_name']}")
        if item.get("material"):
            lines.append(f"Материал: {item['material']}")
        if item.get("description"):
            lines.append(f"Описание: {item['description']}")
        if item["candidates"]:
            lines.append("Кандидаты:")
            for ci, cand in enumerate(item["candidates"]):
                # Наименование подсубпозиции в ТН ВЭД часто — лишь обрывок
                # фразы родительской позиции ("- с питанием от..."), поэтому
                # ВСЕГДА добавляем heading_name (название товарной позиции,
                # 4 знака) как контекст — без него можно спутать, например,
                # локомотивы и светильники, у которых совпал фрагмент текста.
                heading = cand.get("heading_name") or ""
                ctx = f" [{heading}]" if heading else ""
                lines.append(f"  [{ci}] {cand['code']} — {cand['name']}{ctx}")
        else:
            lines.append("Кандидаты: нет")
        lines.append("")
    return "\n".join(lines)


def _classify_llm_batch(conn, batch: list[dict]) -> tuple[dict, set]:
    """batch: список {product_name, description, candidates}. Возвращает
    (results, failed_keys).
    results — {_key: {code, confidence, reason, needs_review}} для товаров,
    у которых были кандидаты и LLM дала разбираемый ответ (без кандидатов —
    LLM не вызывается вовсе, см. classify_batch).
    failed_keys — _key товаров из батчей, где сам вызов LLM или разбор JSON
    провалились технически (не бизнес-ответ "не определён", а ИНФРАСТРУКТУРНЫЙ
    сбой — нет сети/ключа/квоты). Такие товары НЕЛЬЗЯ кэшировать как
    "не определён": иначе временный сбой навсегда похоронит товар без кода,
    даже когда LLM снова доступна (см. classify_batch)."""
    to_ask = [b for b in batch if b["candidates"]]
    if not to_ask:
        return {}, set()
    results = {}
    failed_keys = set()
    for start in range(0, len(to_ask), BATCH_SIZE):
        chunk = to_ask[start:start + BATCH_SIZE]
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(chunk)},
        ]
        # Наблюдалась недетерминированная "ложная" отказная реплика модели
        # ("Я не могу обсуждать эту тему...") на отдельных батчах — тот же
        # набор товаров поштучно проходит нормально. Один повтор запроса
        # устраняет это в тестах; если не помогает — это инфраструктурный
        # сбой батча (не кэшируется, см. docstring выше).
        parsed = None
        for attempt in range(3):
            try:
                raw = ai_client.chat(messages, temperature=0.1, max_tokens=4000)
                parsed = ai_client.extract_json(raw)
            except Exception as e:
                logger.warning("tnved LLM batch call failed (attempt %d): %s", attempt + 1, e)
                continue
            if isinstance(parsed, list):
                break
            logger.warning("tnved LLM returned non-list JSON (attempt %d): %r", attempt + 1, parsed)
            parsed = None
        if not isinstance(parsed, list):
            failed_keys.update(item["_key"] for item in chunk)
            continue
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            idx = entry.get("id")
            if not isinstance(idx, int) or idx < 0 or idx >= len(chunk):
                continue
            item = chunk[idx]
            code = entry.get("code")
            confidence = entry.get("confidence") if entry.get("confidence") in (
                "high", "medium", "low") else "low"
            reason = str(entry.get("reason") or "")[:300]
            if code:
                name = _code_exists(conn, str(code))
                if name is None:
                    # LLM выбрала код не из списка кандидатов — не принимаем
                    results[item["_key"]] = {
                        "code": "", "description": "", "confidence": "low",
                        "reason": "LLM вернула код вне списка кандидатов",
                        "needs_review": True,
                    }
                else:
                    results[item["_key"]] = {
                        "code": str(code), "description": name,
                        "confidence": confidence, "reason": reason or "выбрано LLM",
                        "needs_review": confidence != "high",
                    }
            else:
                results[item["_key"]] = {
                    "code": "", "description": "", "confidence": "low",
                    "reason": reason or "LLM: код не определён",
                    "needs_review": True,
                }
        # LLM ответила, но ПРОПУСТИЛА кого-то из товаров чанка (например,
        # обрезался ответ по max_tokens) — тоже не кэшируем, это не её
        # осознанное решение "не определён", а недополученный ответ.
        for item in chunk:
            if item["_key"] not in results:
                failed_keys.add(item["_key"])
    return results, failed_keys


def classify(product_name: str, description: str | None = None) -> dict:
    """Классификация одного товара. Для массовой обработки используйте
    classify_batch — он не делает отдельный вызов LLM на каждую строку."""
    res = classify_batch([{"product_name": product_name, "description": description}])
    return res[0]


def classify_batch(items: list[dict]) -> list[dict]:
    """items: список {"product_name": str, "description": str|None,
    "material": str|None}. material — опционально, если есть у вызывающей
    стороны (после enrich.py), используется и в FTS-поиске кандидатов, и в
    промпте LLM — материал часто прямо указывает на товарную позицию.
    Возвращает список dict (тот же порядок): code, description, confidence,
    reason, needs_review. Кэш — по normalize(product_name); повторный вызов
    для уже закэшированных названий НЕ обращается к LLM."""
    if not items:
        return []
    with _LOCK:
        conn = _connect()
        try:
            keys = [normalize(it["product_name"]) for it in items]
            out = [None] * len(items)
            to_classify = []  # (index, key, product_name, description)
            for i, (it, key) in enumerate(zip(items, keys)):
                if not key:
                    out[i] = {"code": "", "description": "", "confidence": "low",
                              "reason": "пустое название товара", "needs_review": True}
                    continue
                cached = conn.execute(
                    "SELECT code, description, confidence, reason, needs_review "
                    "FROM classify_cache WHERE norm_key = ?", (key,),
                ).fetchone()
                if cached:
                    out[i] = {
                        "code": cached[0], "description": cached[1],
                        "confidence": cached[2], "reason": cached[3],
                        "needs_review": bool(cached[4]),
                    }
                else:
                    to_classify.append((i, key, it["product_name"], it.get("description"),
                                        it.get("material")))

            if to_classify:
                batch = []
                for i, key, name, desc, material in to_classify:
                    # материал — сильный сигнал для поиска кандидатов (текстиль/
                    # металл/пластик обычно прямо определяют товарную позицию),
                    # добавляем его к тексту FTS-запроса вместе с названием
                    search_text = f"{name} {material}" if material else name
                    cands = _fts_candidates(conn, search_text)
                    batch.append({
                        "_key": key, "product_name": name, "description": desc,
                        "material": material, "candidates": cands,
                    })
                llm_results, failed_keys = _classify_llm_batch(conn, batch)
                to_cache = []
                for i, key, name, desc, material in to_classify:
                    b = llm_results.get(key)
                    has_cands = next(x for x in batch if x["_key"] == key)["candidates"]
                    if b is None:
                        b = {
                            "code": "", "description": "", "confidence": "low",
                            "reason": ("нет кандидатов в справочнике" if not has_cands
                                      else "временный сбой классификации, повторите запуск"),
                            "needs_review": True,
                        }
                    out[i] = b
                    # НЕ кэшируем инфраструктурные сбои (key в failed_keys) —
                    # иначе временная ошибка LLM навсегда закэшируется как
                    # "не определён", и повторный (успешный) запуск не
                    # переклассифицирует этот товар. "Нет кандидатов" и
                    # осознанный ответ LLM "не определён" — кэшируем как обычно.
                    if key not in failed_keys:
                        to_cache.append((key, b["code"], b["description"], b["confidence"],
                                         b["reason"], int(b["needs_review"])))
                if to_cache:
                    conn.executemany(
                        "INSERT OR REPLACE INTO classify_cache"
                        "(norm_key,code,description,confidence,reason,needs_review) "
                        "VALUES (?,?,?,?,?,?)", to_cache,
                    )
                    conn.commit()
            return out
        finally:
            conn.close()
