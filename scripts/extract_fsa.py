"""
Извлекает из деклараций ФСА (json парсера Logintent) товарные записи: название, коды ТН ВЭД,
марка/модель/артикул, описание, область применения. Данные заявителей/изготовителей не берутся.

Выход: training/fsa/fsa_products.jsonl (все позиции) и fsa_train_candidates.jsonl
(ровно один код ТН ВЭД и название до 300 символов — пригодно как пример для обучения).

Запуск: python scripts/extract_fsa.py <declarations_*.json> [<ещё файл> ...]
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "training", "fsa")


def s(v, limit=0):
    v = " ".join(str(v or "").split())
    return v[:limit] if limit else v


def main(paths):
    os.makedirs(OUT, exist_ok=True)
    seen, rows = set(), []
    for p in paths:
        for rec in json.load(open(p, encoding="utf-8")):
            prod = rec.get("product") or {}
            for ident in prod.get("identifications") or []:
                name = s(ident.get("name"))
                codes = [c for c in (ident.get("tnvedCodes") or []) if c]
                key = (rec.get("idDeclaration"), ident.get("idIdentification"))
                if not name or not codes or key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "source": "fsa-declaration",
                    "declaration": s(rec.get("number")),
                    "reg_date": s(rec.get("declRegDate")),
                    "end_date": s(rec.get("declEndDate")),
                    "name": name,
                    "product_full_name": s(prod.get("fullName"), 600),
                    "marking": s(prod.get("marking"), 300),
                    "usage_scope": s(prod.get("usageScope"), 300),
                    "trade_mark": s(ident.get("tradeMark")),
                    "model": s(ident.get("model")),
                    "article": s(ident.get("article")),
                    "type": s(ident.get("type")),
                    "description": s(ident.get("description"), 600),
                    "tnved_codes": codes,
                })
    with open(os.path.join(OUT, "fsa_products.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    cand = [r for r in rows if len(r["tnved_codes"]) == 1 and len(r["name"]) <= 300]
    with open(os.path.join(OUT, "fsa_train_candidates.jsonl"), "w", encoding="utf-8") as f:
        for r in cand:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"позиций: {len(rows)}, кандидатов для обучения (1 код, имя ≤300): {len(cand)} -> {OUT}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1:])
