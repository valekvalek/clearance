"""
Импорт полных цепочек подпозиций ТН ВЭД ЕАЭС из ОФИЦИАЛЬНОГО текста ЕЭК
(PDF по группам, https://eec.eaeunion.org/comission/department/catr/ett/)
в app/tnved.db как таблицы tnved_official_paths(code, name, path) и
tnved_official_headings(heading_code, title) — полные названия товарных позиций
(у 271 позиции в выгрузке ФНС названия нет) и tnved_official_group_notes —
полные примечания к группам (у ФНС обрезаны на 4000 символах: у группы 85
потеряны прим. 8–12, в т.ч. «схемы печатные» и «модули светодиодные»).

Зачем: выгрузка ФНС (build_tnved_db.py) не содержит текстов промежуточных
уровней без собственного кода («прожекторы и лампы узконаправленного света:»,
«прочие:», «из прочих материалов:»), а дерево alta.ru (tnved_tree) в них
часто ошибается. Сверка 2026-10-23 по 13 292 кодам: совпадает 7 939, у
alta.ru неверный текст — 2 215 (напр. 9405 42 003 9 подписан «для освещения
открытых общественных мест…», а это ветка «прочие / из прочих материалов»),
не хватает уровней — 1 728, нет совсем — 1 410. Классификатор берёт путь
отсюда в первую очередь (tnved_classifier._path_title, tnved.headings.leaf_path).

Проверка разбора там же: наименование листа совпало с выгрузкой ФНС у 13 000
кодов, ещё 291 — с точностью до опечаток или обрезки текста у ФНС; глубина
пути равна числу тире у ФНС везде, кроме 9 кодов (4407 97 900 x, 8539 52 000 x
и др.), где по PDF ошибается сама выгрузка ФНС.

Разбор по колонкам страницы: x < 400 — код и наименование, правее — доп. ед.
и ставка (пропускаются). Уровень строки — число тире перед текстом.

Запуск (идемпотентно, трогает только свою таблицу; после — python -m tnved.headings):
    python import_tnved_eec.py --download /path/to/pdf_dir   # скачать и импортировать
    python import_tnved_eec.py --pdf-dir /path/to/pdf_dir    # уже скачанные PDF
"""
import argparse
import glob
import os
import re
import sqlite3
import urllib.request

import fitz  # PyMuPDF

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "tnved.db")
SITE = "https://eec.eaeunion.org"
INDEX_URL = SITE + "/comission/department/catr/ett/"

CODE_RE = re.compile(r"^\s*[+*]?\s*(\d{4}(?: \d{2}(?: \d{2,3}(?: \d)?)?)?)\s*(.*)$")
# уровень — только длинное тире «–» с пробелом после; дефис или тире вплотную
# к тексту — продолжение наименования («- 1 мас.% или менее воды», «–40 °С»)
DASH_RE = re.compile(r"^((?:–\s+)+)(.*)$")
# перечень кодов внутри текста примечания («0403 90 110 0, 1702 11 000 0 …»)
NOT_CODE_LINE_RE = re.compile(r"^[,;]|^и\s|^\d")
RANGE_RE = re.compile(r"^–\s*\d{4}")  # диапазон «0403 90 110 0 – 0403 90 690 0»
PDF_LINK_RE = re.compile(r'href="([^"]*ru\.\d{2}_2022[^"]*\.pdf)"')
HEADER = {"Код", "ТН ВЭД", "Наименование позиции"}
TEXT_MAX_X = 400   # правее — колонки ед. изм. и ставки
CODE_MAX_X = 150   # левее — колонка кода
MIN_FONT = 9       # сноски внизу страницы набраны мельче


def _dashes(s: str) -> int:
    return s.count("–")


def _lines(pdf_path: str):
    with fitz.open(pdf_path) as doc:
        for page in doc:
            rows = []
            for b in page.get_text("dict")["blocks"]:
                for ln in b.get("lines", []):
                    text = "".join(sp["text"] for sp in ln["spans"])
                    size = max((sp["size"] for sp in ln["spans"]), default=0)
                    if text.strip():
                        rows.append((round(ln["bbox"][1]), ln["bbox"][0], text, size))
            rows.sort(key=lambda r: (r[0], r[1]))
            yield from rows


