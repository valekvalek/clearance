"""
Эталонная база товаров: сначала смотрим название, описание и материал товара, ищем в базе
похожие по всем этим полям и только потом ставим код ТН ВЭД (до обращения к модели).

Источник — tests/golden_tnved/reference_examples.csv (эталонные выгруженные примеры, колонки
«Торговая марка, Наименование, Материал, Описание, …, Код ТНВЭД, …»). Повторы строк НЕ
убираются: в таблице tnved_reference лежат все строки, а повторы работают как «голоса»
(в памяти одинаковые записи объединяются со счётчиком n).

Как считается сходство товара с записью базы:
  - название (вес 0.6): взвешенный Жаккар по основам слов (вес слова — idf по названиям базы,
    поэтому «светильник» значит меньше, чем «ld ist70»);
  - описание (0.25) и материал (0.15) — только если они есть и у товара, и у записи (метки
    «Назначение:», «Материал изготовления:», ссылки и заглушки в расчёт не входят);
  - итог = взвешенное среднее по имеющимся полям.
По K=12 ближайшим записям считается голос за каждый код: сумма (сходство² × (1+0.25·ln n)).
Уровень ответа (пороги в tnved/config.json, секция "reference"):
  high   — сходство и доля голосов за лидирующий код не ниже порогов high: код ставится сразу;
  medium — не ниже порогов medium: код ставится, но товар отмечается «проверить код»;
  иначе  — база кода не даёт; соседи уходят моделью как прецеденты (tnved/v2.py).

Записи из прочих источников (source='fsa' — декларации соответствия, коды заявителей)
дают соседей-прецедентов для модели, но уровень high/medium не поднимают: код без обращения
к модели ставится только по записям source 'reference' и 'golden'.

Импорт:       python -m tnved.reference [--csv путь]        (пересоздаёт tnved_reference)
              python -m tnved.reference --with-extra        (+ золотая база и ФСА, кроме holdout)
Калибровка:   python -m tnved.reference --calibrate         (leave-one-name-out, без модели)
"""
import argparse
import csv
import json
import math
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import tnved_classifier as tc  # noqa: E402

ROOT = os.path.dirname(APP_DIR)
GOLDEN_DIR = os.path.join(ROOT, "tests", "golden_tnved")
DEFAULT_CSV = os.path.join(GOLDEN_DIR, "reference_examples.csv")
GOLDEN_CSV = os.path.join(GOLDEN_DIR, "golden_tnved.csv")
HOLDOUT_CSV = os.path.join(GOLDEN_DIR, "golden_tnved_holdout.csv")
FSA_JSONL = os.path.join(ROOT, "training", "fsa", "fsa_train_candidates.jsonl")
TRUSTED_SOURCES = ("reference", "golden")
W_NAME, W_DESC, W_MAT = 0.6, 0.25, 0.15
K_NEIGHBORS = 12
MIN_NAME_SIM = 0.2

_SCHEMA = """
DROP TABLE IF EXISTS tnved_reference;
CREATE TABLE tnved_reference (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    material TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    brand TEXT NOT NULL DEFAULT '',
    code TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'reference'
);
CREATE INDEX idx_tnved_reference_code ON tnved_reference(code);
"""

# заглушки и служебные слова описания (метки обогащения, ссылки)
_PLACEHOLDER = {"", "-", "—", "нет", "не указано", "уточняется", "ссылка на описание", "без тм", "oem"}
_DESC_STOP_WORDS = ("назначение", "область", "применения", "материал", "изготовления", "технические",
                    "параметры", "ссылка", "тип", "не", "указано", "уточняется")
_URL = re.compile(r"https?://\S+")
_SPLIT = re.compile(r"\d+|[^\W\d_]+")
# кириллические двойники латиницы в обозначениях моделей (слова без цифр не трогаем)
_HOMOGLYPHS = str.maketrans("авекмнорстух", "abekmhopctxy")
_DESC_STOP: set[str] = set()


def _desc_stop() -> set[str]:
    global _DESC_STOP
    if not _DESC_STOP:
        _DESC_STOP = {tc._stem(w) for w in _DESC_STOP_WORDS}
    return _DESC_STOP


def tokens(text, drop_labels: bool = False) -> frozenset:
    """Основы значимых слов поля; пусто для заглушек."""
    text = _URL.sub(" ", (text or "")).strip()
    if text.lower() in _PLACEHOLDER:
        return frozenset()
    stems = set()
    for w in tc.normalize(text).split():
        if w in tc._STOPWORDS:
            continue
        if any(ch.isdigit() for ch in w):  # модель/размер: «К-8000С» == «K-8000C», «500г» == «500 г»
            parts = _SPLIT.findall(w.translate(_HOMOGLYPHS))
        else:
            parts = [w]
        stems.update(tc._stem(x) for x in parts if len(x) > 1 and x not in tc._STOPWORDS)
    return frozenset(stems - _desc_stop()) if drop_labels else frozenset(stems)


def ensure(conn) -> bool:
    try:
        return conn.execute("SELECT COUNT(*) FROM tnved_reference").fetchone()[0] > 0
    except sqlite3.OperationalError:
        return False


