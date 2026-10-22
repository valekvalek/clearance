"""
Импорт дерева подпозиций ТН ВЭД ЕАЭС (все уровни, с текстами) из стороннего
скрейпа alta.ru в app/tnved.db как таблицу tnved_tree.

Зачем: официальная выгрузка ФНС (см. build_tnved_db.py) содержит только
10-значные листовые коды без текста промежуточных подпозиций (см. docstring
в tnved_classifier.py). Источник — отдельный, несвязанный с этим проектом
прогон скрейпера alta.ru (`/tnved/get_tree/`, разрешено robots.txt),
17 819 узлов дерева: code, parent_code, level, title. Пошлины/НДС/акциз и
примечания к разделам/группам в этом источнике НЕ выгружены (поле notes
пустое у всех строк, таблица пошлин пуста — скрейпинг упёрся в капчу) —
импортируем только code/parent_code/level/title.

Запуск разово (источник — внешний файл вне этого репозитория, путь задаётся
явно, по умолчанию не предполагается):
    python import_tnved_tree.py --source /path/to/customs_db.sqlite

Таблица НЕ попадает под DROP TABLE при пересборке tnved_codes/tnved_fts
(build_tnved_db.py трогает только свои таблицы) — но этот скрипт тоже
идемпотентен (DROP+CREATE только tnved_tree) и его можно перезапускать.
"""
import argparse
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "tnved.db")

_SCHEMA = """
DROP TABLE IF EXISTS tnved_tree;
CREATE TABLE tnved_tree (
    code TEXT PRIMARY KEY,
    parent_code TEXT,
    level INTEGER NOT NULL,
    title TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tnved_tree_parent ON tnved_tree(parent_code);
"""


def import_tree(source_path: str, db_path: str = DEFAULT_DB) -> int:
    src = sqlite3.connect(source_path)
    rows = src.execute(
        "SELECT code, parent_code, level, title FROM tnved_node"
    ).fetchall()
    src.close()

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.executemany(
            "INSERT INTO tnved_tree(code,parent_code,level,title) VALUES (?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return len(rows)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", required=True,
                     help="путь к customs_db.sqlite со скрейпом alta.ru (таблица tnved_node)")
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()
    n = import_tree(args.source, args.db)
    print(f"✅ Импортировано {n} узлов дерева в {args.db} (таблица tnved_tree)")
