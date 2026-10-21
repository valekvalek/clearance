"""
Обогащение товаров: веб-поиск + перевод/описание через ИИ, закупка на китайских
площадках (deep-ссылки + Oxylabs), и (если нет фото из файла) поиск фото через
Yandex Search API (раздел «Картинки»).
"""
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import ai_client
import cache
import sourcing
from images import _decodable_size
from report import ranges as report_ranges


# ----------------------------------------------------------- обогащение
def _search_query(item):
    return " ".join(x for x in [item.get("brand", ""), item.get("article", ""),
                                item.get("name_original", "")] if x).strip()


_EMPTY_SEARCH = {"answer": "", "results": []}


ENRICH_SYSTEM = """Ты — эксперт по товарам из китайских инвойсов.
Для каждого товара даны его поля и РЕЗУЛЬТАТЫ ВЕБ-ПОИСКА (реальные ссылки и сниппеты).

ГЛАВНОЕ ПРАВИЛО — ТИП ТОВАРА ОПРЕДЕЛЯЕТ ИНВОЙС, А НЕ ПОИСК:
- Наименование из инвойса — истина о ТИПЕ товара. Поиск лишь уточняет детали.
- Артикулы часто СЛУЧАЙНО совпадают с чужими товарами (напр. "TX1-1" — это и ВЧ-трансформатор,
  и модель машинки для стрижки). Если результаты поиска описывают товар ДРУГОГО типа, чем
  наименование в инвойсе — ИГНОРИРУЙ эти результаты: переведи наименование инвойса как есть,
  а product_url верни "".
- Товары одного инвойса обычно тематически связаны (один поставщик). Смотри на соседние товары:
  если в инвойсе гребни и запчасти для машинок для стрижки, то "BLADE FOR I81WS" — это
  "Лезвие для машинки для стрижки I81WS", а НЕ серверное лезвие и НЕ трансформатор.
- Конструкция "X FOR <модель>" означает: X — тип товара, <модель> — модель УСТРОЙСТВА,
  к которому он подходит (не артикул самого X).
- Для НЕОДНОЗНАЧНЫХ наименований ("SPARE PARTS FOR TX1", "ACCESSORIES", "запчасти") тип
  устройства определяй ТОЛЬКО по КОНТЕКСТУ ИНВОЙСА (шапка, название поставщика, остальные
  товары списка) — НЕ по результатам поиска. Пример: если поставщик — фабрика машинок для
  стрижки и рядом лезвия/гребни, то "SPARE PARTS FOR TX1-1" = "Запчасти для машинки для
  стрижки TX1-1" (не трансформатор, не грузовик).
Верни JSON-массив (по одному объекту на товар, В ТОМ ЖЕ ПОРЯДКЕ) с полями:
{
  "name_ru": "перевод наименования на русский (тип товара + бренд + ключевая характеристика). БЕЗ артикула/кода в конце!",
  "name_zh": "название товара на китайском (термин ТИПА товара, а не дословный перевод слова)",
  "brand": "торговая марка/производитель (ОБЯЗАТЕЛЬНО заполнить)",
  "material": "материалы изготовления (ОБЯЗАТЕЛЬНО заполнить)",
  "description": "1-2 предложения: что это и назначение",
  "product_url": "ссылка на карточку ИМЕННО этого товара из результатов поиска, ИНАЧЕ \\"\\""
}
Про brand: поле НИКОГДА не пустое. Бери марку из инвойса; если её там нет — определи
  производителя по артикулу/типу товара и результатам поиска (напр. по префиксу артикула,
  по названию линейки). Если марку определить невозможно — напиши "OEM" (немарочный/безымянный товар).
Про name_ru: НЕ добавляй в конец артикул/код из инвойса (напр. 79090028, NCS-11/100-PNP).
  Нужен именно перевод наименования. Пример: "Ёмкостный датчик уровня Negele" (а не "...Negele NCS-11/100-PNP/X/X/X").
Про material: поле НИКОГДА не пустое и НЕ "не указано". Сначала бери материал из результатов поиска/спецификации.
  Если в сети информации нет — укажи наиболее вероятный материал ПО СМЫСЛУ для такого типа товара
  (напр. для датчика — "нержавеющая сталь", для клеммного винта — "оцинкованная сталь", для зажима КС — "медный сплав / бронза").
СТРОГИЕ правила по product_url:
- Бери URL ТОЛЬКО из предоставленных результатов поиска. НИКОГДА не выдумывай.
- Ставь ссылку ТОЛЬКО если результат — это карточка товара/страница производителя ИМЕННО про этот
  артикул/тип (совпадает бренд и/или артикул). Приоритет: производитель, 1688, alibaba, made-in-china,
  ozon, wildberries, профильные магазины.
- Если товар типовой/без бренда, или среди результатов нет подходящей карточки, или результаты не по теме
  (словари, переводчики, developer.android, google, youtube, wikipedia, соцсети) — верни product_url = "".
  Лучше пусто, чем неверная ссылка.
- Перевод (name_ru/name_zh) делай всегда, даже без ссылки.
Только JSON-массив, без markdown-ограждения."""

