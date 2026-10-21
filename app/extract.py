"""
ИИ-извлечение строк товара из сетки листов (xlsx/xls) и из скана PDF (vision),
плюс разбор чисел/единиц измерения инвойса.
"""
import os
import re
import logging
from concurrent.futures import ThreadPoolExecutor

import ai_client
import currency
from readers import MAX_ROWS, MAX_VISION_PAGES, read_sheets, _read_pdf

logger = logging.getLogger(__name__)

# Быстрая модель для механического извлечения строк (structured outputs).
# Перевод/понимание товара идёт на основной AI_MODEL (ai_client.MODEL).
# Пусто/не задано -> используется основная модель текущего провайдера
# (ai_client.MODEL), а не захардкоженная модель конкретного провайдера.
EXTRACT_MODEL = os.getenv("EXTRACT_MODEL", "").strip() or None


# ----------------------------------------------------------- ИИ-извлечение
EXTRACT_SYSTEM = """Ты — парсер китайских инвойсов и упаковочных листов (Excel).
Тебе дают СЫРУЮ СЕТКУ листов (координаты ячеек = значения). Раскладка КАЖДЫЙ РАЗ РАЗНАЯ.

Задача: найти строки ТОВАРОВ и вернуть их единым JSON-массивом.
Если есть отдельные листы Invoice и Packing List — ОБЪЕДИНИ их: возьми товары из инвойса,
подтяни вес брутто из упаковочного листа по совпадению артикула/внутреннего кода/наименования.
Игнорируй шапки, реквизиты, итоги (TOTAL/ИТОГО/合计), пустые строки.

ЕСЛИ ЗАГРУЖЕНО НЕСКОЛЬКО ФАЙЛОВ (маркеры «ФАЙЛ 1», «ФАЙЛ 2»): это части одной поставки —
обычно ИНВОЙС (цены, артикулы, наименования) + ПАКИНГ-ЛИСТ (веса, коробки, иногда фото).
ОБЪЕДИНИ их в ОДИН список товаров: бери позиции из инвойса и подтягивай к ним веса/данные из
пакинг-листа по совпадению артикула/внутреннего кода/наименования. НЕ дублируй один товар из
разных файлов. В поле "file" укажи номер файла, где находится ГЛАВНАЯ строка товара (с фото).

Для каждого товара верни объект СТРОГО с полями:
{
  "name_original": "исходное наименование как в инвойсе (китайский и/или английский)",
  "brand": "торговая марка/бренд, если явно есть, иначе \\"\\"",
  "article": "АРТИКУЛ ПРОИЗВОДИТЕЛЯ или модель — по нему ищут товар (напр. 404393/000-419..., BJL0501A02, CPK9-HBA1A). НЕ внутренний код заказа.",
  "internal_code": "внутренний код заказа поставщика, если есть (напр. S124811, U124169), иначе \\"\\"",
  "spec": "спецификация/размер/型号规格, иначе \\"\\"",
  "quantity": число,
  "unit": "единица измерения (个, шт, Set,套, м...), по умолчанию шт",
  "unit_price": число (цена за единицу в исходной валюте, 0 если нет),
  "currency": "валюта цен строки, как в инвойсе, ISO-кодом: CNY, USD, EUR, RUB, HKD… (RMB/元/¥ = CNY; если валюта не указана — CNY)",
  "total_price": число (сумма по строке, 0 если нет),
  "gross_weight": число (вес БРУТТО позиции в кг, 0 если нет),
  "net_weight": число (вес НЕТТО позиции в кг, 0 если нет),
  "pack_weight": число (вес упаковки позиции в кг, 0 если нет),
  "file": число — номер файла с главной строкой товара (1, если файл один),
  "sheet": "имя листа, где найден товар",
  "row": число — номер строки R главной строки товара (из маркеров R1, R2...)
}
Про веса: 毛重/G.W. = брутто; 净重/N.W. = нетто; общий вес строки (Kg, T.kg) без уточнения —
считай брутто. Если известны брутто и нетто — pack_weight = их разница.

Правила:
- Если наименование в одной ячейке с внутренним кодом (напр. "№ S124811_Jumo"), то internal_code=S124811, brand=Jumo, а article бери из соседней ячейки со спецификацией/моделью.
- Если количество разбито по колонкам размеров/цветов (напр. детская одежда: 21/122, 22/128...), quantity = СУММА чисел по строке.
- gross_weight — вес брутто ИМЕННО ЭТОЙ позиции. Если вес указан ОДИН РАЗ для всей партии
  (объединённая ячейка, итог внизу) — НЕ приписывай его ни одному товару (ставь 0).
- Возвращай ТОЛЬКО JSON-массив, без пояснений и без markdown-ограждения."""


