"""Фото из исходного Excel привязываются к товарам и в «трудных» раскладках."""
import io

import openpyxl
from openpyxl.drawing.image import Image as XLImage
from PIL import Image as PILImage

import images


def _png(color):
    b = io.BytesIO()
    PILImage.new("RGB", (8, 8), color).save(b, "PNG")
    b.seek(0)
    return b


def _book(sheet_title, anchors, hidden_extra=False):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_title
    for i, cell in enumerate(anchors):
        ws.add_image(XLImage(_png((i * 40, 0, 0))), cell)
    if hidden_extra:
        h = wb.create_sheet("decl")
        h.add_image(XLImage(_png((0, 0, 255))), "A12")
        h.sheet_state = "hidden"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_trailing_space_in_sheet_name():
    data = _book("packing list ", ["B11", "B12"])
    items = [{"src_sheet": "packing list", "src_row": 11}, {"src_sheet": "packing list", "src_row": 12}]
    images._map_excel_images(items, data)
    assert all(it.get("src_image") for it in items)


def test_far_anchors_matched_by_order():
    data = _book("PL", ["B20", "B25"])  # якоря далеко от строк товаров
    items = [{"src_sheet": "PL", "src_row": 11}, {"src_sheet": "PL", "src_row": 12}]
    images._map_excel_images(items, data)
    assert all(it.get("src_image") for it in items)
    assert items[0]["src_image"] != items[1]["src_image"]


def test_unknown_sheet_name_is_not_a_filter():
    data = _book("Лист1", ["B5"])
    items = [{"src_sheet": "Sheet1", "src_row": 5}]
    images._map_excel_images(items, data)
    assert items[0].get("src_image")


def test_hidden_sheet_images_ignored():
    data = _book("PL", [], hidden_extra=True)
    items = [{"src_sheet": "PL", "src_row": 12}]
    images._map_excel_images(items, data)
    assert not items[0].get("src_image")


def test_single_item_call_does_not_guess_by_order():
    data = _book("PL", ["B30"])
    fake = [{"src_sheet": "PL", "src_row": 5}]
    images._map_excel_images(fake, data, by_order=False)
    assert not fake[0].get("src_image")
