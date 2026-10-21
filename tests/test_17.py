"""Проверка шага 1.7 (вариант B): серверные сессии."""
import base64, hashlib, hmac, json, os, sys, tempfile, time, warnings
warnings.filterwarnings("ignore")
D = tempfile.mkdtemp()
os.environ.update(DATA_DIR=D, ADMIN_USER="boss", ADMIN_PASSWORD="bosspass123")
os.environ.pop("ADMIN_RESET_TOKEN", None)
sys.path.insert(0, sys.argv[1])
from fastapi.testclient import TestClient
import app as appmod, auth
auth.create_user("ivan", "ivanpass1")
ok = True
def check(name, cond):
    global ok; ok &= bool(cond); print(("✓ " if cond else "✗ ") + name)
def dev():
    return TestClient(appmod.app, base_url="https://testserver")
def login(c, u="ivan", p="ivanpass1"):
    assert c.post("/api/login", json={"username": u, "password": p}).status_code == 200
    return c
def me(c): return c.get("/api/me").status_code
def legacy_token(user, issued):  # кука старого формата, как её выдавал прежний код
    msg = f"{user}|{issued + auth.SESSION_TTL}"
    sig = hmac.new(auth.SECRET, msg.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{msg}|{sig}".encode()).decode()

print("— выход только на текущем устройстве —")
pc, phone = login(dev()), login(dev())
check("вход с двух устройств: оба 200", me(pc) == 200 and me(phone) == 200)
stolen = pc.cookies.get(auth.COOKIE_NAME)
pc.post("/api/logout")
check("после «Выйти» на ПК: ПК → 401", me(pc) == 401)
check("телефон остался в системе → 200", me(phone) == 200)
thief = dev(); thief.cookies.set(auth.COOKIE_NAME, stolen)
check("скопированная кука ПК после выхода → 401", me(thief) == 401)

print("— смена пароля завершает все устройства, кроме текущего —")
pc = login(dev())
r = phone.post("/api/change-password", json={"old": "ivanpass1", "new": "newpass99"})
check("смена пароля с телефона → 200", r.status_code == 200)
check("телефон (где меняли) остался в системе", me(phone) == 200)
check("ПК выкинуло → 401", me(pc) == 401)

print("— сброс пароля админом и удаление —")
admin = login(dev(), "boss", "bosspass123")
check("админ сбрасывает пароль ivan → 200",
      admin.post("/api/admin/reset-password", json={"username": "ivan", "password": "reset1234"}).status_code == 200)
check("все сессии ivan завершены → 401", me(phone) == 401)
check("админ не затронут", me(admin) == 200)
auth.create_user("petr", "petrpass1"); petr = login(dev(), "petr", "petrpass1")
admin.delete("/api/admin/users/petr")
check("удалённый пользователь → 401", me(petr) == 401)

print("— хранение —")
sess = auth._sess_load()  # теперь в SQLite (app.db), не в sessions.json (шаг 3.5)
raw = phone.cookies.get(auth.COOKIE_NAME) or ""
check(f"в хранилище сессий только хэши ({len(sess)} шт.), самих кук там нет",
      all(len(k) == 64 for k in sess) and raw not in json.dumps(sess))

print("— куки старого формата (перед деплоем все залогинены по ним) —")
auth.create_user("olga", "olgapass1")
old = dev(); old.cookies.set(auth.COOKIE_NAME, legacy_token("olga", int(time.time()) - 3600))
r = old.get("/api/me")
new_cookie = r.cookies.get(auth.COOKIE_NAME)
check("старая кука принимается → 200 (никого не выкидывает)", r.status_code == 200)
check("в ответ выдана новая серверная кука", bool(new_cookie) and auth._read_legacy_token(new_cookie) is None)
# как браузер: кука с тем же именем/путём/хостом заменяет старую
old.cookies.clear(); old.cookies.set(auth.COOKIE_NAME, new_cookie)
check("дальше работает уже по новой куке", me(old) == 200)
old.post("/api/logout")
check("выход после перевода → 401", me(old) == 401)
o2 = dev(); o2.cookies.set(auth.COOKIE_NAME, legacy_token("olga", int(time.time()) - 60))
check("старая кука olga до смены пароля → 200", me(o2) == 200)
auth.set_password("olga", "olganew123")
o3 = dev(); o3.cookies.set(auth.COOKIE_NAME, legacy_token("olga", int(time.time()) - 60))
check("старая кука после смены пароля → 401", me(o3) == 401)
fake = dev(); fake.cookies.set(auth.COOKIE_NAME, base64.urlsafe_b64encode(b"boss|9999999999|deadbeef").decode())
check("подделанная старая кука → 401", me(fake) == 401)
print("\nИТОГ:", "ВСЁ ОК" if ok else "ЕСТЬ ОШИБКИ")
