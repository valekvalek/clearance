"""
«Эталонный» регрессионный тест (шаг 3.3) на зафиксированных синтетических файлах
из tests/fixtures/ (см. fixtures/generate_fixtures.py — реальных образцов инвойсов
под рукой не было). В отличие от остальных тестов, где xlsx/pdf собираются на лету
в каждом прогоне, здесь входные файлы — конкретные зафиксированные байты на диске:
тест фиксирует поведение всего конвейера чтения+извлечения на одном и том же
документе, а не только отдельные функции.

Обычный pytest-тест в процессе (без subprocess) — extract.py/readers.py не трогают
DATA_DIR при импорте, изоляция не нужна (см. docstring conftest.py).
"""
import json
import os

import extract
import readers
import gemini

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _fake_chat(messages, **kw):
    """Эталонный документ маленький — уходит ОДНИМ запросом со всеми листами сразу
    (см. extract_items: total_lines<=70 and total_chars<=9000). Имитируем идеальное
    распознавание+сведение, как должен сделать настоящий ИИ по EXTRACT_SYSTEM, но
    возвращаем числа КАК В ЯЧЕЙКЕ (с запятой/символом валюты) — это специально,
    чтобы тест проверял настоящий разбор чисел в extract._parse_amount, а не только
    саму заглушку."""
    text = messages[-1]["content"]
    assert "BR-001" in text and "KJ-002" in text and "PB-003" in text, text
    assert "ЛИСТ: Invoice" in text and "ЛИСТ: Packing List" in text, text
    items = [
        {"name_original": "婴儿连体衣 Baby Romper", "article": "BR-001", "quantity": 100,
         "unit_price": "1,5", "total_price": "", "currency": "USD",
         "gross_weight": 12.5, "file": 1, "sheet": "Invoice", "row": 2},
        {"name_original": "儿童外套 Kids Jacket", "article": "KJ-002", "quantity": "1,234",
         "unit_price": "3.20", "total_price": "", "currency": "CNY",
         "gross_weight": 45.8, "file": 1, "sheet": "Invoice", "row": 3},
        {"name_original": "塑料纽扣 Plastic Button", "article": "PB-003", "quantity": 5000,
         "unit_price": "0.08", "total_price": "¥400", "currency": "",
         "gross_weight": 8, "file": 1, "sheet": "Invoice", "row": 4},
    ]
    return json.dumps(items)


def _fake_chat_vision(system, user_text, images, **kw):
    assert images, "скан-страница должна дойти до vision"
    return json.dumps([{"name_original": "скан-товар", "quantity": 1, "unit_price": 10,
                        "total_price": 10, "currency": "USD"}])


def test_golden_invoice_xlsx(monkeypatch):
    monkeypatch.setattr(gemini, "chat", _fake_chat)
    monkeypatch.setattr(gemini, "API_KEY", "test")
    with open(os.path.join(FIXTURES, "sample_invoice.xlsx"), "rb") as f:
        data = f.read()

    sheets = readers.read_sheets(data)
    assert {s["name"] for s in sheets} == {"Invoice", "Packing List"}

    items = extract.extract_items(sheets)
    assert len(items) == 3, items

    by_art = {it["article"]: it for it in items}
    # "1,5" — десятичная запятая, не тысячи
    assert by_art["BR-001"]["unit_price"] == 1.5
    # total_price не задан в инвойсе -> досчитан как qty * unit_price
    assert by_art["BR-001"]["total_price"] == 150.0
    # "1,234" — ровно 3 знака после запятой -> разделитель тысяч, не десятичный
    assert by_art["KJ-002"]["quantity"] == 1234
    assert by_art["KJ-002"]["total_price"] == round(1234 * 3.20, 2)
    # валюта не указана отдельно, но есть прямо в цене ("¥400") -> CNY
    assert by_art["PB-003"]["currency"] == "CNY"
    assert by_art["PB-003"]["total_price"] == 400.0


def test_golden_scan_pdf(monkeypatch):
    monkeypatch.setattr(gemini, "chat_vision", _fake_chat_vision)
    monkeypatch.setattr(gemini, "API_KEY", "test")
    with open(os.path.join(FIXTURES, "sample_invoice_scan.pdf"), "rb") as f:
        data = f.read()

    sheets, vision_pages, n_scan = readers._read_pdf(data)
    assert sheets == []            # страница без текста — это скан
    assert n_scan == 1
    assert len(vision_pages) == 1
    assert vision_pages[0][:8] == b"\x89PNG\r\n\x1a\n"

    items = extract.extract_all([data])
    assert items and items[0]["name_original"] == "скан-товар"
