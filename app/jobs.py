"""
Фоновые задачи обработки инвойсов: хранилище задач в памяти и сам прогон
(распознавание -> обогащение -> закупка -> xlsx) с предупреждениями.
"""
import os
import json
import queue
import time
import uuid
import logging
import threading

import auth
import readers
import images
import extract
import enrich
import export
import currency
import activity
import ai_client
import tnved_classifier
from report import Report

logger = logging.getLogger(__name__)

JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_TTL = 2 * 3600  # задачи старше 2 часов удаляются (память контейнера ограничена)

# Статус задачи переживает рестарт сервера — только сам факт «была такая
# задача и что с ней в итоге стало», без result/preview/src_thumbs (это
# бинарные/тяжёлые данные, которые и не нужны: после рестарта мы всё равно
# просим пользователя загрузить файл заново, а не подменяем результат).
JOBS_FILE = os.path.join(auth.DATA_DIR, "jobs.json")
_RESTART_MESSAGE = "Сервер перезапустился во время обработки — загрузите файл заново"
_LOST_RESULT_MESSAGE = "Результат был готов, но не сохранился при перезапуске сервера — загрузите файл заново"


def _persist_locked():
    """Вызывать под JOBS_LOCK. Потеря jobs.json не страшна — задачи просто
    станут неизвестны после рестарта, как раньше (это тот же уровень
    надёжности, что у sessions.json)."""
    snapshot = {jid: {"owner": j.get("owner"), "status": j.get("status"),
                       "message": j.get("message"), "created": j.get("created")}
                for jid, j in JOBS.items()}
    tmp = JOBS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False)
        os.replace(tmp, JOBS_FILE)
    except OSError as e:
        logger.warning("jobs.json не записан (%s)", e)