# домены, которые точно не являются карточкой товара
_BAD_URL = ("translate", "wikipedia", "baike", "zhihu", "developer.android",
            "play.google", "google.", "youtube", "youtu.be", "facebook",
            "twitter", "t.me", "promt", "yandex.ru/?", "reverso")

# ------------------------------------------------- фото через Yandex-картинки
_IMG_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ClearanceAI/1.0; +invoice-processing)"}
_MAX_IMG_BYTES = 8_000_000  # как MAX_UPLOAD_MB с запасом, для одной картинки хватает


def _download_image(url: str, max_bytes: int = _MAX_IMG_BYTES):
    """Скачивает картинку по прямой ссылке и проверяет, что это реально
    декодируемое изображение разумного размера (не битая ссылка/заглушка).
    Любая проблема (сеть, не-картинка, слишком маленькая) — тихо None."""
    if not url.lower().startswith(("http://", "https://")):
        return None
    try:
        req = urllib.request.Request(url, headers=_IMG_HEADERS)
        with urllib.request.urlopen(req, timeout=8) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if ctype and not ctype.startswith("image/"):
                return None
            data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                return None
        size = _decodable_size(data)
        if not size or min(size) < 100:   # иконка/заглушка, не фото товара
            return None
        return data
    except Exception:  # noqa: BLE001
        return None


def _fetch_web_photos(items, workers=4):
    """Для товаров без src_image (нет фото в загруженном файле) ищет фото
    через Yandex Search API, раздел «Картинки» (ai_client.image_search — у
    Yandex это отдельный продукт/эндпоинт от текстового web_search, см.
    yandex.py), по наименованию+артикулу. Берёт первую картинку из
    результатов, которая реально скачивается; остальные результаты не
    трогает. При AI_PROVIDER=gemini image_search ничего не находит (нет
    эквивалента) — товары остаются без фото, как и раньше."""
    targets = [(i, it) for i, it in enumerate(items) if not it.get("src_image")]
    if not targets:
        return 0

    def worker(t):
        i, it = t
        q = _search_query(it)
        if not q:
            return i, None
        cached = cache.get_photo(q)
        if cached is not None:
            return i, cached
        for r in ai_client.image_search(q, count=5):
            img = _download_image(r.get("url", ""))
            if img:
                cache.set_photo(q, img)
                return i, img
        return i, None

    found = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, img in ex.map(worker, targets):
            if img:
                items[i]["src_image"] = img
                found += 1
    return found


