"""
Делит tests/golden_tnved/golden_tnved.csv (status=подтверждён) на train/test
по УНИКАЛЬНОМУ товару (name+description+material+article+brand), чтобы один
и тот же товар не попал одновременно в train и test (иначе оценка на test
будет завышена за счёт заучивания).

Пишет:
  training/golden_tnved_train.csv   — для генерации обучающего jsonl
  tests/golden_tnved/golden_tnved_holdout.csv — для честной оценки (evaluate.py)

Не трогает исходный tests/golden_tnved/golden_tnved.csv.
"""
import csv
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP_DIR = os.path.join(ROOT, "app")
sys.path.insert(0, APP_DIR)
import tnved_classifier as tc  # noqa: E402

SRC = os.path.join(ROOT, "tests", "golden_tnved", "golden_tnved.csv")
TRAIN_OUT = os.path.join(ROOT, "training", "golden_tnved_train.csv")
TEST_OUT = os.path.join(ROOT, "tests", "golden_tnved", "golden_tnved_holdout.csv")

TEST_FRACTION = 0.2
SEED = 42


def row_key(row: dict) -> tuple:
    return (
        tc.normalize(row.get("name")),
        tc.normalize(row.get("description")),
        tc.normalize(row.get("material")),
        tc.normalize(row.get("article")),
        tc.normalize(row.get("brand")),
    )


def main() -> None:
    with open(SRC, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    confirmed = [r for r in rows if (r.get("status") or "").strip() == "подтверждён"]
    keys = sorted(set(row_key(r) for r in confirmed))

    rng = random.Random(SEED)
    rng.shuffle(keys)
    n_test = max(1, round(len(keys) * TEST_FRACTION))
    test_keys = set(keys[:n_test])
    train_keys = set(keys[n_test:])

    train_rows = [r for r in rows if row_key(r) in train_keys or (r.get("status") or "").strip() != "подтверждён"]
    test_rows = [r for r in confirmed if row_key(r) in test_keys]

    os.makedirs(os.path.dirname(TRAIN_OUT), exist_ok=True)
    with open(TRAIN_OUT, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(train_rows)

    with open(TEST_OUT, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(test_rows)

    print(f"Уникальных подтверждённых товаров: {len(keys)}")
    print(f"train keys: {len(train_keys)} -> {TRAIN_OUT} ({len(train_rows)} строк)")
    print(f"test keys:  {len(test_keys)} -> {TEST_OUT} ({len(test_rows)} строк)")


if __name__ == "__main__":
    main()
