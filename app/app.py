"""
InvixAI Web — веб-сервис обработки китайских инвойсов.
Загрузка xlsx -> ИИ-парсинг -> перевод + поиск товара -> экспорт в шаблон.
С простой авторизацией (логин + админ, управляющий учётками).
"""
import os


def _load_env_file():
    """Подхватывает app/.env, если он есть (строки вида ИМЯ=значение) — чтобы
    не экспортировать переменные окружения вручную перед каждым запуском.
    Должно выполниться ДО импорта auth/jobs/currency и т.п. — они читают
    DATA_DIR/YANDEX_API_KEY и прочее через os.getenv() прямо при импорте.
    Переменная, уже заданная в окружении (например, через -e в Docker или
    в настройках хостинга), не перезаписывается — .env только дополняет."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    os.environ.setdefault(key, value)
    except FileNotFoundError:
        pass


_load_env_file()

# Secure-флаг куки сессии: по умолчанию включён (кука отправляется только по
# HTTPS — правильно для прода). Временно отключить (COOKIE_SECURE=0) можно,
# только пока нет домена/HTTPS и сервис проверяется по голому http://IP —
# иначе браузер молча не сохраняет куку после /api/login (вход как будто
# проходит, но сессия не держится). Включить обратно, как только появится
# HTTPS через домен.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1").strip().lower() not in ("0", "false", "no")

from urllib.parse import quote

from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Form
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (HTMLResponse, JSONResponse, Response,
                               FileResponse, RedirectResponse)

import auth
import jobs
from jobs import JOBS, JOBS_LOCK  # noqa: F401  JOBS — для тестов и отладки
import activity
import uploads
import guards
import sourcing
import yandex

app = FastAPI(title="InvixAI Web")
auth.init_admin()

# Шаг 3.7: заголовки безопасности. Все страницы и API — свой домен, никаких
# внешних шрифтов/скриптов/CDN (см. static/*.html), поэтому CSP можно держать
# строгим. 'unsafe-inline' для script/style нужен из-за единого инлайн-
# <script> (см. шаг про редизайн — вынести его во внешний файл нельзя, тесты
# рассчитаны ровно на один блок) и атрибутов onclick/style по месту.
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'self'; form-action 'self'; "
        "object-src 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


@app.middleware("http")
async def _middleware(request: Request, call_next):
    err = guards.csrf_error(request)
    if err:
        return JSONResponse({"detail": err}, status_code=403)
    # огромный запрос отсекаем ДО разбора multipart (он пишется на диск)
    if request.url.path == "/api/process":
        err = uploads.oversized_request(request.headers.get("content-length"))
        if err:
            return JSONResponse({"detail": err}, status_code=413)
    resp = await call_next(request)
    for k, v in _SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    sid = getattr(request.state, "new_sid", None)
    if sid:  # кука старого формата переведена в серверную сессию
        _set_cookie(resp, sid)
    return resp


STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")



# ------------------------------------------------------------- авторизация
def _user(request: Request):
    u, new_sid = auth.read_session(request.cookies.get(auth.COOKIE_NAME))
    if new_sid:  # кука старого формата переведена в серверную сессию
        request.state.new_sid = new_sid
    return u


def _require(request: Request):
    u = _user(request)
    if not u:
        raise HTTPException(401, "Требуется вход")
    return u


def _require_admin(request: Request):
    u = _require(request)
    if u["role"] != "admin":
        raise HTTPException(403, "Только для администратора")
    return u


BUYER_GROUP = "Закупщик"
DEMO_GROUP = "Демо"
DEMO_LIMIT = 5  # обработок инвойсов на учётку группы «Демо», дальше — 402 с предложением подписки


def is_buyer(u) -> bool:
    return bool(u) and (u.get("role") == "admin" or BUYER_GROUP in u.get("groups", []))


def is_demo(u) -> bool:
    return bool(u) and DEMO_GROUP in u.get("groups", [])


def _require_buyer(request: Request):
    u = _require(request)
    if not is_buyer(u):
        raise HTTPException(403, "Доступно только группе «Закупщик»")
    return u


def _set_cookie(resp, token):
    resp.set_cookie(auth.COOKIE_NAME, token, max_age=auth.SESSION_TTL,
                    httponly=True, secure=COOKIE_SECURE, samesite="lax", path="/")


def _fail_check(key):
    wait_min = auth.fail_wait_minutes(key)
    if wait_min:
        raise HTTPException(429, f"Слишком много попыток, попробуйте через {wait_min} мин.")


# ------------------------------------------------------------- страницы
def _page(name):
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not _user(request):
        return RedirectResponse("/login", status_code=302)
    return _page("index.html")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if _user(request):
        return RedirectResponse("/", status_code=302)
    return _page("login.html")


@app.get("/icon.svg")
def icon():
    path = os.path.join(STATIC_DIR, "icon.svg")
    if os.path.exists(path):
        return FileResponse(path, media_type="image/svg+xml")
    raise HTTPException(404)


# ------------------------------------------------------------- auth API
@app.post("/api/login")
async def api_login(request: Request):
    body = await guards.read_json(request)
    username = (body.get("username") or "").strip()
    key = "login:" + username.lower()
    _fail_check(key)
    # pbkdf2 — в пуле потоков, чтобы не блокировать event loop
    u = await run_in_threadpool(auth.verify, username, body.get("password") or "")
    if not u:
        auth.fail_add(key)
        raise HTTPException(401, "Неверный логин или пароль")
    auth.fail_reset(key)
    try:
        activity.on_login(u["username"])
    except Exception:
        pass
    resp = JSONResponse({"ok": True, "username": u["username"], "role": u["role"]})
    _set_cookie(resp, await run_in_threadpool(auth.create_session, u["username"]))
    return resp


@app.post("/api/logout")
def api_logout(request: Request):
    auth.logout(request.cookies.get(auth.COOKIE_NAME))  # только это устройство
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


@app.get("/api/me")
def api_me(request: Request):
    u = _require(request)
    out = {**u, "is_buyer": is_buyer(u)}
    if is_demo(u):
        used = activity.summary(u["username"])["runs"]
        out["demo_runs_left"] = max(0, DEMO_LIMIT - used)
    return out


@app.post("/api/change-password")
async def api_change_password(request: Request):
    u = _require(request)
    body = await guards.read_json(request)
    old = body.get("old") or ""
    new = body.get("new") or ""
    key = "pwd:" + u["username"].lower()
    _fail_check(key)
    if not await run_in_threadpool(auth.verify, u["username"], old):
        auth.fail_add(key)
        raise HTTPException(400, "Текущий пароль неверный")
    auth.fail_reset(key)
    try:
        # завершает все сессии пользователя, включая текущую…
        await run_in_threadpool(auth.set_password, u["username"], new)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # …а этому устройству сразу выдаём новую, чтобы не выкидывать на вход
    resp = JSONResponse({"ok": True})
    _set_cookie(resp, await run_in_threadpool(auth.create_session, u["username"]))
    return resp


# ------------------------------------------------------------- admin API
@app.get("/api/admin/users")
def admin_list(request: Request):
    _require_admin(request)
    return {"users": auth.list_users()}


@app.post("/api/admin/users")
async def admin_create(request: Request):
    _require_admin(request)
    body = await guards.read_json(request)
    groups = [BUYER_GROUP] if body.get("buyer") else []
    try:
        await run_in_threadpool(auth.create_user,
                                (body.get("username") or "").strip(),
                                body.get("password") or "",
                                body.get("role") or "user", groups)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/admin/set-buyer")
async def admin_set_buyer(request: Request):
    _require_admin(request)
    body = await guards.read_json(request)
    username = (body.get("username") or "").strip()
    groups = [BUYER_GROUP] if body.get("buyer") else []
    try:
        auth.set_groups(username, groups)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/api/my/history")
def my_history(request: Request):
    u = _require(request)
    return activity.summary(u["username"])


@app.get("/api/my/result/{rid}")
def my_result(request: Request, rid: str):
    u = _require(request)
    data, name = activity.read_result(u["username"], rid)
    if not data:
        raise HTTPException(404, "Результат не найден")
    return _xlsx_response(data, name)


@app.delete("/api/my/result/{rid}")
def my_result_delete(request: Request, rid: str):
    u = _require(request)
    if not activity.delete_result(u["username"], rid):
        raise HTTPException(404, "Результат не найден")
    return {"ok": True}


@app.get("/api/admin/token-usage")
def admin_token_usage(request: Request):
    """Суммарный расход токенов Yandex AI Studio с момента запуска процесса
    (для разовых замеров «сколько уходит на обработку» — не персистентная
    статистика, сбрасывается при перезапуске контейнера, см. yandex.py)."""
    _require_admin(request)
    with yandex._TOKENS_LOCK:
        return dict(yandex._TOKENS_TOTAL)


@app.get("/api/admin/activity")
def admin_activity(request: Request):
    _require_admin(request)
    stats = activity.all_stats()
    users = {u["username"]: u for u in auth.list_users()}
    rows = []
    for uname, uinfo in users.items():
        st = stats.get(uname, {})
        rows.append({"username": uname, "role": uinfo["role"],
                     "groups": uinfo.get("groups", []),
                     "runs": st.get("runs", 0), "results": st.get("results", 0),
                     "last_login": st.get("last_login", 0),
                     "last_run": st.get("last_run", 0)})
    rows.sort(key=lambda r: r["last_run"], reverse=True)
    # итог за всё время — по ВСЕМ записям статистики (включая удалённые учётки)
    total_runs = sum(v.get("runs", 0) for v in stats.values())
    total_results = sum(v.get("results", 0) for v in stats.values())
    return {"users": rows, "total_runs": total_runs, "total_results": total_results,
            "total_accounts_ever": len(stats)}


@app.get("/api/admin/activity/{username}")
def admin_activity_user(request: Request, username: str):
    _require_admin(request)
    return activity.summary(username)


@app.get("/api/admin/result/{username}/{rid}")
def admin_result(request: Request, username: str, rid: str):
    _require_admin(request)
    data, name = activity.read_result(username, rid)
    if not data:
        raise HTTPException(404, "Результат не найден")
    return _xlsx_response(data, name)


@app.post("/api/admin/reset-password")
async def admin_reset(request: Request):
    _require_admin(request)
    body = await guards.read_json(request)
    try:
        await run_in_threadpool(auth.set_password,
                                (body.get("username") or "").strip(),
                                body.get("password") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.delete("/api/admin/users/{username}")
def admin_delete(request: Request, username: str):
    me = _require_admin(request)
    if username == me["username"]:
        raise HTTPException(400, "Нельзя удалить самого себя")
    if not auth.delete_user(username):
        raise HTTPException(400, "Нельзя удалить (нет пользователя или это админ)")
    return {"ok": True}


# ------------------------------------------------------------- обработка API
@app.post("/api/process")
async def process(request: Request, files: list[UploadFile] = File(...),
                  sourcing: str = Form("0"), web: str = Form("0")):
    u = _require(request)
    do_sourcing = str(sourcing).lower() in ("1", "true", "yes", "on")
    # «Закупка (Китай)» всегда ищет в интернете (в т.ч. фото) — чекбокса
    # для этого режима в интерфейсе нет, значение из формы игнорируется
    web_search = True if do_sourcing else str(web).lower() in ("1", "true", "yes", "on")
    if do_sourcing and not is_buyer(u):
        raise HTTPException(403, "Закупка доступна только группе «Закупщик»")
    if is_demo(u) and activity.summary(u["username"])["runs"] >= DEMO_LIMIT:
        # 402 Payment Required — семантически точный код, фронт по нему
        # показывает отдельное окно «нужна подписка», а не обычную ошибку
        raise HTTPException(402, f"Демо-доступ исчерпан ({DEMO_LIMIT} обработок) — "
                                  f"чтобы продолжить, нужно приобрести подписку")
    if len(files) > uploads.MAX_FILES:
        raise HTTPException(400, f"Не больше {uploads.MAX_FILES} файлов за раз")
    datas = []
    try:
        for f in files:
            uploads.check_extension(f.filename)
            # читаем не больше лимита+1 байт — лишнее в память не попадает
            data = await f.read(uploads.MAX_FILE_BYTES + 1)
            datas.append(await run_in_threadpool(uploads.validate, f.filename, data))
    except uploads.UploadError as e:
        raise HTTPException(e.status, e.message)
    if not datas:
        raise HTTPException(400, "Не выбраны файлы")
    filename = os.path.splitext(files[0].filename)[0]
    if len(files) > 1:
        filename += "_свод"
    job_id = jobs.start(u["username"], datas, [f.filename for f in files],
                        filename + ".xlsx", do_sourcing, web_search)
    return {"job_id": job_id}


@app.get("/api/status/{job_id}")
def status(request: Request, job_id: str):
    u = _require(request)
    with JOBS_LOCK:
        j = jobs.job_of(u, job_id)
        if not j:
            raise HTTPException(404, "Задача не найдена")
        return {"status": j.get("status"), "message": j.get("message"),
                "current": j.get("current", 0), "total": j.get("total", 0),
                "rate": j.get("rate"), "has_result": "result" in j,
                "warnings": j.get("warnings", []), "created": j.get("created", 0)}


@app.post("/api/cancel/{job_id}")
def cancel_job(request: Request, job_id: str):
    u = _require(request)
    if not jobs.cancel(job_id, u["username"]):
        raise HTTPException(404, "Задача не найдена или уже завершена")
    return {"ok": True}


@app.get("/api/preview/{job_id}")
def preview(request: Request, job_id: str):
    u = _require(request)
    with JOBS_LOCK:
        j = jobs.job_of(u, job_id)
        if not j or j.get("status") != "done":
            raise HTTPException(404, "Результат не готов")
        return JSONResponse({"preview": j.get("preview", []),
                             "warnings": j.get("warnings", [])})


@app.get("/api/img/{job_id}/{idx}")
def src_img(request: Request, job_id: str, idx: int):
    u = _require(request)
    with JOBS_LOCK:
        j = jobs.job_of(u, job_id)
        th = (j or {}).get("src_thumbs")
        if not th or idx < 0 or idx >= len(th) or not th[idx]:
            raise HTTPException(404, "Нет картинки")
        data = th[idx]
    return Response(content=data, media_type="image/png")


def _xlsx_response(data: bytes, name: str):
    ascii_name = name.encode("ascii", "ignore").decode() or "invix_result.xlsx"
    disposition = (f"attachment; filename=\"{ascii_name}\"; "
                   f"filename*=UTF-8''{quote(name)}")
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disposition})


@app.get("/api/tz-template")
def tz_template(request: Request):
    _require_buyer(request)
    return _xlsx_response(sourcing.build_tz_template(), "Шаблон_ТЗ_закупка.xlsx")


@app.get("/api/result/{job_id}")
def result(request: Request, job_id: str):
    u = _require(request)
    with JOBS_LOCK:
        j = jobs.job_of(u, job_id)
        if not j or "result" not in j:
            raise HTTPException(404, "Результат не готов")
        data = j["result"]
        name = j.get("result_name", "result.xlsx")
    try:
        activity.on_download(u["username"], name)
    except Exception:
        pass
    return _xlsx_response(data, name)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
