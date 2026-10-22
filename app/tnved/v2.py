"""
Классификатор ТН ВЭД v2 (Фаза 2): атрибуты -> кандидаты-позиции -> выбор
позиции -> выбор листа -> вычисляемая уверенность / отказ при нехватке данных.

Шаги на товар:
  1. Атрибуты (LLM, батчами): что это, материал, назначение, обработка,
     комплектность, параметры, розничная упаковка, недостающие атрибуты и
     подсказки позиций по знаниям модели (heading_hints, проверяются по справочнику).
  2. Кандидаты-позиции (4 знака): текстовый поиск по документам позиций
     (tnved/headings.py) + подсказки LLM, существующие в справочнике.
  3. Выбор позиции (LLM): из кандидатов, с примечаниями к группам кандидатов;
     ответ — позиция, ссылка на правило, одно предложение, недостающие атрибуты.
  4. Выбор листа (LLM): из листьев выбранной позиции с путём подпозиций.
  5. Уверенность и отказ — код (tnved/confidence.py), не LLM.

Скрытые рассуждения у модели не запрашиваются. Выбранные коды проверяются по
справочнику: код вне списка кандидатов не принимается.

Результат — dict контракта classify (code, description, confidence, reason,
needs_review) + расширения: missing_attributes, rule_ref, heading, score,
proposed_code (код, который был бы выдан без отказа — для калибровки), failed.
"""
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import tnved_classifier as tc  # noqa: E402
from tnved import confidence as C  # noqa: E402
from tnved import feedback as F  # noqa: E402
from tnved import headings as H  # noqa: E402

log = logging.getLogger("tnved.v2")

_ATTR_SYSTEM = (
    "Ты помогаешь таможенному брокеру. По названию и описанию товара извлеки признаки, "
    "влияющие на классификацию по ТН ВЭД. Ничего не выдумывай: если признак не следует "
    "из данных, ставь null и добавляй его название в missing_attributes. Ответь строго "
    'JSON-массивом [{"id":0,"what":"...","material":null,"purpose":null,"process":null,'
    '"completeness":null,"parameters":null,"retail_packaging":null,'
    '"missing_attributes":["..."],"heading_hints":["8536"]}]. '
    "what — общее название товара по-русски (1-4 слова, термин таможенной номенклатуры, "
    "если он очевиден из названия; без сведений, которых нет в данных). "
    "missing_attributes — только существенные для выбора кода признаки, которых нет в "
    "данных (например вид топлива, мощность, напряжение, назначение, материал, "
    "комплектность), максимум 4. heading_hints — до 3 четырёхзначных кодов товарных "
    "позиций ТН ВЭД, к которым по твоим знаниям мог бы относиться товар; пустой список, "
    "если не уверен. Без текста вне JSON."
)

_HEADING_SYSTEM = (
    "Ты — классификатор по ТН ВЭД ЕАЭС. Выбери товарную позицию (4 знака) из списка "
    "кандидатов — ровно одну, или null, если ни одна не подходит либо данных "
    "недостаточно. Применяй ОПИ 1: текст товарных позиций и примечания к группам; "
    "приведённые примечания учитывай, особенно исключения. "
    "Если приведены подтверждённые решения по похожим товарам (практика заказчика), опирайся на них и укажи rule_ref «прецедент». Ответь строго JSON-объектом "
    '{"heading":"8536","rule_ref":"ОПИ 1","reason":"одно предложение","missing_attributes":[]}. '
    "rule_ref — применённое правило: «ОПИ 1»..«ОПИ 6» или «прим. к группе NN п.K» (только "
    "если такой пункт есть в приведённом тексте). reason — одно предложение. "
    "missing_attributes — какого признака товара не хватило для однозначного выбора. "
    "Без текста вне JSON."
)

_LEAF_SYSTEM = (
    "Ты — классификатор по ТН ВЭД ЕАЭС. Товарная позиция уже выбрана. Выбери 10-значный "
    "код из списка листьев — ровно один, или null, если данных недостаточно для выбора. "
    "Листья даны с путём подпозиций; «прочие» выбирай, только если ни один конкретный "
    "лист не подходит. "
    "Если приведены подтверждённые решения по похожим товарам (практика заказчика), опирайся на них и укажи rule_ref «прецедент». Ответь строго JSON-объектом "
    '{"code":"8536490000","rule_ref":"ОПИ 1","reason":"одно предложение","missing_attributes":[]}. '
    "rule_ref — «ОПИ 1»..«ОПИ 6». missing_attributes — какого признака товара не хватило. "
    "Без текста вне JSON."
)