def parse_pdf(pdf_path: str, headings: dict | None = None) -> dict[str, dict]:
    """{код10: {"name": текст листа, "path": [тексты уровней над ним]}}.
    headings (если передан) заполняется {код4: полное название товарной позиции}."""
    out: dict[str, dict] = {}
    headings = {} if headings is None else headings
    stack: dict[int, str] = {}
    cur = None  # [уровень | None, текст, код | None]
    started = False

    def close():
        nonlocal cur
        if not cur:
            return
        depth, title, code = cur
        title = re.sub(r"\s+", " ", title).strip()
        code10 = (code or "").replace(" ", "")
        if depth is None and len(code10) == 10:
            stack.clear()  # позиция без подуровней: «0409 00 000 0 Мед натуральный»
            out[code10] = {"name": title, "path": []}
            headings.setdefault(code10[:4], title)
        elif depth:
            for d in [k for k in stack if k >= depth]:
                del stack[d]
            stack[depth] = title
            if len(code10) == 10:
                out[code10] = {"name": title, "path": [stack[d] for d in sorted(stack) if d < depth]}
        elif depth == 0:
            stack.clear()  # товарная позиция (4 знака) — новый корень
            if len(code10) == 4 and title:
                headings[code10] = title
        elif depth is None and len(code10) in (6, 8, 9) and title[:1].isupper():
            stack.clear()  # позиция с одной субпозицией: «0205 00 Мясо лошадей…»
            headings.setdefault(code10[:4], title)
        # depth None без 10-значного кода: строка кода подуровня, чей текст
        # стоит на пиксель выше и уже учтён отдельной строкой — пропускаем
        cur = None

    for _y, x, text, size in _lines(pdf_path):
        t = text.strip()
        if x >= TEXT_MAX_X or t in HEADER:
            continue
        m = CODE_RE.match(text) if x < CODE_MAX_X else None
        if (m and len(m.group(1).replace(" ", "")) in (4, 6, 8, 9, 10)
                and not NOT_CODE_LINE_RE.match(m.group(2).strip())
                and not RANGE_RE.match(m.group(2).strip())):
            close()
            started = True
            code, rest = m.group(1), m.group(2).strip()
            dm = DASH_RE.match(rest)
            if dm:
                cur = [_dashes(dm.group(1)), dm.group(2), code]
            elif len(code.replace(" ", "")) == 4:
                if rest and not rest[0].isupper():
                    continue  # ссылка на позицию в тексте примечаний («0511);», «или 2101);»)
                cur = [0, rest, code]
            else:
                cur = [None, rest, code]  # тире на следующей строке либо позиция без подуровней
            continue
        if not started:
            continue
        dm = DASH_RE.match(t)
        if dm and x >= CODE_MAX_X:
            if cur and cur[0] is None and not cur[1].strip():
                cur[0], cur[1] = _dashes(dm.group(1)), dm.group(2)
            else:
                close()
                cur = [_dashes(dm.group(1)), dm.group(2), None]
            continue
        if cur and x >= CODE_MAX_X and size >= MIN_FONT:
            cur[1] += " " + t
    close()
    return out


_TABLE_HEADER_RE = re.compile(r"Код\s*\n\s*ТН ВЭД\s*\n\s*Наименование позиции")
_GROUP_RE = re.compile(r"ru\.(\d{2})_")


def parse_group_notes(pdf_path: str) -> str:
    """Полный текст примечаний к группе (идут в начале PDF, до первой шапки
    таблицы кодов): «Примечания: 1. …», «Примечания к субпозициям…»,
    «Дополнительные примечания Евразийского экономического союза…».
    У ФНС/ФТС (TNVED2) это поле обрезано на 4000 символов."""
    with fitz.open(pdf_path) as doc:
        text = "\n".join(p.get_text() for p in doc)
    m = _TABLE_HEADER_RE.search(text)
    head = text[:m.start()] if m else ""
    m = re.search(r"(?:Дополнительн\w+\s+)?[Пп]римечани", head)
    return re.sub(r"\s+", " ", head[m.start():]).strip() if m else ""


def download(pdf_dir: str) -> list[str]:
    """Скачивает PDF всех групп по ссылкам со страницы ЕЭК."""
    os.makedirs(pdf_dir, exist_ok=True)
    req = urllib.request.Request(INDEX_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        html = resp.read().decode("utf-8", "replace")
    paths = []
    for href in sorted(set(PDF_LINK_RE.findall(html))):
        url = href if href.startswith("http") else SITE + href
        dst = os.path.join(pdf_dir, os.path.basename(href))
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                                    timeout=120) as resp, open(dst, "wb") as f:
            f.write(resp.read())
        paths.append(dst)
    return paths


def import_paths(pdf_paths: list[str], db_path: str = DEFAULT_DB) -> int:
    rows: dict[str, dict] = {}
    heads: dict[str, str] = {}
    notes: dict[str, str] = {}
    for p in sorted(pdf_paths):
        rows.update(parse_pdf(p, heads))
        g = _GROUP_RE.search(os.path.basename(p))
        if g:
            notes[g.group(1)] = parse_group_notes(p)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
            DROP TABLE IF EXISTS tnved_official_paths;
            CREATE TABLE tnved_official_paths (code TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL);
            DROP TABLE IF EXISTS tnved_official_headings;
            CREATE TABLE tnved_official_headings (heading_code TEXT PRIMARY KEY, title TEXT NOT NULL);
            DROP TABLE IF EXISTS tnved_official_group_notes;
            CREATE TABLE tnved_official_group_notes (group_code TEXT PRIMARY KEY, notes TEXT NOT NULL);
        """)
        conn.executemany("INSERT INTO tnved_official_group_notes VALUES (?,?)",
                         [(g, t) for g, t in notes.items() if t])
        conn.executemany("INSERT INTO tnved_official_headings VALUES (?,?)",
                         [(h, t.rstrip(":").strip()) for h, t in heads.items()])
        conn.executemany(
            "INSERT INTO tnved_official_paths VALUES (?,?,?)",
            [(c, v["name"], " / ".join(s.rstrip(":").strip() for s in v["path"])) for c, v in rows.items()])
        conn.commit()
    finally:
        conn.close()
    return len(rows)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--pdf-dir", help="папка с уже скачанными PDF групп")
    src.add_argument("--download", metavar="PDF_DIR", help="скачать PDF групп с сайта ЕЭК в эту папку")
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()
    pdfs = download(args.download) if args.download else glob.glob(os.path.join(args.pdf_dir, "*.pdf"))
    print(f"PDF: {len(pdfs)}, кодов импортировано: {import_paths(pdfs, args.db)}")
