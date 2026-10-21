"""Шаг 2.10: режим PDF (текст/скан) выбирается по каждой СТРАНИЦЕ отдельно, а не по
всему документу — один файл может содержать и текстовые страницы, и страницы-сканы.
MAX_VISION_PAGES вынесен в переменную окружения. ИИ заменён заглушкой."""
import io, json, os, sys, tempfile, warnings
warnings.filterwarnings("ignore")
os.environ.update(DATA_DIR=tempfile.mkdtemp(), ADMIN_USER="boss", ADMIN_PASSWORD="bosspass123",
                   MAX_VISION_PAGES="2")
sys.path.insert(0, sys.argv[1])
import fitz  # noqa: E402
import extract, readers, gemini  # noqa: E402
from report import Report  # noqa: E402

ok = True
def check(name, cond):
    global ok; ok &= bool(cond); print(("✓ " if cond else "✗ ") + name)


check(f"MAX_VISION_PAGES читается из окружения: {readers.MAX_VISION_PAGES}",
      readers.MAX_VISION_PAGES == 2)

TEXT = "Строка инвойса с товаром номер " * 6  # >=150 символов

print("— _read_pdf: смешанный документ, режим по каждой странице —")
doc = fitz.open()
doc.new_page().insert_text((50, 50), TEXT)
doc.new_page()                                   # пустая = скан
doc.new_page().insert_text((50, 50), TEXT)
sheets, vision_pages, n_scan = readers._read_pdf(doc.tobytes())
check(f"текстовые страницы распознаны как текст: {[s['name'] for s in sheets]}",
      [s["name"] for s in sheets] == ["PDF стр.1", "PDF стр.3"])
check(f"скан-страница посчитана отдельно от текстовых: {n_scan}", n_scan == 1)
check(f"для неё отрендерена картинка: {len(vision_pages)}", len(vision_pages) == 1)
check("скан-страница — это PNG", vision_pages[0][:8] == b"\x89PNG\r\n\x1a\n")

print("— скан-страниц больше лимита MAX_VISION_PAGES: обрезка, остальные только считаются —")
doc2 = fitz.open()
for _ in range(5):
    doc2.new_page()                              # 5 пустых страниц = скан
sheets2, vision2, n_scan2 = readers._read_pdf(doc2.tobytes())
check(f"текстовых страниц нет: {sheets2}", sheets2 == [])
check(f"скан-страниц всего посчитано: {n_scan2}", n_scan2 == 5)
check(f"отрендерено только MAX_VISION_PAGES: {len(vision2)}", len(vision2) == 2)

print("— extract_all: товары и с текстовых, и со скан-страниц ОДНОГО файла —")
def fake_chat(messages, **kw):
    return json.dumps([{"name_original": "text-item", "quantity": 1, "unit_price": 1,
                         "total_price": 1, "currency": "USD", "row": 1}])
def fake_vision(system, user_text, images, **kw):
    return json.dumps([{"name_original": "scan-item", "quantity": 1, "unit_price": 2,
                         "total_price": 2, "currency": "USD"}])
gemini.chat, gemini.chat_vision, gemini.API_KEY = fake_chat, fake_vision, "test"

r = Report()
items = extract.extract_all([doc.tobytes()], report=r, names=["mixed.pdf"])
names = sorted(it["name_original"] for it in items)
check(f"найдены товары и с текстовых, и со скан-страницы одного файла: {names}",
      names == ["scan-item", "text-item"])
check(f"скан-страница (1) в пределах лимита (2) — предупреждения про обрезку нет: {r.items}",
      not any("скан на" in w for w in r.items))

print("— файл, где ВСЕ страницы — скан: товар с vision есть, ложного предупреждения нет —")
r2 = Report()
doc3 = fitz.open()
doc3.new_page()
items3 = extract.extract_all([doc3.tobytes()], report=r2, names=["scan_only.pdf"])
check(f"нет ложного «не найдено ни одной заполненной строки»: {r2.items}",
      not any("не найдено ни одной заполненной строки" in w for w in r2.items))
check(f"товар со скана получен: {items3}",
      items3 and items3[0]["name_original"] == "scan-item")

print("\nИТОГ:", "ВСЁ ОК" if ok else "ЕСТЬ ОШИБКИ")
