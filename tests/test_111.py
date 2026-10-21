"""Проверка шага 1.11: CSRF и строгий JSON."""
import os, sys, tempfile, warnings; warnings.filterwarnings("ignore")
os.environ.update(DATA_DIR=tempfile.mkdtemp(), ADMIN_USER="boss", ADMIN_PASSWORD="bosspass123",
                  ALLOWED_ORIGINS="https://invix.example.com")
sys.path.insert(0, sys.argv[1])
from fastapi.testclient import TestClient
import app as appmod, auth
H = "app-c2dfc305f388.paas-host.example"
c = TestClient(appmod.app, base_url=f"https://{H}")
assert c.post("/api/login", json={"username": "boss", "password": "bosspass123"}).status_code == 200
ok = True
n = [0]
def create(headers, **kw):
    n[0] += 1
    return c.post("/api/admin/users", headers=headers,
                  json={"username": f"user{n[0]}", "password": "longpass1"}, **kw)
def check(name, r, code):
    global ok; good = r.status_code == code; ok &= good
    det = r.json().get("detail", "") if r.status_code != 200 else "ok"
    print(("✓ " if good else f"✗ (получен {r.status_code}) ") + f"{name} → {code} {det}")

print("— браузер с Sec-Fetch-Site —")
check("со своей страницы (same-origin)", create({"Sec-Fetch-Site": "same-origin"}), 200)
check("соседнее приложение на том же хостинге (same-site)", create({"Sec-Fetch-Site": "same-site"}), 403)
check("чужой сайт (cross-site)", create({"Sec-Fetch-Site": "cross-site"}), 403)
check("cross-site, даже с «правильным» Origin", create({"Sec-Fetch-Site": "cross-site", "Origin": f"https://{H}"}), 403)
check("DELETE с чужого сайта", c.delete("/api/admin/users/user1", headers={"Sec-Fetch-Site": "cross-site"}), 403)
check("загрузка файла с чужого сайта", c.post("/api/process", headers={"Sec-Fetch-Site": "cross-site"},
      files=[("files", ("a.xlsx", b"x"))]), 403)
print("— старый браузер: только Origin —")
check("Origin = свой хост", create({"Origin": f"https://{H}"}), 200)
check("Origin = соседнее приложение", create({"Origin": "https://evil.paas-host.example"}), 403)
check("Origin = null (sandbox iframe)", create({"Origin": "null"}), 403)
check("Origin из ALLOWED_ORIGINS (будущий свой домен)", create({"Origin": "https://invix.example.com"}), 200)
check("свой домен за прокси (X-Forwarded-Host)", create({"Origin": "https://invix.site", "X-Forwarded-Host": "invix.site"}), 200)
print("— не браузер —")
check("curl без заголовков", create({}), 200)
check("GET с чужого сайта не блокируется (чтение)", c.get("/api/me", headers={"Sec-Fetch-Site": "cross-site"}), 200)
print("— строгий JSON —")
check("вход с text/plain (как из HTML-формы)", c.post("/api/login", content='{"username":"boss","password":"bosspass123"}',
      headers={"Content-Type": "text/plain"}), 415)
check("вход с form-urlencoded", c.post("/api/login", data={"username": "boss"}), 415)
check("битый JSON", c.post("/api/login", content="{oops", headers={"Content-Type": "application/json"}), 400)
check("JSON-массив вместо объекта", c.post("/api/login", json=[1, 2]), 400)
check("application/json; charset=utf-8", c.post("/api/login", content='{"username":"boss","password":"bosspass123"}',
      headers={"Content-Type": "application/json; charset=utf-8"}), 200)
check("выход (POST без тела) со своей страницы", c.post("/api/logout", headers={"Sec-Fetch-Site": "same-origin"}), 200)
print("\nИТОГ:", "ВСЁ ОК" if ok else "ЕСТЬ ОШИБКИ")
