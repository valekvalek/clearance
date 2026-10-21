"""
InvixAI Web — веб-сервис обработки китайских инвойсов.
Загрузка xlsx -> ИИ-парсинг -> перевод + поиск товара -> экспорт в шаблон.
С простой авторизацией (логин + админ, управляющий учётками).
"""
import os
import time
import uuid
import threading
import traceback
from urllib.parse import quote

from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Form
from fastapi.responses import (HTMLResponse, JSONResponse, Response,
                               FileResponse, RedirectResponse)

import pipeline
import currency
import auth
import activity

app = FastAPI(title="InvixAI Web")
auth.init_admin()

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

JOBS = {}
JOBS_LOCK = threading.Lock()


# ------------------------------------------------------------- авторизация
def _user(request: Request):
    tok = request.cookies.get(auth.COOKIE_NAME)
    return auth.read_token(tok) if tok else None


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


def is_buyer(u) -> bool:
    return bool(u) and (u.get("role") == "admin" or BUYER_GROUP in u.get("groups", []))


def _require_buyer(request: Request):
    u = _require(request)
    if not is_buyer(u):
        raise HTTPException(403, "Доступно только группе «Закупщик»")
    return u


def _set_cookie(resp, token):
    resp.set_cookie(auth.COOKIE_NAME, token, max_age=auth.SESSION_TTL,
                    httponly=True, secure=True, samesite="lax", path="/")


# ------------------------------------------------------------- обработка
def _set(job_id, **kw):
    with JOBS_LOCK:
        JOBS[job_id].update(kw)


def _progress(job_id):
    def cb(current, total, message):
        _set(job_id, current=current, total=total, message=message)
    return cb


def _run_job(job_id, datas, filename, do_sourcing=False, web_search=True, username=""):
    try:
        if username:
            activity.on_run_start(username, filename)
        import vibecode
        if not vibecode.API_KEY:
            _set(job_id, status="error",
                 message="Не задан ключ VibeCode (VIBE_API_KEY) — обратитесь к администратору сервиса")
            return
        _set(job_id, status="parsing", message="Читаю файлы и распознаю товары...")
        items = pipeline.extract_all(datas)
        if not items:
            _set(job_id, status="error", message="Не удалось найти товары в файлах")
            return
        # картинки из исходных файлов (если есть) — приоритет над веб-поиском
        pipeline.map_source_images(items, datas)
        src_thumbs = []
        for it in items:
            t = pipeline._thumb_from_bytes(it["src_image"]) if it.get("src_image") else None
            src_thumbs.append(t[0].getvalue() if t else None)
        _set(job_id, src_thumbs=src_thumbs)
        _set(job_id, status="enriching", total=len(items), current=0,
             message=f"Найдено {len(items)} товаров, обогащаю...")
        rate = currency.get_rate()
        _set(job_id, rate=rate["display"])
        inv_ctx = ""
        for data in datas:
            if data[:5] != b"%PDF-":
                try:
                    ss = pipeline.read_sheets(data)
                    if ss and ss[0]["grid"].strip():
                        inv_ctx = "\n".join(ss[0]["grid"].splitlines()[:12])
                        break
                except Exception:
                    pass
        pipeline.enrich_items(items, progress=_progress(job_id),
                              web_search=web_search, invoice_context=inv_ctx)
        if do_sourcing:
            _set(job_id, status="sourcing", current=0,
                 message="Поиск на китайских площадках...")
            pipeline.source_items(items, progress=_progress(job_id))
        xlsx = pipeline.build_workbook(items, rate["rmb_to_usd"], with_sourcing=do_sourcing)
        preview = []
        for idx, it in enumerate(items):
            row = {
                "name_ru": it["name_ru"] or it["name_original"],
                "name_zh": it["name_zh"], "brand": it["brand"],
                "article": it["article"] or it["internal_code"],
                "qty": it["quantity"], "unit": it["unit"], "url": it["product_url"],
                "photo": (f"/api/img/{job_id}/{idx}" if src_thumbs[idx]
                          else it.get("photo_url", "")),
                "total_usd": round(it["total_price"] * rate["rmb_to_usd"], 2)
                if pipeline._is_rmb(it) else round(it["total_price"], 2),
            }
            if do_sourcing:
                s = it.get("sourcing") or {}
                row["src"] = {
                    "china_url": s.get("china_url", ""), "supplier": s.get("supplier", ""),
                    "price_cn": s.get("price_cn", ""), "moq": s.get("moq", ""),
                    "links": s.get("links", {}),
                }
            preview.append(row)
        result_name = (os.path.splitext(filename)[0]
                       + ("_закупка.xlsx" if do_sourcing else "_result.xlsx"))
        _set(job_id, status="done", message="Готово", result=xlsx, preview=preview,
             result_name=result_name, current=len(items), total=len(items))
        if username:
            try:
                activity.on_result(username, result_name, len(items), xlsx)
            except Exception:
                pass
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", message=f"Ошибка: {e}",
             trace=traceback.format_exc())


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
    body = await request.json()
    u = auth.verify((body.get("username") or "").strip(), body.get("password") or "")
    if not u:
        raise HTTPException(401, "Неверный логин или пароль")
    try:
        activity.on_login(u["username"])
    except Exception:
        pass
    resp = JSONResponse({"ok": True, "username": u["username"], "role": u["role"]})
    _set_cookie(resp, auth.make_token(u["username"]))
    return resp


