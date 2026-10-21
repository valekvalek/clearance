"""Проверка шага 1.5: лимиты и проверка содержимого загружаемых файлов."""
import io
import os
import sys
import tempfile
import zipfile
import warnings

warnings.filterwarnings("ignore")
d = tempfile.mkdtemp()
os.environ.update(DATA_DIR=d, ADMIN_USER="boss", ADMIN_PASSWORD="bosspass123")
os.environ.pop("ADMIN_RESET_TOKEN", None)
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, sys.argv[1])

import fitz  # noqa: E402
import openpyxl  # noqa: E402
import xlwt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as appmod  # noqa: E402
import uploads  # noqa: E402

c = TestClient(appmod.app, base_url="https://testserver")
assert c.post("/api/login", json={"username": "boss", "password": "bosspass123"}).status_code == 200


def xlsx_bytes():
    wb = openpyxl.Workbook()
    wb.active["A1"] = "товар"
    b = io.BytesIO(); wb.save(b); return b.getvalue()


def xls_bytes():
    wb = xlwt.Workbook(); wb.add_sheet("S").write(0, 0, "товар")
    b = io.BytesIO(); wb.save(b); return b.getvalue()


def pdf_bytes(pages, password=None):
    doc = fitz.open()
    for i in range(pages):
        doc.new_page().insert_text((50, 50), f"page {i}")
    kw = {}
    if password:
        kw = dict(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw=password, user_pw=password)
    return doc.tobytes(**kw)


def zip_bytes(entries):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries:
            z.writestr(name, data)
    return b.getvalue()


def upload(*files):
    return c.post("/api/process", files=[("files", (n, b)) for n, b in files],
                  data={"sourcing": "0", "web": "0"})


ok = True


def check(name, r, code):
    global ok
    good = r.status_code == code
    ok &= good
    det = r.json().get("detail", "") if r.status_code != 200 else "принят"
    print(("✓ " if good else f"✗ (получен {r.status_code}) ") + f"{name} → {code}: {det}")


X, XLS = xlsx_bytes(), xls_bytes()
print("— допустимые файлы —")
check("нормальный .xlsx", upload(("inv.xlsx", X)), 200)
check(".xlsm (книга с макросами)", upload(("inv.xlsm", X)), 200)
check(".xltx (шаблон)", upload(("inv.xltx", X)), 200)
check("нормальный .xls", upload(("inv.xls", XLS)), 200)
check(".xls, который на деле xlsx (перепутано расширение)", upload(("inv.xls", X)), 200)
check("PDF на 3 страницы", upload(("inv.pdf", pdf_bytes(3))), 200)
check("PDF с мусором перед заголовком", upload(("inv.pdf", b"\xef\xbb\xbf\r\n" + pdf_bytes(1))), 200)
check("инвойс + пакинг (xlsx + pdf)", upload(("a.xlsx", X), ("b.pdf", pdf_bytes(2))), 200)
big_ok = zip_bytes([("[Content_Types].xml", "<x/>"), ("xl/workbook.xml", "<x/>"),
                    ("xl/media/img.bin", os.urandom(39 * 1024 * 1024))])
check("xlsx 39 МБ с «фото» (несжимаемые данные)", upload(("photos.xlsx", big_ok)), 200)

print("— отказы —")
check("расширение .zip", upload(("a.zip", zip_bytes([("a.txt", "x")]))), 400)
check("обычный zip, переименованный в .xlsx", upload(("a.xlsx", zip_bytes([("a.txt", "x")]))), 400)
check(".exe, переименованный в .xlsx", upload(("a.xlsx", b"MZ\x90\x00" + os.urandom(5000))), 400)
check("расширение .exe", upload(("a.exe", b"MZ\x90\x00")), 400)
check("картинка, переименованная в .pdf", upload(("a.pdf", b"\x89PNG\r\n\x1a\n" + os.urandom(100))), 400)
check("пустой файл", upload(("a.xlsx", b"")), 400)
check("файл 41 МБ", upload(("a.xlsx", b"PK\x03\x04" + os.urandom(41 * 1024 * 1024))), 413)
bomb = zip_bytes([("[Content_Types].xml", "<x/>"), ("xl/sheet.xml", b"\0" * (400 * 1024 * 1024))])
print(f"   (zip-бомба: {len(bomb)//1024} КБ → 400 МБ)")
check("zip-бомба под видом xlsx", upload(("bomb.xlsx", bomb)), 413)
check("PDF на 51 страницу", upload(("a.pdf", pdf_bytes(51))), 400)
check("PDF с паролем", upload(("a.pdf", pdf_bytes(1, "secret"))), 400)
check("битый PDF", upload(("a.pdf", b"%PDF-1.7\n" + os.urandom(3000))), 400)
check("6 файлов", upload(*[(f"f{i}.xlsx", X) for i in range(6)]), 400)

uploads.MAX_REQUEST_BYTES = 10_000  # проверка отсечки по Content-Length
check("запрос больше лимита (middleware)", upload(("a.xlsx", os.urandom(20_000))), 413)

print("\nИТОГ:", "ВСЁ ОК" if ok else "ЕСТЬ ОШИБКИ")
