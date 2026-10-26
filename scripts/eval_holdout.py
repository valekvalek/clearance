"""
Оценка точности подбора кода ТН ВЭД на большом holdout.

  build    — собирает tests/golden_tnved/holdout_large.csv: 26 подтверждённых товаров старого
             holdout + случайная выборка однозначных товаров из reference_examples.csv
             (название -> ровно один код; совпавшие с seed-синонимами исключаются).
  prepare  — для каждой конфигурации делает копию app/tnved.db, из которой вырезаны все названия
             holdout (эталонная база, синонимы, артикулы), и подмешивает нужные источники.
  run <cfg> — прогон классификатора (Yandex, без кэша) на holdout; метрики в JSON.
  report   — сводная таблица по готовым JSON.

Каталог результатов и копий БД: --work (по умолчанию training/eval_work, не коммитится).
Запуск: python scripts/eval_holdout.py build|prepare|run <cfg>|report
"""
import argparse
import csv
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
sys.path.insert(0, APP)
GOLDEN_DIR = os.path.join(ROOT, "tests", "golden_tnved")
OLD_HOLDOUT = os.path.join(GOLDEN_DIR, "golden_tnved_holdout.csv")
REFERENCE = os.path.join(GOLDEN_DIR, "reference_examples.csv")
HOLDOUT = os.path.join(GOLDEN_DIR, "holdout_large.csv")
SYNTH = os.path.join(ROOT, "training", "synthetic", "synthetic_verified.jsonl")
STUB = {"", "-", "—", "нет", "ссылка на описание"}
FIELDS = ["name", "description", "material", "expected_code", "confirmation_source", "status", "article", "brand"]

# конфигурация: (pipeline, use_synonyms, источники эталонной базы)
CONFIGS = {
    "v1_none": ("v1", False, ()),
    "v1_ref": ("v1", True, ("reference",)),
    "v2_none": ("v2", False, ()),
    "v2_ref": ("v2", True, ("reference",)),
    "v2_ref_golden_fsa": ("v2", True, ("reference", "golden", "fsa")),
    "v2_ref_all": ("v2", True, ("reference", "golden", "fsa", "synthetic")),
}


def clean(s):
    s = " ".join((s or "").split())
    return "" if s.lower() in STUB else s


def build(n, seed):
    import tnved_classifier as tc
    old = list(csv.DictReader(open(OLD_HOLDOUT, encoding="utf-8")))
    taken = {tc.normalize(r["name"]) for r in old}
    by_name = {}
    for r in csv.DictReader(open(REFERENCE, encoding="utf-8-sig", newline="")):
        name, code = clean(r.get("Наименование")), (r.get("Код ТНВЭД") or "").strip()
        if not name or not (code.isdigit() and len(code) == 10) or not 3 <= len(name) <= 200:
            continue
        by_name.setdefault(tc.normalize(name), []).append(
            {"name": name, "description": clean(r.get("Описание")), "material": clean(r.get("Материал")),
             "brand": clean(r.get("Торговая марка")), "code": code})
    pool = []
    for key, rows in by_name.items():
        if key in taken or len({r["code"] for r in rows}) != 1 or tc._match_synonym(key):
            continue
        pool.append(rows[0])
    random.Random(seed).shuffle(pool)
    sample = pool[:n]
    with open(HOLDOUT, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, FIELDS)
        w.writeheader()
        for r in old:
            w.writerow({k: r.get(k, "") for k in FIELDS})
        for r in sample:
            w.writerow({"name": r["name"], "description": r["description"], "material": r["material"],
                        "expected_code": r["code"], "confirmation_source": "reference_examples.csv",
                        "status": "эталон", "article": "", "brand": r["brand"]})
    print(f"holdout: {len(old)} подтверждённых + {len(sample)} из эталона (пул однозначных {len(pool)}) -> {HOLDOUT}")


def held_keys():
    import tnved_classifier as tc
    rows = list(csv.DictReader(open(HOLDOUT, encoding="utf-8")))
    return rows, {tc.normalize(r["name"]) for r in rows}, {tc.normalize(r["article"]) for r in rows if r["article"]}


