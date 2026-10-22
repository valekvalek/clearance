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
# Официальная выгрузка ФНС (tnved.db, см. build_tnved_db.py) с 2017 года
# содержит ТОЛЬКО 10-значные листовые коды в виде обрывков фразы
# ("- - - - прочие") — текст промежуточных подпозиций (6/8 знаков: "лампы
# узконаправленного света", "из недрагоценных металлов" и т.п.) в исходных
# файлах ФНС отсутствует физически (проверено вручную на архиве classifer'а,
# см. историю коммита). Без этого контекста LLM видит только общее название
# товарной позиции (4 знака) + обрывок листа и путает соседние подпозиции
# (пример: PWM-светильник улетел в "неэлектрические" 9405.50, подводный
# линейный LED-светильник — в "потолочные/настенные" 9405.11 вместо верной
# "лампы узконаправленного света и прочие" 9405.4x).
#
# tnved_subheading_titles — куратируемый вручную (НЕ генерируется автоматически
# из данных ФНС) справочник текстов подпозиций по префиксу кода. Заполняем
# ТОЛЬКО уровнями, в формулировке которых уверены по официальной номенклатуре
# ТН ВЭД ЕАЭС — остальное не угадываем, чтобы не плодить новые ошибки вместо
# старых. Не попадает под DROP TABLE в build_tnved_db.py — переживает
# пересборку tnved_codes/tnved_fts.
_SUBHEADING_SCHEMA = """
CREATE TABLE IF NOT EXISTS tnved_subheading_titles (
    prefix TEXT PRIMARY KEY,
    title TEXT NOT NULL
);
"""


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(_CACHE_SCHEMA)
    conn.execute(_SUBHEADING_SCHEMA)
    try:  # расширения результата v2 (missing_attributes, rule_ref, ...) — JSON
        conn.execute("ALTER TABLE classify_cache ADD COLUMN extra_json TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    return conn


def _tree_path_title(conn, code: str) -> str:
    """Путь по дереву подпозиций ТН ВЭД из tnved_tree (импорт стороннего
    скрейпа alta.ru, см. import_tnved_tree.py) — явный обход parent_code от
    родителя кода до корня, тексты от общего к частному. Леафовый узел самого
    code НЕ включается — его обрывок уже отдельно показан как cand['name'].
    Если tnved_tree не импортирована или код в ней отсутствует (например,
    редакция классификатора разошлась с датой скрейпа) — пустая строка, без
    выдумывания; тогда _path_title откатывается на curated-справочник."""
    try:
        row = conn.execute(
            "SELECT parent_code FROM tnved_tree WHERE code = ?", (code,)
        ).fetchone()
    except sqlite3.OperationalError:
        return ""  # таблицы tnved_tree нет — импорт не выполнялся
    if not row:
        return ""
    chain = []
    seen = set()
    parent = row[0]
    while parent and parent not in seen:
        seen.add(parent)
        r = conn.execute(
            "SELECT parent_code, title FROM tnved_tree WHERE code = ?", (parent,)
        ).fetchone()
        if not r:
            break
        title = (r[1] or "").strip().rstrip(":").strip()
        if title:
            chain.append(title)
        parent = r[0]
    chain.reverse()
    return " / ".join(chain)


def _curated_path_title(conn, code: str) -> str:
    """Склеивает тексты всех известных (curated, см. tnved_subheading_seed.py)
    предков кода по длине префикса от короткого к длинному, напр.
    '9405' -> '94051' -> '9405110'. Фолбэк для случаев, когда tnved_tree
    (основной источник, см. _tree_path_title) не покрывает код. Коды без
    curated-предков возвращают пустую строку — деградация до прежнего
    поведения (только heading_name), без выдумывания текста."""
    parts = []
    for ln in range(5, len(code)):
        row = conn.execute(
            "SELECT title FROM tnved_subheading_titles WHERE prefix = ?",
            (code[:ln],),
        ).fetchone()
        if row:
            parts.append(row[0])
    return " / ".join(parts)


def _path_title(conn, code: str) -> str:
    """Текст промежуточных подпозиций для кода: приоритет — полное дерево
    alta.ru (tnved_tree, покрывает все товарные позиции), фолбэк — узкий
    curated-справочник (tnved_subheading_titles, только то, что проверено
    вручную без дерева, см. _curated_path_title)."""
    return _tree_path_title(conn, code) or _curated_path_title(conn, code)


def normalize(text: str) -> str:
    """Нормализация названия товара для ключа кэша: нижний регистр, схлопнутые
    пробелы, без пунктуации. Лемматизации/стемминга в проекте нет — не
    добавляем новую зависимость без согласования."""
    text = (text or "").lower().strip()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clean_material(material) -> str:
    """Материал для классификации: заглушка «уточняется» (её ставит enrich.py,
    когда материал не найден) — не сведения о товаре, отбрасываем."""
    m = (material or "").strip()
    return "" if m.lower() in ("", "уточняется", "не указан", "нет данных") else m


def item_key(it: dict) -> str:
    """Ключ кэша: нормализованное название + (если есть) китайское название,
    материал и описание — один и тот же русский заголовок («Насос») с разной
    информацией о товаре не должен делить результат. Ссылка в ключ не входит.
    Для товара только с названием совпадает с normalize(название)."""
    parts = [normalize(it.get("product_name"))]
    for v in (it.get("name_zh"), clean_material(it.get("material")),
              (it.get("description") or "")[:300]):
        n = normalize(v)
        if n:
            parts.append(n)
    return " | ".join(parts) if parts[0] else ""


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


# Короткие служебные слова (предлоги/союзы), которые ПОЛНОСТЬЮ совпадают с
# частым фрагментом листовых наименований ТН ВЭД вида "- для медицинских
# целей", "- для гражданских воздушных судов" и т.п. — такой фрагмент
# встречается в сотнях кодов по всем товарным позициям классификатора. Без
# фильтрации один OR-терм "для"* в запросе "светильник ДЛЯ фонтана" забивал
# top-TOP_K кандидатов этим медицинским/авиационным мусором и вытеснял
# релевантные "- - - - прочие" коды из выдачи ещё до LLM-шага (найдено на
# реальном кейсе: "Светильник для фонтана PWM" получал в кандидатах только
# "для медицинских целей" и общий 9405.50, без единого подходящего "прочие").
# Список — не лемматизация, просто частотные предлоги/союзы/частицы русского
# языка, которые сами по себе не несут классифицирующего смысла товара.
_STOPWORDS = frozenset((
    "для", "и", "с", "со", "от", "на", "по", "из", "без", "под", "над",
    "при", "об", "о", "к", "ко", "за", "до", "не", "или", "a", "an", "the",
    "for", "with", "and", "or",
))


def _fts_candidates(conn, query: str, limit: int = TOP_K) -> list[dict]:
    q = normalize(query)
    if not q:
        return []
    # OR, а не AND: названия из инвойса часто содержат смесь русских слов и
    # иностранных брендов/моделей ("светильник rexin brick") — в русском
    # справочнике бренда нет, и AND-запрос по ВСЕМ словам не даст ни одного
    # совпадения. OR + ранжирование по bm25 находит лучшее совпадение по
    # смысловым (русским) словам, игнорируя "шумовые" непонятные токены.
    # Стоп-слова исключаем ДО построения термов — см. _STOPWORDS выше.
    terms = [f'"{_stem(w)}"*' for w in q.split() if w and w not in _STOPWORDS]
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
        {"code": r[0], "name": r[1], "group_name": r[2], "heading_name": r[3],
         "path_title": _path_title(conn, r[0])}
        for r in rows
    ]


