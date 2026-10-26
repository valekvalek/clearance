"""
Расширение обучающей базы: для каждого 10-значного кода справочника ТН ВЭД
(research/tnved-sources/tnved_codes.csv) просит YandexGPT придумать реалистичные
позиции инвойса. Код — вход, а не ответ модели, поэтому метка верна по построению
(в той мере, в какой модель сгенерировала именно такие товары — это синтетика,
в эталон/holdout не смешивается).

Результат: training/synthetic/synthetic_raw.jsonl (одна позиция на строку).
Возобновляемо (пропускает уже готовые коды), шарды по SHARD кодов; если задан
YADISK_TOKEN — каждый шард выгружается в app:/training/synthetic/.

Запуск: python scripts/expand_dataset.py [--limit N] [--workers 4] [--per-code 6]
Остановка по дате (--until, по умолчанию 2026-10-31: до этой даты API бесплатны).
"""
import argparse
import csv
import datetime
import json
import os
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
import tnved_classifier as tc  # noqa: E402
import yandex  # noqa: E402
import yadisk  # noqa: E402

CODES = os.path.join(ROOT, "research", "tnved-sources", "tnved_codes.csv")
GOLDEN = os.path.join(ROOT, "tests", "golden_tnved", "golden_tnved.csv")
HOLDOUT = os.path.join(ROOT, "tests", "golden_tnved", "golden_tnved_holdout.csv")
REFERENCE = os.path.join(ROOT, "tests", "golden_tnved", "reference_examples.csv")
OUT_DIR = os.path.join(ROOT, "training", "synthetic")
RAW = os.path.join(OUT_DIR, "synthetic_raw.jsonl")
SHARD = 200

SYSTEM = """Ты — таможенный брокер, который готовит обучающие примеры для классификатора ТН ВЭД ЕАЭС.
Тебе дан ОДИН код ТН ВЭД с официальным наименованием. Придумай реалистичные позиции из
инвойсов/упаковочных листов поставщиков (Китай и др.), которые ОДНОЗНАЧНО классифицируются
именно в этот код, а не в соседние.
Правила:
- Каждая позиция — конкретный товар, как его пишут в инвойсе: тип, ключевые характеристики,
  при необходимости модель/серия. Названия разнообразь (разные виды товара внутри кода,
  разные формулировки, часть можно писать как в китайских инвойсах: коротко, с размерами/мощностью).
- Для кодов «прочие/прочее» давай товары, которые подходят под позицию, но НЕ подходят под
  перечисленные соседние коды.
- Не выдумывай бренды: бренд указывай только если он реально существует и выпускает такой товар,
  иначе пустая строка. Артикул не нужен.
- material — основной материал/состав ("" если для товара не имеет смысла).
- description — 1–2 предложения: назначение и ключевые технические признаки, влияющие на код.
- Не повторяй позиции из примеров.
Верни ТОЛЬКО JSON-массив объектов {"name": ..., "brand": ..., "material": ..., "description": ...}."""


def _clean(s):
    return " ".join(str(s or "").split())


def load_codes():
    rows = list(csv.DictReader(open(CODES, encoding="utf-8")))
    by_parent = defaultdict(list)
    for r in rows:
        if r["level"] == "10":
            by_parent[r["parent_code"]].append(r)
    leaves = [r for r in rows if r["level"] == "10"]
    return leaves, by_parent


def load_seeds():
    seeds = defaultdict(list)
    for path, mapper in ((GOLDEN, lambda r: (r["expected_code"], r["name"], r.get("material"), r.get("description"))),
                         (REFERENCE, lambda r: (r["Код ТНВЭД"], r["Наименование"], r["Материал"], r["Описание"]))):
        for r in csv.DictReader(open(path, encoding="utf-8")):
            if path == GOLDEN and (r.get("status") or "").strip() != "подтверждён":
                continue
            code, name, mat, desc = mapper(r)
            code, name = (code or "").strip(), _clean(name)
            if len(code) == 10 and name and len(seeds[code]) < 3 and name not in [s[0] for s in seeds[code]]:
                seeds[code].append((name, _clean(mat), _clean(desc)[:200]))
    return seeds


def held_names():
    return {tc.normalize(r["name"]) for r in csv.DictReader(open(HOLDOUT, encoding="utf-8"))}


def done_codes():
    if not os.path.exists(RAW):
        return set()
    out = set()
    with open(RAW, encoding="utf-8") as f:
        for line in f:
            try:
                out.add(json.loads(line)["code"])
            except Exception:  # noqa: BLE001
                pass
    return out


