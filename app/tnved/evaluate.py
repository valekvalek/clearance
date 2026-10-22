"""
Фаза 1 плана повышения точности ТН ВЭД: прогон tnved_classifier.classify по
эталонному набору (tests/golden_tnved/golden_tnved.csv) и расчёт метрик.

В метрики попадают ТОЛЬКО строки status=подтверждён (см.
tests/golden_tnved/README.md) — строки status=кандидат не подтверждены и
могут быть сами неверны, их учёт исказил бы точность.

Метрики (считаются на ПОДТВЕРЖДЁННЫХ строках):
  coverage        — доля строк, где classify() вообще вернул непустой code
                    (среди ВСЕХ подтверждённых строк, не только отвеченных)
  accuracy@N      — для N из {4,6,8,10}: доля строк среди ОТВЕЧЕННЫХ (code != ''),
                    где первые N знаков predicted code совпали с первыми N
                    знаками expected_code. accuracy@10 — точное совпадение.
  needs_review_rate — доля отвеченных строк с needs_review=true
  errors_by_group  — для несовпавших (accuracy@10 == False) строк среди
                    отвеченных: группировка по (группа_ожидаемая -> группа_
                    предсказанная), группа — первые 2 знака кода

Запуск:
    cd app && python -m tnved.evaluate
    (или: python tnved/evaluate.py — добавляет app/ в sys.path сам)
"""
import csv
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
PROJECT_ROOT = os.path.dirname(APP_DIR)
DEFAULT_GOLDEN = os.path.join(PROJECT_ROOT, "tests", "golden_tnved", "golden_tnved.csv")

if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)


def load_golden(path: str = DEFAULT_GOLDEN) -> list[dict]:
    """Читает golden_tnved.csv, возвращает только строки status=подтверждён."""
    rows = []
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if (row.get("status") or "").strip() == "подтверждён":
                rows.append(row)
    return rows


def _digits(code: str) -> str:
    return "".join(ch for ch in (code or "") if ch.isdigit())


def evaluate(golden_rows: list[dict], use_cache: bool = True,
             use_synonyms: bool = True, retries: int = 2, pipeline: str | None = None,
             novel: bool = False) -> dict:
    """Прогоняет tnved_classifier.classify_batch по golden_rows (уже
    отфильтрованным на status=подтверждён) и считает метрики. Каждое уникальное
    (название, описание, материал) классифицируется один раз, оценивается по
    всем строкам эталона. use_cache/use_synonyms=False — честная оценка:
    синонимы собраны из этого же эталона, кэш хранит прошлые ответы LLM.
    Временные сбои LLM (reason "временный сбой...") повторяются retries раз."""
    import tnved_classifier as tc

    total = len(golden_rows)
    answered = 0
    needs_review_count = 0
    acc_hits = {4: 0, 6: 0, 8: 0, 10: 0}
    errors_by_group = Counter()
    mismatches = []

    def _key(row):
        return (row["name"], row.get("description") or None, row.get("material") or None)

    unique = list(dict.fromkeys(_key(r) for r in golden_rows))
    expected_by_key: dict = {}
    for r in golden_rows:
        expected_by_key.setdefault(_key(r), set()).add(r["expected_code"].strip())
    items = [{"product_name": n, "description": d, "material": m} for n, d, m in unique]
    if novel:  # жёсткая оценка: прецеденты с ожидаемым кодом недоступны (v2)
        for it, k in zip(items, unique):
            it["_exclude_codes"] = expected_by_key[k]
    results = tc.classify_batch(items, use_cache=use_cache, use_synonyms=use_synonyms, pipeline=pipeline)
    for _ in range(retries):
        failed = [i for i, r in enumerate(results)
                  if str(r.get("reason", "")).startswith("временный сбой")]
        if not failed:
            break
        redo = tc.classify_batch([items[i] for i in failed], use_cache=use_cache,
                                 use_synonyms=use_synonyms, pipeline=pipeline)
        for i, r in zip(failed, redo):
            results[i] = r
    by_key = dict(zip(unique, results))

    proposed_hits = {4: 0, 10: 0}
    proposed_n = 0
    heading_n = heading_hits = 0
    by_conf = {}
    for row in golden_rows:
        name = row["name"]
        expected = _digits(row["expected_code"])
        result = by_key[_key(row)]
        predicted = _digits(result["code"])
        proposed = _digits(result.get("proposed_code", ""))
        if proposed:
            proposed_n += 1
            proposed_hits[4] += proposed[:4] == expected[:4]
            proposed_hits[10] += proposed == expected
            b = by_conf.setdefault(result.get("confidence", "?"), [0, 0, 0])
            b[0] += 1
            b[1] += proposed[:4] == expected[:4]
            b[2] += proposed == expected
        if result.get("heading"):
            heading_n += 1
            heading_hits += result["heading"] == expected[:4]

        if predicted:
            answered += 1
            if result.get("needs_review"):
                needs_review_count += 1
            for n in (4, 6, 8, 10):
                if len(expected) >= n and len(predicted) >= n and expected[:n] == predicted[:n]:
                    acc_hits[n] += 1
            if predicted[:10] != expected[:10]:
                errors_by_group[(expected[:2], predicted[:2])] += 1
                mismatches.append({
                    "name": name, "expected": expected, "predicted": predicted,
                    "confidence": result.get("confidence"), "reason": result.get("reason"),
                })
        else:
            mismatches.append({
                "name": name, "expected": expected, "predicted": "",
                "confidence": result.get("confidence"), "reason": result.get("reason"),
            })

    metrics = {
        "total_confirmed": total,
        "coverage": answered / total if total else 0.0,
        "answered": answered,
        "needs_review_rate": needs_review_count / answered if answered else 0.0,
        "accuracy_at": {
            n: (acc_hits[n] / answered if answered else 0.0) for n in (4, 6, 8, 10)
        },
        "errors_by_group": errors_by_group.most_common(),
        "proposed": {"n": proposed_n, "acc4": proposed_hits[4] / proposed_n if proposed_n else 0.0,
                     "acc10": proposed_hits[10] / proposed_n if proposed_n else 0.0},
        "heading_accuracy": heading_hits / heading_n if heading_n else None,
        "by_confidence": by_conf,
        "results": {f"{k[0]}": v for k, v in by_key.items()},
        "mismatches": mismatches,
    }
    return metrics


