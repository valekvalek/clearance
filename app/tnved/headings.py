"""
Поиск на уровне товарной позиции (4 знака) для классификатора v2.

Для каждой позиции строится «документ»: название позиции + наименования всех
её листовых кодов + тексты промежуточных подпозиций из tnved_tree (импорт
alta.ru). Причина: в выгрузке ФНС у части позиций (~21%) нет названия, а
листы — обрывки фраз; поиск по одному названию позиции даёт мало. На эталоне
(63 уникальных названия) нужная позиция попадает в топ-15 у 41/63 против 24/63
при плоском поиске по 15 листам.

Таблицы создаются отдельным вызовом build() (идемпотентно, только свои
таблицы) — их надо перезапускать после пересборки tnved.db:
    python -m tnved.headings
"""
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import tnved_classifier as tc  # noqa: E402

_TAGS = re.compile(r"\[[^\]]*\]")


def _strip_tags(text: str) -> str:
    return re.sub(r"\s+", " ", _TAGS.sub(" ", text or "")).strip()


def build(db_path: str | None = None) -> int:
    """Пересоздаёт tnved_heading_meta и tnved_heading_fts. Возвращает число позиций."""
    conn = sqlite3.connect(db_path or tc.DB_PATH)
    try:
        titles = {h: (n or "") for h, n in conn.execute(
            "SELECT heading_code, MAX(heading_name) FROM tnved_codes GROUP BY heading_code")}
        groups = dict(conn.execute("SELECT heading_code, MAX(group_code) FROM tnved_codes GROUP BY heading_code"))
        tree_titles = {}
        bodies: dict[str, list[str]] = {h: [] for h in titles}
        try:
            for code, title in conn.execute("SELECT code, title FROM tnved_tree WHERE level=4"):
                tree_titles[code] = _strip_tags(title)
            for code, title in conn.execute("SELECT code, title FROM tnved_tree WHERE level BETWEEN 5 AND 9"):
                if code[:4] in bodies:
                    bodies[code[:4]].append(_strip_tags(title))
        except sqlite3.OperationalError:
            pass  # tnved_tree не импортирована — документ из листьев и названия
        for h, name in conn.execute("SELECT heading_code, name FROM tnved_codes"):
            bodies[h].append(name)
        conn.executescript("""
            DROP TABLE IF EXISTS tnved_heading_meta;
            CREATE TABLE tnved_heading_meta (heading_code TEXT PRIMARY KEY, group_code TEXT NOT NULL, title TEXT NOT NULL);
            DROP TABLE IF EXISTS tnved_heading_fts;
            CREATE VIRTUAL TABLE tnved_heading_fts USING fts5(heading UNINDEXED, title, body);
        """)
        meta, fts = [], []
        for h in sorted(titles):
            title = titles[h] or tree_titles.get(h, "")
            meta.append((h, groups[h], title))
            fts.append((h, title, " ".join(bodies[h])))
        conn.executemany("INSERT INTO tnved_heading_meta VALUES (?,?,?)", meta)
        conn.executemany("INSERT INTO tnved_heading_fts VALUES (?,?,?)", fts)
        conn.commit()
        return len(meta)
    finally:
        conn.close()


def search(conn, query: str, k: int = 15) -> list[tuple[str, float]]:
    """Топ-k позиций по bm25 по документам позиций: [(heading_code, score)],
    score > 0, больше — лучше. Стоп-слова и грубый стемминг — как в плоском поиске."""
    terms = [f'"{tc._stem(w)}"*' for w in tc.normalize(query).split()
             if w and w not in tc._STOPWORDS]
    if not terms:
        return []
    try:
        rows = conn.execute(
            "SELECT heading, -bm25(tnved_heading_fts, 0, 1, 1) FROM tnved_heading_fts "
            "WHERE tnved_heading_fts MATCH ? ORDER BY 2 DESC LIMIT ?",
            (" OR ".join(terms), k)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(r[0], float(r[1])) for r in rows]


def heading_info(conn, heading_code: str) -> dict | None:
    row = conn.execute("SELECT group_code, title FROM tnved_heading_meta WHERE heading_code=?",
                       (heading_code,)).fetchone()
    return {"heading": heading_code, "group": row[0], "title": row[1]} if row else None


def group_notes(conn, group_code: str, limit: int = 1500) -> str:
    try:
        row = conn.execute("SELECT notes FROM tnved_group_notes WHERE group_code=?", (group_code,)).fetchone()
    except sqlite3.OperationalError:
        return ""
    return (row[0] or "")[:limit] if row else ""


def leaf_path(conn, code: str) -> str:
    """Тексты промежуточных подпозиций над листом (tnved_tree), без уровня
    позиции и выше (позиция показывается отдельно), без тегов [с ..]/[по ..].
    Цепочки с самоссылкой parent_code обрываются (~24% узлов источника)."""
    try:
        row = conn.execute("SELECT parent_code FROM tnved_tree WHERE code=?", (code,)).fetchone()
    except sqlite3.OperationalError:
        return ""
    parts, seen = [], set()
    parent = row[0] if row else None
    while parent and parent not in seen:
        seen.add(parent)
        r = conn.execute("SELECT parent_code, level, title FROM tnved_tree WHERE code=?", (parent,)).fetchone()
        if not r or r[1] <= 4:
            break
        t = _strip_tags(r[2]).rstrip(":").strip()
        if t:
            parts.append(t)
        parent = r[0]
    parts.reverse()
    return " / ".join(parts)


def leaves(conn, heading_code: str, query: str, cap: int = 60) -> list[dict]:
    """Листья позиции для выбора кода: при >cap — самые релевантные запросу
    (bm25 по tnved_fts), остальные добираются в порядке кода."""
    rows = conn.execute("SELECT code, name FROM tnved_codes WHERE heading_code=? ORDER BY code",
                        (heading_code,)).fetchall()
    if len(rows) > cap:
        terms = [f'"{tc._stem(w)}"*' for w in tc.normalize(query).split() if w and w not in tc._STOPWORDS]
        ranked = []
        if terms:
            try:
                ranked = [r[0] for r in conn.execute(
                    "SELECT code FROM tnved_fts WHERE tnved_fts MATCH ? AND code LIKE ? ORDER BY bm25(tnved_fts) LIMIT ?",
                    (" OR ".join(terms), heading_code + "%", cap))]
            except sqlite3.OperationalError:
                ranked = []
        order = ranked + [r[0] for r in rows if r[0] not in set(ranked)]
        keep = set(order[:cap])
        rows = [r for r in rows if r[0] in keep]
    return [{"code": c, "name": n, "path": leaf_path(conn, c)} for c, n in rows]


if __name__ == "__main__":
    print("позиций:", build())
