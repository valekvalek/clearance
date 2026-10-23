"""
Общая обвязка pytest для tests/.

ВАЖНО про изоляцию: app/auth.py читает DATA_DIR (и другие настройки) ОДИН РАЗ при
первом импорте и держит их в module-level переменных (auth.DATA_DIR и т.п.). Внутри
одного процесса pytest модуль импортируется один раз и это состояние разделяется
между всеми тестами процесса — в отличие от старых тестов-скриптов, каждый из
которых запускался ОТДЕЛЬНЫМ процессом (python test_XX.py путь) и получал чистое
состояние. Поэтому:
  - DATA_DIR/ADMIN_* выставляются здесь ОДИН РАЗ, до того как что-либо в tests/
    успеет импортировать app-модули (conftest.py загружается первым);
  - тесты, которым нужна настоящая изоляция (отдельный DATA_DIR, разные пользователи,
    проверка рестарта и т.п.), запускаются как раньше — отдельным процессом через
    tests/test_legacy_scripts.py (см. его докстринг);
  - новые тесты, которым изоляция не нужна (чистые функции извлечения/экспорта без
    обращения к DATA_DIR), можно писать как обычные pytest-тесты в этом процессе —
    см. tests/test_golden.py.
"""
import os
import sys
import tempfile

APP_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app"))

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="invix_pytest_"))
os.environ.setdefault("ADMIN_USER", "boss")
os.environ.setdefault("ADMIN_PASSWORD", "bosspass123")
os.environ.pop("ADMIN_RESET_TOKEN", None)
# Все тесты (включая легаси-скрипты, см. test_legacy_scripts.py) подменяют
# gemini.chat/gemini.chat_vision/gemini.API_KEY напрямую (`gemini.chat = ...`)
# после импорта — ai_client.py (см. app/ai_client.py) переадресует вызовы на
# модуль-провайдер ДИНАМИЧЕСКИ, поэтому такая подмена сработает только если
# провайдер — gemini. По умолчанию (без этой строки) ai_client выбрал бы yandex,
# и фейки гемини тихо не применялись бы, а тесты пытались бы стучаться в
# настоящий Yandex API. setdefault — у пользователя/CI эта переменная не
# выставлена, так что на прод/реальный запуск это не влияет.
os.environ.setdefault("AI_PROVIDER", "gemini")
# classify_ai.py (см. app/classify_ai.py) — ОТДЕЛЬНЫЙ от ai_client диспетчер
# для классификации ТН ВЭД, по умолчанию всегда yandex независимо от
# AI_PROVIDER. Тестам классификатора (tnved_classifier.py/tnved/v2.py) нужен
# тот же фейковый gemini.chat, что и остальным тестам — иначе эта
# классификация пыталась бы стучаться в настоящий Yandex API.
os.environ.setdefault("CLASSIFY_AI_PROVIDER", "gemini")

if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import pytest  # noqa: E402

# Старые тестовые скрипты (шаги 1.4–2.10) — обычные программы с кодом на верхнем
# уровне модуля, рассчитанные на запуск СВОИМ процессом (см. test_legacy_scripts.py
# и его докстринг). Если дать pytest собрать их как обычные test_*.py-модули, он
# импортирует и тем самым ВЫПОЛНИТ их прямо в процессе pytest — второй раз, в общем
# состоянии (DATA_DIR и т.п.), вперемешку с подпроцессным запуском через обёртку.
# Поэтому явно исключаем их из обычного сбора тестов.
LEGACY_SCRIPTS = [
    "test_14", "test_15", "test_16", "test_17", "test_18", "test_19",
    "test_110", "test_110b", "test_111",
    "test_21", "test_22", "test_23", "test_24", "test_25", "test_26",
    "test_27", "test_28", "test_29", "test_210",
]
collect_ignore = [f"{name}.py" for name in LEGACY_SCRIPTS]


@pytest.fixture(scope="session")
def app_dir():
    """Абсолютный путь к app/ — для подпроцессов старых тестов-скриптов."""
    return APP_DIR


@pytest.fixture(scope="session")
def fixtures_dir():
    return os.path.join(os.path.dirname(__file__), "fixtures")