def print_report(metrics: dict) -> None:
    print(f"Подтверждённых строк в эталоне: {metrics['total_confirmed']}")
    print(f"Отвечено (code != ''): {metrics['answered']} "
          f"(coverage = {metrics['coverage']:.1%})")
    print(f"Доля needs_review среди отвеченных: {metrics['needs_review_rate']:.1%}")
    print("Точность среди отвеченных, по числу совпавших знаков:")
    for n in (4, 6, 8, 10):
        print(f"  accuracy@{n}: {metrics['accuracy_at'][n]:.1%}")
    if metrics["proposed"]["n"]:
        p = metrics["proposed"]
        print(f"Предложенный код без учёта отказов (v2): n={p['n']}, acc@4={p['acc4']:.1%}, acc@10={p['acc10']:.1%}")
        if metrics["heading_accuracy"] is not None:
            print(f"Верная позиция среди выбранных: {metrics['heading_accuracy']:.1%}")
        for lvl, (n, h4, h10) in sorted(metrics["by_confidence"].items()):
            print(f"  уверенность {lvl}: n={n}, позиция верна {h4}/{n}, код верен {h10}/{n}")
    print("Топ расхождений по группам (ожидаемая группа -> предсказанная группа, count):")
    for (exp_g, pred_g), count in metrics["errors_by_group"][:15]:
        print(f"  {exp_g} -> {pred_g or '(нет кода)'}: {count}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-cache", action="store_true", help="не читать/не писать classify_cache")
    ap.add_argument("--no-synonyms", action="store_true", help="отключить таблицу синонимов")
    ap.add_argument("--dump", help="сохранить расхождения в JSON")
    ap.add_argument("--novel", action="store_true", help="v2: исключить прецеденты с ожидаемым кодом (оценка на новых товарах)")
    ap.add_argument("--pipeline", choices=["v1", "v2"], help="версия классификатора (по умолчанию env/v1)")
    args = ap.parse_args()
    try:  # как app.py: ключи провайдера из app/.env, не перезаписывая окружение
        with open(os.path.join(APP_DIR, ".env"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass
    golden = load_golden()
    if not golden:
        print(f"Нет подтверждённых строк в {DEFAULT_GOLDEN}")
        sys.exit(1)
    metrics = evaluate(golden, use_cache=not args.no_cache, use_synonyms=not args.no_synonyms,
                       pipeline=args.pipeline, novel=args.novel)
    print_report(metrics)
    if args.dump:
        import json
        with open(args.dump, "w", encoding="utf-8") as f:
            json.dump(metrics, f, ensure_ascii=False, indent=1, default=str)
