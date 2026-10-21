#!/bin/bash
# Все проверки доработки — теперь через pytest (шаг 3.3). ИИ заменён заглушкой, сеть не нужна.
# Запуск:  PYTHON=/путь/к/venv/bin/python tests/run_all.sh
set -u
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
export PYTHONDONTWRITEBYTECODE=1

"$PY" -m pytest . -q
fail=$?

[ $fail = 0 ] && echo "ИТОГ: ВСЁ ОК" || echo "ИТОГ: ЕСТЬ ОШИБКИ"
exit $fail
