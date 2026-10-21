"""Шаг 2.8: gemini.chat/chat_vision не повторяют запрос при 4xx (кроме 429),
учитывают Retry-After при 429, и у задачи есть общий лимит времени выполнения."""
import io, os, sys, tempfile, time, warnings
warnings.filterwarnings("ignore")
os.environ.update(DATA_DIR=tempfile.mkdtemp(), ADMIN_USER="boss", ADMIN_PASSWORD="bosspass123")
os.environ.pop("ADMIN_RESET_TOKEN", None)
sys.path.insert(0, sys.argv[1])
import urllib.error
import gemini, jobs

ok = True
def check(name, cond):
    global ok; ok &= bool(cond); print(("✓ " if cond else "✗ ") + name)

gemini.API_KEY = "test"


def http_error(code, headers=None):
    import email.message
    hdrs = email.message.Message()
    for k, v in (headers or {}).items():
        hdrs[k] = v
    return urllib.error.HTTPError("http://x", code, "err", hdrs, io.BytesIO(b"body"))


print("— 4xx (кроме 429) не повторяется —")
calls = {"n": 0}
def post_400(model, payload, timeout=180):
    calls["n"] += 1
    raise http_error(400)
gemini._post = post_400
sleeps = []
real_sleep = time.sleep
time.sleep = lambda s: sleeps.append(s)
try:
    try:
        gemini.chat([{"role": "user", "content": "hi"}], max_retries=3)
        raised = False
    except RuntimeError as e:
        raised = "HTTP 400" in str(e)
finally:
    time.sleep = real_sleep
check(f"HTTP 400: запрос отправлен один раз (было бы {3} при старом поведении): {calls['n']}", calls["n"] == 1)
check("HTTP 400: ошибка поднята сразу, без ожидания между повторами", raised and not sleeps)

print("— 429 повторяется (это не «плохой» запрос, а временный лимит) —")
calls = {"n": 0}
def post_429_then_ok(model, payload, timeout=180):
    calls["n"] += 1
    if calls["n"] == 1:
        raise http_error(429)
    return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
gemini._post = post_429_then_ok
sleeps = []
time.sleep = lambda s: sleeps.append(s)
try:
    result = gemini.chat([{"role": "user", "content": "hi"}], max_retries=3)
finally:
    time.sleep = real_sleep
check(f"HTTP 429: повтор состоялся, второй вызов успешен: {calls['n']} вызова(ов), результат {result!r}",
      calls["n"] == 2 and result == "ok")

print("— 429 с Retry-After ждёт указанное время, а не дефолтные секунды —")
calls = {"n": 0}
def post_429_retry_after(model, payload, timeout=180):
    calls["n"] += 1
    if calls["n"] == 1:
        raise http_error(429, {"Retry-After": "7"})
    return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
gemini._post = post_429_retry_after
sleeps = []
time.sleep = lambda s: sleeps.append(s)
try:
    gemini.chat([{"role": "user", "content": "hi"}], max_retries=3)
finally:
    time.sleep = real_sleep
check(f"пауза перед повтором взята из Retry-After: {sleeps}", sleeps and sleeps[0] == 7.0)

print("— 5xx по-прежнему повторяется с обычной задержкой —")
calls = {"n": 0}
def post_500_then_ok(model, payload, timeout=180):
    calls["n"] += 1
    if calls["n"] == 1:
        raise http_error(500)
    return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
gemini._post = post_500_then_ok
try:
    result = gemini.chat([{"role": "user", "content": "hi"}], max_retries=3)
except Exception:
    result = None
check(f"HTTP 500: повтор состоялся, второй вызов успешен: {calls['n']} вызова(ов)", calls["n"] == 2 and result == "ok")

print("— общий лимит времени на задачу —")
check(f"JOB_TIME_LIMIT читается из окружения (по умолчанию 1800с): {jobs.JOB_TIME_LIMIT}",
      jobs.JOB_TIME_LIMIT == 1800)
try:
    jobs._check_deadline(time.time() - jobs.JOB_TIME_LIMIT - 1)
    raised = False
except TimeoutError as e:
    raised = "время выполнения" in str(e)
check(f"дедлайн превышен -> TimeoutError с понятным сообщением: {raised}", raised)
try:
    jobs._check_deadline(time.time())  # только что стартовали — лимит не мог истечь
    ok_within = True
except TimeoutError:
    ok_within = False
check("в пределах лимита — исключение не бросается", ok_within)

print("\nИТОГ:", "ВСЁ ОК" if ok else "ЕСТЬ ОШИБКИ")
