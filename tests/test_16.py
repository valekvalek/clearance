"""Проверка шага 1.6: задачи доступны только владельцу."""
import io, os, sys, tempfile, time, warnings
warnings.filterwarnings("ignore")
os.environ.update(DATA_DIR=tempfile.mkdtemp(), ADMIN_USER="boss", ADMIN_PASSWORD="bosspass123")
os.environ.pop("ADMIN_RESET_TOKEN", None); os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, sys.argv[1])
import openpyxl
from fastapi.testclient import TestClient
import app as appmod, auth
auth.create_user("ivan", "ivanpass1"); auth.create_user("petr", "petrpass1")

def client(u, p):
    c = TestClient(appmod.app, base_url="https://testserver")
    assert c.post("/api/login", json={"username": u, "password": p}).status_code == 200
    return c
ivan, petr, boss = client("ivan", "ivanpass1"), client("petr", "petrpass1"), client("boss", "bosspass123")

wb = openpyxl.Workbook(); b = io.BytesIO(); wb.save(b)
jid = ivan.post("/api/process", files=[("files", ("inv.xlsx", b.getvalue()))]).json()["job_id"]
print(f"job_id длиной {len(jid)}, владелец в задаче: {appmod.JOBS[jid]['owner']}")
time.sleep(0.5)  # без ключа Gemini задача сразу завершится ошибкой — подменим на «готово»
with appmod.JOBS_LOCK:
    appmod.JOBS[jid].update(status="done", result=b"XLSX", result_name="r.xlsx",
                            preview=[{"name_ru": "секрет"}], src_thumbs=[b"PNG"])
ok = True
for path in (f"/api/status/{jid}", f"/api/preview/{jid}", f"/api/img/{jid}/0", f"/api/result/{jid}"):
    codes = {n: c.get(path).status_code for n, c in (("ivan", ivan), ("petr", petr), ("admin", boss))}
    good = codes == {"ivan": 200, "petr": 404, "admin": 404}
    ok &= good
    print(("✓ " if good else "✗ ") + f"{path.split('/')[2]:8} {codes}")
r = petr.get(f"/api/status/{jid}").json()["detail"]
nx = petr.get("/api/status/" + "0" * 32).json()["detail"]
print(("✓ " if r == nx else "✗ ") + f"чужая и несуществующая задача неотличимы: «{r}» / «{nx}»")
ok &= r == nx
print("\nИТОГ:", "ВСЁ ОК" if ok else "ЕСТЬ ОШИБКИ")
