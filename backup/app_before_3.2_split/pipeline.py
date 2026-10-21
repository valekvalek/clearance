"""
Ядро обработки инвойса: чтение сетки -> ИИ-извлечение строк ->
обогащение (перевод + поиск товара) -> экспорт в шаблон 12 колонок.
"""
import io
import os
import re
from concurrent.futures import ThreadPoolExecutor

import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

import vibecode
import sourcing
import currency
from report import ranges as report_ranges

# Быстрая модель для механического извлечения строк (structured outputs).
# Перевод/понимание товара идёт на основной AI_MODEL (BitrixGPT 5.5 Thinking).
EXTRACT_MODEL = os.getenv("EXTRACT_MODEL", "bitrix/bitrixgpt-5.5")

# Строго по образцу import (22).xlsx — 12 колонок, порядок и заголовки НЕ менять.
OUTPUT_COLUMNS = [
    "Наименование",
    "Китайское наименование товара",
    "Фото товара",
    "Материал",
    "Описание",
    "Ед. измерения",
    "Количество",
    "Вес брутто",
    "Торговая марка",
    "Артикул",
    "Общая закупочная цена $",
    "Закупочная цена за штуку $",
]

MAX_COLS = 40    # широкие инвойсы: веса/итоги часто в правых колонках
MAX_ROWS = 3000  # до ~1500+ позиций на лист (крупные инвойсы/пакинги)


# ---------------------------------------------------------------- чтение xlsx/xls
def _grid_from_rows(row_iter, info=None):
    """Из итератора строк (списки значений) -> текст сетки с координатами.
    info (dict) получает truncated=True, если лист длиннее MAX_ROWS."""
    lines = []
    for ri, row in enumerate(row_iter, start=1):
        if ri > MAX_ROWS:
            if info is not None:
                info["truncated"] = True
            break
        cells = []
        has = False
        for ci, v in enumerate(row):
            if ci >= MAX_COLS:
                break
            if v is None or v == "":
                cells.append("")
            else:
                s = str(v).replace("\n", " ").replace("\t", " ").strip()
                cells.append(s[:60])
                if s:
                    has = True
        if has:
            lines.append(
                f"R{ri}: " + " | ".join(
                    f"{get_column_letter(i + 1)}={c}" for i, c in enumerate(cells) if c
                )
            )
    return "\n".join(lines)


def read_sheets(data: bytes):
    """Читает книгу из bytes (.xlsx или .xls) -> [{'name', 'grid'}]."""
    # .xls (OLE2) начинается с D0CF11E0; .xlsx (zip) — с PK
    if data[:4] == b"\xd0\xcf\x11\xe0":
        return _read_xls(data)
    sheets = []
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    for ws in wb.worksheets:
        sh = {"name": ws.title}
        sh["grid"] = _grid_from_rows(ws.iter_rows(values_only=True), sh)
        sheets.append(sh)
    wb.close()
    return sheets


def _read_xls(data: bytes):
    import xlrd
    wb = xlrd.open_workbook(file_contents=data)
    sheets = []
    for sh in wb.sheets():
        rows = ([sh.cell_value(r, c) for c in range(sh.ncols)] for r in range(sh.nrows))
        info = {"name": sh.name}
        info["grid"] = _grid_from_rows(rows, info)
        sheets.append(info)
    return sheets


# ------------------------------------------- картинки из ИСХОДНОГО инвойса
def _decodable_size(b: bytes):
    """Полный декод картинки. Возвращает (w,h) или None."""
    try:
        from PIL import Image as PILImage
        im = PILImage.open(io.BytesIO(b))
        im.load()
        return im.size
    except Exception:
        return None


def _scan_blob_images(data: bytes):
    """Достаёт JPEG/PNG-блобы из бинарного .xls (без привязки к строкам).
    JPEG: маркер конца FFD9 может встретиться ВНУТРИ данных, поэтому перебираем
    маркеры, пока картинка не декодируется целиком."""
    raw = []
    i = 0
    while True:
        s = data.find(b"\xff\xd8\xff", i)
        if s < 0:
            break
        e, blob = s + 3, None
        for _ in range(30):
            e = data.find(b"\xff\xd9", e)
            if e < 0:
                break
            cand = data[s:e + 2]
            if len(cand) > 3000 and _decodable_size(cand):
                blob = cand
                break
            e += 2
        if blob:
            raw.append(blob)
            i = s + len(blob)
        else:
            i = s + 3
    i = 0
    while True:
        s = data.find(b"\x89PNG\r\n\x1a\n", i)
        if s < 0:
            break
        e = data.find(b"IEND", s)
        if e < 0:
            break
        blob = data[s:e + 8]
        if len(blob) > 3000 and _decodable_size(blob):
            raw.append(blob)
        i = e + 8
    # фильтр мелочи (логотипы/иконки) + дедуп
    out, seen = [], set()
    for b in raw:
        h = hash(b)
        if h in seen:
            continue
        size = _decodable_size(b)
        if not size or min(size) < 100:
            continue
        seen.add(h)
        out.append(b)
    return out


