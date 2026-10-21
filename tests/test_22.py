"""Шаг 2.2: крупные файлы режутся ПО ЛИСТАМ (не произвольными окнами через границы
листов), инвойс и пакинг-лист, распознанные раздельными кусками, сводятся в коде по
артикулу/коду/наименованию; фото, оставшееся у слитой строки, подхватывается через
src_alt. ИИ заменён заглушкой."""
import io, json, os, re, sys, tempfile, warnings
warnings.filterwarnings("ignore")
os.environ.update(DATA_DIR=tempfile.mkdtemp(), ADMIN_USER="boss", ADMIN_PASSWORD="bosspass123")
sys.path.insert(0, sys.argv[1])
import openpyxl
import extract, images, readers, gemini
from report import Report

ok = True
def check(name, cond):
    global ok; ok &= bool(cond); print(("✓ " if cond else "✗ ") + name)


def blank_item(**kw):
    it = {"name_original": "", "brand": "", "article": "", "internal_code": "", "spec": "",
          "quantity": 0.0, "unit": "шт", "unit_price": 0.0, "currency_raw": "", "currency": None,
          "total_price": 0.0, "gross_weight": 0.0, "net_weight": 0.0, "pack_weight": 0.0,
          "src_file": 1, "src_sheet": "", "src_row": 0, "name_ru": "", "name_zh": "",
          "material": "", "description": "", "product_url": "", "photo_url": "",
          "sourcing": None, "src_image": None, "src_alt": []}
    it.update(kw)
    return it


print("— _merge_invoice_packing: сведение по ключу, порядок и номера не меняются —")
inv_a = blank_item(article="ART1", unit_price=10.0, total_price=20.0, quantity=2, src_row=5)
pack_a = blank_item(article="ART1", gross_weight=3.5, src_file=2, src_sheet="Packing", src_row=7)
inv_b = blank_item(internal_code="COD2", unit_price=5.0, total_price=5.0, quantity=1, src_row=6)
pack_b = blank_item(internal_code="COD2", gross_weight=1.2, src_file=2, src_sheet="Packing", src_row=8)
inv_c = blank_item(name_original="Товар С", unit_price=1.0, total_price=1.0, quantity=1)
pack_c = blank_item(name_original="товар с", gross_weight=0.9, src_file=2, src_sheet="Packing", src_row=9)
lone_weight = blank_item(article="ART99", gross_weight=9.9, src_row=42)  # без пары — не с чем сводить
items = [inv_a, pack_a, inv_b, lone_weight, pack_b, inv_c, pack_c]
merged = extract._merge_invoice_packing(items)
check(f"дубли-веса убраны, лишний вес без пары остался: {len(merged)} записей (было 7)", len(merged) == 4)
check("порядок и позиции оставшихся товаров не изменились",
      merged == [inv_a, inv_b, lone_weight, inv_c])
check(f"ART1: вес подтянут из пакинг-листа = {inv_a['gross_weight']}", inv_a["gross_weight"] == 3.5)
check(f"сведение по internal_code (артикула нет): вес = {inv_b['gross_weight']}", inv_b["gross_weight"] == 1.2)
check(f"сведение по наименованию (без артикула/кода, без учёта регистра): вес = {inv_c['gross_weight']}",
      inv_c["gross_weight"] == 0.9)
check("строка пакинг-листа запомнена в src_alt (вдруг там фото)",
      inv_a["src_alt"] == [{"file": 2, "sheet": "Packing", "row": 7}])
check("товар без пары (ART99) остался как есть, с ценой не смешался", lone_weight["gross_weight"] == 9.9)

print("— крупный файл: чанк не смешивает два листа, шапка-контекст — своя для каждого листа —")
N = 100
wb = openpyxl.Workbook()
ws1 = wb.active; ws1.title = "Invoice"
for n in range(1, N + 1):
    ws1.append([f"item{n}", f"ART{n:03d}", n % 5 + 1, round(1.0 + n * 0.1, 2)])
