"""
Провайдер ИИ для классификации ТН ВЭД (tnved_classifier.py, tnved/v2.py) —
ОТДЕЛЬНЫЙ от ai_client.py, который обслуживает извлечение/обогащение
(extract.py/enrich.py: парсинг инвойсов, перевод, описание, поиск материала/фото).

Решение 2026-10-23: подбор кода ТН ВЭД остаётся на Yandex AI Studio ВСЕГДА,
даже когда AI_PROVIDER переключён на другого провайдера (например "kimi")
для остальной части пайплайна — классификация откалибрована именно под
Yandex (см. tnved/config.json: calibration), переключать её на неоткалиброванную
модель вместе с обогащением не нужно.

CLASSIFY_AI_PROVIDER — если задан явно, переопределяет провайдера
классификации (используется тестами, см. tests/conftest.py, которые гонят
классификатор через фейковый gemini.chat). На проде обычно не задан — тогда
всегда yandex, независимо от AI_PROVIDER.

Как и в ai_client.py: атрибуты переадресуются на модуль-провайдер через
__getattr__ (PEP 562), не привязываются при импорте — чтобы подмена
gemini.chat в тестах работала."""
import os

_PROVIDER = os.getenv("CLASSIFY_AI_PROVIDER", "yandex").strip().lower()

if _PROVIDER == "gemini":
    import gemini as _impl
elif _PROVIDER == "kimi":
    import kimi as _impl
else:
    import yandex as _impl

PROVIDER = _PROVIDER


def __getattr__(name):
    return getattr(_impl, name)
