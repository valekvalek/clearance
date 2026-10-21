"""
Картинки товара: извлечение из исходного инвойса (xlsx/xls/pdf), сопоставление
со строкой товара по якорю/позиции, PNG-миниатюры для вставки в xlsx-результат.
"""
import io

import openpyxl

try:
    from PIL import Image as _PILImage
    # >40 Мпикс — предупреждение, >80 Мпикс — DecompressionBombError (картинка пропускается)
    _PILImage.MAX_IMAGE_PIXELS = 40_000_000
except Exception:  # noqa: BLE001
    pass


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
    # сортируем товары по качеству лучшего совпадения (сначала те, у кого есть
    # точное/близкое совпадение) — иначе при жадном проходе "по порядку товаров"
    # более далёкий товар может забрать себе картинку, которая по расстоянию
    # ближе другому товару, рассматриваемому позже.
    candidates = []
    for it in items:
        r = it.get("src_row") or 0
        sh = it.get("src_sheet") or ""
        best, bestd = None, 99
        for im in imgs:
            if im.get("_used"):
                continue
            if sh and im["sheet"] and im["sheet"] != sh:
                continue
            d = abs(im["row"] - r)
            if d < bestd:
                bestd, best = d, im
        if best is not None and bestd <= 2:
            candidates.append((bestd, it, best))
    for _, it, im in sorted(candidates, key=lambda c: c[0]):
        if im.get("_used"):
            continue  # картинку уже забрал товар с более точным совпадением
        it["src_image"] = im["data"]
        im["_used"] = True
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