def _xls_images_anchored(data: bytes):
    """Картинки из .xls С ПРИВЯЗКОЙ К СТРОКАМ: OLE2 -> BIFF (MsoDrawing+Continue)
    -> Escher (BSE/BLIP + SpContainer: OPT.pib + ClientAnchor.row)."""
    import struct
    try:
        import olefile
        ole = olefile.OleFileIO(io.BytesIO(data))
        name = "Workbook" if ole.exists("Workbook") else (
            "Book" if ole.exists("Book") else None)
        if not name:
            return []
        s = ole.openstream(name).read()
    except Exception:
        return []
    buf = bytearray()
    i, collecting = 0, False
    while i + 4 <= len(s):
        rid = int.from_bytes(s[i:i + 2], "little")
        ln = int.from_bytes(s[i + 2:i + 4], "little")
        p = s[i + 4:i + 4 + ln]
        if rid in (0x00EB, 0x00EC):      # MsoDrawingGroup / MsoDrawing
            buf += p
            collecting = True
        elif rid == 0x003C and collecting:  # Continue
            buf += p
        else:
            collecting = False
        i += 4 + ln
    buf = bytes(buf)
    blips, shapes = [], []

    def walk(b, off, end, depth=0, cur=None):
        while off + 8 <= end and depth < 10:
            try:
                vi, rt, ln2 = struct.unpack_from("<HHI", b, off)
            except Exception:
                return
            ver, inst, ps = vi & 0xF, vi >> 4, off + 8
            if rt == 0xF007:                       # BSE: blip внутри после 36 байт
                walk(b, ps + 36, min(ps + ln2, end), depth + 1, cur)
            elif rt == 0xF004:                     # SpContainer (фигура)
                c = {"pib": None, "row": None}
                walk(b, ps, min(ps + ln2, end), depth + 1, c)
                if c["pib"] is not None:
                    shapes.append(c)
            elif ver == 0xF:                       # прочие контейнеры
                walk(b, ps, min(ps + ln2, end), depth + 1, cur)
            elif 0xF018 <= rt <= 0xF117:           # BLIP (данные картинки)
                payload = b[ps:ps + ln2]
                for sig in (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n"):
                    p2 = payload.find(sig, 0, 64)
                    if p2 >= 0:
                        blips.append(payload[p2:])
                        break
                else:
                    blips.append(None)             # держим индексацию pib
            elif rt == 0xF00B and cur is not None:  # OPT: свойство 260 = pib
                o = ps
                for _ in range(inst):
                    if o + 6 > ps + ln2:
                        break
                    pid, val = struct.unpack_from("<HI", b, o)
                    if (pid & 0x3FFF) == 260:
                        cur["pib"] = val
                    o += 6
            elif rt == 0xF010 and cur is not None and ln2 >= 18:  # ClientAnchor
                cur["row"] = struct.unpack_from("<H", b, ps + 6)[0] + 1
            off = ps + ln2

    walk(buf, 0, len(buf))
    out = []
    for shp in shapes:
        idx = (shp["pib"] or 0) - 1
        if shp["row"] is None or not (0 <= idx < len(blips)) or not blips[idx]:
            continue
        b = blips[idx]
        size = _decodable_size(b)
        if size and min(size) >= 100:
            out.append({"sheet": "", "row": shp["row"], "data": b})
    return out


def read_source_images(data: bytes):
    """Картинки из исходного файла.
    xlsx -> {'anchored': [{sheet,row,data}]}; xls -> anchored (Escher) или ordered."""
    if data[:4] == b"\xd0\xcf\x11\xe0":
        anchored = _xls_images_anchored(data)
        if anchored:
            return {"anchored": anchored}
        return {"ordered": _scan_blob_images(data)}
    out = []
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data))  # НЕ read_only — нужны картинки
        for ws in wb.worksheets:
            for img in getattr(ws, "_images", []):
                try:
                    out.append({"sheet": ws.title,
                                "row": img.anchor._from.row + 1,
                                "data": img._data()})
                except Exception:
                    continue
    except Exception:
        pass
    return {"anchored": out}


