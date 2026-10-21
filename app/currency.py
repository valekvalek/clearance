"""
Курсы валют ЦБ РФ и пересчёт любых сумм в доллары. Только stdlib.
Источники по порядку: cbr.ru (официальный XML) -> cbr-xml-daily.ru (зеркало JSON)
-> последний удачный курс с диска (с предупреждением). Кеш в памяти на час.
"""
import json
import os
import re
import logging
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET

import auth  # auth.DATA_DIR — постоянный том

logger = logging.getLogger(__name__)

CBR_XML_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
CBR_JSON_URL = "https://www.cbr-xml-daily.ru/daily_json.js"
CACHE_TTL = 3600
RATES_FILE = os.path.join(auth.DATA_DIR, "rates.json")
_LOCK = threading.Lock()
_CACHE = {"rates": None, "at": 0.0}

# разумные границы: если курс вне их — источник считаем сломанным
_SANITY = {"CNY": (5, 10), "EUR": (0.6, 1.2), "RUB": (30, 300)}  # единиц за 1 USD


# ------------------------------------------------------------- загрузка курсов
def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 InvixAI"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read()


def _num(s) -> float:
    return float(str(s).replace(",", ".").replace(" ", ""))


def _from_cbr_xml(raw: bytes):
    root = ET.fromstring(raw)
    rub = {v.findtext("CharCode"): _num(v.findtext("Value")) / _num(v.findtext("Nominal"))
           for v in root.findall("Valute")}
    return rub, root.get("Date", "")


def _from_cbr_json(raw: bytes):
    d = json.loads(raw.decode("utf-8"))
    rub = {code: float(v["Value"]) / float(v["Nominal"]) for code, v in d["Valute"].items()}
    date = d.get("Date", "")[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        y, m, dd = date.split("-")
        date = f"{dd}.{m}.{y}"
    return rub, date


def _build(rub_per_unit: dict, date: str, source: str) -> dict:
    """{код: рублей за 1 единицу} -> {код: долларов за 1 единицу}."""
    usd_rub = rub_per_unit.get("USD")
    if not usd_rub:
        raise ValueError("в ответе ЦБ нет курса USD")
    usd_per = {code: r / usd_rub for code, r in rub_per_unit.items() if r > 0}
    usd_per["RUB"] = 1 / usd_rub
    usd_per["USD"] = 1.0
    for code, (lo, hi) in _SANITY.items():
        per_usd = 1 / usd_per[code] if usd_per.get(code) else None
        if per_usd is None or not lo <= per_usd <= hi:
            raise ValueError(f"неправдоподобный курс {code}: {per_usd}")
    return {"usd_per": usd_per, "date": date, "source": source, "stale": False}


def _save_disk(rates):
    try:
        tmp = RATES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rates, f)
        os.replace(tmp, RATES_FILE)
    except OSError as e:
        logger.warning("не удалось сохранить курсы: %s", e)


def _load_disk():
    try:
        with open(RATES_FILE, encoding="utf-8") as f:
            rates = json.load(f)
        rates["stale"] = True
        return rates
    except (OSError, ValueError):
        return None


def get_rates() -> dict:
    """Курсы ЦБ РФ: {'usd_per': {код: $ за 1 ед.}, 'date', 'source', 'stale', 'error'}.
    usd_per пустой, если курсов нет совсем (ни сети, ни сохранённых)."""
    with _LOCK:
        if _CACHE["rates"] and time.time() - _CACHE["at"] < CACHE_TTL:
            return _CACHE["rates"]
        errors = []
        for url, parse, source in ((CBR_XML_URL, _from_cbr_xml, "ЦБ РФ"),
                                   (CBR_JSON_URL, _from_cbr_json, "ЦБ РФ (cbr-xml-daily.ru)")):
            try:
                rates = _build(*parse(_get(url)), source)
                _CACHE.update(rates=rates, at=time.time())
                _save_disk(rates)
                return rates
            except Exception as e:  # noqa: BLE001
                errors.append(f"{source}: {str(e)[:120]}")
        err = "; ".join(errors)
        logger.warning("курсы ЦБ недоступны: %s", err)
        rates = _load_disk() or {"usd_per": {}, "date": "", "source": "", "stale": True}
        rates["error"] = err
        # повторим запрос не раньше чем через 5 минут, а не на каждой задаче
        _CACHE.update(rates=rates, at=time.time() - CACHE_TTL + 300)
        return rates