def _candidates_with_material(conn, name: str, material: str | None,
                              limit: int = TOP_K) -> list[dict]:
    """Кандидаты по названию + (опционально) по названию+материалу, слитые
    так, чтобы материал мог ТОЛЬКО добавить кандидатов, но не мог вытеснить
    найденные по названию. Раньше материал подмешивался В ОДИН bm25-запрос с
    названием — редкое специфичное слово материала (например, "нержавеющая
    сталь") ранжировалось выше общеупотребительного "светильник" и вытесняло
    из топ-15 все реальные кандидаты по освещению в пользу кандидатов по
    металлопрокату. Раздельный поиск + объединение с приоритетом названия
    устраняет это, сохраняя пользу материала (находит коды, которые по
    одному названию не находятся, см. test_fts_candidates_uses_material_hint)."""
    by_name = _fts_candidates(conn, name, limit=limit)
    if not material:
        return by_name
    seen = {c["code"] for c in by_name}
    by_combo = _fts_candidates(conn, f"{name} {material}", limit=limit)
    merged = list(by_name)
    for c in by_combo:
        if c["code"] not in seen and len(merged) < limit:
            merged.append(c)
            seen.add(c["code"])
    return merged


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
        if item.get("name_zh"):
            lines.append(f"Название (кит.): {item['name_zh']}")
        if clean_material(item.get("material")):
            lines.append(f"Материал: {item['material']}")
        if item.get("description"):
            lines.append(f"Описание: {item['description']}")
        if item.get("product_url"):
            lines.append(f"Ссылка на товар: {item['product_url']}")
        if item["candidates"]:
            lines.append("Кандидаты:")
            for ci, cand in enumerate(item["candidates"]):
                # Наименование подсубпозиции в ТН ВЭД часто — лишь обрывок
                # фразы родительской позиции ("- с питанием от..."), поэтому
                # ВСЕГДА добавляем heading_name (название товарной позиции,
                # 4 знака) как контекст — без него можно спутать, например,
                # локомотивы и светильники, у которых совпал фрагмент текста.
                heading = cand.get("heading_name") or ""
                path = cand.get("path_title") or ""
                # path_title (curated, см. _path_title) — текст промежуточных
                # подпозиций между товарной позицией и листовым кодом; когда
                # для префикса кода нет curated-записи, остаётся "" и формат
                # деградирует до прежнего (только heading_name) без выдумывания.
                ctx_parts = [p for p in (heading, path) if p]
                ctx = f" [{' / '.join(ctx_parts)}]" if ctx_parts else ""
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


