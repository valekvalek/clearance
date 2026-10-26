"""
Дособирает декларации ФСА парсером Logintent (/Users/user1/Downloads/LogintentParser, его
классы используются как есть, с его же паузами), складывая результат в training/fsa/raw/,
а не в репозиторий парсера. Продолжает с уже собранных ID. Затем дополняет коды ТН ВЭД.

Запуск: .venv-fsa/bin/python scripts/fsa_collect.py <сколько_новых_ID> [ASC|DESC]
Портал отдаёт ограниченную глубину выдачи (~1800 ID на срез), поэтому второй срез берётся
сортировкой ASC (от старых деклараций к новым). Уже собранные ID пропускаются.
"""
import os
import shutil
import sys
from pathlib import Path

PARSER = Path("/Users/user1/Downloads/LogintentParser")
sys.path.insert(0, str(PARSER / "src"))

import json  # noqa: E402
import random  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from playwright.sync_api import sync_playwright  # noqa: E402

from logintent.auth.fgis_token_provider import FgisTokenProvider  # noqa: E402
from logintent.processing.tnved_enricher import TnvedEnricher  # noqa: E402
from logintent.scraping.fsa_scraper import FsaScraper  # noqa: E402

_orig_fetch_token = FgisTokenProvider._fetch_token


def _fetch_token_in_thread(self):
    """Токен берётся в отдельном потоке: sync_playwright нельзя вкладывать в уже открытый."""
    box = {}

    def run():
        try:
            box["v"] = _orig_fetch_token(self)
        except Exception as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=run)
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]
    return box["v"]


FgisTokenProvider._fetch_token = _fetch_token_in_thread

class SlicedScraper(FsaScraper):
    """FsaScraper с выбором направления сортировки списка (тот же фильтр: происхождение Китай)."""

    sort = "DESC"

    def collect_ids(self, total_needed=500):
        all_ids = self._load_ids()
        known = set(all_ids)
        got, page_num = 0, 0
        print(f"Starting collection of {total_needed} new IDs (sort {self.sort})...")
        print(f"Loaded {len(all_ids)} existing IDs from {self.ids_file}")
        with sync_playwright() as playwright:
            browser = playwright.firefox.launch(headless=True)
            page = browser.new_context().new_page()
            while got < total_needed:
                body = {"size": 50, "page": page_num, "count": 0, "filter": {"idProductOrigin": ["156"]},
                        "columnsSort": [{"column": "declDate", "sort": self.sort}]}
                response = self._request_with_retries(
                    lambda headers: page.request.post(
                        "https://pub.fsa.gov.ru/api/v1/rds/common/declarations/get",
                        data=json.dumps(body), headers=headers, timeout=120000),
                    f"ID collection page {page_num}")
                if response is None:
                    break
                items = response.json().get("items", [])
                if not items:
                    break
                for d in items:
                    i = str(d["id"])
                    if i not in known:
                        known.add(i)
                        all_ids.append(i)
                        got += 1
                        if got >= total_needed:
                            break
                self._save_ids(all_ids)
                print(f"Collected {got}/{total_needed} new IDs ({len(all_ids)} total)")
                page_num += 1
                time.sleep(random.uniform(10, 15))
            browser.close()
        self._save_ids(all_ids)
        return all_ids


ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "training" / "fsa" / "raw"
RAW.mkdir(parents=True, exist_ok=True)

SEEDS = {
    PARSER / "data/raw/declarations/declaration_ids.json": RAW / "declaration_ids.json",
    PARSER / "data/raw/declarations/declarations_china.json": RAW / "declarations_china.json",
    PARSER / "data/reference/tnved_id_map.json": RAW / "tnved_id_map.json",
}


def main(n_new: int, sort: str = "DESC"):
    for src, dst in SEEDS.items():
        if not dst.exists():
            shutil.copy(src, dst)
    SlicedScraper.sort = sort
    scraper = SlicedScraper(output_file=RAW / "declarations_china.json", ids_file=RAW / "declaration_ids.json")
    ids = scraper.collect_ids(total_needed=n_new)
    scraper.process_declarations(ids)
    TnvedEnricher(input_file=RAW / "declarations_china.json",
                  output_file=RAW / "declarations_china_tnved.json",
                  tnved_id_map_file=RAW / "tnved_id_map.json").enrich_records()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 500,
         sys.argv[2].upper() if len(sys.argv) > 2 else "DESC")