def _enrich_one_batch(batch, batch_search, ctx_block=""):
    """Переводит/обогащает товары батча на месте. -> множество индексов, которые ИИ заполнил."""
    filled = set()
    payload_lines = []
    for i, (it, sr) in enumerate(zip(batch, batch_search), 1):
        res_txt = "\n".join(
            f"    - {r['title']} | {r['url']} | {r['content'][:180]}"
            for r in sr.get("results", [])
        ) or "    (нет результатов)"
        ans = (sr.get("answer") or "")[:400]
        payload_lines.append(
            f"Товар #{i}:\n"
            f"  наименование: {it['name_original']}\n"
            f"  бренд: {it['brand']}\n"
            f"  артикул: {it['article']}\n"
            f"  спецификация: {it['spec']}\n"
            f"  сводка поиска: {ans}\n"
            f"  результаты поиска:\n{res_txt}"
        )
    messages = [
        {"role": "system", "content": ENRICH_SYSTEM},
        {"role": "user", "content": ctx_block + "Обработай товары:\n\n"
                                    + "\n\n".join(payload_lines)},
    ]
    try:
        # быстрый провал: короче таймаут и меньше повторов, чтобы не подвешивать всё
        resp = ai_client.chat(messages, temperature=0.2, max_tokens=8000,
                             timeout=120, max_retries=2)
        data = ai_client.extract_json(resp)
    except Exception:
        data = None
    if isinstance(data, list):
        for idx, (it, sr, d) in enumerate(zip(batch, batch_search, data)):
            if not isinstance(d, dict):
                continue
            filled.add(idx)
            it["name_ru"] = str(d.get("name_ru", "")).strip()
            it["name_zh"] = str(d.get("name_zh", "")).strip()
            it["material"] = str(d.get("material", "")).strip()
            it["description"] = str(d.get("description", "")).strip()
            # торговая марка: инвойс > ИИ > OEM (никогда не пусто)
            inv_brand = (it.get("brand") or "").strip()
            if not inv_brand or inv_brand.lower() in ("unknown", "unk", "n/a", "нет"):
                ai_brand = str(d.get("brand", "")).strip()
                it["brand"] = ai_brand or "OEM"
            # name_ru: срезать артикул/код с конца, если модель всё же добавила
            art = it.get("article") or it.get("internal_code")
            if art and it["name_ru"].rstrip().endswith(art):
                it["name_ru"] = it["name_ru"].rstrip()[:-len(art)].rstrip(" ,·-–—")
            # материал никогда не пустой
            if not it["material"] or it["material"].lower() in (
                    "не указано", "-", "—", "нет данных", "unknown", "n/a", "нет"):
                it["material"] = "уточняется"
            url = str(d.get("product_url", "")).strip()
            # принимаем ссылку только если: она реальна (есть в результатах поиска)
            # и не из чёрного списка. Никаких форс-фолбэков на случайный результат.
            result_urls = {r["url"] for r in sr.get("results", []) if r.get("url")}
            if url:
                if url not in result_urls or any(b in url.lower() for b in _BAD_URL):
                    url = ""
            it["product_url"] = url
    return filled


