"""Локальный сервер с ИИ-заглушкой: кусок R45–R89 не распознаётся, батч с item150 не переводится."""
import json, os, re, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, sys.argv[1]); os.chdir(sys.argv[1])
# ai_client.py (см. app/ai_client.py) переадресует chat/API_KEY на gemini.py
# только если AI_PROVIDER=gemini — иначе заглушка ниже (gemini.chat = fake_chat)
# тихо не применится, и сервер попробует стучаться в настоящий Yandex API.
os.environ.setdefault("AI_PROVIDER", "gemini")
import currency, gemini

def fake_chat(messages, **kw):
    text = messages[-1]["content"]
    if "Разбери инвойс" in text:
        body = text.split("СТРОКИ:\n", 1)[1] if "СТРОКИ:\n" in text else text
        if "R50:" in body:
            raise RuntimeError("HTTP 502: Bad Gateway")
        return json.dumps([{"name_original": m.group(2), "quantity": m.group(3), "unit_price": 1.5,
                            "total_price": 1.5 * int(m.group(3)), "currency": "USD", "row": int(m.group(1))}
                           for m in re.finditer(r"^R(\d+): A=(item\d+) \| B=(\d+)", body, re.M)])
    names = re.findall(r"наименование: (\S+)", text)
    if "item150" in names:
        return "это не JSON"
    return json.dumps([{"name_ru": "Товар " + n, "name_zh": "商品", "material": "сталь", "brand": "OEM"} for n in names])

gemini.chat = fake_chat
gemini.API_KEY = "test"
currency.get_rates = lambda: {"usd_per": {"USD": 1.0, "CNY": 1 / 7}, "date": "01.01.2026", "source": "ЦБ РФ", "stale": False}
import uvicorn
uvicorn.run("app:app", host="127.0.0.1", port=8765, log_level="warning")
