"""Сборка tnved_codes.csv из PDF ТН ВЭД ЕАЭС (eec.eaeunion.org, по группам).

Запуск (PyMuPDF уже есть в app/.venv, новых зависимостей нет):
    app/.venv/bin/python -I research/tnved-sources/raw/scripts/build_tnved_codes.py

Что извлекается из каждого PDF группы:
  * строка группы (level=2): название, полные примечания (до первой шапки таблицы);
  * строки с кодом (level = число цифр: 4 позиция, 6/8/9 подсубпозиции, 10 полный код);
  * уровень вложенности — число тире «–» перед текстом (в PDF уровни без кода
    («прочие:», «из пластмассы:») тоже есть — они входят в name_full, но отдельной
    строкой не выводятся, т.к. кода у них нет);
  * доп. единица измерения — колонка x≈400–445 на строке кода («–» = нет).
Ставки пошлин и сноски (4*, 5*) не извлекаются.
"""
import csv
import glob
import json
import os
import re
import sys

import fitz  # PyMuPDF

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.dirname(HERE)
OUT_DIR = os.path.dirname(RAW)
PDF_DIR = os.path.join(RAW, "eec_pdf")

CODE_RE = re.compile(r"^\s*[+*]?\s*(\d{4}(?: \d{2}(?: \d{2,3}(?: \d)?)?)?)\s*(.*)$")
DASH_RE = re.compile(r"^((?:–\s+)+)(.*)$")      # уровень — «– » с пробелом после
NOT_CODE_RE = re.compile(r"^[,;]|^и\s|^\d")       # перечень кодов внутри примечания
RANGE_RE = re.compile(r"^–\s*\d{4}")              # «0403 90 110 0 – 0403 90 690 0»
HEADER = {"Код", "ТН ВЭД", "Наименование позиции", "Доп.", "ед.", "изм."}
TEXT_MAX_X, CODE_MAX_X, UNIT_MIN_X, UNIT_MAX_X, MIN_FONT = 400, 150, 400, 447, 9
TABLE_HEADER = re.compile(r"Код\s*\n\s*ТН ВЭД\s*\n\s*Наименование позиции")
NOTES_START = re.compile(r"(?:Дополнительн\w+\s+)?[Пп]римечани")
SEP = " — "


def lines(doc):
    for page in doc:
        rows = []
        for b in page.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                text = "".join(sp["text"] for sp in ln["spans"])
                size = max((sp["size"] for sp in ln["spans"]), default=0)
                if text.strip():
                    rows.append((page.number, round(ln["bbox"][1]), ln["bbox"][0], text, size))
        rows.sort(key=lambda r: (r[0], r[1], r[2]))
        yield from rows


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip().rstrip(":").strip()


def group_header_and_notes(doc):
    """-> ((код группы, название), примечания). Если группа открывает раздел, в начале файла
    стоят «РАЗДЕЛ …» и примечания к разделу — они кладутся в notes как префикс."""
    text = "\n".join(p.get_text() for p in doc)
    m = TABLE_HEADER.search(text)
    head = re.sub(r"\s+", " ", text[:m.start()] if m else "").strip()
    g = re.search(r"ГРУППА\s+(\d{2})\s+", head)
    if not g:
        return (None, ""), ""
    before, after = head[:g.start()].strip(), head[g.end():]
    n = NOTES_START.search(after)
    title = after[:n.start()] if n else after
    notes = after[n.start():].strip() if n else ""
    if before.startswith("РАЗДЕЛ"):
        notes = before + " || " + notes if notes else before
    return (g.group(1), title.strip()), notes


