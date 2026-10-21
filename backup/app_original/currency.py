"""Курс RMB (CNY) -> USD. Только stdlib."""
import json
import urllib.request

DEFAULT_USD_TO_RMB = 7.25


def get_rate() -> dict:
    """Возвращает {'usd_to_rmb': float, 'rmb_to_usd': float, 'display': str, 'live': bool}."""
    for url, extractor in (
        ("https://api.exchangerate-api.com/v4/latest/USD",
         lambda d: d["rates"].get("CNY")),
        ("https://www.cbr-xml-daily.ru/daily_json.js",
         lambda d: d["Valute"]["USD"]["Value"] /
                   (d["Valute"]["CNY"]["Value"] / d["Valute"]["CNY"]["Nominal"])),
    ):
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8"))
            cny = extractor(data)
            if cny and cny > 0:
                return {
                    "usd_to_rmb": cny,
                    "rmb_to_usd": 1.0 / cny,
                    "display": f"1 USD = {cny:.2f} RMB",
                    "live": True,
                }
        except Exception:
            continue
    return {
        "usd_to_rmb": DEFAULT_USD_TO_RMB,
        "rmb_to_usd": 1.0 / DEFAULT_USD_TO_RMB,
        "display": f"1 USD = {DEFAULT_USD_TO_RMB:.2f} RMB (по умолчанию)",
        "live": False,
    }