def _extra_rows():
    """Золотая база (подтверждённые, без holdout) и ФСА (один код) — строки (name, material,
    description, brand, code, source)."""
    held = set()
    if os.path.exists(HOLDOUT_CSV):
        with open(HOLDOUT_CSV, encoding="utf-8", newline="") as f:
            held = {tc.normalize(r["name"]) for r in csv.DictReader(f)}
    rows = []
    if os.path.exists(GOLDEN_CSV):
        with open(GOLDEN_CSV, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                code, name = (r.get("expected_code") or "").strip(), (r.get("name") or "").strip()
                if (r.get("status") or "").strip() == "подтверждён" and name and code.isdigit() \
                        and len(code) == 10 and tc.normalize(name) not in held:
                    rows.append((name, (r.get("material") or "").strip(), (r.get("description") or "").strip(),
                                 (r.get("brand") or "").strip(), code, "golden"))
    if os.path.exists(FSA_JSONL):
        with open(FSA_JSONL, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                code, name = ((r.get("tnved_codes") or [""])[0] or "").strip(), (r.get("name") or "").strip()
                if name and code.isdigit() and len(code) == 10 and tc.normalize(name) not in held:
                    rows.append((name, "", (r.get("description") or "").strip(),
                                 (r.get("trade_mark") or "").strip(), code, "fsa"))
    return rows


def import_csv(csv_path: str = DEFAULT_CSV, db_path: str | None = None, with_extra: bool = False) -> int:
    """Пересоздаёт tnved_reference из CSV. Берутся все строки с названием и 10-значным кодом."""
    rows = []
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            name, code = (r.get("Наименование") or "").strip(), (r.get("Код ТНВЭД") or "").strip()
            if name and code.isdigit() and len(code) == 10:
                rows.append((name, (r.get("Материал") or "").strip(), (r.get("Описание") or "").strip(),
                             (r.get("Торговая марка") or "").strip(), code, "reference"))
    if with_extra:
        rows += _extra_rows()
    conn = sqlite3.connect(db_path or tc.DB_PATH)
    try:
        conn.executescript(_SCHEMA)
        conn.executemany("INSERT INTO tnved_reference(name, material, description, brand, code, source) "
                         "VALUES (?,?,?,?,?,?)", rows)
        conn.commit()
    finally:
        conn.close()
    return len(rows)


class Index:
    """Записи базы в памяти (одинаковые объединены со счётчиком) + инвертированный индекс по названию."""

    def __init__(self, rows):
        groups: dict = {}
        for row in rows:
            name, material, description, code = row[:4]
            trusted = (row[4] if len(row) > 4 else "reference") in TRUSTED_SOURCES
            key = (tokens(name), tokens(description, True), tokens(material), code, trusted)
            g = groups.get(key)
            if g:
                g["n"] += 1
            else:
                groups[key] = {"name": name, "name_t": key[0], "desc_t": key[1], "mat_t": key[2],
                               "code": code, "n": 1, "name_key": tc.normalize(name), "trusted": trusted}
        self.entries = [g for g in groups.values() if g["name_t"]]
        df = Counter(t for e in self.entries for t in e["name_t"])
        n = len(self.entries)
        self.idf = {t: math.log(1 + n / c) for t, c in df.items()}
        self.inv = defaultdict(list)
        for i, e in enumerate(self.entries):
            for t in e["name_t"]:
                self.inv[t].append(i)

    def _wj(self, a: frozenset, b: frozenset) -> float:
        if not a or not b:
            return 0.0
        idf = self.idf
        inter = sum(idf.get(t, 1.0) for t in a & b)
        union = sum(idf.get(t, 1.0) for t in a | b)
        return inter / union if union else 0.0

    def lookup(self, names: list[str], description, material, exclude_name_keys=frozenset(), cfg=None) -> dict:
        q_names = [t for t in (tokens(n) for n in names) if t]
        if not q_names:
            return {"level": "none", "code": "", "neighbors": []}
        q_desc, q_mat = tokens(description, True), tokens(material)
        cand = set()
        for qt in q_names:
            for t in qt:
                cand.update(self.inv.get(t, ()))
        scored = []
        for i in cand:
            e = self.entries[i]
            if e["name_key"] in exclude_name_keys:
                continue
            s_name = max(self._wj(qt, e["name_t"]) for qt in q_names)
            if s_name < MIN_NAME_SIM:
                continue
            num, den = W_NAME * s_name, W_NAME
            if q_desc and e["desc_t"]:
                num, den = num + W_DESC * self._wj(q_desc, e["desc_t"]), den + W_DESC
            if q_mat and e["mat_t"]:
                num, den = num + W_MAT * self._wj(q_mat, e["mat_t"]), den + W_MAT
            scored.append((num / den, e))
        scored.sort(key=lambda x: -x[0])
        top = scored[:K_NEIGHBORS]
        if not top:
            return {"level": "none", "code": "", "neighbors": []}
        votes: dict = defaultdict(float)
        best_sim: dict = {}
        trusted_sim: dict = defaultdict(float)
        support: dict = defaultdict(int)
        sample: dict = {}
        for sim, e in top:
            votes[e["code"]] += sim * sim * (1 + 0.25 * math.log(e["n"]))
            best_sim[e["code"]] = max(best_sim.get(e["code"], 0.0), sim)
            if e["trusted"]:
                trusted_sim[e["code"]] = max(trusted_sim[e["code"]], sim)
            support[e["code"]] += e["n"]
            sample.setdefault(e["code"], e["name"])
        total = sum(votes.values())
        code = max(votes, key=votes.get)
        share = votes[code] / total if total else 0.0
        neighbors = [{"name": sample[c], "code": c, "n": support[c], "sim": round(best_sim[c], 2)}
                     for c in sorted(votes, key=lambda c: -best_sim[c])[:4]]
        res = {"code": code, "sim": round(best_sim[code], 3), "share": round(share, 3),
               "support": support[code], "neighbors": neighbors}
        # уровень high/medium — только если лидирующий код подтверждён эталонной записью
        res["level"] = level_for(trusted_sim[code], res["share"], cfg)
        return res


def level_for(sim: float, share: float, cfg=None) -> str:
    c = (cfg or _config())
    if sim >= c["sim_high"] and share >= c["share_high"]:
        return "high"
    if sim >= c["sim_medium"] and share >= c["share_medium"]:
        return "medium"
    return "low"


_CFG = None


def _config() -> dict:
    global _CFG
    if _CFG is None:
        from tnved import confidence as C
        _CFG = C.load_config().get("reference") or {"sim_high": 2, "share_high": 2, "sim_medium": 2, "share_medium": 2}
    return _CFG


_INDEX_CACHE: dict = {}


def index_for(conn) -> "Index | None":
    """Индекс по базе подключения; пересобирается, если изменилось число строк."""
    try:
        n = conn.execute("SELECT COUNT(*) FROM tnved_reference").fetchone()[0]
    except sqlite3.OperationalError:
        return None
    if not n:
        return None
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    key = (path, n)
    if key not in _INDEX_CACHE:
        _INDEX_CACHE.clear()
        try:
            rows = conn.execute("SELECT name, material, description, code, source FROM tnved_reference").fetchall()
        except sqlite3.OperationalError:  # таблица старой схемы, без колонки source
            rows = conn.execute("SELECT name, material, description, code FROM tnved_reference").fetchall()
        _INDEX_CACHE[key] = Index(rows)
    return _INDEX_CACHE[key]


def lookup(conn, item: dict) -> "dict | None":
    """Поиск по эталонной базе для товара классификатора; None — базы нет."""
    idx = index_for(conn)
    if idx is None:
        return None
    names = [item.get("product_name") or "", item.get("name_original") or ""]
    return idx.lookup(names, item.get("description"), tc.clean_material(item.get("material")))


def _load_csv_rows(csv_path):
    rows = []
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            name, code = (r.get("Наименование") or "").strip(), (r.get("Код ТНВЭД") or "").strip()
            if name and code.isdigit() and len(code) == 10:
                rows.append((name, (r.get("Материал") or "").strip(), (r.get("Описание") or "").strip(), code))
    return rows


def calibrate(csv_path: str = DEFAULT_CSV) -> None:
    """Leave-one-name-out: каждый уникальный товар (название+описание+материал) ищется в базе
    без записей с тем же названием — так видно, как работает поиск на НОВЫХ названиях."""
    rows = _load_csv_rows(csv_path)
    idx = Index(rows)
    queries = {}
    for name, material, description, code in rows:
        queries.setdefault((name, description, material), set()).add(code)
    results = []
    for (name, description, material), codes in queries.items():
        r = idx.lookup([name], description, tc.clean_material(material),
                       exclude_name_keys={tc.normalize(name)}, cfg={"sim_high": 2, "share_high": 2,
                                                                     "sim_medium": 2, "share_medium": 2})
        if r["code"]:
            results.append((r["sim"], r["share"], r["code"] in codes))
        else:
            results.append((0.0, 0.0, False))
    n = len(results)
    print(f"записей базы: {len(rows)}, уникальных товаров-запросов: {n} (leave-one-name-out)")
    print("sim>= share>= | отвечено  точность")
    for sim_t in (0.4, 0.45, 0.5, 0.6, 0.7, 0.8, 0.9):
        for share_t in (0.7, 0.85, 0.95):
            ans = [ok for s, sh, ok in results if s >= sim_t and sh >= share_t]
            if ans:
                print(f"{sim_t:4} {share_t:5} | {len(ans):5d} ({100 * len(ans) / n:4.1f}%)  {100 * sum(ans) / len(ans):5.1f}%")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--with-extra", action="store_true", help="добавить золотую базу и ФСА (кроме holdout)")
    args = ap.parse_args()
    if args.calibrate:
        calibrate(args.csv)
    else:
        print("строк импортировано в tnved_reference:", import_csv(args.csv, with_extra=args.with_extra))
