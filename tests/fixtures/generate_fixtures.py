"""
Генератор «эталонных» синтетических инвойсов для tests/test_golden.py.

Реальных образцов инвойсов под рукой не было (см. docs/ПЛАН_ДОРАБОТКИ.md, шаг 3.3),
поэтому эталоны — синтетические, но по структуре как настоящие: инвойс + отдельный
пакинг-лист с весами (сведение по артикулу), разные валюты, числа в разных форматах
(запятая/точка, разделитель тысяч), и скан-страница в PDF. Файлы сохранены в этой же
папке и используются в тестах напрямую (не генерируются на каждый прогон), чтобы
tests/test_golden.py проверял конкретные зафиксированные данные, а не каждый раз
новый случайный документ.

Перезапуск (если понадобится обновить эталоны): python generate_fixtures.py
"""
import os
import openpyxl

HERE = os.path.dirname(__file__)


def build_xlsx():
    wb = openpyxl.Workbook()
    inv = wb.active
    inv.title = "Invoice"
    inv.append(["Наименование", "Артикул", "Кол-во", "Цена за шт.", "Сумма", "Валюта"])
    # сознательно разные форматы чисел и валют, как в реальных инвойсах
    inv.append(["婴儿连体衣 Baby Romper", "BR-001", 100, "1,5", "150", "USD"])
    inv.append(["儿童外套 Kids Jacket", "KJ-002", "1,234", "3.20", "", "CNY"])  # total_price досчитается
    inv.append(["塑料纽扣 Plastic Button", "PB-003", 5000, "0.08", "¥400", ""])  # валюта из цены

    pack = wb.create_sheet("Packing List")
    pack.append(["Артикул", "Вес брутто (кг)"])
    pack.append(["BR-001", "12,5"])
    pack.append(["KJ-002", "45.8"])
    pack.append(["PB-003", "8"])

    wb.save(os.path.join(HERE, "sample_invoice.xlsx"))


def build_scan_pdf():
    import fitz
    doc = fitz.open()
    p = doc.new_page()  # страница-скан: без извлекаемого текста (имитация скана)
    # немного графики, чтобы страница не была полностью пустой — на распознавание
    # текста (get_text()) это не влияет, read_pdf всё равно отправит её в vision
    p.draw_rect(fitz.Rect(50, 50, 200, 150))
    doc.save(os.path.join(HERE, "sample_invoice_scan.pdf"))


if __name__ == "__main__":
    build_xlsx()
    build_scan_pdf()
    print("Эталонные файлы сохранены в", HERE)