def _map_excel_images(items, data):
    """Сопоставление картинок Excel (xlsx/xls) с товарами по строке-якорю."""
    try:
        info = read_source_images(data)
    except Exception:
        return
    imgs = info.get("anchored", [])
    for it in items:
        r = it.get("src_row") or 0
        sh = it.get("src_sheet") or ""
        best, bestd = None, 99
        for im in imgs:
            if sh and im["sheet"] and im["sheet"] != sh:
                continue
            d = abs(im["row"] - r)
            if d < bestd:
                bestd, best = d, im
        if best is not None and bestd <= 2:
            it["src_image"] = best["data"]
    # без якорей — по порядку при точном равенстве количества
    if "ordered" in info:
        blobs = info["ordered"]
        f_items = [it for it in items if not it.get("src_image")]
        if blobs and len(blobs) == len(f_items):
            for it, b in zip(f_items, blobs):
                it["src_image"] = b


def _map_pdf_images(items, data):
    """Сопоставление картинок PDF: колонка 'Image' -> строка товара по Y (поиск артикула)."""
    import fitz
    import statistics
    doc = fitz.open(stream=data, filetype="pdf")
    allimgs = []
    for pi in range(len(doc)):
        p = doc[pi]
        for im in p.get_images(full=True):
            xref = im[0]
            try:
                b = doc.extract_image(xref)["image"]
            except Exception:
                continue
            for r in p.get_image_rects(xref):
                allimgs.append({"pi": pi, "cx": (r.x0 + r.x1) / 2,
                                "cy": (r.y0 + r.y1) / 2,
                                "w": r.width, "h": r.height, "data": b, "used": False})
    if not allimgs:
        return
    colx = statistics.median([a["cx"] for a in allimgs])
    col = [a for a in allimgs if abs(a["cx"] - colx) <= 35 and min(a["w"], a["h"]) >= 12]
    used_y = set()

    def find_y(key):
        if not key or len(key) < 4:
            return None
        for pi in range(len(doc)):
            for r in sorted(doc[pi].search_for(key), key=lambda rr: rr.y0):
                k = (pi, round(r.y0))
                if k in used_y:
                    continue
                used_y.add(k)
                return pi, (r.y0 + r.y1) / 2
        return None

    for it in items:
        key = (it.get("article") or "").strip()
        if len(key) < 4:
            words = [w for w in (it.get("name_original") or "").split() if len(w) >= 4]
            key = words[0] if words else ""
        res = find_y(key)
        if not res:
            continue
        pi, y = res
        best, bd = None, 30
        for a in col:
            if a["used"] or a["pi"] != pi:
                continue
            d = abs(a["cy"] - y)
            if d < bd:
                bd, best = d, a
        if best:
            best["used"] = True
            it["src_image"] = best["data"]


def map_source_images(items, datas):
    """Сопоставляет картинки исходных файлов (xlsx/xls/pdf) с товарами -> it['src_image'].
    datas: bytes (один файл) или список bytes (несколько файлов)."""
    if isinstance(datas, (bytes, bytearray)):
        datas = [datas]
    for fi, data in enumerate(datas, 1):
        f_items = [it for it in items if (it.get("src_file") or 1) == fi and not it.get("src_image")]
        if not f_items:
            continue
        try:
            if data[:5] == b"%PDF-":
                _map_pdf_images(f_items, data)
            else:
                _map_excel_images(f_items, data)
        except Exception:
            continue
    _map_alt_images(items, datas)
    return items


