"""
Обучающий датасет для дообучения LLM из подтверждённых решений эталона
(tests/golden_tnved/golden_tnved.csv, status=подтверждён).

Формат — JSONL, по одному примеру на строку, в виде Yandex AI Studio
(дообучение генеративной модели):
  {"request": [{"role": "system", "text": ...}, {"role": "user", "text": ...}],
   "response": "{\"heading\": \"8536\", \"code\": \"8536490000\"}"}
Другой формат — --format chat даёт {"messages": [{"role", "content"}, ...]}
(OpenAI-совместимый).

В ответе только то, что подтверждено эталоном: позиция и код. Правила ОПИ,
примечания и рассуждения НЕ добавляются — в эталоне их нет, придумывать нельзя.
Названия, для которых подтверждено НЕСКОЛЬКО разных кодов (противоречие),
в датасет не попадают.

Эталонная выгрузка «товары_ПРАВИЛЬНО» (tests/golden_tnved/reference_examples.csv, колонки
Наименование/Материал/Описание/Торговая марка/Код ТНВЭД) выгружается отдельно: --reference.
Повторы строк НЕ убираются (так задумана эталонная база); исключаются только строки без
10-значного кода и входы-противоречия (один и тот же вход, разные коды).

Запуск:  python -m tnved.make_training [--out файл] [--format yandex|chat] [--reference]
"""
import argparse
import csv
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import tnved_classifier as tc  # noqa: E402
from tnved.feedback import DEFAULT_GOLDEN  # noqa: E402

DEFAULT_OUT = os.path.join(os.path.dirname(APP_DIR), "training", "finetune_tnved.jsonl")

DEFAULT_REFERENCE = os.path.join(os.path.dirname(APP_DIR), "tests", "golden_tnved", "reference_examples.csv")
DEFAULT_REFERENCE_OUT = os.path.join(os.path.dirname(APP_DIR), "training", "finetune_reference_tnved.jsonl")
_PLACEHOLDER = {"", "-", "—", "нет", "не указано", "уточняется", "ссылка на описание", "без тм"}

SYSTEM = ("Ты — классификатор товаров по ТН ВЭД ЕАЭС. По описанию товара верни JSON "
          '{"heading": "4 знака", "code": "10 знаков"}. Только JSON, без пояснений.')


def build_examples(csv_path: str = DEFAULT_GOLDEN) -> list[dict]:
    """Уникальные (название, описание, материал, артикул, бренд) -> код; конфликтные
    (один и тот же вход, разные коды) исключаются."""
    codes: dict[tuple, set] = defaultdict(set)
    shown: dict[tuple, dict] = {}
    with open(csv_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if (row.get("status") or "").strip() != "подтверждён":
                continue
            name = " ".join(row["name"].split())  # в эталоне бывают двойные пробелы
            key = (tc.normalize(name), tc.normalize(row.get("description")),
                   tc.normalize(row.get("material")), tc.normalize(row.get("article")),
                   tc.normalize(row.get("brand")))
            codes[key].add(row["expected_code"].strip())
            shown[key] = {"name": name, "description": (row.get("description") or "").strip(),
                          "material": (row.get("material") or "").strip(),
                          "article": " ".join((row.get("article") or "").split()),
                          "brand": " ".join((row.get("brand") or "").split())}
    out = []
    for key, cs in codes.items():
        if len(cs) != 1:
            continue
        code = next(iter(cs))
        d = shown[key]
        lines = [f"Название: {d['name']}"]
        if d["brand"]:
            lines.append(f"Бренд: {d['brand']}")
        if d["article"]:
            lines.append(f"Артикул: {d['article']}")
        if d["material"]:
            lines.append(f"Материал: {d['material']}")
        if d["description"]:
            lines.append(f"Описание: {d['description']}")
        out.append({"user": "\n".join(lines),
                    "response": json.dumps({"heading": code[:4], "code": code}, ensure_ascii=False)})
    return sorted(out, key=lambda e: e["user"])


def build_reference_examples(csv_path: str = DEFAULT_REFERENCE) -> list[dict]:
    """Все строки эталонной выгрузки в порядке файла, повторы сохраняются."""
    rows, codes = [], defaultdict(set)
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            name = " ".join((row.get("Наименование") or "").split())
            code = (row.get("Код ТНВЭД") or "").strip()
            if not name or not (code.isdigit() and len(code) == 10):
                continue
            lines = [f"Название: {name}"]
            for label, col in (("Бренд", "Торговая марка"), ("Материал", "Материал"), ("Описание", "Описание")):
                v = " ".join((row.get(col) or "").split())
                if v.lower() not in _PLACEHOLDER:
                    lines.append(f"{label}: {v}")
            user = "\n".join(lines)
            rows.append((user, code))
            codes[user].add(code)
    return [{"user": u, "response": json.dumps({"heading": c[:4], "code": c}, ensure_ascii=False)}
            for u, c in rows if len(codes[u]) == 1]


def _dump(examples: list[dict], path: str, fmt: str) -> int:
    with open(path, "w", encoding="utf-8") as f:
        for e in examples:
            if fmt == "chat":
                rec = {"messages": [{"role": "system", "content": SYSTEM},
                                    {"role": "user", "content": e["user"]},
                                    {"role": "assistant", "content": e["response"]}]}
            else:
                rec = {"request": [{"role": "system", "text": SYSTEM},
                                   {"role": "user", "text": e["user"]}],
                       "response": e["response"]}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return len(examples)


def write(path: str, fmt: str = "yandex", csv_path: str = DEFAULT_GOLDEN) -> int:
    return _dump(build_examples(csv_path), path, fmt)


def write_reference(path: str = DEFAULT_REFERENCE_OUT, fmt: str = "yandex",
                    csv_path: str = DEFAULT_REFERENCE) -> int:
    return _dump(build_reference_examples(csv_path), path, fmt)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--format", choices=["yandex", "chat"], default="yandex")
    ap.add_argument("--reference", action="store_true", help="выгрузить эталонную базу reference_examples.csv")
    args = ap.parse_args()
    if args.reference:
        out = args.out if args.out != DEFAULT_OUT else DEFAULT_REFERENCE_OUT
        print(f"примеров: {write_reference(out, args.format)} -> {out}")
    else:
        print(f"примеров: {write(args.out, args.format)} -> {args.out}")