_EMPTY_ATTRS = {"what": None, "material": None, "purpose": None, "process": None,
                "completeness": None, "parameters": None, "retail_packaging": None,
                "missing_attributes": [], "heading_hints": []}


def _chat_json(messages: list, kind: type, attempts: int = 3):
    for attempt in range(attempts):
        try:
            parsed = tc.ai_client.extract_json(
                tc.ai_client.chat(messages, temperature=0.1, max_tokens=3000))
        except Exception as e:  # сеть/ключ/квота — повторяем, затем сбой
            log.warning("v2 LLM call failed (attempt %d): %s", attempt + 1, e)
            continue
        if isinstance(parsed, kind):
            return parsed
        log.warning("v2 LLM returned %r instead of %s (attempt %d)", type(parsed), kind.__name__, attempt + 1)
    return None


def _product_block(item: dict, attrs: dict) -> str:
    lines = [f"Название: {item['product_name']}"]
    if item.get("name_zh"):
        lines.append(f"Название (кит.): {item['name_zh']}")
    if tc.clean_material(item.get("material")):
        lines.append(f"Материал: {item['material']}")
    if item.get("description"):
        lines.append(f"Описание: {item['description']}")
    if item.get("product_url"):
        lines.append(f"Ссылка на товар: {item['product_url']}")
    shown = {k: v for k, v in attrs.items()
             if k in ("what", "material", "purpose", "process", "completeness", "parameters",
                      "retail_packaging") and v}
    if shown:
        lines.append("Извлечённые признаки: " + "; ".join(f"{k}={v}" for k, v in shown.items()))
    return "\n".join(lines)


def extract_attributes(items: list[dict], batch: int = 5) -> list[dict | None]:
    out: list[dict | None] = [None] * len(items)
    for start in range(0, len(items), batch):
        chunk = items[start:start + batch]
        text = "\n\n".join(
            f"### Товар {i}\n" + _product_block(it, {}) for i, it in enumerate(chunk))
        parsed = _chat_json([{"role": "system", "content": _ATTR_SYSTEM},
                             {"role": "user", "content": text}], list)
        for entry in parsed or []:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), int):
                continue
            if 0 <= entry["id"] < len(chunk):
                attrs = dict(_EMPTY_ATTRS)
                attrs.update({k: entry.get(k) for k in _EMPTY_ATTRS if k in entry})
                for k in ("missing_attributes", "heading_hints"):
                    if not isinstance(attrs[k], list):
                        attrs[k] = []
                out[start + entry["id"]] = attrs
    return out


def _precedent_text(prec: list[dict]) -> str:
    if not prec:
        return ""
    return ("\n\nПодтверждённые решения по похожим товарам:\n" + "\n".join(
        f"  «{p['name']}» -> {p['code']}" for p in prec))


def _as_list(v) -> list[str]:
    return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []


