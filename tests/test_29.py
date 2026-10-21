"""Шаг 2.9: статус задач переживает рестарт сервера. jobs.json пишется при
каждой смене status и подхватывается при следующем импорте jobs (то есть при
старте сервера) — вместо вечного 404 у фронта, который опрашивает задачу,
которой больше нет в памяти после рестарта."""
import io, json, os, sys, tempfile, time, warnings
warnings.filterwarnings("ignore")
DATA_DIR = tempfile.mkdtemp()
os.environ.update(DATA_DIR=DATA_DIR, ADMIN_USER="boss", ADMIN_PASSWORD="bosspass123")
os.environ.pop("ADMIN_RESET_TOKEN", None)
sys.path.insert(0, sys.argv[1])
import jobs

ok = True
def check(name, cond):
    global ok; ok &= bool(cond); print(("✓ " if cond else "✗ ") + name)

JOBS_FILE = os.path.join(DATA_DIR, "jobs.json")

print("— set_job со сменой status пишет jobs.json —")
jobs.JOBS["j1"] = {"status": "queued", "message": "В очереди", "current": 0,
                   "total": 0, "created": time.time(), "owner": "ivan", "warnings": []}
jobs.set_job("j1", status="parsing", message="Читаю файлы...")
with open(JOBS_FILE, encoding="utf-8") as f:
    saved = json.load(f)
check("задача попала в jobs.json", "j1" in saved)
check("статус сохранён", saved["j1"]["status"] == "parsing")
check("тяжёлые поля (result/preview/src_thumbs) не пишутся",
      "result" not in saved["j1"] and "preview" not in saved["j1"])

print("— set_job без смены status не трогает файл —")
os.remove(JOBS_FILE)
jobs.set_job("j1", current=5, total=10)
check("файл не пересоздан (пишем только на смену статуса)", not os.path.exists(JOBS_FILE))

print("— восстановление после рестарта: задача \"в процессе\" -> ошибка —")
now = time.time()
with open(JOBS_FILE, "w", encoding="utf-8") as f:
    json.dump({
        "in_progress": {"owner": "ivan", "status": "enriching",
                         "message": "Обогащаю...", "created": now},
        "was_done": {"owner": "ivan", "status": "done",
                     "message": "Готово", "created": now},
        "was_error": {"owner": "ivan", "status": "error",
                      "message": "Ошибка: бла-бла", "created": now},
        "too_old": {"owner": "ivan", "status": "error",
                    "message": "древняя", "created": now - jobs.JOB_TTL - 10},
    }, f)
jobs.JOBS.clear()
jobs.restore_after_restart()
check(f"старая по TTL задача не восстановлена: {list(jobs.JOBS)}", "too_old" not in jobs.JOBS)
check("незавершённая задача стала ошибкой", jobs.JOBS["in_progress"]["status"] == "error")
check("сообщение про рестарт понятное",
      "перезапустился" in jobs.JOBS["in_progress"]["message"])
check("бывшая \"done\" тоже стала ошибкой (результата всё равно нет)",
      jobs.JOBS["was_done"]["status"] == "error" and "перезапуске" in jobs.JOBS["was_done"]["message"])
check("бывшая \"error\" сохранила исходное сообщение",
      jobs.JOBS["was_error"]["message"] == "Ошибка: бла-бла")
check("владелец сохранён (job_of продолжит отличать чужую задачу от 404)",
      jobs.JOBS["in_progress"]["owner"] == "ivan")
check("у восстановленной задачи нет result/preview (эндпоинты вернут 404, а не подмену)",
      "result" not in jobs.JOBS["was_done"] and "preview" not in jobs.JOBS["was_done"])

print("— повреждённый jobs.json не роняет сервер, просто теряем статусы —")
with open(JOBS_FILE, "w", encoding="utf-8") as f:
    f.write("не json")
jobs.JOBS.clear()
try:
    jobs.restore_after_restart()
    survived = True
except Exception:
    survived = False
check("восстановление не бросает исключение на битом файле", survived)
check("JOBS просто пуст", jobs.JOBS == {})

print("\nИТОГ:", "ВСЁ ОК" if ok else "ЕСТЬ ОШИБКИ")
