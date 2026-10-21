import os, sys, tempfile, warnings; warnings.filterwarnings("ignore")
os.environ.update(DATA_DIR=tempfile.mkdtemp(), ADMIN_USER="boss", ADMIN_PASSWORD="bosspass123"); sys.path.insert(0, sys.argv[1])
from fastapi.testclient import TestClient
import app as appmod
c = TestClient(appmod.app, base_url="https://testserver")
c.post("/api/login", json={"username": "boss", "password": "bosspass123"})
ok = True
for name, code in [("a'\"<x>", 400), ("ab", 400), ("a" * 41, 400), ("иван", 400), ("a b", 400),
                   ("KKK", 200), ("Ivan.Petrov-2_x", 200)]:
    r = c.post("/api/admin/users", json={"username": name, "password": "longpass1"})
    good = r.status_code == code; ok &= good
    print(("✓ " if good else "✗ ") + f"логин {name[:20]!r} → {r.status_code} {r.json().get('detail', '')}")
print("ИТОГ:", "ВСЁ ОК" if ok else "ЕСТЬ ОШИБКИ")
