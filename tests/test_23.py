"""Шаг 2.3: дедуп после распознавания крупного файла — по позиции исходной строки
(src_file, src_sheet, src_row), а не по содержимому. Раньше две ОДИНАКОВЫЕ, но
законные строки (тот же товар в двух разных заказах по той же цене) склеивались в
одну. ИИ заменён заглушкой."""
import io, json, os, re, sys, tempfile, warnings
warnings.filterwarnings("ignore")
os.environ.update(DATA_DIR=tempfile.mkdtemp(), ADMIN_USER="boss", ADMIN_PASSWORD="bosspass123")
sys.path.insert(0, sys.argv[1])
import openpyxl
import extract, gemini
from report import Report

ok = True
def check(name, cond):
    global ok; ok &= bool(cond); print(("✓ " if cond else "✗ ") + name)


def blank_item(**kw):
    it = {"name_original": "", "brand": "", "article": "", "internal_code": "", "spec": "",
          "quantity": 0.0, "unit": "шт", "unit_price": 0.0, "currency_raw": "", "currency": None,
          "total_price": 0.0, "gross_weight": 0.0, "net_weight": 0.0, "pack_weight": 0.0,
          "src_file": 1, "src_sheet": "Invoice", "src_row": 0, "name_ru": "", "name_zh": "",
          "material": "", "description": "", "product_url": "", "photo_url": "",
          "sourcing": None, "src_image": None, "src_alt": []}
    it.update(kw)
    return it


print("— _dedup_items: юнит-проверки —")
same_pos_a = blank_item(name_original="X", article="A1", quantity=1, unit_price=1.0, src_row=10)
same_pos_b = blank_item(name_original="X", article="A1", quantity=1, unit_price=1.0, src_row=10)  # реальный повтор
two_orders_a = blank_item(name_original="Y", article="A2", quantity=5, unit_price=2.0, src_row=11)
two_orders_b = blank_item(name_original="Y", article="A2", quantity=5, unit_price=2.0, src_row=12)  # другая строка!
diff_sheet = blank_item(name_original="Z", article="A3", quantity=1, unit_price=1.0, src_row=10, src_sheet="Packing")
no_row_a = blank_item(name_original="N", article="A4", quantity=1, unit_price=1.0, src_row=0)
no_row_b = blank_item(name_original="N", article="A4", quantity=1, unit_price=1.0, src_row=0)  # позиции нет — по содержимому

items = [same_pos_a, same_pos_b, two_orders_a, two_orders_b, diff_sheet, no_row_a, no_row_b]
uniq = extract._dedup_items(items)
has = lambda x: any(x is y for y in uniq)  # dict'и с одинаковым содержимым равны по ==, нужна identity
check(f"настоящий повтор (та же позиция) убран: {len(uniq)} из {len(items)}", not has(same_pos_b) and has(same_pos_a))
check("два одинаковых на вид товара из РАЗНЫХ строк — оба остались (это не дубль)",
      has(two_orders_a) and has(two_orders_b))
check("та же строка №10, но другой лист — не считается дублем", has(diff_sheet))
check("без позиции (src_row=0) дедуп по содержимому — как раньше", has(no_row_a) and not has(no_row_b))
check(f"итог: {len(uniq)} записей (было 7, убрано 2 настоящих дубля)", len(uniq) == 5)

print("— крупный файл: два одинаковых заказа одного товара не склеиваются —")
N = 90
wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Invoice"
for n in range(1, N + 1):
    # первые 2 строки — ОДИН И ТОТ ЖЕ товар (тот же артикул, то же кол-во, та же цена),
    # но это две разные позиции инвойса (два разных заказа), их нельзя склеивать
    art = "SAME" if n <= 2 else f"ART{n:03d}"
    ws.append([f"item{n}", art, 3, 9.99])
b = io.BytesIO(); wb.save(b)

def fake_chat(messages, **kw):
    t = messages[-1]["content"]
    body = t.split("СТРОКИ:\n", 1)[1] if "СТРОКИ:\n" in t else t
    items = [{"name_original": m.group(2), "article": m.group(3), "quantity": m.group(4),
              "unit_price": m.group(5), "total_price": "", "currency": "USD", "row": int(m.group(1))}
             for m in re.finditer(r"^R(\d+): A=(\S+) \| B=(\S+) \| C=(\d+) \| D=([\d.]+)$", body, re.M)]
    return json.dumps(items)

gemini.chat, gemini.API_KEY = fake_chat, "test"
r = Report()
items = extract.extract_all([b.getvalue()], report=r, names=["combo.xlsx"])
same_art = [it for it in items if it["article"] == "SAME"]
check(f"{N} строк, оба заказа товара SAME на месте (не склеены в один): {len(items)} товаров, SAME: {len(same_art)}",
      len(items) == N and len(same_art) == 2)
check("у обоих заказов SAME разные исходные строки (src_row)",
      len(same_art) == 2 and same_art[0]["src_row"] != same_art[1]["src_row"])

print("— настоящий повтор от шапки-контекста (одна и та же строка попала в два куска) —")
r = Report()
uniq = extract._dedup_items([
    blank_item(name_original="dup", article="DUP", quantity=1, unit_price=5.0, src_row=1, src_sheet="Invoice"),
    blank_item(name_original="dup", article="DUP", quantity=1, unit_price=5.0, src_row=1, src_sheet="Invoice"),
])
check("строка R1, повторно распознанная шапкой-контекстом соседнего куска, встречается один раз", len(uniq) == 1)

print("\nИТОГ:", "ВСЁ ОК" if ok else "ЕСТЬ ОШИБКИ")