@app.post("/api/logout")
def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


@app.get("/api/me")
def api_me(request: Request):
    u = _require(request)
    return {**u, "is_buyer": is_buyer(u)}


@app.post("/api/change-password")
async def api_change_password(request: Request):
    u = _require(request)
    body = await request.json()
    old = body.get("old") or ""
    new = body.get("new") or ""
    if not auth.verify(u["username"], old):
        raise HTTPException(400, "Текущий пароль неверный")
    if len(new) < 4:
        raise HTTPException(400, "Пароль слишком короткий (мин. 4 символа)")
    auth.set_password(u["username"], new)
    return {"ok": True}


# ------------------------------------------------------------- admin API
@app.get("/api/admin/users")
def admin_list(request: Request):
    _require_admin(request)
    return {"users": auth.list_users()}


@app.post("/api/admin/users")
async def admin_create(request: Request):
    _require_admin(request)
    body = await request.json()
    groups = [BUYER_GROUP] if body.get("buyer") else []
    try:
        auth.create_user((body.get("username") or "").strip(),
                         body.get("password") or "",
                         body.get("role") or "user", groups)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/admin/set-buyer")
async def admin_set_buyer(request: Request):
    _require_admin(request)
    body = await request.json()
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
    body = await request.json()
    try:
        auth.set_password((body.get("username") or "").strip(),
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
    web_search = str(web).lower() in ("1", "true", "yes", "on")
    if do_sourcing and not is_buyer(u):
        raise HTTPException(403, "Закупка доступна только группе «Закупщик»")
    ok_ext = (".xlsx", ".xls", ".pdf")
    datas = []
    for f in files:
        if not f.filename.lower().endswith(ok_ext):
            raise HTTPException(400, f"Файл {f.filename}: нужен .xlsx/.xls/.pdf")
        datas.append(await f.read())
    if not datas:
        raise HTTPException(400, "Не выбраны файлы")
    filename = os.path.splitext(files[0].filename)[0]
    if len(files) > 1:
        filename += "_свод"
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        # чистим задачи старше 2 часов (память контейнера ограничена)
        cutoff = time.time() - 7200
        for k in [k for k, v in JOBS.items() if v.get("created", 0) < cutoff]:
            del JOBS[k]
        JOBS[job_id] = {"status": "queued", "message": "В очереди",
                        "current": 0, "total": 0, "created": time.time()}
    threading.Thread(target=_run_job,
                     args=(job_id, datas, filename + ".xlsx", do_sourcing,
                           web_search, u["username"]),
                     daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/status/{job_id}")
def status(request: Request, job_id: str):
    _require(request)
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            raise HTTPException(404, "Задача не найдена")
        return {"status": j.get("status"), "message": j.get("message"),
                "current": j.get("current", 0), "total": j.get("total", 0),
                "rate": j.get("rate"), "has_result": "result" in j}


@app.get("/api/preview/{job_id}")
def preview(request: Request, job_id: str):
    _require(request)
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j or j.get("status") != "done":
            raise HTTPException(404, "Результат не готов")
        return JSONResponse({"preview": j.get("preview", [])})


@app.get("/api/img/{job_id}/{idx}")
def src_img(request: Request, job_id: str, idx: int):
    _require(request)
    with JOBS_LOCK:
        j = JOBS.get(job_id)
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


@app.get("/api/result/{job_id}")
def result(request: Request, job_id: str):
    u = _require(request)
    with JOBS_LOCK:
        j = JOBS.get(job_id)
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
