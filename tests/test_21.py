"""Шаг 2.1: потери товаров видны (повторы + предупреждения). ИИ заменён заглушкой."""
import io, json, os, re, sys, tempfile, time, warnings
warnings.filterwarnings("ignore")
os.environ.update(DATA_DIR=tempfile.mkdtemp(), ADMIN_USER="boss", ADMIN_PASSWORD="bosspass123")
sys.path.insert(0, sys.argv[1])
import fitz, openpyxl
from fastapi.testclient import TestClient
import app as appmod, currency, extract, readers, gemini
from report import Report

ok = True
def check(name, cond):
    global ok; ok &= bool(cond); print(("✓ " if cond else "✗ ") + name)

# ---------------- заглушка ИИ ----------------
calls = {"R100_chunk": 0, "enrich_item3": 0, "vision": 0}
def fake_chat(messages, **kw):
    text = messages[-1]["content"]
    if "Разбери инвойс" in text:
        body = text.split("СТРОКИ:\n", 1)[1] if "СТРОКИ:\n" in text else text
        if "R50:" in body:
            raise RuntimeError("HTTP 502: Bad Gateway")          # кусок падает всегда
        if "R100:" in body:
            calls["R100_chunk"] += 1
            if calls["R100_chunk"] == 1:
                return "извините, не могу"                       # падает только 1-й раз
        items = [{"name_original": m.group(2), "quantity": m.group(3), "unit_price": 1.5,
                  "total_price": 1.5 * int(m.group(3)), "currency": "USD", "row": int(m.group(1))}
                 for m in re.finditer(r"^R(\d+): A=(item\d+) \| B=(\d+)", body, re.M)]
        return json.dumps(items)
    if "Обработай товары" in text:
        names = re.findall(r"наименование: (\S+)", text)
        if "item150" in names:
            return "это не JSON"                                 # батч без перевода всегда
        out = [{"name_ru": "RU " + n, "name_zh": "中" + n, "material": "сталь", "brand": "B"} for n in names]
        if "item3" in names and len(names) == 10:
            calls["enrich_item3"] += 1
            return json.dumps(out[:8])                           # ИИ «потерял» 2 из 10
        return json.dumps(out)
    raise AssertionError("неожиданный запрос к ИИ")

def fake_vision(system, user_text, images, **kw):
    calls["vision"] += 1
    if calls["vision"] == 1:
        raise RuntimeError("timeout")
    return json.dumps([{"name_original": "scan-item", "quantity": 1}])

gemini.chat, gemini.chat_vision = fake_chat, fake_vision
gemini.API_KEY = "test"
currency.get_rates = lambda: {"usd_per": {"USD": 1.0, "CNY": 1 / 7}, "date": "01.01.2026", "source": "ЦБ РФ", "stale": False}

def xlsx(rows, title="Invoice"):
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = title
    ws.append(["Name", "Qty", "Price"])
    for n in range(1, rows + 1):
        ws.append([f"item{n}", n, 1.5])
    b = io.BytesIO(); wb.save(b); return b.getvalue()

# ---------------- 1. полный прогон через API ----------------
print("— полный прогон через API: большой инвойс (200 строк) —")
c = TestClient(appmod.app, base_url="https://testserver")
c.post("/api/login", json={"username": "boss", "password": "bosspass123"})
jid = c.post("/api/process", files=[("files", ("big.xlsx", xlsx(200)))]).json()["job_id"]
for _ in range(100):
    st = c.get(f"/api/status/{jid}").json()
    if st["status"] in ("done", "error"):
        break
    time.sleep(0.1)
print("  статус:", st["status"], "|", st["message"])
for w in st["warnings"]:
    print("  ⚠", w)
check("задача завершена, статус говорит о предупреждениях", st["status"] == "done" and "предупреждения (2)" in st["message"])
check("кусок R46–R90 назван точно (файл, лист, строки) — куски режутся по листу, маркер листа в счёт строк не входит",
      any("строки R46–R90 (файл 1 «big.xlsx», лист «Invoice»)" in w for w in st["warnings"]))
check(f"кусок с R100 упал 1 раз и восстановлен повтором (вызовов: {calls['R100_chunk']})",
      calls["R100_chunk"] == 2 and not any("R91–R135" in w or "R100" in w for w in st["warnings"]))
pv = c.get(f"/api/preview/{jid}").json()
rows = pv["preview"]
got = {r["name_ru"].replace("RU ", "") for r in rows}
check(f"в результате 155 товаров (200 − 45 потерянных), потерянные — ровно item45…item89",
      len(rows) == 155 and not any(f"item{n}" in got for n in range(45, 90)))
check(f"батч, где ИИ «потерял» 2 товара, дозаполнен повтором (вызовов: {calls['enrich_item3']})",
      all(r["name_ru"].startswith("RU ") for r in rows[:10]))
bad = [i + 1 for i, r in enumerate(rows) if not r["name_ru"].startswith("RU ")]
wtr = [w for w in pv["warnings"] if "без перевода" in w]
check(f"без перевода {len(bad)} товаров № {bad[0]}–{bad[-1]}, предупреждение их называет: {wtr}",
      len(bad) == 10 and wtr and f"№ {bad[0]}–{bad[-1]}" in wtr[0])
check("предупреждения те же и в предпросмотре", pv["warnings"] == st["warnings"])
res = c.get(f"/api/result/{jid}")
wb = openpyxl.load_workbook(io.BytesIO(res.content))
check(f"в xlsx листы {wb.sheetnames}: Result первый, «Предупреждения» второй",
      wb.sheetnames == ["Result", "Предупреждения"])
sheet = [r[0] for r in wb["Предупреждения"].iter_rows(min_row=2, values_only=True)]
check("на листе «Предупреждения» тот же список", sheet == st["warnings"])

# ---------------- 2. extract_all напрямую ----------------
print("— отдельные случаи —")
r = Report()
extract.extract_all([b"garbage-not-excel", xlsx(3)], report=r, names=["broken.xlsx", "ok.xlsx"])
check(f"нечитаемый файл назван: {r.items[0] if r.items else '—'}",
      any("Файл 1 «broken.xlsx» не прочитан" in w for w in r.items))

r = Report()
doc = fitz.open()
for _ in range(10):
    doc.new_page()   # страницы без текста = «скан»
items = extract.extract_all([doc.tobytes()], report=r, names=["scan.pdf"])
check(f"скан: упал 1 раз, восстановлен повтором (вызовов vision: {calls['vision']}), товар получен",
      calls["vision"] == 2 and items and items[0]["name_original"] == "scan-item")
check(f"скан на 10 стр.: предупреждение о первых 8: {r.items}",
      any("скан на 10 стр., распознаны только первые 8" in w for w in r.items))

r = Report()
sheets = readers.read_sheets(xlsx(3005))
check("лист длиннее 3000 строк помечен как обрезанный", sheets[0].get("truncated"))

r = Report()
items = extract.extract_all([xlsx(3)], report=r, names=["small.xlsx"])
check(f"маленький файл без проблем: {len(items)} товара, предупреждений нет", len(items) == 3 and not r.items)

print("\nИТОГ:", "ВСЁ ОК" if ok else "ЕСТЬ ОШИБКИ")
