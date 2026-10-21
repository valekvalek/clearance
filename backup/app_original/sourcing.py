"""
Модуль закупки на китайских площадках.
1) Deep-ссылки (без ключей) — поиск товара на 1688/Taobao/Tmall/Alibaba/Made-in-China.
2) Адаптер Oxylabs (при наличии OXYLABS_USER/OXYLABS_PASS) — реальный fetch страницы
   1688 через их прокси + best-effort разбор цены/MOQ/поставщика.
Всё аккуратно деградирует до deep-ссылок, если ключей нет или разбор не удался.
"""
import os
import re
import json
import base64
import urllib.request
import urllib.error
from urllib.parse import quote

OXY_USER = os.getenv("OXYLABS_USER", "")
OXY_PASS = os.getenv("OXYLABS_PASS", "")
OXY_ENDPOINT = os.getenv("OXYLABS_ENDPOINT", "https://realtime.oxylabs.io/v1/queries")


def _kw(item) -> str:
    """Поисковый запрос: китайское название + артикул."""
    parts = []
    if item.get("name_zh"):
        parts.append(re.sub(r"\s+", " ", item["name_zh"]).strip())
    art = item.get("article") or ""
    # добавляем латинский артикул (хорошо ищется), китайский текст уже есть
    if art and re.search(r"[A-Za-z0-9]", art):
        parts.append(art.split()[0])
    kw = " ".join(parts).strip()
    return kw or item.get("name_ru", "") or art


def deep_links(item) -> dict:
    """Готовые ссылки-поиски на китайских площадках."""
    kw = _kw(item)
    q = quote(kw)
    links = {
        "1688": f"https://s.1688.com/selloffer/offer_search.htm?keywords={q}",
        "Taobao": f"https://s.taobao.com/search?q={q}",
        "Tmall": f"https://list.tmall.com/search_product.htm?q={q}",
        "Alibaba": f"https://www.alibaba.com/trade/search?SearchText={q}",
        "Made-in-China": f"https://www.made-in-china.com/productdirectory.do?word={q}",
    }
    # поиск по фото на 1688 (拍立淘), если есть фото
    if item.get("photo_url"):
        links["1688 (по фото)"] = (
            "https://s.1688.com/youyuan/index.htm?tab=imageSearch&imageAddress="
            + quote(item["photo_url"])
        )
    return links


# --------------------------------------------------------- Oxylabs (опционально)
def oxylabs_available() -> bool:
    return bool(OXY_USER and OXY_PASS)


def _oxy_fetch(url: str, render: bool = False, timeout: int = 60) -> str:
    """Скачивает страницу через Oxylabs Web Scraper API. Возвращает HTML или ''."""
    if not oxylabs_available():
        return ""
    payload = {"source": "universal", "url": url}
    if render:
        payload["render"] = "html"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(OXY_ENDPOINT, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    token = base64.b64encode(f"{OXY_USER}:{OXY_PASS}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        results = d.get("results") or []
        if results:
            return results[0].get("content") or ""
    except Exception:
        return ""
    return ""


_RE_OFFER = re.compile(r"detail\.1688\.com/offer/(\d+)\.html")


def _parse_1688_search(html: str) -> str:
    m = _RE_OFFER.search(html)
    if m:
        return f"https://detail.1688.com/offer/{m.group(1)}.html"
    return ""


def _first(patterns, html):
    for p in patterns:
        m = re.search(p, html)
        if m:
            return m.group(1).strip()
    return ""


def _parse_1688_product(html: str) -> dict:
    price = _first([
        r'"referencePrice"\s*:\s*"?([\d.]+)',
        r'"price"\s*:\s*"?([\d.]+)',
        r'¥\s*([\d.]+)',
    ], html)
    moq = _first([
        r'起订量[^\d]{0,6}(\d+)',
        r'"minOrderQuantity"\s*:\s*"?(\d+)',
        r'最小起订[^\d]{0,6}(\d+)',
    ], html)
    supplier = _first([
        r'"companyName"\s*:\s*"([^"]{2,80})"',
        r'"sellerLoginId"\s*:\s*"([^"]{2,80})"',
        r'公司名称[:：]\s*([^<\n]{2,80})',
    ], html)
    return {"price": price, "moq": moq, "supplier": supplier}


def source_via_oxylabs(item) -> dict:
    """Реальный поиск на 1688 через Oxylabs. Возвращает частично заполненный dict."""
    out = {"china_url": "", "supplier": "", "price_cn": "", "moq": ""}
    if not oxylabs_available():
        return out
    kw = _kw(item)
    if not kw:
        return out
    search_url = f"https://s.1688.com/selloffer/offer_search.htm?keywords={quote(kw)}"
    html = _oxy_fetch(search_url, render=True)
    offer = _parse_1688_search(html)
    if not offer:
        return out
    out["china_url"] = offer
    phtml = _oxy_fetch(offer, render=True)
    if phtml:
        p = _parse_1688_product(phtml)
        if p["price"]:
            out["price_cn"] = f"¥{p['price']}"
        out["moq"] = p["moq"]
        out["supplier"] = p["supplier"]
    return out


def source_item(item) -> dict:
    """Единая точка: deep-ссылки всегда + Oxylabs-данные, если доступны."""
    res = {
        "china_url": "", "supplier": "", "price_cn": "", "moq": "",
        "pay": "",  # QR/оплата — статически недоступно, оставляем пустым
        "links": deep_links(item),
    }
    if oxylabs_available():
        try:
            res.update({k: v for k, v in source_via_oxylabs(item).items() if v})
        except Exception:
            pass
    # если карточку 1688 не нашли — ссылка на товар = deep-поиск 1688
    if not res["china_url"]:
        res["china_url"] = res["links"].get("1688", "")
    return res


def links_text(links: dict) -> str:
    return "\n".join(f"{k}: {u}" for k, u in links.items())
