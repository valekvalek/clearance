"""
Сборка локального справочника ТН ВЭД ЕАЭС (10-значные коды) в SQLite.

Источник — официальный классификатор ФНС России (data.nalog.ru/files/tnved/tnved.zip,
страница nalog.gov.ru/rn77/program/5961290/). Формат ФНС хранит ПОЛНУЮ историю
классификатора (каждый код — с датами начала/окончания действия на каждую
редакцию), поэтому "текущими" считаются записи с ПУСТОЙ датой окончания —
то есть ещё не отменённые ни одной последующей редакцией.

Файлы архива (кодировка cp866, разделитель "|"):
  TNVED1.TXT — разделы (не используется: официальный машиночитаемый файл не
               даёт связи раздел -> группы, поэтому иерархию "раздел" не
               заполняем, чтобы не придумывать её самостоятельно)
  TNVED2.TXT — группы (2 знака):     группа|???|наименование|заметки|с|по|
               (структура содержит доп. служебное поле — парсим по последним
               двум датам и наименованию по индексу, см. _parse_level)
  TNVED3.TXT — товарные позиции (4 знака): группа|позиция|наименование|с|по|
  TNVED4.Txt — подсубпозиции (10 знаков): группа|позиция|подпозиция(6)|наименование|с|по|

Первая строка каждого файла — заголовок вида "133|20260427|0000|", где
второе поле — дата редакции файла (YYYYMMDD). Используется как дата
актуальности справочника.

Запуск: python build_tnved_db.py [--db tnved.db]
"""
import argparse
import os
import sqlite3
import sys
import urllib.request
import zipfile
from datetime import date, datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SOURCE_URL = "https://data.nalog.ru/files/tnved/tnved.zip"
# Без браузерного User-Agent data.nalog.ru отвечает 403 Forbidden.
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"),
    "Referer": "https://www.nalog.gov.ru/rn77/program/5961290/",
}
ENCODING = "cp866"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "tnved.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
DROP TABLE IF EXISTS tnved_codes;
CREATE TABLE tnved_codes (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    group_code TEXT NOT NULL,
    group_name TEXT NOT NULL DEFAULT '',
    heading_code TEXT NOT NULL,
    heading_name TEXT NOT NULL DEFAULT '',
    valid_from TEXT NOT NULL
);
DROP TABLE IF EXISTS tnved_fts;
CREATE VIRTUAL TABLE tnved_fts USING fts5(
    code UNINDEXED, name, group_name, heading_name
);
"""


def _fetch_zip(url: str) -> bytes:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def _read_member(zf: zipfile.ZipFile, name: str) -> list[str]:
    with zf.open(name) as f:
        text = f.read().decode(ENCODING)
    return text.split("\n")


def _header_date(lines: list[str]) -> str:
    """'133|20260427|0000|' -> '2026-04-27'."""
    parts = lines[0].strip().split("|")
    raw = parts[1]
    return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"


def _parse_level(lines: list[str], n_code_fields: int) -> dict:
    """Общий разбор TNVED2/3/4: первые n_code_fields полей — части кода,
    затем наименование, затем дата_с, дата_по (последние два поля).
    Берём только записи с ПУСТОЙ датой_по (действующие сейчас).
    Возвращает {код: (наименование, дата_с)}."""
    out = {}
    for line in lines[1:]:
        line = line.rstrip("\r\n")
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < n_code_fields + 3:
            continue
        date_to = parts[-2]
        if date_to != "":
            continue  # историческая, отменённая запись — пропускаем
        code = "".join(parts[0:n_code_fields])
        name = parts[n_code_fields].strip()
        date_from = parts[-3]
        out[code] = (name, date_from)
    return out


def build(db_path: str = DEFAULT_DB, source_url: str = SOURCE_URL) -> dict:
    print(f"Скачиваю классификатор: {source_url}")
    raw = _fetch_zip(source_url)
    print(f"✅ Загружено {len(raw)} байт")

    with zipfile.ZipFile(__import__("io").BytesIO(raw)) as zf:
        names = {n.lower(): n for n in zf.namelist()}
        tnved2 = _read_member(zf, names["tnved2.txt"])
        tnved3 = _read_member(zf, names["tnved3.txt"])
        tnved4 = _read_member(zf, names["tnved4.txt"] if "tnved4.txt" in names else names["tnved4.txt".lower()])

    edition_date = _header_date(tnved4)
    print(f"✅ Редакция классификатора (ФНС России): {edition_date}")

    groups = _parse_level(tnved2, 1)       # код группы (2 знака) -> (имя, дата_с)
    headings = _parse_level(tnved3, 2)     # код позиции (4 знака) -> (имя, дата_с)
    codes = _parse_level(tnved4, 3)        # 10-значный код -> (имя, дата_с)
    print(f"✅ Разобрано: групп {len(groups)}, позиций {len(headings)}, "
          f"10-значных кодов {len(codes)}")

    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        rows = []
        fts_rows = []
        skipped = 0
        for code, (name, date_from) in codes.items():
            if len(code) != 10 or not code.isdigit() or not name:
                skipped += 1
                continue
            group_code = code[0:2]
            heading_code = code[0:4]
            group_name = groups.get(group_code, ("", ""))[0]
            heading_name = headings.get(heading_code, ("", ""))[0]
            rows.append((code, name, group_code, group_name, heading_code,
                         heading_name, date_from))
            fts_rows.append((code, name, group_name, heading_name))
        conn.executemany(
            "INSERT INTO tnved_codes(code,name,group_code,group_name,"
            "heading_code,heading_name,valid_from) VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        conn.executemany(
            "INSERT INTO tnved_fts(code,name,group_name,heading_name) "
            "VALUES (?,?,?,?)", fts_rows,
        )
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('edition_date',?)",
                     (edition_date,))
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('source_url',?)",
                     (source_url,))
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('built_at',?)",
                     (datetime.utcnow().isoformat(timespec="seconds") + "Z",))
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('code_count',?)",
                     (str(len(rows)),))
        conn.commit()
        if skipped:
            print(f"⚠ Пропущено (не 10 цифр / пустое имя): {skipped}")
        print(f"✅ Записано в {db_path}: {len(rows)} кодов")
        return {"db_path": db_path, "edition_date": edition_date,
                "code_count": len(rows)}
    finally:
        conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB, help="путь к файлу SQLite (по умолчанию app/tnved.db)")
    ap.add_argument("--source-url", default=SOURCE_URL)
    args = ap.parse_args()
    info = build(args.db, args.source_url)
    print(f"ГОТОВО: {info['code_count']} кодов, редакция {info['edition_date']}, файл {info['db_path']}")