def _load_persisted() -> dict:
    try:
        with open(JOBS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning("jobs.json не читается (%s) — статусы прежних задач потеряны", e)
        return {}


def restore_after_restart():
    """Вызывается один раз при импорте модуля (то есть при старте сервера).
    Задачи из прошлого запуска, не удалённые по TTL, заводим в JOBS заново —
    иначе фронт, который их опрашивает, вечно получает 404 и не понимает,
    что произошло (см. шаг 2.9 плана доработки)."""
    now = time.time()
    cutoff = now - JOB_TTL
    persisted = _load_persisted()
    with JOBS_LOCK:
        for jid, j in persisted.items():
            created = j.get("created", 0)
            if created < cutoff:
                continue
            status = j.get("status")
            if status == "error":
                message = j.get("message") or _RESTART_MESSAGE
            elif status == "done":
                status = "error"
                message = _LOST_RESULT_MESSAGE
            else:
                status = "error"
                message = _RESTART_MESSAGE
            JOBS[jid] = {"status": status, "message": message, "current": 0,
                         "total": 0, "created": created,
                         "owner": j.get("owner", ""), "warnings": []}


# Общее время на задачу: распознавание + обогащение + закупка (каждый вызов к
# Gemini внутри уже ограничен своим таймаутом/повторами — это лимит на ВСЮ
# задачу целиком, чтобы она не могла зависнуть на часы из-за череды повторов).
JOB_TIME_LIMIT = int(os.getenv("JOB_TIME_LIMIT", str(30 * 60)))

# Задачи выполняются фиксированным пулом воркеров, а не по потоку на загрузку:
# иначе при нескольких загрузках сразу каждая уже сама параллелит распознавание/
# перевод/поиск в 8/6/6 потоков — без общего лимита число потоков и запросов к
# ИИ ничем не ограничено. Сверх лимита задачи ждут своей очереди со
# статусом «В очереди №N»; общий лимит запросов к самому ИИ — отдельно,
# в YANDEX_MAX_CONCURRENT/GEMINI_MAX_CONCURRENT (в зависимости от провайдера,
# см. yandex.py/gemini.py — там же, где несколько задач ни при чём —
# внутри одной задачи потоков тоже больше, чем воркеров тут).
JOB_WORKERS = max(1, int(os.getenv("JOB_WORKERS", "3")))
_JOB_QUEUE = queue.Queue()
_PENDING_LOCK = threading.Lock()
_PENDING = []  # job_id, ожидающие свободного воркера, по порядку поступления


def set_job(job_id, **kw):
    with JOBS_LOCK:
        JOBS[job_id].update(kw)
        if "status" in kw:
            _persist_locked()


def _progress(job_id):
    def cb(current, total, message):
        set_job(job_id, current=current, total=total, message=message)
    return cb


def job_of(u, job_id):
    """Задача этого пользователя или None (вызывать под JOBS_LOCK).
    Чужая задача неотличима от несуществующей — 404, а не 403."""
    j = JOBS.get(job_id)
    return j if j and j.get("owner") == u["username"] else None


def _update_queue_positions():
    with _PENDING_LOCK:
        pending = list(_PENDING)
    for i, jid in enumerate(pending, 1):
        set_job(jid, message=f"В очереди №{i}")


def _worker():
    while True:
        job_id, args = _JOB_QUEUE.get()
        with _PENDING_LOCK:
            if job_id in _PENDING:
                _PENDING.remove(job_id)
        _update_queue_positions()
        try:
            run_job(job_id, *args)
        finally:
            _JOB_QUEUE.task_done()


restore_after_restart()

for _ in range(JOB_WORKERS):
    threading.Thread(target=_worker, daemon=True).start()


def start(owner, datas, names, filename, do_sourcing, web_search) -> str:
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        cutoff = time.time() - JOB_TTL
        for k in [k for k, v in JOBS.items() if v.get("created", 0) < cutoff]:
            del JOBS[k]
        JOBS[job_id] = {"status": "queued", "message": "В очереди",
                        "current": 0, "total": 0, "created": time.time(),
                        "owner": owner, "warnings": []}
        _persist_locked()
    with _PENDING_LOCK:
        _PENDING.append(job_id)
    _JOB_QUEUE.put((job_id, (datas, names, filename, do_sourcing, web_search, owner)))
    _update_queue_positions()
    return job_id


def _invoice_context(datas):
    """Шапка первой таблицы — контекст для перевода."""
    for data in datas:
        if data[:5] != b"%PDF-":
            try:
                ss = readers.read_sheets(data)
                if ss and ss[0]["grid"].strip():
                    return "\n".join(ss[0]["grid"].splitlines()[:12])
            except Exception:
                pass
    return ""


def _preview(job_id, items, src_thumbs, rates, do_sourcing):
    preview = []
    for idx, it in enumerate(items):
        total_usd = export.usd(it, "total_price", rates)
        row = {
            "name_ru": it["name_ru"] or it["name_original"],
            "name_zh": it["name_zh"], "brand": it["brand"],
            "article": it["article"] or it["internal_code"],
            "qty": it["quantity"], "unit": it["unit"], "url": it["product_url"],
            "photo": (f"/api/img/{job_id}/{idx}" if src_thumbs[idx]
                      else it.get("photo_url", "")),
            "total_usd": total_usd,
            # для подсветки в предпросмотре (шаг 3.6) — без перевода/без цены
            "no_translation": not it["name_ru"],
            "no_price": total_usd == "" or total_usd is None,
            # код ТН ВЭД для таблицы результата: что проверить и чего не хватило
            "tnved_code": it.get("tnved_code") or "",
            "tnved_description": it.get("tnved_description") or "",
            "tnved_title": tnved_classifier.heading_title(it.get("tnved_code") or ""),
            "tnved_confidence": it.get("tnved_confidence") or "",
            "tnved_needs_review": bool(it.get("tnved_needs_review", True)),
            "tnved_missing": list(it.get("tnved_missing") or []),
        }
        if do_sourcing:
            s = it.get("sourcing") or {}
            row["src"] = {
                "china_url": s.get("china_url", ""), "supplier": s.get("supplier", ""),
                "price_cn": s.get("price_cn", ""), "moq": s.get("moq", ""),
                "links": s.get("links", {}),
            }
        preview.append(row)
    return preview


def _check_deadline(started: float):
    left = JOB_TIME_LIMIT - (time.time() - started)
    if left < 0:
        raise TimeoutError(f"превышено общее время выполнения задачи ({JOB_TIME_LIMIT // 60} мин)")


class JobCancelled(Exception):
    """Пользователь нажал «Отменить» — поднимается на следующей проверке
    внутри run_job. Уже запущенный запрос к ИИ никто не прерывает на лету,
    только следующий шаг конвейера (как и с JOB_TIME_LIMIT)."""


def cancel(job_id: str, username: str) -> bool:
    """Запрашивает отмену своей же незавершённой задачи. True, если задача
    найдена, принадлежит этому пользователю и ещё не завершилась."""
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j or j.get("owner") != username or j.get("status") in ("done", "error"):
            return False
        j["cancel_requested"] = True
    return True


def _check_cancelled(job_id: str):
    with JOBS_LOCK:
        if JOBS.get(job_id, {}).get("cancel_requested"):
            raise JobCancelled()


def run_job(job_id, datas, names, filename, do_sourcing=False, web_search=True, username=""):
    report = Report(job_id)
    started = time.time()
    try:
        _check_cancelled(job_id)  # отменили, пока задача ждала своей очереди
        if username:
            activity.on_run_start(username, filename)
        if not ai_client.API_KEY:
            set_job(job_id, status="error",
                    message=f"Не задан ключ ИИ для провайдера {ai_client.PROVIDER} "
                            f"— обратитесь к администратору сервиса")
            return
        set_job(job_id, status="parsing", message="Читаю файлы и распознаю товары...")
        items = extract.extract_all(datas, report=report, names=names)
        if not items:
            msg = "Не удалось найти товары в файлах"
            if report.items:
                msg += ": " + "; ".join(report.items)
            set_job(job_id, status="error", message=msg, warnings=report.items)
            return
        set_job(job_id, warnings=report.items)
        _check_deadline(started)
        _check_cancelled(job_id)
        # картинки из исходных файлов (если есть) — приоритет над веб-поиском
        images.map_source_images(items, datas)

        def _thumbs():
            out = []
            for it in items:
                t = images._thumb_from_bytes(it["src_image"]) if it.get("src_image") else None
                out.append(t[0].getvalue() if t else None)
            return out

        # предварительно — только фото из файла, чтобы превью уже показывало
        # что есть, пока идёт обогащение (может занять время)
        set_job(job_id, src_thumbs=_thumbs())
        set_job(job_id, status="enriching", total=len(items), current=0,
                message=f"Найдено {len(items)} товаров, обогащаю...")
        rates = currency.get_rates()
        for w in currency.conversion_warnings(items, rates):
            report.add(w)
        set_job(job_id, rate=currency.rate_display(rates, [it["currency"] for it in items]))
        enrich.enrich_items(items, progress=_progress(job_id), web_search=web_search,
                            invoice_context=_invoice_context(datas), report=report)
        _check_deadline(started)
        _check_cancelled(job_id)
        # enrich_items (при включённом web_search) мог добавить it["src_image"]
        # товарам без фото из файла — пересчитываем миниатюры для итогового превью.
        src_thumbs = _thumbs()
        set_job(job_id, src_thumbs=src_thumbs)
        if do_sourcing:
            set_job(job_id, status="sourcing", current=0,
                    message="Поиск на китайских площадках...")
            enrich.source_items(items, progress=_progress(job_id))
        _check_deadline(started)
        _check_cancelled(job_id)
        set_job(job_id, status="enriching", message="Определяю коды ТН ВЭД...")
        try:
            codes = tnved_classifier.classify_batch([
                {"product_name": it["name_ru"] or it["name_original"] or "",
                 "description": it.get("description"),
                 "material": it.get("material"),
                 "name_zh": it.get("name_zh"),
                 "product_url": it.get("product_url"),
                 "name_original": it.get("name_original"),
                 "article": it.get("article"),
                 "brand": it.get("brand")}
                for it in items
            ])
            for it, cl in zip(items, codes):
                it["tnved_code"] = cl["code"]
                it["tnved_description"] = cl["description"]
                it["tnved_confidence"] = cl["confidence"]
                it["tnved_needs_review"] = cl["needs_review"]
                it["tnved_missing"] = tnved_classifier.missing_ru(cl.get("missing_attributes"))
        except Exception as e:
            logging.getLogger("jobs").warning("tnved classify_batch failed: %s", e)
            for it in items:
                it.setdefault("tnved_needs_review", True)
        _check_deadline(started)
        _check_cancelled(job_id)
        warnings = report.items
        xlsx = export.build_workbook(items, rates, with_sourcing=do_sourcing,
                                     warnings=warnings)
        result_name = ("ClearanceAI_" + os.path.splitext(filename)[0]
                       + ("_закупка.xlsx" if do_sourcing else "_result.xlsx"))
        message = (f"Готово, есть предупреждения ({len(warnings)})" if warnings else "Готово")
        # запись в историю (и выгрузка на Яндекс.Диск, если настроен, — сетевой
        # вызов) ДО того, как клиенту станет виден статус "done": иначе фронт
        # (showResult -> loadRecent) может обновить ленту раньше, чем запись
        # реально появится, и результат на миг "потеряется" из вида.
        if username:
            try:
                activity.on_result(username, result_name, len(items), xlsx)
            except Exception:
                pass
        set_job(job_id, status="done", message=message, result=xlsx,
                preview=_preview(job_id, items, src_thumbs, rates, do_sourcing),
                warnings=warnings, result_name=result_name,
                current=len(items), total=len(items))
    except JobCancelled:
        set_job(job_id, status="error", message="Отменено пользователем",
                warnings=report.items)
    except Exception as e:  # noqa: BLE001
        # Пользователю — короткое понятное сообщение, полная трассировка — только
        # в лог (раньше писалась в неиспользуемое поле job["trace"] и терялась).
        logger.exception("задача %s упала", job_id)
        set_job(job_id, status="error", message=f"Ошибка: {e}", warnings=report.items)