_ROW_RE = re.compile(r"^R(\d+):")


def _file_label(fi, names=None):
    name = names[fi - 1] if names and 0 < fi <= len(names) else ""
    return f"файл {fi} «{name}»" if name else f"файл {fi}"


def extract_items(sheets, report=None, names=None):
    non_empty = [s for s in sheets if s["grid"].strip()]
    if not non_empty:
        raise RuntimeError("Файл пустой или не содержит распознаваемых строк")

    total_chars = sum(len(s["grid"]) for s in non_empty)
    total_lines = sum(s["grid"].count("\n") + 1 for s in non_empty)
    # небольшой ввод — один быстрый вызов (с одним повтором). ИИ видит инвойс и
    # пакинг-лист целиком и сводит их сам, по инструкции в EXTRACT_SYSTEM
    if total_lines <= 70 and total_chars <= 9000:
        parts = [f"===== ФАЙЛ {s.get('file', 1)} · ЛИСТ: {s['name']} =====\n{s['grid']}"
                 for s in non_empty]
        items, err = _extract_with_retry("\n\n".join(parts))
        if err:
            raise RuntimeError(f"ИИ не смог распознать товары ({err})")
        return _merge_invoice_packing(items)

    # КРУПНЫЙ ввод — режем ПО ЛИСТАМ и распознаём ПАРАЛЛЕЛЬНО (быстро, без зависаний).
    # Кусок никогда не обрывает лист посередине в другой лист/файл: у каждого куска
    # свой маркер «ФАЙЛ N · ЛИСТ» и своя шапка-контекст (первые строки ЭТОГО листа),
    # а не первые строки всего документа — иначе инвойс и пакинг-лист, оказавшиеся в
    # разных кусках, распознаются каждый сам по себе и инструкция «объедини их» в
    # EXTRACT_SYSTEM их не видит вместе. Сведение в этом случае — отдельным шагом
    # после распознавания, см. _merge_invoice_packing.
    step = 45
    tasks = []  # (fi, имя_листа, r-номера строк куска) — для текста и предупреждений
    texts = []
    for s in non_empty:
        fi = s.get("file", 1)
        lines = s["grid"].splitlines()
        header_ctx = "\n".join(lines[:8])
        marker = f"===== ФАЙЛ {fi} · ЛИСТ: {s['name']} ====="
        for i in range(0, len(lines), step):
            block_lines = lines[i:i + step]
            block = "\n".join(block_lines)
            text = f"{marker}\n{block}" if i == 0 else (
                f"{marker}\nКОНТЕКСТ ШАПКИ:\n{header_ctx}\n\nСТРОКИ:\n{block}")
            r_nums = [int(m.group(1)) for ln in block_lines if (m := _ROW_RE.match(ln))]
            tasks.append((fi, s["name"], r_nums))
            texts.append(text)

    all_items = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_extract_with_retry, texts))
    for (fi, sheet, r_nums), (part_items, err) in zip(tasks, results):
        # какого файла/листа кусок — мы знаем точно (сами его вырезали), это надёжнее
        # самоотчёта ИИ (поля file/sheet в ответе) и важно для дедупа по позиции ниже
        for it in part_items:
            it["src_file"] = fi
            it["src_sheet"] = sheet
        all_items.extend(part_items)
        if err and report:
            if r_nums:
                where = (f"строки R{min(r_nums)}–R{max(r_nums)}" if len(r_nums) > 1
                          else f"строка R{r_nums[0]}")
                where += f" ({_file_label(fi, names)}, лист «{sheet}»)"
            else:
                where = f"{_file_label(fi, names)}, лист «{sheet}»"
            report.add(f"Не распознаны {where}: {err}. Проверьте эти позиции вручную")

    return _merge_invoice_packing(_dedup_items(all_items))


def _dedup_items(items):
    """Кусок с «шапкой-контекстом» мог повторить первые строки предыдущего куска —
    их и убираем. Дедуп по ПОЗИЦИИ исходной строки (src_file, src_sheet, src_row),
    а не по содержимому: две настоящие разные строки с одинаковыми на вид данными
    (тот же товар в двух заказах по той же цене) — это не дубль, а два товара.
    Если позицию строки ИИ не вернул (src_row=0), сравниваем по содержимому — для
    такой строки отличить дубль иначе нечем."""
    seen_pos, seen_sig, uniq = set(), set(), []
    for it in items:
        row = it.get("src_row") or 0
        if row:
            key = (it.get("src_file") or 1, (it.get("src_sheet") or "").strip().lower(), row)
            if key in seen_pos:
                continue
            seen_pos.add(key)
        else:
            sig = (it["name_original"].lower(), it["article"].lower(),
                   it["internal_code"].lower(), it["quantity"], it["unit_price"])
            if sig in seen_sig:
                continue
            seen_sig.add(sig)
        uniq.append(it)
    return uniq


