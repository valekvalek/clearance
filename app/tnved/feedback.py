"""
Обратная связь от подтверждённых решений: таблица tnved_synonyms.

Хранит подтверждённые пары «название товара -> код ТН ВЭД» (из эталона
tests/golden_tnved/golden_tnved.csv, status=подтверждён, и из новых решений,
добавляемых record()). Используется:
  - exact(): точное совпадение нормализованного названия -> код (короткое
    замыкание классификатора); если для названия подтверждено НЕСКОЛЬКО разных
    кодов, совпадение не используется (противоречие — решает человек);
  - similar(): прецеденты по похожим товарам для промптов и кандидатов v2.

Импорт эталона:  python -m tnved.feedback [--csv путь]
(идемпотентен: пересоздаёт записи с source='golden' и не трогает record()).
"""
import argparse
import csv
import os
import sqlite3
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import tnved_classifier as tc  # noqa: E402

DEFAULT_GOLDEN = os.path.join(os.path.dirname(APP_DIR), "tests", "golden_tnved", "golden_tnved.csv")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tnved_synonyms (
    norm_name TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    n INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (norm_name, code)
);
"""


def ensure(conn) -> None:
    conn.execute(_SCHEMA)


def import_golden(csv_path: str = DEFAULT_GOLDEN, db_path: str | None = None) -> int:
    counts: dict[tuple[str, str], int] = Counter()
    names: dict[str, str] = {}
    with open(csv_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if (row.get("status") or "").strip() != "подтверждён":
                continue
            key = tc.normalize(row["name"])
            if key:
                counts[(key, row["expected_code"].strip())] += 1
                names.setdefault(key, row["name"].strip())
    conn = sqlite3.connect(db_path or tc.DB_PATH)
    try:
        ensure(conn)
        conn.execute("DELETE FROM tnved_synonyms WHERE source='golden'")
        conn.executemany(
            "INSERT INTO tnved_synonyms(norm_name,code,name,n,source) VALUES (?,?,?,?,'golden')",
            [(k, c, names[k], n) for (k, c), n in counts.items()])
        conn.commit()
        return len(counts)
    finally:
        conn.close()


def record(conn, name: str, code: str, source: str = "user") -> None:
    """Добавляет одно подтверждённое решение (повтор увеличивает n)."""
    ensure(conn)
    key = tc.normalize(name)
    conn.execute(
        "INSERT INTO tnved_synonyms(norm_name,code,name,n,source) VALUES (?,?,?,1,?) "
        "ON CONFLICT(norm_name,code) DO UPDATE SET n=n+1", (key, code.strip(), name.strip(), source))
    conn.commit()


def exact(conn, norm_name: str) -> tuple[str, int] | None:
    """(код, n) если для названия подтверждён ровно один код, иначе None."""
    try:
        rows = conn.execute("SELECT code, n FROM tnved_synonyms WHERE norm_name=?", (norm_name,)).fetchall()
    except sqlite3.OperationalError:
        return None
    return (rows[0][0], rows[0][1]) if len(rows) == 1 else None


def _tokens(norm: str) -> set[str]:
    return {tc._stem(w) for w in norm.split() if w and w not in tc._STOPWORDS and len(w) > 1}


def similar(conn, name: str, k: int = 4, exclude_codes=()) -> list[dict]:
    """Подтверждённые решения по похожим названиям (по доле общих стемов),
    кроме точно такого же названия — оно обрабатывается exact(). Одинаковые
    пары (название, код) уже агрегированы; по одному коду берётся лучшая запись.
    exclude_codes — коды, которые не использовать (только для оценки на «новых» товарах)."""
    key = tc.normalize(name)
    q = _tokens(key)
    if not q:
        return []
    try:
        rows = conn.execute("SELECT norm_name, code, name, n FROM tnved_synonyms").fetchall()
    except sqlite3.OperationalError:
        return []
    best: dict[str, tuple[float, dict]] = {}
    for norm, code, nm, n in rows:
        if norm == key or code in exclude_codes:
            continue
        t = _tokens(norm)
        if not t:
            continue
        sim = len(q & t) / len(q | t)
        if sim > 0 and (code not in best or sim > best[code][0]):
            best[code] = (sim, {"name": nm, "code": code, "n": n, "sim": round(sim, 2)})
    return [v for _, v in sorted(best.values(), key=lambda x: -x[0])[:k]]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", default=DEFAULT_GOLDEN)
    args = ap.parse_args()
    print("пар (название, код) импортировано:", import_golden(args.csv))