def _map_alt_images(items, datas):
    """Фото не нашлось у главной строки товара — пробуем строки, слитые в неё при
    сведении инвойса и пакинг-листа (_merge_invoice_packing, src_alt): фото инвойса
    и пакинг-листа часто стоит только у одной из двух исходных строк товара."""
    for it in items:
        if it.get("src_image"):
            continue
        for alt in it.get("src_alt") or []:
            fi = alt.get("file") or 0
            if not (1 <= fi <= len(datas)):
                continue
            data = datas[fi - 1]
            fake = dict(it, src_file=fi, src_sheet=alt.get("sheet") or "", src_row=alt.get("row") or 0)
            try:
                if data[:5] == b"%PDF-":
                    _map_pdf_images([fake], data)
                else:
                    _map_excel_images([fake], data)
            except Exception:
                continue
            if fake.get("src_image"):
                it["src_image"] = fake["src_image"]
                break


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
    быть фото, см. map_source_images). Порядок и номера остальных товаров не трогаем —
    от них зависит нумерация «Товар № N» в предупреждениях и предпросмотре."""
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
    resp = vibecode.chat(messages, temperature=0.1, max_tokens=14000,
                         model=EXTRACT_MODEL, timeout=150, max_retries=2)
    data = vibecode.extract_json(resp)
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
            print(f"[extract] чанк не распознан: {err}", flush=True)
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


# ------------------------------------------------------------- PDF-инвойсы
MAX_VISION_PAGES = int(os.getenv("MAX_VISION_PAGES", "8"))


def _read_pdf(data: bytes):
    """PDF -> (sheets, vision_pages, n_scan_pages). Режим выбирается по каждой странице
    отдельно: страница с достаточным текстом попадает в sheets, страница-скан рендерится в
    PNG для vision (не больше MAX_VISION_PAGES штук на файл, остальные только считаются)."""
    import fitz  # PyMuPDF
    doc = fitz.open(stream=data, filetype="pdf")
    sheets, vision_pages = [], []
    for pi in range(len(doc)):
        text = doc[pi].get_text()
        if len(text.strip()) >= 150:
            lines = [f"R{i + 1}: {ln.strip()}"
                     for i, ln in enumerate(text.splitlines()) if ln.strip()][:MAX_ROWS]
            sheets.append({"name": f"PDF стр.{pi + 1}", "grid": "\n".join(lines)})
        elif len(vision_pages) < MAX_VISION_PAGES:
            vision_pages.append(doc[pi].get_pixmap(dpi=140).tobytes("png"))
    n_scan_pages = len(doc) - len(sheets)
    return sheets, vision_pages, n_scan_pages


def extract_items_vision(page_images, default_file=1):
    """Извлечение товаров со СКАНА инвойса (PDF-изображения) через vision-модель."""
    resp = vibecode.chat_vision(
        EXTRACT_SYSTEM,
        "Это скан китайского инвойса. Распознай товары и верни JSON-массив.",
        page_images[:MAX_VISION_PAGES], model=EXTRACT_MODEL, max_tokens=16000)
    data = vibecode.extract_json(resp)
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


# ----------------------------------------------------------- обогащение
def _search_query(item):
    return " ".join(x for x in [item.get("brand", ""), item.get("article", ""),
                                item.get("name_original", "")] if x).strip()


_EMPTY_SEARCH = {"answer": "", "results": []}


ENRICH_SYSTEM = """Ты — эксперт по товарам из китайских инвойсов.
Для каждого товара даны его поля и РЕЗУЛЬТАТЫ ВЕБ-ПОИСКА (реальные ссылки и сниппеты).

ГЛАВНОЕ ПРАВИЛО — ТИП ТОВАРА ОПРЕДЕЛЯЕТ ИНВОЙС, А НЕ ПОИСК:
- Наименование из инвойса — истина о ТИПЕ товара. Поиск лишь уточняет детали.
- Артикулы часто СЛУЧАЙНО совпадают с чужими товарами (напр. "TX1-1" — это и ВЧ-трансформатор,
  и модель машинки для стрижки). Если результаты поиска описывают товар ДРУГОГО типа, чем
  наименование в инвойсе — ИГНОРИРУЙ эти результаты: переведи наименование инвойса как есть,
  а product_url верни "".
- Товары одного инвойса обычно тематически связаны (один поставщик). Смотри на соседние товары:
  если в инвойсе гребни и запчасти для машинок для стрижки, то "BLADE FOR I81WS" — это
  "Лезвие для машинки для стрижки I81WS", а НЕ серверное лезвие и НЕ трансформатор.
- Конструкция "X FOR <модель>" означает: X — тип товара, <модель> — модель УСТРОЙСТВА,
  к которому он подходит (не артикул самого X).
- Для НЕОДНОЗНАЧНЫХ наименований ("SPARE PARTS FOR TX1", "ACCESSORIES", "запчасти") тип
  устройства определяй ТОЛЬКО по КОНТЕКСТУ ИНВОЙСА (шапка, название поставщика, остальные
  товары списка) — НЕ по результатам поиска. Пример: если поставщик — фабрика машинок для
  стрижки и рядом лезвия/гребни, то "SPARE PARTS FOR TX1-1" = "Запчасти для машинки для
  стрижки TX1-1" (не трансформатор, не грузовик).
