"""Шаг 2.7: очередь задач (фиксированное число воркеров, «В очереди №N») и общий
лимит одновременных запросов к Gemini (весь процесс, а не одна задача)."""
import io, json, os, sys, tempfile, threading, time, warnings
warnings.filterwarnings("ignore")
os.environ.update(DATA_DIR=tempfile.mkdtemp(), ADMIN_USER="boss", ADMIN_PASSWORD="bosspass123")
os.environ.pop("ADMIN_RESET_TOKEN", None)
# JOB_WORKERS и GEMINI_MAX_CONCURRENT читаются один раз при импорте jobs.py/gemini.py
os.environ["JOB_WORKERS"] = "2"
os.environ["GEMINI_MAX_CONCURRENT"] = "3"
sys.path.insert(0, sys.argv[1])
import openpyxl
from fastapi.testclient import TestClient
import app as appmod, jobs, gemini

ok = True
def check(name, cond):
    global ok; ok &= bool(cond); print(("✓ " if cond else "✗ ") + name)

real_chat = gemini.chat  # часть 2 зовёт настоящий chat()/_post() — заглушка части 1 не должна утечь

print("— jobs.py: воркеров ровно JOB_WORKERS (переменная окружения) —")
check(f"JOB_WORKERS=2 подхвачен: {jobs.JOB_WORKERS}", jobs.JOB_WORKERS == 2)

print("— очередь: сверх числа воркеров задачи ждут со статусом «В очереди №N» —")
release = threading.Event()
started = {"n": 0}
started_lock = threading.Lock()

def fake_chat_blocking(messages, **kw):
    with started_lock:
        started["n"] += 1
    release.wait(10)  # держим воркер занятым, пока тест не отпустит
    return json.dumps([{"name_original": "x", "quantity": 1, "unit_price": 1,
                        "total_price": 1, "currency": "USD", "row": 1}])

gemini.chat, gemini.API_KEY = fake_chat_blocking, "test"
wb = openpyxl.Workbook(); wb.active.append(["x"]); b = io.BytesIO(); wb.save(b)
c = TestClient(appmod.app, base_url="https://testserver")
c.post("/api/login", json={"username": "boss", "password": "bosspass123"})

jids = [c.post("/api/process", files=[("files", (f"inv{i}.xlsx", b.getvalue()))]).json()["job_id"]
        for i in range(3)]
for _ in range(50):  # ждём, пока оба воркера возьмут свои задачи и упрутся в блокировку
    if started["n"] >= 2:
        break
    time.sleep(0.05)
statuses = {jid: c.get(f"/api/status/{jid}").json() for jid in jids}
running = [jid for jid, st in statuses.items() if st["status"] != "queued"]
queued = [jid for jid, st in statuses.items() if st["status"] == "queued"]
check(f"в работе ровно {jobs.JOB_WORKERS} задачи, в очереди — остальные: running={len(running)}, queued={len(queued)}",
      len(running) == jobs.JOB_WORKERS and len(queued) == 3 - jobs.JOB_WORKERS)
check(f"задача в очереди показывает позицию «В очереди №1»: {[statuses[j]['message'] for j in queued]}",
      queued and all(statuses[j]["message"] == "В очереди №1" for j in queued))

release.set()  # отпускаем все задачи — очередь должна разгрестись
for _ in range(100):
    dones = [c.get(f"/api/status/{jid}").json()["status"] for jid in jids]
    if all(s in ("done", "error") for s in dones):
        break
    time.sleep(0.05)
check(f"после освобождения воркеров все {len(jids)} задачи дошли до конца: {dones}",
      all(s == "done" for s in dones))

print("— gemini: общий лимит одновременных запросов (весь процесс) —")
gemini.chat = real_chat  # возвращаем настоящий chat() — иначе вызов ниже уйдёт в заглушку части 1
import urllib.request

GEMINI_LIMIT = gemini.GEMINI_MAX_CONCURRENT
check(f"GEMINI_MAX_CONCURRENT=3 подхвачен: {GEMINI_LIMIT}", GEMINI_LIMIT == 3)

active = {"n": 0, "max": 0}
active_lock = threading.Lock()

class FakeResp:
    def read(self):
        return json.dumps({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}).encode()
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False

def fake_urlopen(req, timeout=None):
    with active_lock:
        active["n"] += 1
        active["max"] = max(active["max"], active["n"])
    time.sleep(0.08)  # держим «запрос» открытым подольше, чтобы остальные упёрлись в лимит
    with active_lock:
        active["n"] -= 1
    return FakeResp()

real_urlopen = urllib.request.urlopen
urllib.request.urlopen = fake_urlopen
try:
    N_THREADS = GEMINI_LIMIT * 4  # заведомо больше лимита — иначе лимит нечем проверить
    threads = [threading.Thread(target=lambda: gemini.chat([{"role": "user", "content": "hi"}]))
               for _ in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
finally:
    urllib.request.urlopen = real_urlopen

check(f"{N_THREADS} потоков одновременно звали gemini.chat, пиковая параллельность запросов "
      f"к Gemini — {active['max']} (не больше лимита {GEMINI_LIMIT})", active["max"] <= GEMINI_LIMIT)
check(f"лимит реально ограничивал (иначе проверка бессмысленна): пик={active['max']} = лимиту={GEMINI_LIMIT}",
      active["max"] == GEMINI_LIMIT)

print("\nИТОГ:", "ВСЁ ОК" if ok else "ЕСТЬ ОШИБКИ")
