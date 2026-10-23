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
        try:  # тексты уровней из официального текста ЕЭК (import_tnved_eec.py)
            seen_levels: dict[str, set] = {h: set() for h in bodies}
            for code, path in conn.execute("SELECT code, path FROM tnved_official_paths"):
                for part in path.split(" / "):
                    if code[:4] in bodies and part and part not in seen_levels[code[:4]]:
                        seen_levels[code[:4]].add(part)
                        bodies[code[:4]].append(part)
        except sqlite3.OperationalError:
            pass
        try:  # полные названия позиций из ЕЭК: у ФНС у 271 позиции названия нет
            official_titles = dict(conn.execute("SELECT heading_code, title FROM tnved_official_headings"))
        except sqlite3.OperationalError:
            official_titles = {}
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
            title = official_titles.get(h) or titles[h] or tree_titles.get(h, "")
            meta.append((h, groups[h], title))
            fts.append((h, title, " ".join(bodies[h])))
        conn.executemany("INSERT INTO tnved_heading_meta VALUES (?,?,?)", meta)
        conn.executemany("INSERT INTO tnved_heading_fts VALUES (?,?,?)", fts)
        conn.commit()
        return len(meta)
    finally:
        conn.close()


# Начало бытового слова -> основа слова, которым товар назван в тексте ТН ВЭД.
# Без них поиск позиций не доходит до нужной: «припой» в номенклатуре не
# встречается, там «пайка» (3810 пасты, 8311 проволока); «разъём» — «соединитель».
_QUERY_SYNONYMS = {
    "припо": ["пайк"], "паяльн": ["пайк"],
    "разъем": ["соединител"], "разъём": ["соединител"], "коннектор": ["соединител"],
}


# То же, но по точному слову (начало «плат» совпало бы с «платье», «платок»):
# «печатная плата» в номенклатуре — «схемы печатные» (8534).
_QUERY_SYNONYMS_EXACT = {
    **{w: ["схем"] for w in ("плата", "платы", "плате", "плату", "платой", "плат")},
    "pcb": ["схем", "печатн"], "pcba": ["схем", "печатн"],
}


def _query_terms(query: str) -> list[str]:
    stems = []
    for w in tc.normalize(query).split():
        if w and w not in tc._STOPWORDS:
            stems.append(tc._stem(w))
            stems += [x for k, v in _QUERY_SYNONYMS.items() if w.startswith(k) for x in v]
            stems += _QUERY_SYNONYMS_EXACT.get(w, [])
    return list(dict.fromkeys(stems))


def search(conn, query: str, k: int = 15) -> list[tuple[str, float]]:
    """Топ-k позиций по bm25 по документам позиций: [(heading_code, score)],
    score > 0, больше — лучше. Стоп-слова и грубый стемминг — как в плоском поиске,
    плюс таможенные синонимы бытовых слов (_QUERY_SYNONYMS)."""
    terms = [f'"{s}"*' for s in _query_terms(query)]
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


# начало пронумерованного примечания: «… 8. В товарной позиции 8534 …»
_NOTE_SPLIT = re.compile(r"(?<=[.;:])\s+(?=\d{1,2}\.\s+[(«\"A-ZА-ЯЁ])")


def _full_group_notes(conn, group_code: str) -> str:
    """Полные примечания ЕЭК (import_tnved_eec.py), иначе — обрезанные ФНС."""
    for sql in ("SELECT notes FROM tnved_official_group_notes WHERE group_code=?",
                "SELECT notes FROM tnved_group_notes WHERE group_code=?"):
        try:
            row = conn.execute(sql, (group_code,)).fetchone()
        except sqlite3.OperationalError:
            continue
        if row and row[0]:
            return row[0]
    return ""


def group_notes(conn, group_code: str, limit: int = 1500, headings=()) -> str:
    """Примечания к группе для подсказки модели. headings — позиции-кандидаты
    этой группы: берутся пункты, где они упомянуты (напр. прим. 8 «схемы
    печатные» для 8534, прим. 11 «модули светодиодные» для 8539), плюс первый
    пункт («в данную группу не включаются»), в порядке текста. Без headings —
    начало текста, как раньше."""
    text = _full_group_notes(conn, group_code)
    if not headings or not text:
        return text[:limit]
    parts = _NOTE_SPLIT.split(text)
    # parts[0] — «Примечания:» (+ примечания к разделу), parts[1] — примечание 1
    keep = [i for i, p in enumerate(parts)
            if i <= 1 or any(h in p.replace(" ", "") for h in headings)]
    out = " … ".join(parts[i] for i in keep)
    return out[:limit]


def leaf_path(conn, code: str) -> str:
    """Тексты промежуточных подпозиций над листом (tnved_tree), без уровня
    позиции и выше (позиция показывается отдельно), без тегов [с ..]/[по ..].
    Цепочки с самоссылкой parent_code обрываются (~24% узлов источника).
    Если импортирован официальный текст ЕЭК (tnved_official_paths) — берётся он."""
    official = tc.official_path_title(conn, code)
    if official is not None:
        return official
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