ws2 = wb.create_sheet("Packing")
for n in range(1, N + 1):
    ws2.append([f"ART{n:03d}", round(0.5 * n, 2)])
b = io.BytesIO(); wb.save(b)
real_sheets = readers.read_sheets(b.getvalue())
headers = {s["name"]: "\n".join(s["grid"].splitlines()[:8]) for s in real_sheets}

captured = []
def fake_chat(messages, **kw):
    t = messages[-1]["content"]
    captured.append(t)
    is_inv = "ЛИСТ: Invoice" in t
    body = t.split("СТРОКИ:\n", 1)[1] if "СТРОКИ:\n" in t else t
    if is_inv:
        items = [{"name_original": m.group(2), "article": m.group(3), "quantity": m.group(4),
                  "unit_price": m.group(5), "total_price": "", "currency": "USD", "row": int(m.group(1))}
                 for m in re.finditer(r"^R(\d+): A=(\S+) \| B=(\S+) \| C=(\d+) \| D=([\d.]+)$", body, re.M)]
    else:
        items = [{"article": m.group(2), "quantity": 1, "gross_weight": m.group(3), "row": int(m.group(1))}
                 for m in re.finditer(r"^R(\d+): A=(\S+) \| B=([\d.]+)$", body, re.M)]
    return json.dumps(items)

gemini.chat, gemini.API_KEY = fake_chat, "test"
r = Report()
items = extract.extract_all([b.getvalue()], report=r, names=["combo.xlsx"])

mixed = [t for t in captured if ("ЛИСТ: Invoice" in t) and ("ЛИСТ: Packing" in t)]
check(f"ни один из {len(captured)} кусков не смешал оба листа", not mixed)
bad_ctx = []
for t in captured:
    if "КОНТЕКСТ ШАПКИ:\n" not in t:
        continue
    sheet = "Invoice" if "ЛИСТ: Invoice" in t else "Packing"
    ctx = t.split("КОНТЕКСТ ШАПКИ:\n", 1)[1].split("\n\nСТРОКИ:", 1)[0]
    if ctx != headers[sheet]:
        bad_ctx.append(sheet)
check("шапка-контекст в продолжении куска — первые строки ИМЕННО этого листа", not bad_ctx)
check(f"листов длиннее 45 строк — есть куски с контекстом ({sum('КОНТЕКСТ ШАПКИ' in t for t in captured)})",
      any("КОНТЕКСТ ШАПКИ" in t for t in captured))

check(f"итог: {N} товаров, дублей нет", len(items) == N)
by_art = {it["article"]: it for it in items}
check("вес из Packing подтянут ко всем товарам инвойса",
      all(by_art[f"ART{n:03d}"]["gross_weight"] == round(0.5 * n, 2) for n in range(1, N + 1)))
check("цена из Invoice на месте (без искажений)",
      by_art["ART050"]["unit_price"] == round(1.0 + 50 * 0.1, 2))
check("предупреждений нет — все куски распознаны", not r.items)

print("— фото у строки пакинг-листа подхватывается через src_alt —")
IMG = b"\xff\xd8\xffFAKEJPEGDATA"
def fake_read_source_images(data):
    return {"anchored": [{"sheet": "Packing", "row": 7, "data": IMG}]}
images.read_source_images = fake_read_source_images
it = blank_item(article="ARTX", src_file=1, src_sheet="Invoice", src_row=99,
                 src_alt=[{"file": 1, "sheet": "Packing", "row": 7}])
images.map_source_images([it], b"fake-xlsx-bytes")
check(f"фото не нашлось у строки инвойса, но нашлось у строки пакинг-листа из src_alt: {it['src_image']}",
      it["src_image"] == IMG)

print("\nИТОГ:", "ВСЁ ОК" if ok else "ЕСТЬ ОШИБКИ")
