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
CREATE TABLE IF NOT EXISTS tnved_synonym_articles (
    article_norm TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    n INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (article_norm, code)
);
"""


def ensure(conn) -> None:
    conn.executescript(_SCHEMA)
    try:  # бренд — слабый признак похожести, не ключ (у одного бренда бывают десятки кодов)
        conn.execute("ALTER TABLE tnved_synonyms ADD COLUMN brand TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass


def informative_article(norm: str) -> bool:
    """Артикул как ключ: не короче 4 знаков и с цифрой (отсекает «БА», «нет», «OEM»).
    В эталоне все 87 артикулов однозначно соответствуют одному коду."""
    return len(norm) >= 4 and any(ch.isdigit() for ch in norm)


def import_golden(csv_path: str = DEFAULT_GOLDEN, db_path: str | None = None) -> int:
    counts: dict[tuple[str, str], int] = Counter()
    art_counts: dict[tuple[str, str], int] = Counter()
    names: dict[str, str] = {}
    brands: dict[tuple[str, str], str] = {}
    with open(csv_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if (row.get("status") or "").strip() != "подтверждён":
                continue
            key = tc.normalize(row["name"])
            code = row["expected_code"].strip()
            if key:
                counts[(key, code)] += 1
                names.setdefault(key, " ".join(row["name"].split()))
                if (row.get("brand") or "").strip():
                    brands.setdefault((key, code), tc.normalize(row["brand"]))
                art = tc.normalize(row.get("article"))
                if informative_article(art):
                    art_counts[(art, code)] += 1
                    names.setdefault("art:" + art, " ".join(row["name"].split()))
    conn = sqlite3.connect(db_path or tc.DB_PATH)
    try:
        ensure(conn)
        conn.execute("DELETE FROM tnved_synonyms WHERE source='golden'")
        conn.execute("DELETE FROM tnved_synonym_articles WHERE source='golden'")
        conn.executemany(
            "INSERT INTO tnved_synonyms(norm_name,code,name,n,source,brand) VALUES (?,?,?,?,'golden',?)",
            [(k, c, names[k], n, brands.get((k, c), "")) for (k, c), n in counts.items()])
        conn.executemany(
            "INSERT INTO tnved_synonym_articles(article_norm,code,name,n,source) VALUES (?,?,?,?,'golden')",
            [(a, c, names["art:" + a], n) for (a, c), n in art_counts.items()])
        conn.commit()
        return len(counts)
    finally:
        conn.close()


def record(conn, name: str, code: str, source: str = "user", article: str = "",
           brand: str = "", name_original: str = "") -> None:
    """Добавляет одно подтверждённое решение (повтор увеличивает n): по названию,
    по исходному названию из инвойса (если отличается) и по артикулу."""
    ensure(conn)
    code = code.strip()
    for nm in dict.fromkeys(x for x in (name, name_original) if tc.normalize(x)):
        conn.execute(
            "INSERT INTO tnved_synonyms(norm_name,code,name,n,source,brand) VALUES (?,?,?,1,?,?) "
            "ON CONFLICT(norm_name,code) DO UPDATE SET n=n+1",
            (tc.normalize(nm), code, nm.strip(), source, tc.normalize(brand)))
    art = tc.normalize(article)
    if informative_article(art):
        conn.execute(
            "INSERT INTO tnved_synonym_articles(article_norm,code,name,n,source) VALUES (?,?,?,1,?) "
            "ON CONFLICT(article_norm,code) DO UPDATE SET n=n+1", (art, code, name.strip(), source))
    conn.commit()


def exact(conn, norm_name: str) -> tuple[str, int] | None:
    """(код, n) если для названия подтверждён ровно один код, иначе None."""
    try:
        rows = conn.execute("SELECT code, n FROM tnved_synonyms WHERE norm_name=?", (norm_name,)).fetchall()
    except sqlite3.OperationalError:
        return None
    return (rows[0][0], rows[0][1]) if len(rows) == 1 else None


def exact_any(conn, norm_names: list[str], norm_article: str = "") -> tuple[str, int, str] | None:
    """Точное совпадение по любому из названий (русское, исходное из инвойса) и по
    артикулу. Возвращает (код, n, чем найдено), только если ВСЕ найденные совпадения
    указывают на один код; расхождение (название говорит одно, артикул другое) —
    None, решает классификатор с прецедентами."""
    found: dict[str, tuple[int, str]] = {}
    for nm in norm_names:
        hit = exact(conn, nm)
        if hit:
            n0, via0 = found.get(hit[0], (0, "название"))
            found[hit[0]] = (n0 + hit[1], via0)
    if informative_article(norm_article):
        try:
            rows = conn.execute("SELECT code, n FROM tnved_synonym_articles WHERE article_norm=?",
                                (norm_article,)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        if len(rows) == 1:
            n0, via0 = found.get(rows[0][0], (0, "артикул"))
            found[rows[0][0]] = (n0 + rows[0][1], via0 if n0 else "артикул")
        elif len(rows) > 1:
            return None
    if len(found) != 1:
        return None
    code, (n, via) = next(iter(found.items()))
    return code, n, via


def _tokens(norm: str) -> set[str]:
    return {tc._stem(w) for w in norm.split() if w and w not in tc._STOPWORDS and len(w) > 1}


def similar(conn, name, k: int = 4, exclude_codes=(), brand: str = "") -> list[dict]:
    """Подтверждённые решения по похожим названиям (по доле общих стемов), кроме точно
    такого же названия — оно обрабатывается exact(). name — строка или список названий
    (русское и исходное): берётся наибольшая похожесть. Совпадение бренда добавляет
    0.1 к похожести (слабый признак, только при наличии общих слов). По одному коду —
    лучшая запись. exclude_codes — коды, которые не использовать (оценка на «новых»)."""
    names = [name] if isinstance(name, str) else list(name)
    keys = {tc.normalize(x) for x in names if tc.normalize(x)}
    qs = [_tokens(x) for x in keys]
    qs = [q for q in qs if q]
    if not qs:
        return []
    bnorm = tc.normalize(brand)
    try:
        rows = conn.execute("SELECT norm_name, code, name, n, brand FROM tnved_synonyms").fetchall()
    except sqlite3.OperationalError:
        return []
    best: dict[str, tuple[float, dict]] = {}
    for norm, code, nm, n, br in rows:
        if norm in keys or code in exclude_codes:
            continue
        t = _tokens(norm)
        if not t:
            continue
        sim = max(len(q & t) / len(q | t) for q in qs)
        if sim > 0 and bnorm and br == bnorm:
            sim = min(1.0, sim + 0.1)
        if sim > 0 and (code not in best or sim > best[code][0]):
            best[code] = (sim, {"name": nm, "code": code, "n": n, "sim": round(sim, 2)})
    return [v for _, v in sorted(best.values(), key=lambda x: -x[0])[:k]]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", default=DEFAULT_GOLDEN)
    args = ap.parse_args()
    print("пар (название, код) импортировано:", import_golden(args.csv))
