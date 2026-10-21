"""
Обёртка pytest над самостоятельными тестовыми скриптами (шаги 1.4–2.10).

Каждый test_XX.py — отдельный скрипт, рассчитанный на запуск СВОИМ процессом
(`python test_XX.py путь/к/app`): он сам выставляет DATA_DIR/ADMIN_* и импортирует
app-модули в чистом состоянии. Это осознанно сохранено как есть (см. docstring
conftest.py) — переписывать на обычные pytest-функции в одном процессе означало бы
сначала переделывать auth.py/jobs.py под внешнюю инъекцию состояния, а это отдельная,
более рискованная задача, не входящая в «перевести тесты на pytest».

Эта обёртка просто даёт тот же набор проверок, что раньше запускал tests/run_all.sh,
единой командой `pytest`: отдельный узел на каждый скрипт, с именем и текстом ошибки
при провале, совместимость с `pytest -k`, `--lf` и т.п.
"""
import os
import subprocess
import sys

import pytest

from conftest import LEGACY_SCRIPTS as SCRIPTS

# Переменные, которые conftest.py выставляет для pytest-собственных тестов
# (test_golden.py). Каждый скрипт сам выставляет себе DATA_DIR/ADMIN_* заново —
# но если они уже унаследованы от родителя, `os.environ.setdefault(...)` внутри
# скрипта их не перебьёт, и скрипт потеряет свою изоляцию. Поэтому запускаем
# подпроцесс с окружением, из которого эти переменные убраны — ровно как было
# при запуске скрипта напрямую, без родительского pytest-процесса над ним.
_STRIP_ENV = ("DATA_DIR", "ADMIN_USER", "ADMIN_PASSWORD", "ADMIN_RESET_TOKEN")


@pytest.mark.parametrize("script", SCRIPTS)
def test_legacy_script(script, app_dir):
    path = os.path.join(os.path.dirname(__file__), f"{script}.py")
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    r = subprocess.run(
        [sys.executable, path, app_dir],
        capture_output=True, text=True, timeout=180,
        cwd=os.path.dirname(__file__), env=env,
    )
    out = r.stdout + r.stderr
    assert "Traceback" not in out, out
    assert "✗" not in out, out
    if "ИТОГ:" in out:
        assert "ВСЁ ОК" in out, out