def build_prompt(leaf, siblings, seed, per_code):
    sib = "; ".join(_clean(s["name_short"])[:90] for s in siblings if s["code"] != leaf["code"])[:900]
    parts = [f"Код: {leaf['code']}", f"Полное наименование: {_clean(leaf['name_full'])}"]
    if leaf["unit"]:
        parts.append(f"Доп. единица: {leaf['unit']}")
    parts.append(f"Соседние коды этой позиции (НЕ подходят): {sib or '—'}")
    if seed:
        parts.append("Примеры реальных позиций этого кода (не повторять):\n" +
                     "\n".join(f"- {n} | материал: {m or '—'}" for n, m, _ in seed))
    parts.append(f"Придумай {per_code} разных позиций.")
    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="сколько кодов обработать (0 = все)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--per-code", type=int, default=6)
    ap.add_argument("--until", default="2026-10-31", help="после этой даты (включительно) не запускать")
    ap.add_argument("--max-calls", type=int, default=0)
    args = ap.parse_args()
    until = datetime.date.fromisoformat(args.until)

    os.makedirs(OUT_DIR, exist_ok=True)
    leaves, by_parent = load_codes()
    seeds, held, done = load_seeds(), held_names(), done_codes()
    todo = [l for l in leaves if l["code"] not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"кодов всего {len(leaves)}, готово {len(done)}, в работе {len(todo)}", flush=True)

    lock = threading.Lock()
    state = {"calls": 0, "ok": 0, "items": 0, "fail": 0, "shard": [], "shard_n": len(done) // SHARD}

    def flush_shard(force=False):
        if not state["shard"] or (len(state["shard"]) < SHARD and not force):
            return
        state["shard_n"] += 1
        name = f"part_{datetime.datetime.now():%Y%m%d_%H%M%S}_{state['shard_n']:04d}.jsonl"
        data = "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in state["shard"]).encode("utf-8")
        state["shard"] = []
        if yadisk.available():
            ok = yadisk.upload(f"training/synthetic/{name}", data, timeout=120)
            print(f"  шард {name}: Яндекс.Диск {'ok' if ok else 'ОШИБКА'}", flush=True)

    def work(leaf):
        if datetime.date.today() > until:
            return leaf["code"], None, "after-cutoff"
        msgs = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": build_prompt(leaf, by_parent.get(leaf["parent_code"], []),
                                                         seeds.get(leaf["code"], []), args.per_code)}]
        try:
            resp = yandex.chat(msgs, temperature=0.7, max_tokens=2500, timeout=90, max_retries=2)
            data = yandex.extract_json(resp)
        except Exception as e:  # noqa: BLE001
            return leaf["code"], None, str(e)[:120]
        return leaf["code"], data, ""

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {}
        for leaf in todo:
            if args.max_calls and len(futs) >= args.max_calls:
                break
            futs[ex.submit(work, leaf)] = leaf
        for fu in as_completed(futs):
            leaf = futs[fu]
            code, data, err = fu.result()
            with lock:
                state["calls"] += 1
                if err == "after-cutoff":
                    continue
                if not isinstance(data, list):
                    state["fail"] += 1
                    continue
                rows, seen = [], set()
                for d in data:
                    if not isinstance(d, dict):
                        continue
                    name = _clean(d.get("name"))
                    key = tc.normalize(name)
                    if len(name) < 3 or key in seen or key in held:
                        continue
                    seen.add(key)
                    rows.append({"code": code, "name": name, "brand": _clean(d.get("brand")),
                                 "material": _clean(d.get("material")),
                                 "description": _clean(d.get("description")),
                                 "source": "synthetic-yandex", "full_name": _clean(leaf["name_full"])})
                if not rows:
                    state["fail"] += 1
                    continue
                with open(RAW, "a", encoding="utf-8") as f:
                    for r in rows:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
                state["ok"] += 1
                state["items"] += len(rows)
                state["shard"].extend(rows)
                flush_shard()
                if state["calls"] % 100 == 0:
                    print(f"{state['calls']}/{len(futs)} вызовов, кодов ok {state['ok']}, "
                          f"позиций {state['items']}, сбоев {state['fail']}", flush=True)
    with lock:
        flush_shard(force=True)
    print(f"ГОТОВО: вызовов {state['calls']}, кодов ok {state['ok']}, позиций {state['items']}, "
          f"сбоев {state['fail']} -> {RAW}", flush=True)


if __name__ == "__main__":
    main()