Верни JSON-массив (по одному объекту на товар, В ТОМ ЖЕ ПОРЯДКЕ) с полями:
{
  "name_ru": "перевод наименования на русский (тип товара + бренд + ключевая характеристика). БЕЗ артикула/кода в конце!",
  "name_zh": "название товара на китайском (термин ТИПА товара, а не дословный перевод слова)",
  "brand": "торговая марка/производитель (ОБЯЗАТЕЛЬНО заполнить)",
  "material": "материалы изготовления (ОБЯЗАТЕЛЬНО заполнить)",
  "description": "1-2 предложения: что это и назначение",
  "product_url": "ссылка на карточку ИМЕННО этого товара из результатов поиска, ИНАЧЕ \\"\\""
}
Про brand: поле НИКОГДА не пустое. Бери марку из инвойса; если её там нет — определи
  производителя по артикулу/типу товара и результатам поиска (напр. по префиксу артикула,
  по названию линейки). Если марку определить невозможно — напиши "OEM" (немарочный/безымянный товар).
Про name_ru: НЕ добавляй в конец артикул/код из инвойса (напр. 79090028, NCS-11/100-PNP).
  Нужен именно перевод наименования. Пример: "Ёмкостный датчик уровня Negele" (а не "...Negele NCS-11/100-PNP/X/X/X").
Про material: поле НИКОГДА не пустое и НЕ "не указано". Сначала бери материал из результатов поиска/спецификации.
  Если в сети информации нет — укажи наиболее вероятный материал ПО СМЫСЛУ для такого типа товара
  (напр. для датчика — "нержавеющая сталь", для клеммного винта — "оцинкованная сталь", для зажима КС — "медный сплав / бронза").
СТРОГИЕ правила по product_url:
- Бери URL ТОЛЬКО из предоставленных результатов поиска. НИКОГДА не выдумывай.
- Ставь ссылку ТОЛЬКО если результат — это карточка товара/страница производителя ИМЕННО про этот
  артикул/тип (совпадает бренд и/или артикул). Приоритет: производитель, 1688, alibaba, made-in-china,
  ozon, wildberries, профильные магазины.
- Если товар типовой/без бренда, или среди результатов нет подходящей карточки, или результаты не по теме
  (словари, переводчики, developer.android, google, youtube, wikipedia, соцсети) — верни product_url = "".
  Лучше пусто, чем неверная ссылка.
