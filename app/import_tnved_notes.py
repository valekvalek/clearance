"""
Импорт примечаний к разделам и группам ТН ВЭД ЕАЭС из официального архива ФНС
(data.nalog.ru/files/tnved/tnved.zip, тот же источник, что и build_tnved_db.py)
в app/tnved.db: таблицы tnved_section_notes и tnved_group_notes.

Форматы (кодировка cp866, разделитель "|", первая строка — заголовок с датой
редакции файла; на каждый код хранится ВСЯ история редакций, действующая —
запись с пустой датой окончания):
  TNVED1.TXT — разделы: раздел|наименование|примечания|с|по|
  TNVED2.TXT — группы:  раздел|группа|наименование|примечания|с|по|

Редакция файлов с примечаниями (TNVED1 — 2022-01-13, TNVED2 — 2022-08-11)
старше редакции TNVED4 с кодами (2026-04-27): ФНС не обновляла эти два файла.
Дата редакции записывается в каждую строку (колонка edition_date) — перед
использованием в классификаторе помнить, что примечания могли измениться.

Таблицы пересоздаются (DROP+CREATE только своих), остальные таблицы tnved.db
(tnved_codes, tnved_fts, classify_cache, tnved_tree, ...) не затрагиваются.

Запуск: python import_tnved_notes.py [--zip /path/tnved.zip] [--db tnved.db]
Без --zip архив скачивается с источника.
"""
import argparse
import io
import os
import re
import sqlite3
import sys
import zipfile

from build_tnved_db import DEFAULT_DB, ENCODING, SOURCE_URL, _fetch_zip, _header_date

_SCHEMA = """
DROP TABLE IF EXISTS tnved_section_notes;
CREATE TABLE tnved_section_notes (
    section_code TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    notes TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    edition_date TEXT NOT NULL
);
DROP TABLE IF EXISTS tnved_group_notes;
CREATE TABLE tnved_group_notes (
    group_code TEXT PRIMARY KEY,
    section_code TEXT NOT NULL,
    title TEXT NOT NULL,
    notes TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    edition_date TEXT NOT NULL
);
"""


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _read_member(zf: zipfile.ZipFile, name: str) -> list[str]:
    real = {n.lower(): n for n in zf.namelist()}[name.lower()]
    with zf.open(real) as f:
        return f.read().decode(ENCODING).split("\n")


def _current(lines: list[str], n_fields: int) -> list[list[str]]:
    """Действующие записи (пустая дата окончания) с ровно n_fields полями."""
    out = []
    for line in lines[1:]:
        line = line.rstrip("\r\n")
        if not line:
            continue
        parts = line.split("|")
        if len(parts) != n_fields or parts[-2] != "":
            continue
        out.append(parts)
    return out


def import_notes(zip_bytes: bytes, db_path: str = DEFAULT_DB) -> dict:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        t1 = _read_member(zf, "TNVED1.TXT")
        t2 = _read_member(zf, "TNVED2.TXT")
    ed1, ed2 = _header_date(t1), _header_date(t2)

    sections = [(p[0], _clean(p[1]), _clean(p[2]), p[-3], ed1)
                for p in _current(t1, 6)]
    groups = [(p[1], p[0], _clean(p[2]), _clean(p[3]), p[-3], ed2)
              for p in _current(t2, 7)]

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.executemany("INSERT INTO tnved_section_notes VALUES (?,?,?,?,?)", sections)
        conn.executemany("INSERT INTO tnved_group_notes VALUES (?,?,?,?,?,?)", groups)
        conn.commit()
    finally:
        conn.close()
    return {"sections": len(sections), "groups": len(groups),
            "section_edition": ed1, "group_edition": ed2}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--zip", help="путь к уже скачанному tnved.zip (иначе скачивается)")
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()
    if args.zip:
        with open(args.zip, "rb") as f:
            raw = f.read()
    else:
        raw = _fetch_zip(SOURCE_URL)
    info = import_notes(raw, args.db)
    print(f"✅ Разделов: {info['sections']} (редакция {info['section_edition']}), "
          f"групп: {info['groups']} (редакция {info['group_edition']}) -> {args.db}")
