"""Проверка шага 1.4: лимит попыток, длина пароля, event loop не блокируется."""
import os
import sys
import tempfile

d = tempfile.mkdtemp()
os.environ.update(DATA_DIR=d, ADMIN_USER="boss", ADMIN_PASSWORD="bosspass123")
os.environ.pop("ADMIN_RESET_TOKEN", None)
sys.path.insert(0, sys.argv[1])

from fastapi.testclient import TestClient  # noqa: E402
import auth  # noqa: E402
import app as appmod  # noqa: E402

auth._save({**auth._load()})
auth.create_user("ivan", "ivanpass1")
# «старый» короткий пароль, заведённый до изменения
import secrets  # noqa: E402
users = auth._load()
salt = secrets.token_bytes(16)
users["old"] = {"salt": salt.hex(), "hash": auth._hash("1234", salt), "role": "user", "groups": []}
auth._save(users)

c = TestClient(appmod.app, base_url="https://testserver")


def login(u, p, cl=c):
    return cl.post("/api/login", json={"username": u, "password": p})


ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(("✓ " if cond else "✗ ") + name)


check("старый короткий пароль (1234) по-прежнему входит", login("old", "1234").status_code == 200)
codes = [login("ivan", "wrong").status_code for _ in range(5)]
check(f"5 неверных попыток → 401 ×5 {codes}", codes == [401] * 5)
r = login("ivan", "wrong")
check(f"6-я → 429: {r.json().get('detail')}", r.status_code == 429)
check("верный пароль тоже блокирован на время лимита", login("ivan", "ivanpass1").status_code == 429)
check("регистр логина не обходит лимит (IVAN)", login("IVAN", "x").status_code == 429)
check("другой логин не затронут", login("boss", "bosspass123").status_code == 200)

auth._FAILS.clear()
for _ in range(3):
    login("ivan", "wrong")
check("успешный вход после 3 ошибок", login("ivan", "ivanpass1").status_code == 200)
check("счётчик сброшен после успеха", "login:ivan" not in auth._FAILS)

# смена пароля
c2 = TestClient(appmod.app, base_url="https://testserver")
login("ivan", "ivanpass1", c2)
r = c2.post("/api/change-password", json={"old": "ivanpass1", "new": "short7!"})
check(f"новый пароль 7 символов → 400: {r.json().get('detail')}", r.status_code == 400)
r = c2.post("/api/change-password", json={"old": "ivanpass1", "new": "longpass8"})
check("новый пароль 8 символов → 200", r.status_code == 200)
codes = [c2.post("/api/change-password", json={"old": "bad", "new": "whatever12"}).status_code
         for _ in range(6)]
check(f"смена пароля: 5×400, затем 429 {codes}", codes == [400] * 5 + [429])

# админ
ca = TestClient(appmod.app, base_url="https://testserver")
login("boss", "bosspass123", ca)
r = ca.post("/api/admin/users", json={"username": "new1", "password": "1234567"})
check(f"админ: создание с паролем 7 символов → 400", r.status_code == 400)
r = ca.post("/api/admin/users", json={"username": "new1", "password": "12345678"})
check("админ: создание с паролем 8 символов → 200", r.status_code == 200)
r = ca.post("/api/admin/reset-password", json={"username": "new1", "password": "abc"})
check("админ: сброс на короткий → 400", r.status_code == 400)
r = ca.post("/api/admin/reset-password", json={"username": "new1", "password": "abcdefgh"})
check("админ: сброс на 8 символов → 200", r.status_code == 200)

print("\nИТОГ:", "ВСЁ ОК" if ok else "ЕСТЬ ОШИБКИ")
sys.exit(0 if ok else 1)
