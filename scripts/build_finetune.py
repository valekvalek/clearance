"""
Сборка обучающего датасета для Yandex AI Studio из golden_tnved.csv (подтверждён)
и reference_examples.csv. Дубли схлопываются, конфликтные входы (один вход — разные
коды) и всё, что попало в holdout (по названию), исключается.

С флагом --all добавляются деклараты ФСА (training/fsa/fsa_train_candidates.jsonl) и
проверенная синтетика (training/synthetic/synthetic_verified.jsonl). При конфликте входов
с разными кодами исключаются все варианты.

Запуск: python scripts/build_finetune.py [--out training/finetune_tnved_v2.jsonl] [--all]
"""
import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
import tnved_classifier as tc  # noqa: E402
from tnved.make_training import SYSTEM  # noqa: E402

GOLDEN = os.path.join(ROOT, "tests", "golden_tnved", "golden_tnved.csv")
HOLDOUT = os.path.join(ROOT, "tests", "golden_tnved", "golden_tnved_holdout.csv")
REFERENCE = os.path.join(ROOT, "tests", "golden_tnved", "reference_examples.csv")
FSA = os.path.join(ROOT, "training", "fsa", "fsa_train_candidates.jsonl")
SYNTH = os.path.join(ROOT, "training", "synthetic", "synthetic_verified.jsonl")
CODE_RE = re.compile(r"\d{10}")
STUB = {"", "-", "—", "нет", "ссылка на описание"}


def clean(s):
    s = " ".join((s or "").split())
    return "" if s.lower() in STUB else s


def read_golden(path, confirmed_only=True):
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if confirmed_only and (r.get("status") or "").strip() != "подтверждён":
                continue
            yield {"name": clean(r["name"]), "brand": clean(r.get("brand")),
                   "article": clean(r.get("article")), "material": clean(r.get("material")),
                   "description": clean(r.get("description")),
                   "code": (r["expected_code"] or "").strip()}


def read_reference(path):
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            yield {"name": clean(r["Наименование"]), "brand": clean(r["Торговая марка"]),
                   "article": "", "material": clean(r["Материал"]),
                   "description": clean(r["Описание"]), "code": (r["Код ТНВЭД"] or "").strip()}


def read_fsa(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            yield {"name": clean(r["name"]), "brand": clean(r.get("trade_mark")),
                   "article": clean(r.get("article") or r.get("model")), "material": "",
                   "description": clean(r.get("description")), "code": (r["tnved_codes"] or [""])[0].strip()}


def read_synthetic(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            yield {"name": clean(r["name"]), "brand": clean(r.get("brand")), "article": "",
                   "material": clean(r.get("material")), "description": clean(r.get("description")),
                   "code": (r["code"] or "").strip()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="добавить ФСА и проверенную синтетику")
    ap.add_argument("--out", default=os.path.join(ROOT, "training", "finetune_tnved_v2.jsonl"))
    args = ap.parse_args()
    out_path = args.out

    held = {tc.normalize(r["name"]) for r in read_golden(HOLDOUT, confirmed_only=False)}
    codes = defaultdict(set)
    shown = {}
    stats = {"rows": 0, "bad": 0, "held": 0}
    sources = [read_golden(GOLDEN), read_reference(REFERENCE)]
    if args.all:
        sources += [read_fsa(FSA), read_synthetic(SYNTH)]
    for src in sources:
        for r in src:
            stats["rows"] += 1
            if not r["name"] or not CODE_RE.fullmatch(r["code"]):
                stats["bad"] += 1
                continue
            if tc.normalize(r["name"]) in held:
                stats["held"] += 1
                continue
            key = tuple(tc.normalize(r[k]) for k in ("name", "description", "material", "article", "brand"))
            codes[key].add(r["code"])
            shown.setdefault(key, r)

    examples, conflicts = [], 0
    for key, cs in codes.items():
        if len(cs) != 1:
            conflicts += 1
            continue
        code = next(iter(cs))
        d = shown[key]
        lines = [f"Название: {d['name']}"]
        for label, k in (("Бренд", "brand"), ("Артикул", "article"),
                         ("Материал", "material"), ("Описание", "description")):
            if d[k]:
                lines.append(f"{label}: {d[k]}")
        examples.append({"request": [{"role": "system", "text": SYSTEM},
                                     {"role": "user", "text": "\n".join(lines)}],
                         "response": json.dumps({"heading": code[:4], "code": code}, ensure_ascii=False)})
    examples.sort(key=lambda e: e["request"][1]["text"])
    with open(out_path, "w", encoding="utf-8") as f:
        for e in examples:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"прочитано строк: {stats['rows']}, битых: {stats['bad']}, из holdout (исключено): {stats['held']}")
    print(f"конфликтных входов исключено: {conflicts}")
    print(f"примеров: {len(examples)} -> {out_path}")


if __name__ == "__main__":
    main()