def _classify_one(item: dict, attrs: dict | None, cfg: dict) -> dict:
    lim = cfg["limits"]
    attrs = attrs or dict(_EMPTY_ATTRS)
    conn = tc._connect()
    try:
        query = f"{item['product_name']} {attrs.get('what') or ''}"
        retrieved = H.search(conn, query, lim["retrieval_top"])
        prec = F.similar(conn, item['product_name'], lim['precedents'], item.get('_exclude_codes', ()))
        prec_heads = [p['code'][:4] for p in prec if H.heading_info(conn, p['code'][:4])]
        hints = [h for h in (str(x).strip() for x in _as_list(attrs.get("heading_hints")))
                 if len(h) == 4 and h.isdigit() and H.heading_info(conn, h)][:3]
        retrieved_codes = [h for h, _ in retrieved]
        cand_codes = []
        for h in prec_heads + retrieved_codes + hints:
            if h not in cand_codes:
                cand_codes.append(h)
        cand_codes = cand_codes[:lim["heading_candidates"]]
        missing = _as_list(attrs.get("missing_attributes"))
        base = {"code": "", "description": "", "confidence": "low", "needs_review": True,
                "missing_attributes": missing, "rule_ref": "", "heading": "", "score": 0.0,
                "proposed_code": "", "failed": False, "hints": hints, "retrieved": cand_codes,
                "precedents": prec, "attributes": attrs}
        if not cand_codes:
            return {**base, "reason": "нет кандидатов-позиций в справочнике"}

        # --- выбор позиции ---
        infos = [H.heading_info(conn, h) for h in cand_codes]
        notes, seen_groups = [], []
        for inf in infos:
            if inf["group"] not in seen_groups and len(seen_groups) < lim["notes_groups"]:
                seen_groups.append(inf["group"])
                text = H.group_notes(conn, inf["group"], lim["notes_chars"])
                if text:
                    notes.append(f"Примечания к группе {inf['group']} (начало): {text}")
        cand_text = "\n".join(
            f"  [{i}] {inf['heading']} — {inf['title'][:170] or '(название отсутствует в справочнике)'} "
            f"[группа {inf['group']}]" for i, inf in enumerate(infos))
        user = (_product_block(item, attrs) + "\n\nКандидаты (товарные позиции):\n" + cand_text
                + _precedent_text(prec) + ("\n\n" + "\n\n".join(notes) if notes else ""))
        res = _chat_json([{"role": "system", "content": _HEADING_SYSTEM},
                          {"role": "user", "content": user}], dict)
        if res is None:
            return {**base, "failed": True, "reason": "временный сбой классификации, повторите запуск"}
        heading = str(res.get("heading") or "").strip()
        missing += _as_list(res.get("missing_attributes"))
        if heading not in cand_codes:
            return {**base, "missing_attributes": list(dict.fromkeys(missing)),
                    "rule_ref": str(res.get("rule_ref") or ""),
                    "reason": str(res.get("reason") or "позиция не определена")[:300]}

        # --- выбор листа ---
        leaves = H.leaves(conn, heading, query, lim["leaf_cap"])
        leaf_text = "\n".join(
            f"  [{i}] {lf['code']} — " + (f"{lf['path']} / " if lf["path"] else "") + lf["name"]
            for i, lf in enumerate(leaves))
        title = next(i["title"] for i in infos if i["heading"] == heading)
        user = (_product_block(item, attrs) + f"\n\nТоварная позиция {heading}: {title[:170]}\n\n"
                "Листья:\n" + leaf_text + _precedent_text(prec))
        res2 = _chat_json([{"role": "system", "content": _LEAF_SYSTEM},
                           {"role": "user", "content": user}], dict)
        if res2 is None:
            return {**base, "failed": True, "heading": heading,
                    "reason": "временный сбой классификации, повторите запуск"}
        code = str(res2.get("code") or "").strip()
        missing = list(dict.fromkeys(missing + _as_list(res2.get("missing_attributes"))))
        valid = {lf["code"]: lf["name"] for lf in leaves}
        rule = str(res2.get("rule_ref") or res.get("rule_ref") or "")
        reason = str(res2.get("reason") or res.get("reason") or "")[:300]
        if code not in valid:
            return {**base, "heading": heading, "missing_attributes": missing, "rule_ref": rule,
                    "reason": reason or "лист не определён"}

        # --- уверенность (код, не LLM) ---
        scores = [s for _, s in retrieved]
        gap = 1.0 if len(scores) == 1 else (
            (scores[0] - scores[1]) / scores[0] if len(scores) > 1 and scores[0] > 0 else 0.0)
        signals = {"rank": (retrieved_codes.index(heading) + 1) if heading in retrieved_codes else None,
                   "hint": heading in hints, "gap": gap, "missing": len(missing),
                   "precedent": any(p["code"] == code for p in prec[:2])}
        conf = C.compute(signals, cfg)
        out = {**base, "heading": heading, "proposed_code": code, "description": valid[code],
               "rule_ref": rule, "reason": reason, "missing_attributes": missing,
               "score": conf["score"], "confidence": conf["level"], "signals": signals}
        if conf["abstain"]:
            out.update(code="", description="", confidence="low", needs_review=True,
                       reason=f"отказ: мало данных/уверенности (score={conf['score']}); " + reason)
        else:
            out.update(code=code, needs_review=conf["level"] != "high")
        return out
    finally:
        conn.close()


def classify_items(items: list[dict], cfg: dict | None = None) -> list[dict]:
    """items: [{product_name, description, material}] -> список результатов v2."""
    cfg = cfg or C.load_config()
    attrs = extract_attributes(items)
    with ThreadPoolExecutor(max_workers=cfg["limits"]["workers"]) as ex:
        return list(ex.map(lambda p: _classify_one(p[0], p[1], cfg), zip(items, attrs)))
