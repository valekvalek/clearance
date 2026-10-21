"""
Ядро обработки инвойса: чтение сетки -> ИИ-извлечение строк ->
обогащение (перевод + поиск товара) -> экспорт в шаблон 12 колонок.
"""
import io
import os
import re
import urllib.request
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor

import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

import vibecode
import sourcing

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
def _grid_from_rows(row_iter):
    """Из итератора строк (списки значений) -> текст сетки с координатами."""
    lines = []
    for ri, row in enumerate(row_iter, start=1):
        if ri > MAX_ROWS:
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
        sheets.append({"name": ws.title,
                       "grid": _grid_from_rows(ws.iter_rows(values_only=True))})
    wb.close()
    return sheets


def _read_xls(data: bytes):
    import xlrd
    wb = xlrd.open_workbook(file_contents=data)
    sheets = []
    for sh in wb.sheets():
        rows = ([sh.cell_value(r, c) for c in range(sh.ncols)] for r in range(sh.nrows))
        sheets.append({"name": sh.name, "grid": _grid_from_rows(rows)})
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
    return items


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
  "currency": "RMB/CNY/USD (по умолчанию RMB)",
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


def read_all(datas):
    """Читает НЕСКОЛЬКО файлов -> общий список листов с пометкой файла."""
    sheets = []
    for fi, data in enumerate(datas, 1):
        try:
            for s in read_sheets(data):
                s = dict(s)
                s["file"] = fi
                sheets.append(s)
        except Exception:
            continue
    return sheets


def extract_items(sheets):
    parts = []
    for s in sheets:
        if s["grid"].strip():
            fi = s.get("file", 1)
            parts.append(f"===== ФАЙЛ {fi} · ЛИСТ: {s['name']} =====\n{s['grid']}")
    grid_text = "\n\n".join(parts)
    if not grid_text.strip():
        raise RuntimeError("Файл пустой или не содержит распознаваемых строк")

    lines = grid_text.splitlines()
    # небольшой файл — один быстрый вызов
    if len(lines) <= 70 and len(grid_text) <= 9000:
        items = _extract_chunk(grid_text)
        if not items:
            raise RuntimeError("Не удалось извлечь товары из файла")
        return items

    # КРУПНЫЙ файл — режем на чанки строк и распознаём ПАРАЛЛЕЛЬНО (быстро, без зависаний)
    header_ctx = "\n".join(lines[:8])   # шапка/названия колонок как контекст
    step = 45
    chunks = []
    for i in range(0, len(lines), step):
        block = "\n".join(lines[i:i + step])
        chunks.append(block if i == 0 else "КОНТЕКСТ ШАПКИ:\n" + header_ctx + "\n\nСТРОКИ:\n" + block)

    all_items = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for part_items in ex.map(_extract_chunk_safe, chunks):
            all_items.extend(part_items)

    # дедуп (шапка-контекст мог повторить первые строки)
    seen, uniq = set(), []
    for it in all_items:
        sig = (it["name_original"].lower(), it["article"].lower(),
               it["internal_code"].lower(), it["quantity"], it["unit_price"])
        if sig in seen:
            continue
        seen.add(sig)
        uniq.append(it)
    if not uniq:
        raise RuntimeError("Не удалось извлечь товары из файла")
    return uniq


def _extract_chunk(grid_text: str):
    rule = ("ВАЖНО: если есть блок «КОНТЕКСТ ШАПКИ» — из него товары НЕ извлекай, "
            "он только для понимания колонок. Бери товары ТОЛЬКО из блока «СТРОКИ».\n\n"
            if "КОНТЕКСТ ШАПКИ" in grid_text else "")
    messages = [
        {"role": "system", "content": EXTRACT_SYSTEM},
        {"role": "user", "content": rule + "Разбери инвойс. Верни JSON-массив товаров.\n\n" + grid_text},
    ]
    resp = vibecode.chat(messages, temperature=0.1, max_tokens=14000,
                         model=EXTRACT_MODEL, timeout=150, max_retries=2)
    try:
        return _items_from_json(vibecode.extract_json(resp))
    except Exception:
        print(f"[extract] чанк не распарсен (len={len(resp) if resp else 0})", flush=True)
        return []