def _merge_key(it):
    """Ключ товара для сведения инвойс/пакинг-лист: артикул, иначе внутренний код,
    иначе наименование. Пусто везде -> None (не с чем сводить)."""
    for f in ("article", "internal_code", "name_original"):
        v = (it.get(f) or "").strip().lower()
        if v:
            return v
    return None


def _has_price(it):
    return bool(it["unit_price"] or it["total_price"])


def _has_weight(it):
    return bool(it["gross_weight"] or it["net_weight"] or it["pack_weight"])


def _merge_invoice_packing(items):
    """Если инвойс и пакинг-лист листа распознавались раздельными кусками (см. выше),
    один и тот же товар может прийти двумя записями: с ценой без веса и с весом без
    цены. Сводим по артикулу/коду/наименованию — вес переносится в запись с ценой,
    запись-дубль без цены убирается (её строка запоминается в src_alt — там может
    быть фото, см. images.map_source_images). Порядок и номера остальных товаров не
    трогаем — от них зависит нумерация «Товар № N» в предупреждениях и предпросмотре."""
    by_key = {}
    for it in items:
        if _has_price(it):
            k = _merge_key(it)
            if k:
                by_key.setdefault(k, []).append(it)

    drop = set()
    for it in items:
        if _has_price(it) or not _has_weight(it):
            continue
        matches = by_key.get(_merge_key(it))
        target = next((m for m in matches if not _has_weight(m)), None) if matches else None
        if target is None:
            continue
        target["gross_weight"] = target["gross_weight"] or it["gross_weight"]
        target["net_weight"] = target["net_weight"] or it["net_weight"]
        target["pack_weight"] = target["pack_weight"] or it["pack_weight"]
        target["src_alt"].append(
            {"file": it["src_file"], "sheet": it["src_sheet"], "row": it["src_row"]})
        drop.add(id(it))
    return [it for it in items if id(it) not in drop]


def _extract_chunk(grid_text: str):
    """Бросает исключение, если ответ ИИ не разобран; [] — только если товаров правда нет."""
    rule = ("ВАЖНО: если есть блок «КОНТЕКСТ ШАПКИ» — из него товары НЕ извлекай, "
            "он только для понимания колонок. Бери товары ТОЛЬКО из блока «СТРОКИ».\n\n"
            if "КОНТЕКСТ ШАПКИ" in grid_text else "")
    messages = [
        {"role": "system", "content": EXTRACT_SYSTEM},
        {"role": "user", "content": rule + "Разбери инвойс. Верни JSON-массив товаров.\n\n" + grid_text},
    ]
    resp = ai_client.chat(messages, temperature=0.1, max_tokens=14000,
                         model=EXTRACT_MODEL, timeout=150, max_retries=2)
    data = ai_client.extract_json(resp)
    if data is None:
        raise RuntimeError(f"ответ ИИ не разобран (длина {len(resp) if resp else 0})")
    return _items_from_json(data)


def _extract_with_retry(grid_text: str, attempts: int = 2):
    """-> (товары, None) или ([], текст ошибки) после всех попыток."""
    err = None
    for _ in range(attempts):
        try:
            return _extract_chunk(grid_text), None
        except Exception as e:  # noqa: BLE001
            err = str(e)[:200]
            logger.warning("чанк не распознан: %s", err)
    return [], err