# ------------------------------------------------------------- валюты
_ALIASES = {  # слово/символ -> ISO-код (сверяются после явных трёхбуквенных кодов)
    "人民币": "CNY", "元": "CNY", "YUAN": "CNY", "RENMINBI": "CNY", "¥": "CNY", "￥": "CNY",
    "ЮАН": "CNY",
    "US$": "USD", "$": "USD", "美元": "USD", "DOLLAR": "USD", "ДОЛЛАР": "USD",
    "€": "EUR", "EURO": "EUR", "欧元": "EUR", "ЕВРО": "EUR",
    "₽": "RUB", "РУБ": "RUB", "卢布": "RUB", "RUBLE": "RUB", "ROUBLE": "RUB",
    "HK$": "HKD", "港币": "HKD", "港元": "HKD",
    "A$": "AUD", "AU$": "AUD", "C$": "CAD", "CA$": "CAD", "S$": "SGD", "NT$": "TWD",
    "£": "GBP", "英镑": "GBP", "日元": "JPY", "円": "JPY",
}
_CODE_FIX = {"RMB": "CNY", "RUR": "RUB", "USDOLLAR": "USD"}
DEFAULT_CURRENCY = "CNY"  # в инвойсе валюта не указана — китайский поставщик, юани


def normalize_currency(raw):
    """Написание валюты из инвойса -> ISO-код ('CNY', 'USD', 'EUR'…) или None, если непонятно."""
    s = str(raw or "").strip().upper()
    if not s:
        return DEFAULT_CURRENCY
    if s.replace(" ", "") == "RMB/CNY/USD":  # ИИ повторил шаблон — валюта не была указана
        return DEFAULT_CURRENCY
    codes = {_CODE_FIX.get(c, c) for c in re.findall(r"(?<![A-Z])[A-Z]{3}(?![A-Z])", s)}
    codes.discard("PCS")
    if len(codes) == 1:
        return codes.pop()
    if len(codes) > 1:
        return None  # «USD/EUR» — неоднозначно
    # длинные варианты раньше коротких: HK$ раньше $, US$ раньше $
    for alias in sorted(_ALIASES, key=len, reverse=True):
        if alias in s:
            return _ALIASES[alias]
    return None


def to_usd(amount, code, rates):
    """Сумма в валюте code -> доллары или None (валюта неизвестна / нет курса)."""
    if not code:
        return None
    if code == "USD":  # доллары не пересчитываем — курс не нужен
        return float(amount or 0)
    k = (rates or {}).get("usd_per", {}).get(code)
    if k is None:
        return None
    return float(amount or 0) * k


def rate_display(rates, codes) -> str:
    """Строка для интерфейса: 'ЦБ РФ на 25.09.2026: 1 USD = 84.91 RUB · 1 USD = 6.71 CNY'."""
    usd_per = (rates or {}).get("usd_per") or {}
    if not usd_per:
        return "курс ЦБ недоступен"
    parts = []
    for code in sorted(c for c in set(codes) if c and c != "USD" and c in usd_per):
        k = usd_per[code]
        parts.append(f"1 {code} = {k:.4f} USD" if k >= 1 else f"1 USD = {1 / k:.2f} {code}")
    head = f"{rates.get('source') or 'ЦБ РФ'} на {rates.get('date') or '?'}"
    if rates.get("stale"):
        head += " (сохранённый, свежий недоступен)"
    return head + (": " + " · ".join(parts) if parts else "")


def conversion_warnings(items, rates) -> list:
    """Что не удалось перевести в доллары — для блока «Проверьте вручную»."""
    from report import ranges
    usd_per = (rates or {}).get("usd_per") or {}
    unknown, no_rate = {}, {}
    for n, it in enumerate(items, 1):
        code = it.get("currency")
        if not code:
            unknown.setdefault(it.get("currency_raw") or "?", []).append(n)
        elif code != "USD" and code not in usd_per:
            no_rate.setdefault(code, []).append(n)
    out = []
    for raw, nums in unknown.items():
        out.append(f"Товары № {ranges(nums)}: валюта «{raw}» не распознана — суммы в $ "
                   f"не рассчитаны (ячейки пустые)")
    for code, nums in no_rate.items():
        why = "курсы ЦБ недоступны" if not usd_per else f"у ЦБ РФ нет курса {code}"
        out.append(f"Товары № {ranges(nums)}: {why} — суммы в {code} не переведены в $ "
                   f"(ячейки пустые)")
    needs_rate = any(it.get("currency") not in (None, "USD") for it in items)
    if needs_rate and usd_per and rates.get("stale"):
        out.append(f"Свежий курс ЦБ сейчас недоступен — использован сохранённый курс на "
                   f"{rates.get('date') or '?'}")
    return out
