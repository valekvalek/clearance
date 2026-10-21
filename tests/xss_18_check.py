import sys
from html.parser import HTMLParser
out = open(sys.argv[1], encoding="utf-8").read()
bad, links, datausers = [], [], []
class P(HTMLParser):
    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if k.startswith("on") and not (tag == "img" and v == "this.style.display='none'"):
                bad.append(f"<{tag} {k}={v!r}>")
            if k in ("href", "src"):
                links.append(v)
                if not (v.startswith(("http://", "https://")) or v.startswith("/api/")):
                    bad.append(f"<{tag} {k}={v!r}> — опасная схема")
            if k == "data-user":
                datausers.append(v)
        if tag == "script":
            bad.append("<script> в разметке")
P().feed(out)
print("ссылки в разметке:", links)
print("имена из data-user (как их увидит JS):", datausers)
print("опасное:", bad or "нет")
print("ИТОГ:", "ВСЁ ОК" if not bad else "ЕСТЬ ОШИБКИ")