def enrich_items(items, progress=None, batch_size=10,
                 search_workers=6, batch_workers=6, web_search=True,
                 invoice_context="", report=None):
    total = len(items)
    # контекст инвойса: шапка документа + полный список товаров (тематика)
    all_names = "; ".join((it.get("name_original") or "")[:45] for it in items[:50])
    ctx_block = ""
    if invoice_context or all_names:
        ctx_block = ("КОНТЕКСТ ИНВОЙСА (шапка документа):\n"
                     + (invoice_context or "(нет)")[:1500]
                     + f"\n\nВСЕ ТОВАРЫ ЭТОГО ИНВОЙСА: {all_names}\n\n")
    # 1) поиск по товарам (с ДЕДУПЛИКАЦИЕЙ — одинаковые запросы ищем 1 раз)
    if web_search:
        if progress:
            progress(0, total, "Поиск товаров в интернете...")
        # НЕ ищем для позиций, у которых уже есть фото из файла (экономим вайбы):
        # «есть фото — в интернете не ищем».
        queries = [("" if it.get("src_image") else _search_query(it)) for it in items]
        uniq = list(dict.fromkeys(q for q in queries if q))

        def search_cached(q):
            cached = cache.get_search(q)
            if cached is not None:
                return cached
            res = ai_client.search(q)
            if not res.get("error"):
                cache.set_search(q, res.get("answer", ""), res.get("results", []))
            return res

        with ThreadPoolExecutor(max_workers=search_workers) as ex:
            res = list(ex.map(search_cached, uniq))
        qmap = dict(zip(uniq, res))
        searches = [qmap.get(q, _EMPTY_SEARCH) if q else _EMPTY_SEARCH for q in queries]
        failed = sum(1 for r in res if r.get("error"))
        if failed and report:
            report.add(f"Поиск в интернете не сработал для {failed} из {len(res)} запросов — "
                       f"у этих товаров нет ссылок, материал определён без поиска")
    else:
        # экономный режим — без интернет-поиска (0 списаний), только перевод + фото из файлов
        searches = [_EMPTY_SEARCH for _ in items]

    # 2) обогащение — батчи параллельно, с плавным прогрессом
    batches = [(s, items[s:s + batch_size], searches[s:s + batch_size])
               for s in range(0, total, batch_size)]
    from threading import Lock
    counter = {"done": 0}
    lock = Lock()
    untranslated = []  # номера товаров (с 1), оставшихся без перевода

    def run(batch):
        start, b_items, b_search = batch
        filled = _enrich_one_batch(b_items, b_search, ctx_block)
        missing = [i for i in range(len(b_items)) if i not in filled]
        if missing:  # один повтор — только для незаполненных товаров
            again = _enrich_one_batch([b_items[i] for i in missing],
                                      [b_search[i] for i in missing], ctx_block)
            missing = [m for j, m in enumerate(missing) if j not in again]
        with lock:
            untranslated.extend(start + i + 1 for i in missing)
        if progress:
            with lock:
                counter["done"] += len(b_items)
                progress(min(counter["done"], total), total,
                         f"Переведено {min(counter['done'], total)}/{total}")

    with ThreadPoolExecutor(max_workers=batch_workers) as ex:
        list(ex.map(run, batches))
    if untranslated and report:
        report.add(f"{len(untranslated)} товар(ов) без перевода и описания (№ "
                   f"{report_ranges(untranslated)}) — в файле оставлено исходное наименование")

    # 3) ФОТО: приоритет — из загруженных файлов (src_image, уже проставлено
    #    images.map_source_images СНАЧАЛА, см. jobs.py). Если его там нет и
    #    включён веб-поиск — ищем фото через Yandex Search API, раздел
    #    «Картинки» (отдельный запрос от текстового web_search выше). При
    #    выключенном web_search фото остаётся только из файла.
    if web_search:
        found_photos = _fetch_web_photos(items)
        if found_photos and report:
            report.add(f"Фото из интернета (Яндекс.Картинки) подставлено для {found_photos} товар(ов), "
                       f"у которых не было своего фото в файле")
    # гарантия: торговая марка и материал никогда не пустые
    for it in items:
        if not (it.get("brand") or "").strip():
            it["brand"] = "OEM"
        if not (it.get("material") or "").strip():
            it["material"] = "уточняется"
    return items


# ----------------------------------------------------------- закупка (Китай)
def source_items(items, progress=None):
    """Заполняет it['sourcing'] (deep-ссылки + данные Oxylabs, если доступны)."""
    from threading import Lock
    total = len(items)
    if progress:
        progress(0, total, "Поиск на китайских площадках...")
    done = {"n": 0}
    lock = Lock()

    def run(it):
        try:
            it["sourcing"] = sourcing.source_item(it)
        except Exception:
            it["sourcing"] = {"links": {}, "china_url": "", "supplier": "",
                              "price_cn": "", "moq": "", "pay": ""}
        if progress:
            with lock:
                done["n"] += 1
                progress(done["n"], total, f"Закупка {done['n']}/{total}")

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(run, items))
    return items