def prepare(work):
    import tnved_classifier as tc
    _, held, held_art = held_keys()
    os.makedirs(os.path.join(work, "db"), exist_ok=True)
    src = sqlite3.connect(os.path.join(APP, "tnved.db"))
    base = src.execute("SELECT name, material, description, brand, code, source FROM tnved_reference").fetchall()
    src.close()
    base = [r for r in base if tc.normalize(r[0]) not in held]
    synth = []
    if os.path.exists(SYNTH):
        for line in open(SYNTH, encoding="utf-8"):
            r = json.loads(line)
            if tc.normalize(r["name"]) not in held:
                synth.append((r["name"], r.get("material", ""), r.get("description", ""), r.get("brand", ""),
                              r["code"], "synthetic"))
    print(f"из базы вырезано по названиям holdout; строк базы {len(base)}, синтетики {len(synth)}")
    for cfg, (_, _, sources) in CONFIGS.items():
        path = os.path.join(work, "db", cfg + ".db")
        shutil.copy(os.path.join(APP, "tnved.db"), path)
        conn = sqlite3.connect(path)
        rows = [r for r in base if r[5] in sources] + (synth if "synthetic" in sources else [])
        conn.execute("DELETE FROM tnved_reference")
        conn.executemany("INSERT INTO tnved_reference(name, material, description, brand, code, source) "
                         "VALUES (?,?,?,?,?,?)", rows)
        for (nm,) in conn.execute("SELECT norm_name FROM tnved_synonyms").fetchall():
            if nm in held:
                conn.execute("DELETE FROM tnved_synonyms WHERE norm_name=?", (nm,))
        for (a,) in conn.execute("SELECT article_norm FROM tnved_synonym_articles").fetchall():
            if a in held_art:
                conn.execute("DELETE FROM tnved_synonym_articles WHERE article_norm=?", (a,))
        conn.execute("DELETE FROM classify_cache")
        conn.commit()
        conn.close()
        print(f"  {cfg}: строк в tnved_reference {len(rows)}")


def run(cfg, work, limit):
    pipeline, synonyms, _ = CONFIGS[cfg]
    os.environ["TNVED_DB_PATH"] = os.path.join(work, "db", cfg + ".db")
    from tnved import evaluate as E
    rows = list(csv.DictReader(open(HOLDOUT, encoding="utf-8")))
    if limit:
        rows = rows[:limit]
    m = E.evaluate(rows, use_cache=False, use_synonyms=synonyms, pipeline=pipeline)
    m["config"] = cfg
    m["expected"] = {r["name"]: r["expected_code"].strip() for r in rows}
    m["sources"] = {r["name"]: r["status"] for r in rows}
    os.makedirs(os.path.join(work, "results"), exist_ok=True)
    out = os.path.join(work, "results", cfg + ".json")
    json.dump(m, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    print(cfg, "готово ->", out)


def _stats(m, only=None):
    exp, res, srcs = m["expected"], m["results"], m["sources"]
    names = [n for n in exp if only is None or srcs[n] == only]
    tot = len(names)
    ans = [n for n in names if (res[n].get("code") or "").strip()]
    d = lambda n: "".join(c for c in res[n]["code"] if c.isdigit())
    h10 = sum(d(n) == exp[n] for n in ans)
    h4 = sum(d(n)[:4] == exp[n][:4] for n in ans)
    ref_auto = [n for n in ans if str(res[n].get("reason", "")).startswith("похожие эталонные")]
    ref_hit = sum(d(n) == exp[n] for n in ref_auto)
    return tot, len(ans), h10, h4, len(ref_auto), ref_hit


def report(work):
    print(f"{'конфигурация':20} {'всего':>5} {'ответ':>6} {'код 10 зн.':>16} {'позиция 4 зн.':>16} {'автоответ базы':>20}")
    for cfg in CONFIGS:
        p = os.path.join(work, "results", cfg + ".json")
        if not os.path.exists(p):
            continue
        m = json.load(open(p, encoding="utf-8"))
        for label, only in (("все", None), ("подтв.", "подтверждён")):
            tot, ans, h10, h4, ra, rh = _stats(m, only)
            if not tot:
                continue
            print(f"{cfg + ' ' + label:20} {tot:5d} {ans:6d} {h10:5d} ({100 * h10 / tot:4.1f}%) "
                  f"{h4:5d} ({100 * h4 / tot:4.1f}%) {rh:4d}/{ra:<4d}" + (f" ({100 * rh / ra:.0f}%)" if ra else ""))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["build", "prepare", "run", "report"])
    ap.add_argument("cfg", nargs="?")
    ap.add_argument("--n", type=int, default=400)
    ap.add_argument("--seed", type=int, default=20261026)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--work", default=os.path.join(ROOT, "training", "eval_work"))
    a = ap.parse_args()
    if a.cmd == "build":
        build(a.n, a.seed)
    elif a.cmd == "prepare":
        prepare(a.work)
    elif a.cmd == "run":
        run(a.cfg, a.work, a.limit)
    else:
        report(a.work)