- Перевод (name_ru/name_zh) делай всегда, даже без ссылки.
Только JSON-массив, без markdown-ограждения."""

# домены, которые точно не являются карточкой товара
_BAD_URL = ("translate", "wikipedia", "baike", "zhihu", "developer.android",
            "play.google", "google.", "youtube", "youtu.be", "facebook",
            "twitter", "t.me", "promt", "yandex.ru/?", "reverso")


def _enrich_one_batch(batch, batch_search, ctx_block=""):
    """Переводит/обогащает товары батча на месте. -> множество индексов, которые ИИ заполнил."""
    filled = set()
    payload_lines = []
    for i, (it, sr) in enumerate(zip(batch, batch_search), 1):
        res_txt = "\n".join(
            f"    - {r['title']} | {r['url']} | {r['content'][:180]}"
            for r in sr.get("results", [])
        ) or "    (нет результатов)"
        ans = (sr.get("answer") or "")[:400]
        payload_lines.append(
            f"Товар #{i}:\n"
            f"  наименование: {it['name_original']}\n"
            f"  бренд: {it['brand']}\n"
            f"  артикул: {it['article']}\n"
            f"  спецификация: {it['spec']}\n"
            f"  сводка поиска: {ans}\n"
            f"  результаты поиска:\n{res_txt}"
        )
    messages = [
        {"role": "system", "content": ENRICH_SYSTEM},
        {"role": "user", "content": ctx_block + "Обработай товары:\n\n"
                                    + "\n\n".join(payload_lines)},
    ]
    try:
        # быстрый провал: короче таймаут и меньше повторов, чтобы не подвешивать всё
        resp = vibecode.chat(messages, temperature=0.2, max_tokens=8000,
                             timeout=120, max_retries=2)
        data = vibecode.extract_json(resp)
    except Exception:
        data = None
    if isinstance(data, list):
        for idx, (it, sr, d) in enumerate(zip(batch, batch_search, data)):
            if not isinstance(d, dict):
                continue
            filled.add(idx)
            it["name_ru"] = str(d.get("name_ru", "")).strip()
            it["name_zh"] = str(d.get("name_zh", "")).strip()
            it["material"] = str(d.get("material", "")).strip()
            it["description"] = str(d.get("description", "")).strip()
            # торговая марка: инвойс > ИИ > OEM (никогда не пусто)
            inv_brand = (it.get("brand") or "").strip()
            if not inv_brand or inv_brand.lower() in ("unknown", "unk", "n/a", "нет"):
                ai_brand = str(d.get("brand", "")).strip()
                it["brand"] = ai_brand or "OEM"
            # name_ru: срезать артикул/код с конца, если модель всё же добавила
            art = it.get("article") or it.get("internal_code")
            if art and it["name_ru"].rstrip().endswith(art):
                it["name_ru"] = it["name_ru"].rstrip()[:-len(art)].rstrip(" ,·-–—")
            # материал никогда не пустой
            if not it["material"] or it["material"].lower() in (
                    "не указано", "-", "—", "нет данных", "unknown", "n/a", "нет"):
                it["material"] = "уточняется"
            url = str(d.get("product_url", "")).strip()
            # принимаем ссылку только если: она реальна (есть в результатах поиска)
            # и не из чёрного списка. Никаких форс-фолбэков на случайный результат.
            result_urls = {r["url"] for r in sr.get("results", []) if r.get("url")}
            if url:
                if url not in result_urls or any(b in url.lower() for b in _BAD_URL):
                    url = ""
            it["product_url"] = url
    return filled


def enrich_items(items, progress=None, batch_size=10,
                 search_workers=6, batch_workers=6, web_search=True,
                 invoice_context="", report=None):
    total = len(items)
    # контекст инвойса: шапка документа + полный список товаров (тематика)
    all_names = "; ".join((it.get("name_original") or "")[:45] for it in items[:50])
    ctx_block = ""
    if invoice_context or all_names:
        ctx_block = ("КОНТЕКСТ ИНВОЙСА (шапка документа):\n"
                     + (invoice_context or "(нет)")[:1500]
                     + f"\n\nВСЕ ТОВАРЫ ЭТОГО ИНВОЙСА: {all_names}\n\n")
    # 1) поиск по товарам (с ДЕДУПЛИКАЦИЕЙ — одинаковые запросы ищем 1 раз)
    if web_search:
        if progress:
            progress(0, total, "Поиск товаров в интернете...")
        # НЕ ищем для позиций, у которых уже есть фото из файла (экономим вайбы):
        # «есть фото — в интернете не ищем».
        queries = [("" if it.get("src_image") else _search_query(it)) for it in items]
        uniq = list(dict.fromkeys(q for q in queries if q))
        with ThreadPoolExecutor(max_workers=search_workers) as ex:
            res = list(ex.map(vibecode.search, uniq))
        qmap = dict(zip(uniq, res))
        searches = [qmap.get(q, _EMPTY_SEARCH) if q else _EMPTY_SEARCH for q in queries]
        failed = sum(1 for r in res if r.get("error"))
        if failed and report:
            report.add(f"Поиск в интернете не сработал для {failed} из {len(res)} запросов — "
                       f"у этих товаров нет ссылок, материал определён без поиска")
    else:
        # экономный режим — без интернет-поиска (0 списаний), только перевод + фото из файлов
        searches = [_EMPTY_SEARCH for _ in items]

    # 2) обогащение — батчи параллельно, с плавным прогрессом
    batches = [(s, items[s:s + batch_size], searches[s:s + batch_size])
               for s in range(0, total, batch_size)]
    from threading import Lock
    counter = {"done": 0}
    lock = Lock()
    untranslated = []  # номера товаров (с 1), оставшихся без перевода

    def run(batch):
        start, b_items, b_search = batch
        filled = _enrich_one_batch(b_items, b_search, ctx_block)
        missing = [i for i in range(len(b_items)) if i not in filled]
        if missing:  # один повтор — только для незаполненных товаров
            again = _enrich_one_batch([b_items[i] for i in missing],
                                      [b_search[i] for i in missing], ctx_block)
            missing = [m for j, m in enumerate(missing) if j not in again]
        with lock:
            untranslated.extend(start + i + 1 for i in missing)
        if progress:
            with lock:
                counter["done"] += len(b_items)
                progress(min(counter["done"], total), total,
                         f"Переведено {min(counter['done'], total)}/{total}")

    with ThreadPoolExecutor(max_workers=batch_workers) as ex:
        list(ex.map(run, batches))
    if untranslated and report:
        report.add(f"{len(untranslated)} товар(ов) без перевода и описания (№ "
                   f"{report_ranges(untranslated)}) — в файле оставлено исходное наименование")

    # 3) ФОТО — ТОЛЬКО из загруженных файлов (src_image, проставлено в map_source_images).
    #    Поиск/скачивание картинок из интернета ОТКЛЮЧЁН полностью (по требованию).
    # гарантия: торговая марка и материал никогда не пустые
    for it in items:
        if not (it.get("brand") or "").strip():
            it["brand"] = "OEM"
        if not (it.get("material") or "").strip():
            it["material"] = "уточняется"
    return items


def _thumb_from_bytes(data: bytes, box: int = 96):
    """Квадратная PNG-миниатюра (padding белым) — чтобы ровно ложилась в ячейку.
    Возвращает (BytesIO, (box, box)) или None."""
    try:
        from PIL import Image as PILImage, ImageFile
        ImageFile.LOAD_TRUNCATED_IMAGES = True  # терпимость к слегка обрезанным
        im = PILImage.open(io.BytesIO(data))
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGB")
        im.thumbnail((box, box))
        # вписываем в квадрат box×box на белом фоне (единый размер под ячейку)
        canvas = PILImage.new("RGB", (box, box), (255, 255, 255))
        canvas.paste(im.convert("RGB"),
                     ((box - im.size[0]) // 2, (box - im.size[1]) // 2))
        out = io.BytesIO()
        canvas.save(out, format="PNG")
        out.seek(0)
        return out, (box, box)
    except Exception:
        return None


# ----------------------------------------------------------- закупка (Китай)
def source_items(items, progress=None):
    """Заполняет it['sourcing'] (deep-ссылки + данные Oxylabs, если доступны)."""
    from threading import Lock
    total = len(items)
    if progress:
        progress(0, total, "Поиск на китайских площадках...")
    done = {"n": 0}
    lock = Lock()

    def run(it):
        try:
            it["sourcing"] = sourcing.source_item(it)
        except Exception:
            it["sourcing"] = {"links": {}, "china_url": "", "supplier": "",
                              "price_cn": "", "moq": "", "pay": ""}
        if progress:
            with lock:
                done["n"] += 1
                progress(done["n"], total, f"Закупка {done['n']}/{total}")

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(run, items))
    return items


SOURCING_COLUMNS = [
    "Ссылка на товар (Китай)", "Поставщик (продавец)",
    "Закупочная цена (Китай)", "Мин. партия (MOQ)",
    "Ссылка на оплату", "Поиск на площадках",
]
SOURCING_W = {13: 40, 14: 28, 15: 20, 16: 14, 17: 24, 18: 55}


# ----------------------------------------------------------- экспорт (по образцу)
HEADER_FILL = PatternFill(start_color="FFCFE2F3", end_color="FFCFE2F3", fill_type="solid")
HEADER_FONT = Font(name="Arial", size=11, bold=False, color="FF000000")
DATA_FONT = Font(name="Arial", size=11)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
# ширины колонок из образца import (22).xlsx
COL_W = {1: 17.71, 2: 19, 3: 13, 4: 13, 5: 13, 6: 14.71, 7: 14.29, 8: 13,
         9: 16.43, 10: 13, 11: 29.57, 12: 37}
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "\n")
HEADER_ROW = 2   # строка 1 — номера колонок, строка 2 — заголовки, данные с 3-й
DATA_START = 3


def _embed_images(ws, items):
    """Вставляет картинки СТРОГО В ЯЧЕЙКУ «Фото товара» (C) через two-cell anchor
    (editAs=twoCell) — картинка привязана к обоим углам ячейки, Битрикс видит её
    в нужной клетке и не путает наименования.
    Приоритет: картинка из ИСХОДНОГО инвойса -> найденная в вебе -> заглушка."""
    try:
        from openpyxl.drawing.image import Image as XLImage
        from openpyxl.drawing.spreadsheet_drawing import TwoCellAnchor, AnchorMarker
    except Exception:
        return
    targets = [(i + DATA_START, it) for i, it in enumerate(items)]

    def dl(pair):
        r, it = pair
        if it.get("src_image"):          # ТОЛЬКО фото из загруженного файла
            return r, _thumb_from_bytes(it["src_image"])
        return r, None                   # интернет-картинки отключены

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(dl, targets))
    INSET = 12700  # ~1px отступ от границ ячейки (EMU)
    for r, res in results:
        if not res:
            cell = ws.cell(r, 3)
            if not cell.value:
                cell.value = "Изображение не найдено"
                cell.font = Font(name="Arial", size=9, italic=True, color="999999")
            continue
        buf, (w, h) = res
        try:
            xi = XLImage(buf)
            # C{r} = колонка index 2, строка index r-1 (0-based) .. до D{r+1}
            _from = AnchorMarker(col=2, colOff=INSET, row=r - 1, rowOff=INSET)
            _to = AnchorMarker(col=3, colOff=-INSET, row=r, rowOff=-INSET)
            xi.anchor = TwoCellAnchor(editAs="twoCell", _from=_from, to=_to)
            ws.add_image(xi)
            ws.cell(r, 3).value = None
        except Exception:
            continue


def build_workbook(items, rates, with_sourcing=False, embed_images=True, warnings=None):
    """rates — курсы ЦБ из currency.get_rates(); все суммы пишутся в долларах."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Result"
    columns = OUTPUT_COLUMNS + (SOURCING_COLUMNS if with_sourcing else [])
    ncols = len(columns)

    # строка 1 — номера колонок (как в образце)
    for ci in range(1, ncols + 1):
        c = ws.cell(1, ci, ci)
        c.alignment = CENTER
        c.font = DATA_FONT
    # строка 2 — заголовки (заливка CFE2F3, по центру, без жирного)
    for ci, title in enumerate(columns, 1):
        c = ws.cell(HEADER_ROW, ci, title)
        c.fill = HEADER_FILL
        c.font = HEADER_FONT
        c.alignment = CENTER

    # данные с 3-й строки
    for i, it in enumerate(items):
        r = DATA_START + i
        desc = it["description"] or ""
        if it["product_url"]:
            desc = (desc + f"\nСсылка: {it['product_url']}").strip()
        values = [
            it["name_ru"] or it["name_original"] or "",
            it["name_zh"] or "",
            it.get("photo_url", ""),   # заменится картинкой в _embed_images
            it["material"] or "",
            desc,
            it["unit"],
            it["quantity"],
            it["gross_weight"] or "",
            it["brand"] or "",
            it["article"] or it["internal_code"],
            usd(it, "total_price", rates),
            usd(it, "unit_price", rates, ndigits=4),
        ]
        if with_sourcing:
            s = it.get("sourcing") or {}
            values += [
                s.get("china_url", ""), s.get("supplier", ""),
                s.get("price_cn", ""), s.get("moq", ""),
                s.get("pay", ""), sourcing.links_text(s.get("links", {})),
            ]
        for ci, v in enumerate(values, 1):
            c = ws.cell(r, ci, v)
            if isinstance(v, str) and v[:1] in _FORMULA_START:
                # текст из инвойса/ИИ не должен стать формулой (=HYPERLINK, DDE…)
                c.data_type = "s"
                c.quotePrefix = True
            c.font = DATA_FONT
            c.alignment = CENTER
        ws.row_dimensions[r].height = 43.35

    # высоты и ширины по образцу
    ws.row_dimensions[1].height = 14.25
    ws.row_dimensions[HEADER_ROW].height = 42.75
    widths = dict(COL_W)
    if with_sourcing:
        widths.update(SOURCING_W)
    for ci, w in widths.items():
        ws.column_dimensions[get_column_letter(ci)].width = w

    if embed_images:
        _embed_images(ws, items)
    if warnings:
        _add_warnings_sheet(wb, warnings)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _add_warnings_sheet(wb, warnings):
    """Второй лист: что не удалось распознать/перевести. Лист Result остаётся первым."""
    ws = wb.create_sheet("Предупреждения")
    ws.cell(1, 1, "Проверьте вручную — эти данные могут отсутствовать или быть неполными:")
    ws.cell(1, 1).font = Font(name="Arial", size=11, bold=True, color="FFB45309")
    for i, msg in enumerate(warnings, 2):
        c = ws.cell(i, 1, str(msg))
        c.data_type = "s"  # текст с именами файлов не должен стать формулой
        c.font = DATA_FONT
        c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 120


def usd(it, key, rates, ndigits=2):
    """Сумма товара в долларах (по умолчанию 2 знака — для цены за штуку передают 4,
    иначе копеечная позиция округляется в $0.00) или "" — если валюта/курс неизвестны:
    пустая ячейка лучше, чем юани или рубли под видом долларов."""
    v = currency.to_usd(it.get(key), it.get("currency"), rates)
    return "" if v is None else round(v, ndigits)