from tnved_synonyms_seed import EXACT_SYNONYMS, KEYWORD_SYNONYMS

_EXACT_SYNONYMS_BY_KEY = {normalize(s["pattern"]): s for s in EXACT_SYNONYMS}


def _match_synonym(key: str) -> dict | None:
    """Быстрый патч к точности (см. tnved_synonyms_seed.py): проверяется ДО
    похода в FTS/LLM. key — уже normalize()-нормализованное название. Коды
    здесь не гадаются — только устойчивые паттерны с ≥2 независимыми
    подтверждениями из golden-набора (см. docstring tnved_synonyms_seed.py).
    Возвращает dict в формате classify_batch-результата или None."""
    exact = _EXACT_SYNONYMS_BY_KEY.get(key)
    if exact:
        return exact
    words = set(key.split())
    for kw in KEYWORD_SYNONYMS:
        if all(w in key for w in kw["all_of"]) and any(w in key for w in kw["any_of"]):
            return kw
    return None


def classify(product_name: str, description: str | None = None) -> dict:
    """Классификация одного товара. Для массовой обработки используйте
    classify_batch — он не делает отдельный вызов LLM на каждую строку."""
    res = classify_batch([{"product_name": product_name, "description": description}])
    return res[0]


def classify_batch(items: list[dict], use_cache: bool = True,
                   use_synonyms: bool = True, pipeline: str | None = None) -> list[dict]:
    """items: список {"product_name": str, "description": str|None,
    "material": str|None}. material — опционально, если есть у вызывающей
    стороны (после enrich.py), используется и в FTS-поиске кандидатов, и в
    промпте LLM — материал часто прямо указывает на товарную позицию.
    Возвращает список dict (тот же порядок): code, description, confidence,
    reason, needs_review. Кэш — по normalize(product_name); повторный вызов
    для уже закэшированных названий НЕ обращается к LLM.
    use_cache=False / use_synonyms=False — для честной оценки (tnved/evaluate.py):
    без чтения и записи кэша и без таблицы синонимов, собранной из эталона.
    pipeline: "v1" (FTS по листьям + LLM) или "v2" (tnved/v2.py: атрибуты, позиция, лист,
    вычисляемая уверенность); по умолчанию env TNVED_PIPELINE, иначе v1. Результаты v2 кэшируются
    отдельно (ключ с префиксом "v2:") и содержат доп. поля (missing_attributes, rule_ref, ...)."""
    if not items:
        return []
    use_v2 = (pipeline or os.getenv("TNVED_PIPELINE", "v1")) == "v2"
    with _LOCK:
        conn = _connect()
        try:
            name_keys = [normalize(it["product_name"]) for it in items]
            keys = [item_key(it) for it in items]
            out = [None] * len(items)
            to_classify = []  # (index, key, product_name, description)
            for i, (it, key, nkey) in enumerate(zip(items, keys, name_keys)):
                if not key:
                    out[i] = {"code": "", "description": "", "confidence": "low",
                              "reason": "пустое название товара", "needs_review": True}
                    continue
                syn = _match_synonym(nkey) if use_synonyms else None
                if use_synonyms and not syn:  # подтверждённые решения (tnved/feedback.py)
                    from tnved import feedback as _fb
                    ex = _fb.exact(conn, nkey)
                    if ex:
                        syn = {"code": ex[0], "evidence": ex[1]}
                if syn:
                    name = _code_exists(conn, syn["code"])
                    out[i] = {
                        "code": syn["code"], "description": name or "",
                        "confidence": "high", "needs_review": False,
                        "reason": f"синоним (подтверждено {syn['evidence']}x в эталоне)",
                    }
                    continue
                cached = conn.execute(
                    "SELECT code, description, confidence, reason, needs_review, extra_json "
                    "FROM classify_cache WHERE norm_key = ?", (("v2:" + key) if use_v2 else key,),
                ).fetchone() if use_cache else None
                if cached:
                    out[i] = {
                        "code": cached[0], "description": cached[1],
                        "confidence": cached[2], "reason": cached[3],
                        "needs_review": bool(cached[4]),
                    }
                    if cached[5]:
                        out[i].update(json.loads(cached[5]))
                else:
                    to_classify.append((i, key, it["product_name"], it.get("description"),
                                        it.get("material")))

            if to_classify and use_v2:
                from tnved import v2 as _v2
                res = _v2.classify_items([items[i] for i, *_ in to_classify])
                rows = []
                for (i, key, *_), r in zip(to_classify, res):
                    out[i] = r
                    if not r.get("failed"):
                        extra = {k: v for k, v in r.items() if k not in (
                            "code", "description", "confidence", "reason", "needs_review")}
                        rows.append(("v2:" + key, r["code"], r["description"], r["confidence"],
                                     r["reason"], int(r["needs_review"]),
                                     json.dumps(extra, ensure_ascii=False)))
                if rows and use_cache:
                    conn.executemany(
                        "INSERT OR REPLACE INTO classify_cache(norm_key,code,description,confidence,"
                        "reason,needs_review,extra_json) VALUES (?,?,?,?,?,?,?)", rows)
                    conn.commit()
            elif to_classify:
                batch = []
                for i, key, name, desc, material in to_classify:
                    cands = _candidates_with_material(conn, name, clean_material(material))
                    batch.append({
                        "_key": key, "product_name": name, "description": desc,
                        "material": material, "candidates": cands,
                        "name_zh": items[i].get("name_zh"), "product_url": items[i].get("product_url"),
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
                if to_cache and use_cache:
                    conn.executemany(
                        "INSERT OR REPLACE INTO classify_cache"
                        "(norm_key,code,description,confidence,reason,needs_review) "
                        "VALUES (?,?,?,?,?,?)", to_cache,
                    )
                    conn.commit()
            return out
        finally:
            conn.close()