def _extract_chunk_safe(grid_text: str):
    try:
        return _extract_chunk(grid_text)
    except Exception:
        return []


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
        items.append({
            "name_original": str(d.get("name_original", "")).strip(),
            "brand": str(d.get("brand", "")).strip(),
            "article": str(d.get("article", "")).strip(),
            "internal_code": str(d.get("internal_code", "")).strip(),
            "spec": str(d.get("spec", "")).strip(),
            "quantity": _num(d.get("quantity")),
            "unit": normalize_unit(d.get("unit")),
            "unit_price": _num(d.get("unit_price")),
            "currency": str(d.get("currency", "RMB")).strip() or "RMB",
            "total_price": _num(d.get("total_price")),
            "gross_weight": _num(d.get("gross_weight")),
            "net_weight": _num(d.get("net_weight")),
            "pack_weight": _num(d.get("pack_weight")),
            "src_file": int(_num(d.get("file")) or default_file),
            "src_sheet": str(d.get("sheet", "")).strip(),
            "src_row": int(_num(d.get("row"))),
            # обогащённые
            "name_ru": "", "name_zh": "", "material": "",
            "description": "", "product_url": "", "photo_url": "",
            "sourcing": None, "src_image": None,
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
def _read_pdf(data: bytes):
    """PDF -> ('text', sheets) для текстовых или ('vision', [png_bytes]) для сканов."""
    import fitz  # PyMuPDF
    doc = fitz.open(stream=data, filetype="pdf")
    full = "".join(p.get_text() for p in doc)
    if len(full.strip()) >= 150:
        sheets = []
        for pi in range(len(doc)):
            lines = [f"R{i + 1}: {ln.strip()}"
                     for i, ln in enumerate(doc[pi].get_text().splitlines())
                     if ln.strip()][:MAX_ROWS]
            sheets.append({"name": f"PDF стр.{pi + 1}", "grid": "\n".join(lines)})
        return ("text", sheets)
    # скан: рендерим страницы в PNG для vision (не больше 8 страниц)
    pages = []
    for pi in range(min(len(doc), 8)):
        pages.append(doc[pi].get_pixmap(dpi=140).tobytes("png"))
    return ("vision", pages)


def extract_items_vision(page_images, default_file=1):
    """Извлечение товаров со СКАНА инвойса (PDF-изображения) через vision-модель."""
    resp = vibecode.chat_vision(
        EXTRACT_SYSTEM,
        "Это скан китайского инвойса. Распознай товары и верни JSON-массив.",
        page_images[:8], model=EXTRACT_MODEL, max_tokens=16000)
    return _items_from_json(vibecode.extract_json(resp), default_file)


def extract_all(datas):
    """Единая точка: xlsx/xls/pdf (текст и скан) -> список товаров."""
    sheets, vision = [], []
    for fi, data in enumerate(datas, 1):
        try:
            if data[:5] == b"%PDF-":
                mode, payload = _read_pdf(data)
                if mode == "text":
                    for s in payload:
                        s = dict(s); s["file"] = fi; sheets.append(s)
                else:
                    vision.append((fi, payload))
            else:
                for s in read_sheets(data):
                    s = dict(s); s["file"] = fi; sheets.append(s)
        except Exception:
            continue
    items = []
    if sheets:
        items += extract_items(sheets)
    for fi, pages in vision:
        try:
            items += extract_items_vision(pages, default_file=fi)
        except Exception:
            continue
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


def _num(v):
    try:
        if v is None or v == "":
            return 0
        return float(str(v).replace(",", "").replace(" ", ""))
    except Exception:
        return 0


# ----------------------------------------------------- извлечение фото товара
_BAD_IMG = ("logo", "icon", "sprite", "placeholder", "blank", "pixel",
            "avatar", "favicon", ".svg", "data:", "flag", "/lang",
            "loader", "spinner", "captcha", "1x1", "spacer", "/ui/",
            # логотипы/шейр-картинки маркетплейсов и сайтов (частая подмена фото)
            "yastatic", "/share", "sharing", "default-", "-default", "default.",
            "ogimage", "og-image", "og_image", "no-photo", "nophoto",
            "no-image", "noimage", "/stub", "brand-", "-brand",
            # баннеры/промо категорий
            "/banner", "/bnr/", "productbnrgroup", "/promo", "/slide", "/hero-",
            # трекеры/счётчики/аналитика (точные паттерны, чтобы не задеть static.*)
            "dot.asp", "toplist", "/counter", "counter.php", "webcounter",
            "analytics", "metrika", "google-analytics", "doubleclick",
            "/stat.php", "hit.php", "webvisor", "?http=",
            "mc.yandex", "/watch/", "yandex.ru/watch", "top-fwz1",
            "counter.yadro", "liveinternet", "/rk.php", "openstat",
            "fls-na", "fls-eu", "amazon-adsystem", "adsystem", "/uedata")


def _safe_url(url: str) -> str:
    """Кодирует не-ASCII символы (©, кириллица…) в URL, чтобы urlopen не падал."""
    try:
        from urllib.parse import urlsplit, urlunsplit, quote
        p = urlsplit(url)
        return urlunsplit((p.scheme, p.netloc, quote(p.path),
                           quote(p.query, safe="=&%+"), quote(p.fragment)))
    except Exception:
        return url


def _ok_img(u: str) -> bool:
    if not u:
        return False
    low = u.lower()
    if low.startswith("data:"):
        return False
    return not any(b in low for b in _BAD_IMG)


def _extract_image(html: str) -> str:
    """Ищет URL фото товара в HTML: og:image / twitter:image / JSON-LD / <img>."""
    candidates = []
    # og:image / twitter:image — любой порядок атрибутов
    for pat in (
        r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image)["\']',
    ):
        for m in re.finditer(pat, html, re.I):
            candidates.append(m.group(1).strip())
    # JSON-LD "image"
    m = re.search(r'"image"\s*:\s*"([^"]+)"', html)
    if m:
        candidates.append(m.group(1))
    m = re.search(r'"image"\s*:\s*\[\s*"([^"]+)"', html)
    if m:
        candidates.append(m.group(1))
    # запасной вариант — <img> из страницы
    for m in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.I):
        candidates.append(m.group(1))
    for u in candidates:
        if _ok_img(u):
            return u
    return ""


def _http_get(url: str, limit: int = 400_000, timeout: int = 8) -> str:
    """Бесплатный GET страницы (без платного поиска). Возвращает текст или ''."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return ""
    try:
        req = urllib.request.Request(_safe_url(url), headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = r.headers.get("Content-Type", "")
            if "html" not in ctype and "text" not in ctype and ctype:
                return ""
            return r.read(limit).decode("utf-8", "ignore")
    except Exception:
        return ""


def fetch_product_image(url: str) -> str:
    """ОТКЛЮЧЕНО: поиск картинок в интернете убран. Фото только из файлов."""
    return ""


def _fetch_product_image_DISABLED(url: str) -> str:
    """Заходит на карточку товара и вытаскивает URL фото (абсолютный)."""
    html = _http_get(url)
    if not html:
        return ""
    img = _extract_image(html)
    if not img:
        return ""
    img = urljoin(url, img.replace("&amp;", "&"))
    if not img.lower().startswith(("http://", "https://")):
        return ""
    return img


def fetch_image_made_in_china(item) -> str:
    """ОТКЛЮЧЕНО: поиск картинок в интернете убран. Фото только из файлов."""
    return ""


def _fetch_image_made_in_china_DISABLED(item) -> str:
    kw = item.get("name_zh") or item.get("name_original") or ""
    if not kw:
        return ""
    art_raw = item.get("article") or ""
    if art_raw and re.search(r"[A-Za-z0-9]", art_raw):
        kw = f"{kw} {art_raw.split()[0]}"
    from urllib.parse import quote
    url = ("https://www.made-in-china.com/productdirectory.do?word=" + quote(kw.strip())
           + "&subaction=hunt&style=b&mode=and&code=0&comProvince=nolimit"
           + "&order=0&isOpenCorrection=1")
    html = _http_get(url, limit=600_000, timeout=12)
    if not html:
        return ""
    imgs = re.findall(
        r'https://image\.made-in-china\.com/[^\s"\'<>]+?\.(?:jpg|jpeg|png|webp)', html, re.I)
    seen = []
    for u in imgs:
        if u not in seen:
            seen.append(u)
    if not seen:
        return ""
    # СТРОГО: берём фото ТОЛЬКО если артикул есть в URL картинки (высокая релевантность).
    # Иначе НЕ подставляем случайное фото — лучше пусто, чем чужой товар.
    art = re.sub(r"[^A-Za-z0-9]", "", art_raw).lower()
    if len(art) >= 5:
        for u in seen:
            if art in re.sub(r"[^A-Za-z0-9]", "", u).lower():
                return u
    return ""


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
        for it, sr, d in zip(batch, batch_search, data):
            if not isinstance(d, dict):
                continue
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


def enrich_items(items, progress=None, batch_size=10,
                 search_workers=6, batch_workers=6, web_search=True,
                 invoice_context=""):
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
    else:
        # экономный режим — без интернет-поиска (0 списаний), только перевод + фото из файлов
        searches = [_EMPTY_SEARCH for _ in items]

    # 2) обогащение — батчи параллельно, с плавным прогрессом
    batches = [(items[s:s + batch_size], searches[s:s + batch_size])
               for s in range(0, total, batch_size)]
    from threading import Lock
    counter = {"done": 0}
    lock = Lock()

    def run(pair):
        _enrich_one_batch(pair[0], pair[1], ctx_block)
        if progress:
            with lock:
                counter["done"] += len(pair[0])
                progress(min(counter["done"], total), total,
                         f"Переведено {min(counter['done'], total)}/{total}")

    with ThreadPoolExecutor(max_workers=batch_workers) as ex:
        list(ex.map(run, batches))

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


def _download_thumbnail(url: str, box: int = 96):
    """Скачивает картинку и делает PNG-миниатюру. Возвращает (BytesIO, (w,h)) или None."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    try:
        req = urllib.request.Request(_safe_url(url), headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120 Safari/537.36"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = r.read(4_000_000)
        return _thumb_from_bytes(data, box)
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


def build_workbook(items, rmb_to_usd, with_sourcing=False, embed_images=True):
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
            round(it["total_price"] * rmb_to_usd, 2) if _is_rmb(it) else round(it["total_price"], 2),
            round(it["unit_price"] * rmb_to_usd, 2) if _is_rmb(it) else round(it["unit_price"], 2),
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
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _is_rmb(it):
    return (it.get("currency") or "RMB").upper() in ("RMB", "CNY", "¥", "元")
