"""
JS/XSS-проверки интерфейса (бывший «хвост» tests/run_all.sh): синтаксис единого
<script>-блока index.html, отсутствие опасной разметки после XSS-инъекции в данные,
работоспособность кнопок админки. Требуют node — если его нет, тесты пропускаются
(как и раньше пропускались в run_all.sh).
"""
import os
import re
import shutil
import subprocess
import sys

import pytest

TESTS_DIR = os.path.dirname(__file__)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node не найден")


def _index_html(app_dir):
    return os.path.join(app_dir, "static", "index.html")


def _script_block(app_dir):
    html = open(_index_html(app_dir), encoding="utf-8").read()
    m = re.search(r"<script>(.*)</script>", html, re.S)
    assert m, "в index.html не найден единый <script>-блок"
    return m.group(1)


def test_js_syntax(app_dir, tmp_path):
    js_file = tmp_path / "script.js"
    js_file.write_text(_script_block(app_dir), encoding="utf-8")
    r = subprocess.run(["node", "--check", str(js_file)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_no_dangerous_markup_after_xss(app_dir, tmp_path):
    out_html = tmp_path / "rendered.html"
    r = subprocess.run(
        ["node", "xss_18.js", _index_html(app_dir), str(out_html)],
        capture_output=True, text=True, cwd=TESTS_DIR,
    )
    assert r.returncode == 0, r.stderr
    check = subprocess.run(
        [sys.executable, "xss_18_check.py", str(out_html)],
        capture_output=True, text=True, cwd=TESTS_DIR,
    )
    assert "ВСЁ ОК" in check.stdout, check.stdout + check.stderr


def test_admin_buttons_wired(app_dir):
    r = subprocess.run(
        ["node", "click_18.js", _index_html(app_dir)],
        capture_output=True, text=True, cwd=TESTS_DIR,
    )
    assert r.stdout.strip().startswith("✓"), r.stdout + r.stderr
