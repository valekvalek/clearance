"""
Выгрузка открытых официальных данных ТН ВЭД/ГС и загрузка их на Яндекс.Диск
(app:/training/official/). Честный User-Agent, пауза между запросами, лимит размера.
Файлы только сохраняются (не открываются и не выполняются). Возобновляемо: скачанное
пропускается. Манифест с sha256 — official_manifest.csv.

Запуск: python scripts/collect_official.py [--no-upload]
"""
import argparse
import csv
import hashlib
import os
import re
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
import yadisk  # noqa: E402

DUMP = os.path.join(ROOT, "research", "official_dump")
UA = "clearance-research/1.0 (non-commercial reference data collection)"
DELAY = 2.0
MAX_BYTES = 60_000_000
EEC = "https://eec.eaeunion.org"
EEC_INDEX = EEC + "/comission/department/catr/ett/"
PDF_RE = re.compile(r'href="([^"]*ru\.\d{2}_2022[^"]*\.pdf)"')

SINGLE = [
    ("fns", "tnved.zip", "https://data.nalog.ru/files/tnved/tnved.zip"),
    ("fns", "tnved_discr.docx", "https://www.nalog.gov.ru/html/sites/www.new.nalog.ru/doc/tnved_discr.docx"),
    ("comtrade", "classificationH6.json", "https://comtrade.un.org/data/cache/classificationH6.json"),
    ("harmonized-system", "harmonized-system.csv",
     "https://raw.githubusercontent.com/datasets/harmonized-system/main/data/harmonized-system.csv"),
    ("usitc-hts", "hts_export.json",
     "https://hts.usitc.gov/reststop/exportList?from=0101&to=9999&format=JSON&styles=false"),
]


def fetch(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise RuntimeError("слишком большой файл")
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-upload", action="store_true")
    no_upload = ap.parse_args().no_upload

    jobs = list(SINGLE)
    try:
        html = fetch(EEC_INDEX).decode("utf-8", "replace")
        for href in sorted(set(PDF_RE.findall(html))):
            jobs.append(("eec", os.path.basename(href), href if href.startswith("http") else EEC + href))
        time.sleep(DELAY)
    except Exception as e:  # noqa: BLE001
        print("индекс ЕЭК не загрузился:", e)

    manifest, ok, fail = [], 0, 0
    for src, name, url in jobs:
        d = os.path.join(DUMP, src)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            data = open(path, "rb").read()
        else:
            try:
                data = fetch(url)
                open(path, "wb").write(data)
                time.sleep(DELAY)
            except Exception as e:  # noqa: BLE001
                print(f"ОШИБКА {src}/{name}: {str(e)[:100]}")
                fail += 1
                continue
        ok += 1
        manifest.append((src, name, url, len(data), hashlib.sha256(data).hexdigest()))
        if not no_upload and yadisk.available():
            if not yadisk.upload(f"training/official/{src}/{name}", data, timeout=180):
                print(f"  не выгружено на Диск: {src}/{name}")
    mpath = os.path.join(DUMP, "official_manifest.csv")
    with open(mpath, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["source", "file", "url", "bytes", "sha256"])
        w.writerows(manifest)
    if not no_upload and yadisk.available():
        yadisk.upload("training/official/official_manifest.csv", open(mpath, "rb").read())
    print(f"скачано/есть: {ok}, ошибок: {fail}, файлов в манифесте: {len(manifest)}")


if __name__ == "__main__":
    main()