def _items_from_json(data, default_file=1):
    # модель могла обернуть массив в объект {"items":[...]}, {"products":[...]}...
    if isinstance(data, dict):
        for k in ("items", "products", "goods", "товары", "rows", "data", "result"):
            if isinstance(data.get(k), list):
                data = data[k]
                break
        else:
            lists = [v for v in data.values() if isinstance(v, list)]
            data = lists[0] if lists else [data]  # один товар как объект
    if not isinstance(data, list):
        raise RuntimeError("Не удалось извлечь товары (ИИ не вернул массив)")
    items = []
    for d in data:
        if not isinstance(d, dict):
            continue
        qty = _num(d.get("quantity"))
        unit_price, cur_unit, ok_unit = _parse_amount(d.get("unit_price"))
        total_price, cur_total, ok_total = _parse_amount(d.get("total_price"))
        # досчитываем недостающее: total = qty × unit_price, и наоборот
        if not total_price and qty and unit_price:
            total_price = qty * unit_price
        elif not unit_price and qty and total_price:
            unit_price = total_price / qty

        currency_raw = str(d.get("currency", "")).strip()
        cur = currency.normalize_currency(currency_raw)  # None — непонятно
        if not currency_raw and (cur_unit or cur_total):
            # поле валюты пусто — берём валюту, найденную прямо в цене
            cur = cur_unit or cur_total

        price_unrecognized = None
        if not unit_price and not total_price and (not ok_unit or not ok_total):
            raw = str(d.get("unit_price") if not ok_unit else d.get("total_price") or "").strip()
            if raw:
                price_unrecognized = raw

        items.append({
            "name_original": str(d.get("name_original", "")).strip(),
            "brand": str(d.get("brand", "")).strip(),
            "article": str(d.get("article", "")).strip(),
            "internal_code": str(d.get("internal_code", "")).strip(),
            "spec": str(d.get("spec", "")).strip(),
            "quantity": qty,
            "unit": normalize_unit(d.get("unit")),
            "unit_price": unit_price,
            "currency_raw": currency_raw,
            "currency": cur,
            "total_price": total_price,
            "gross_weight": _num(d.get("gross_weight")),
            "net_weight": _num(d.get("net_weight")),
            "pack_weight": _num(d.get("pack_weight")),
            "src_file": int(_num(d.get("file")) or default_file),
            "src_sheet": str(d.get("sheet", "")).strip(),
            "src_row": int(_num(d.get("row"))),
            "price_unrecognized": price_unrecognized,
            # обогащённые
            "name_ru": "", "name_zh": "", "material": "",
            "description": "", "product_url": "", "photo_url": "",
            "sourcing": None, "src_image": None, "src_alt": [],
        })
    nz = [it for it in items if it["gross_weight"]]
    if len(items) >= 5 and len(nz) == 1:
        nz[0]["gross_weight"] = 0
    for it in items:
        if it["gross_weight"] and it["net_weight"] and not it["pack_weight"]:
            diff = round(it["gross_weight"] - it["net_weight"], 2)
            if diff > 0:
                it["pack_weight"] = diff
    return items


def extract_items_vision(page_images, default_file=1):
    """Извлечение товаров со СКАНА инвойса (PDF-изображения) через vision-модель."""
    resp = ai_client.chat_vision(
        EXTRACT_SYSTEM,
        "Это скан китайского инвойса. Распознай товары и верни JSON-массив.",
        page_images[:MAX_VISION_PAGES], model=EXTRACT_MODEL, max_tokens=16000)
    data = ai_client.extract_json(resp)
    if data is None:
        raise RuntimeError("ответ ИИ не разобран")
    return _items_from_json(data, default_file)


def extract_all(datas, report=None, names=None):
    """Единая точка: xlsx/xls/pdf (текст и скан) -> список товаров.
    Всё, что не удалось прочитать/распознать, уходит в report (если передан)."""
    def warn(msg):
        if report:
            report.add(msg)

    sheets, vision = [], []
    for fi, data in enumerate(datas, 1):
        label = _file_label(fi, names)
        vpages = []
        try:
            if data[:5] == b"%PDF-":
                file_sheets, vpages, n_scan = _read_pdf(data)
                if vpages:
                    vision.append((fi, vpages))
                    if n_scan > len(vpages):
                        warn(f"{label.capitalize()}: скан на {n_scan} стр., распознаны только "
                             f"первые {len(vpages)}. Остальные страницы загрузите отдельным файлом")
            else:
                file_sheets = read_sheets(data)
        except Exception as e:  # noqa: BLE001
            warn(f"{label.capitalize()} не прочитан: {str(e)[:200]}")
            continue
        if not any(s["grid"].strip() for s in file_sheets) and not vpages:
            warn(f"{label.capitalize()}: не найдено ни одной заполненной строки")
        for s in file_sheets:
            if s.get("truncated"):
                warn(f"Лист «{s['name']}» ({label}): обработаны только первые {MAX_ROWS} строк")
            s = dict(s); s["file"] = fi; sheets.append(s)
    items = []
    if sheets:
        try:
            items += extract_items(sheets, report, names)
        except Exception as e:  # noqa: BLE001
            warn(f"Таблицы не распознаны: {str(e)[:200]}")
    for fi, pages in vision:
        err = None
        for _ in range(2):  # один повтор
            try:
                items += extract_items_vision(pages, default_file=fi)
                err = None
                break
            except Exception as e:  # noqa: BLE001
                err = str(e)[:200]
        if err:
            warn(f"Скан ({_file_label(fi, names)}) не распознан: {err}")
    for i, it in enumerate(items, 1):
        raw = it.pop("price_unrecognized", None)
        if raw:
            warn(f"Товар № {i}: цена «{raw}» не распознана")
    return items


