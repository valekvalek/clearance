"""
Выбор провайдера ИИ через env AI_PROVIDER ("yandex" — основной, по умолчанию;
"gemini"/"kimi" — альтернативы). Управляет извлечением/обогащением
(extract.py/enrich.py) — т.е. парсингом инвойсов, переводом, описанием,
поиском материала/фото. Остальной код обращается только сюда (ai_client.chat,
ai_client.API_KEY...), не импортируя yandex.py/gemini.py/kimi.py напрямую —
чтобы переключение провайдера было одной переменной окружения, а не правкой
нескольких файлов.

Классификация ТН ВЭД (tnved_classifier.py/tnved/v2.py) через ЭТОТ модуль НЕ
идёт — у неё отдельный диспетчер classify_ai.py, который по умолчанию всегда
на Yandex, независимо от AI_PROVIDER здесь (решение 2026-10-23: даже если
обогащение переключено на Kimi, подбор кода ТН ВЭД остаётся на откалиброванном
Yandex). См. classify_ai.py.

ВАЖНО: атрибуты (chat, chat_vision, search, extract_json, API_KEY, MODEL)
переадресуются на модуль-провайдер через __getattr__ (PEP 562), а НЕ
привязываются к функциям/значениям один раз при импорте. Это нужно тестам
(tests/test_2*.py и др.), которые после импорта подменяют gemini.chat/
gemini.API_KEY напрямую (`gemini.chat = fake_chat`) — с привязкой "один раз
при импорте" такая подмена молча не действовала бы, т.к. ai_client держал бы
старую ссылку на функцию."""
import os

_PROVIDER = os.getenv("AI_PROVIDER", "yandex").strip().lower()

if _PROVIDER == "gemini":
    import gemini as _impl
elif _PROVIDER == "kimi":
    import kimi as _impl
else:
    import yandex as _impl

PROVIDER = _PROVIDER


def __getattr__(name):
    return getattr(_impl, name)
