"""
Проверка загружаемых файлов: только Excel (xlsx/xlsm/xltx/xltm/xls) и PDF.
Лимиты числа/размера файлов, сверка содержимого с расширением,
защита от zip-бомб и слишком длинных PDF. Лимиты настраиваются через env.
"""
import io
import os
import zipfile

MAX_FILES = int(os.getenv("MAX_UPLOAD_FILES", "5"))
MAX_FILE_MB = int(os.getenv("MAX_UPLOAD_MB", "40"))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
MAX_UNZIPPED_MB = int(os.getenv("MAX_UNZIPPED_MB", "300"))
MAX_ZIP_ENTRIES = 10000
MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "50"))
# весь запрос: все файлы + запас на поля формы
MAX_REQUEST_BYTES = MAX_FILES * MAX_FILE_BYTES + 1024 * 1024

EXCEL_ZIP_EXT = (".xlsx", ".xlsm", ".xltx", ".xltm")
EXCEL_EXT = EXCEL_ZIP_EXT + (".xls",)
PDF_EXT = (".pdf",)
ALLOWED_EXT = EXCEL_EXT + PDF_EXT
ALLOWED_HINT = ".xlsx / .xlsm / .xltx / .xltm / .xls / .pdf"

_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP = b"PK\x03\x04"


class UploadError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def oversized_request(content_length):
    """Текст ошибки, если заявленный размер запроса больше лимита, иначе None."""
    try:
        size = int(content_length or 0)
    except ValueError:
        return None
    if size > MAX_REQUEST_BYTES:
        return f"Слишком большой запрос: не больше {MAX_FILES} файлов по {MAX_FILE_MB} МБ"
    return None


def check_extension(name: str):
    if not (name or "").lower().endswith(ALLOWED_EXT):
        raise UploadError(400, f"Файл «{name}»: допустимы только {ALLOWED_HINT}")


def _check_excel_zip(name: str, data: bytes):
    """xlsx — это zip; пускаем только настоящую книгу Excel, не любой архив."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except zipfile.BadZipFile:
        raise UploadError(400, f"Файл «{name}» повреждён или это не Excel")
    names = {i.filename for i in infos}
    if "[Content_Types].xml" not in names or not any(n.startswith("xl/") for n in names):
        raise UploadError(400, f"Файл «{name}» — не книга Excel (архивы не принимаются)")
    if len(infos) > MAX_ZIP_ENTRIES:
        raise UploadError(400, f"Файл «{name}»: слишком сложная структура книги")
    if sum(i.file_size for i in infos) > MAX_UNZIPPED_MB * 1024 * 1024:
        raise UploadError(413, f"Файл «{name}»: после распаковки больше "
                               f"{MAX_UNZIPPED_MB} МБ — разбейте его на части")


def _check_excel_ole(name: str, data: bytes):
    """Старый .xls (OLE2). Тот же контейнер у .doc/.ppt/.msg — ищем поток книги."""
    try:
        import olefile
        ole = olefile.OleFileIO(io.BytesIO(data))
        ok = ole.exists("Workbook") or ole.exists("Book")
        ole.close()
    except Exception:
        raise UploadError(400, f"Файл «{name}» повреждён или это не Excel")
    if not ok:
        raise UploadError(400, f"Файл «{name}» — не книга Excel")


def _check_pdf(name: str, data: bytes) -> bytes:
    start = data.find(b"%PDF-", 0, 1024)
    if start < 0:
        raise UploadError(400, f"Файл «{name}» — не PDF")
    data = data[start:]  # мусор до заголовка мешает определению формата дальше
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        raise UploadError(400, f"Файл «{name}»: PDF повреждён и не открывается")
    try:
        if doc.needs_pass:
            raise UploadError(400, f"Файл «{name}»: PDF защищён паролем")
        if doc.page_count > MAX_PDF_PAGES:
            raise UploadError(400, f"Файл «{name}»: {doc.page_count} стр., "
                                   f"максимум {MAX_PDF_PAGES}")
    finally:
        doc.close()
    return data


def validate(name: str, data: bytes) -> bytes:
    """Проверяет файл; возвращает (возможно нормализованные) байты или бросает UploadError."""
    if not data:
        raise UploadError(400, f"Файл «{name}» пустой")
    if len(data) > MAX_FILE_BYTES:
        raise UploadError(413, f"Файл «{name}» больше {MAX_FILE_MB} МБ")
    low = (name or "").lower()
    if low.endswith(PDF_EXT):
        return _check_pdf(name, data)
    # Excel: расширения xls/xlsx часто перепутаны — судим по содержимому
    if data.startswith(_ZIP):
        _check_excel_zip(name, data)
        return data
    if data.startswith(_OLE2):
        _check_excel_ole(name, data)
        return data
    raise UploadError(400, f"Файл «{name}» — не Excel (содержимое не совпадает с расширением)")