_UNIT_CJK = {
    "套": "компл.", "个": "шт", "件": "шт", "只": "шт", "台": "шт", "支": "шт",
    "条": "шт", "块": "шт", "根": "шт", "米": "м", "双": "пара", "盒": "кор.",
    "箱": "кор.", "卷": "рулон", "张": "лист", "片": "шт",
}
_UNIT_LAT = {
    "set": "компл.", "sets": "компл.", "kit": "компл.",
    "pcs": "шт", "pc": "шт", "pce": "шт", "piece": "шт", "pieces": "шт",
    "ea": "шт", "each": "шт", "unit": "шт", "units": "шт", "nos": "шт", "no": "шт",
    "m": "м", "meter": "м", "metre": "м", "meters": "м", "metres": "м",
    "kg": "кг", "kgs": "кг", "g": "г", "t": "т", "ton": "т",
    "pair": "пара", "pairs": "пара",
    "box": "кор.", "boxes": "кор.", "ctn": "кор.", "carton": "кор.", "cartons": "кор.",
    "roll": "рулон", "rolls": "рулон", "sheet": "лист", "sheets": "лист",
    "l": "л", "liter": "л", "litre": "л",
}
_UNIT_RUS = {"шт", "компл.", "м", "кг", "г", "т", "пара", "кор.", "рулон",
             "лист", "л", "м2", "м3", "упак."}


def normalize_unit(u) -> str:
    """Приводит единицу измерения к русскому (套Set→компл., 个/PCS→шт, m→м...)."""
    if not u:
        return "шт"
    s = str(u).strip()
    for ch in s:
        if ch in _UNIT_CJK:
            return _UNIT_CJK[ch]
    low = s.lower().replace(".", "")
    words = re.findall(r"[a-zа-яё]+", low)
    for w in words:
        if w in _UNIT_LAT:
            return _UNIT_LAT[w]
    for w in words:
        if w in _UNIT_RUS or (w + ".") in _UNIT_RUS:
            return w if w in _UNIT_RUS else w + "."
    # уже кириллица (напр. "шт." / "компл")
    if re.search(r"[а-яё]", low):
        return s
    return "шт"


_SPACE_RE = re.compile(r"[\s  -  ⁠]")  # обычные и неразрывные пробелы


def _parse_amount(v):
    """Число из ячейки инвойса: срезает валюту/пробелы (в т.ч. неразрывные), сама
    определяет десятичный разделитель и разделитель тысяч:
    '12,5'->12.5, '1,234'->1234, '1.234,56'->1234.56, '1,234.56'->1234.56,
    '¥12.5'/'12.5 USD'/'€5' -> число + валюта из символа/кода.
    Возвращает (число, код_валюты_или_None, распознано_ли).
    Непустая строка без единой цифры (напр. «по запросу») -> (0, валюта, False)."""
    if v is None or v == "":
        return 0.0, None, True
    s = str(v).strip()
    cur = currency.normalize_currency(s)
    s = _SPACE_RE.sub("", s)  # пробелы (в т.ч. неразрывные как разделитель тысяч) — прочь
    cleaned = re.sub(r"[^0-9,.\-]", "", s)
    if not any(ch.isdigit() for ch in cleaned):
        return 0.0, cur, False
    neg = cleaned.startswith("-")
    cleaned = cleaned.strip("-")
    last_comma, last_dot = cleaned.rfind(","), cleaned.rfind(".")
    if last_comma != -1 and last_dot != -1:
        if last_comma > last_dot:  # "1.234,56" — запятая десятичная
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:  # "1,234.56" — точка десятичная
            cleaned = cleaned.replace(",", "")
    elif last_comma != -1:
        if cleaned.count(",") > 1 or len(cleaned) - last_comma - 1 == 3:
            cleaned = cleaned.replace(",", "")  # "1,234,567" / "1,234" — тысячи
        else:
            cleaned = cleaned.replace(",", ".")  # "12,5" — десятичная
    elif cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")  # "1.234.567" — тысячи
    try:
        val = float(cleaned)
    except ValueError:
        return 0.0, cur, False
    return (-val if neg else val), cur, True


def _num(v):
    return _parse_amount(v)[0]