def parse_group(pdf_path, edition_date):
    doc = fitz.open(pdf_path)
    (grp, grp_title), notes = group_header_and_notes(doc)
    if not grp:
        grp = re.search(r"ru\.(\d{2})_", os.path.basename(pdf_path)).group(1)
    rows = [dict(code=grp, level=2, parent_code="", name_short=clean(grp_title), name_full=clean(grp_title),
                 notes=notes, explanatory_notes="", unit="", edition_date=edition_date, source_id="S01",
                 pending_change="")]
    stack = {}          # depth -> (text, code|None)
    heading = {"code": "", "title": ""}
    cur = None          # [depth|None, text, code|None, y, page]
    units = {}          # (page, y) -> text
    started = False
    table_seen = False  # до шапки таблицы идут название/примечания группы — коды в них не ищем

    def unit_for(page, y):
        for dy in (0, 1, -1, 2, -2, 3, -3):
            u = units.get((page, y + dy))
            if u:
                return u
        return ""

    def close():
        nonlocal cur
        if not cur:
            return
        depth, title, code, y, page = cur
        cur = None
        title = clean(title)
        digits = (code or "").replace(" ", "")
        if depth == 0:
            stack.clear()
            if len(digits) == 4 and title:
                heading.update(code=digits, title=title)
                rows.append(dict(code=digits, level=4, parent_code=grp, name_short=title, name_full=title,
                                 notes="", explanatory_notes="", unit="", edition_date=edition_date,
                                 source_id="S01", pending_change=""))
            return
        if depth is None:
            if title and title[:1].isupper() and len(digits) in (6, 8, 9, 10):
                # позиция, у которой код печатается длиннее 4 знаков, а название стоит в той же
                # строке: «8534 00 Схемы печатные», «0409 00 000 0 Мед натуральный»
                stack.clear()
                h4 = digits[:4]
                if h4 != heading["code"]:
                    heading.update(code=h4, title=title)
                    rows.append(dict(code=h4, level=4, parent_code=grp, name_short=title, name_full=title,
                                     notes="", explanatory_notes="", unit="", edition_date=edition_date,
                                     source_id="S01", pending_change=""))
                rows.append(dict(code=digits, level=len(digits), parent_code=h4, name_short=title,
                                 name_full=title, notes="", explanatory_notes="",
                                 unit=unit_for(page, y) if len(digits) == 10 else "",
                                 edition_date=edition_date, source_id="S01", pending_change=""))
            elif len(digits) == 10 and title:           # позиция без подуровней, название строчными
                stack.clear()
                rows.append(dict(code=digits, level=10, parent_code=heading["code"] or grp, name_short=title,
                                 name_full=SEP.join(x for x in (heading["title"], title) if x),
                                 notes="", explanatory_notes="", unit=unit_for(page, y),
                                 edition_date=edition_date, source_id="S01", pending_change=""))
            return
        for d in [k for k in stack if k >= depth]:
            del stack[d]
        parent = next((stack[d][1] for d in sorted(stack, reverse=True) if stack[d][1]), None)
        chain = [stack[d][0] for d in sorted(stack)]
        stack[depth] = (title, digits or None)
        if digits and len(digits) in (6, 8, 9, 10) and title:
            rows.append(dict(code=digits, level=len(digits), parent_code=parent or heading["code"] or grp,
                             name_short=title,
                             name_full=SEP.join(x for x in [heading["title"], *chain, title] if x),
                             notes="", explanatory_notes="",
                             unit=unit_for(page, y) if len(digits) == 10 else "",
                             edition_date=edition_date, source_id="S01", pending_change=""))

    for page, y, x, text, size in lines(doc):
        t = text.strip()
        if not table_seen:
            table_seen = t == "Наименование позиции"
            continue
        if UNIT_MIN_X <= x < UNIT_MAX_X and t not in HEADER and started:
            units[(page, y)] = "" if t in ("–", "-") else t
        if x >= TEXT_MAX_X or t in HEADER:
            continue
        m = CODE_RE.match(text) if x < CODE_MAX_X else None
        if (m and len(m.group(1).replace(" ", "")) in (4, 6, 8, 9, 10)
                and not NOT_CODE_RE.match(m.group(2).strip()) and not RANGE_RE.match(m.group(2).strip())):
            code, rest = m.group(1), m.group(2).strip()
            n = len(code.replace(" ", ""))
            if n == 4 and rest and not rest[0].isupper():
                continue                       # ссылка на позицию в тексте примечания
            close()
            started = True
            dm = DASH_RE.match(rest)
            if dm:
                cur = [dm.group(1).count("–"), dm.group(2), code, y, page]
            elif n == 4:
                cur = [0, rest, code, y, page]
            else:
                cur = [None, rest, code, y, page]
            continue
        if not started:
            continue
        dm = DASH_RE.match(t)
        if dm and x >= CODE_MAX_X:
            if cur and cur[0] is None and not cur[1].strip():
                cur[0], cur[1] = dm.group(1).count("–"), dm.group(2)
            else:
                close()
                cur = [dm.group(1).count("–"), dm.group(2), None, y, page]
            continue
        if cur and x >= CODE_MAX_X and size >= MIN_FONT:
            cur[1] += " " + t
    close()
    # 9-значная строка без своих детей и подуровня «0409 00 000 0 …» уже учтена; дубли убираем
    seen, out = set(), []
    for r in rows:
        if r["code"] in seen:
            continue
        seen.add(r["code"])
        out.append(r)
    return out


def main():
    manifest = {}
    with open(os.path.join(RAW, "eec_pdf_manifest.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            manifest[r["file"]] = r["edition_date_from_filename"]
    allrows = []
    for p in sorted(glob.glob(os.path.join(PDF_DIR, "ru.*.pdf"))):
        allrows += parse_group(p, manifest.get(os.path.basename(p), ""))
    fields = ["code", "level", "parent_code", "name_short", "name_full", "notes", "explanatory_notes",
              "unit", "edition_date", "source_id", "pending_change"]
    out_path = os.path.join(OUT_DIR, "tnved_codes.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(allrows)
    by = {}
    for r in allrows:
        by[r["level"]] = by.get(r["level"], 0) + 1
    print(json.dumps({"rows": len(allrows), "by_level": by}, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
