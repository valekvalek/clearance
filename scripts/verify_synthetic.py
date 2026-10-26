"""
Проверка синтетики: для каждой сгенерированной позиции YandexGPT (без подсказки, какой код
задумывался) выбирает код среди соседних кодов той же позиции. Остаются только позиции,
где выбор совпал с задуманным кодом. Результат: training/synthetic/synthetic_verified.jsonl,
отбракованное — synthetic_rejected.jsonl. Возобновляемо (по уже обработанным кодам).

Запуск: python scripts/verify_synthetic.py [--limit N] [--workers 4] [--until 2026-10-31]
"""
import argparse
import datetime
import json
import os
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import expand_dataset as ed  # noqa: E402
import yandex  # noqa: E402
import yadisk  # noqa: E402

VERIFIED = os.path.join(ed.OUT_DIR, "synthetic_verified.jsonl")
REJECTED = os.path.join(ed.OUT_DIR, "synthetic_rejected.jsonl")

SYSTEM = """Ты — таможенный классификатор ТН ВЭД ЕАЭС. Тебе дан список кандидатов-кодов и список товаров.
Для КАЖДОГО товара выбери ровно один код из кандидатов, который подходит лучше всего по
наименованию кода. Опирайся только на текст кандидатов. Код «прочие» выбирай, только если
товар не подходит под более конкретные кандидаты.
Верни ТОЛЬКО JSON-массив {"i": номер товара, "code": "10 знаков"} по одному на товар."""


def processed():
    out = set()
    for p in (VERIFIED, REJECTED):
        if os.path.exists(p):
            for line in open(p, encoding="utf-8"):
                try:
                    out.add(json.loads(line)["code"])
                except Exception:  # noqa: BLE001
                    pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--until", default="2026-10-31")
    args = ap.parse_args()
    until = datetime.date.fromisoformat(args.until)

    leaves, by_parent = ed.load_codes()
    parent_of = {l["code"]: l["parent_code"] for l in leaves}
    groups = defaultdict(list)
    for line in open(ed.RAW, encoding="utf-8"):
        r = json.loads(line)
        groups[r["code"]].append(r)
    done = processed()
    todo = [c for c in groups if c not in done and c in parent_of]
    if args.limit:
        todo = todo[:args.limit]
    print(f"кодов с синтетикой {len(groups)}, проверено {len(done)}, в работе {len(todo)}", flush=True)

    def work(code):
        if datetime.date.today() > until:
            return code, None
        cands = by_parent[parent_of[code]]
        items = groups[code]
        user = ("Кандидаты:\n" + "\n".join(f"{c['code']} — {ed._clean(c['name_full'])[:220]}" for c in cands) +
                "\n\nТовары:\n" + "\n".join(
                    f"{i}. {it['name']}" + (f" | материал: {it['material']}" if it["material"] else "") +
                    (f" | {it['description'][:160]}" if it["description"] else "")
                    for i, it in enumerate(items, 1)))
        try:
            data = yandex.extract_json(yandex.chat(
                [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                temperature=0.0, max_tokens=1200, timeout=90, max_retries=2))
        except Exception:  # noqa: BLE001
            return code, None
        return code, data

    lock = threading.Lock()
    n = {"ok": 0, "bad": 0, "fail": 0, "calls": 0}
    buf = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(work, c) for c in todo]
        for fu in as_completed(futs):
            code, data = fu.result()
            with lock:
                n["calls"] += 1
                if not isinstance(data, list):
                    n["fail"] += 1
                    continue
                pick = {d.get("i"): str(d.get("code", "")).strip() for d in data if isinstance(d, dict)}
                for i, it in enumerate(groups[code], 1):
                    good = pick.get(i) == code
                    n["ok" if good else "bad"] += 1
                    rec = {**it, "verified": good, "judge_code": pick.get(i, "")}
                    with open(VERIFIED if good else REJECTED, "a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    if good:
                        buf.append(rec)
                if len(buf) >= 1000 and yadisk.available():
                    name = f"verified_{datetime.datetime.now():%Y%m%d_%H%M%S}.jsonl"
                    data_b = "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in buf).encode("utf-8")
                    print(f"  Яндекс.Диск {name}: {'ok' if yadisk.upload('training/synthetic/' + name, data_b, timeout=120) else 'ОШИБКА'}", flush=True)
                    buf = []
                if n["calls"] % 100 == 0:
                    print(f"{n['calls']}/{len(futs)}: принято {n['ok']}, отбраковано {n['bad']}, сбоев {n['fail']}", flush=True)
    if buf and yadisk.available():
        name = f"verified_{datetime.datetime.now():%Y%m%d_%H%M%S}.jsonl"
        yadisk.upload("training/synthetic/" + name,
                      "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in buf).encode("utf-8"), timeout=120)
    tot = n["ok"] + n["bad"]
    print(f"ГОТОВО: принято {n['ok']}, отбраковано {n['bad']} ({n['bad'] / tot:.0%} брака), "
          f"сбоев вызовов {n['fail']}" if tot else "ГОТОВО: нет данных", flush=True)


if __name__ == "__main__":
    main()
