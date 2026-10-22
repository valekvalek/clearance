"""Шрифты интерфейса отдаются с своего домена (CSP запрещает CDN) и только из static/fonts."""
from fastapi.testclient import TestClient

import app as appmod


def test_font_served_and_names_restricted():
    c = TestClient(appmod.app)
    r = c.get("/fonts/IBMPlexSans-var-latin.woff2")
    assert r.status_code == 200 and r.content[:4] == b"wOF2"
    assert r.headers["content-type"] == "font/woff2"
    assert c.get("/fonts/OFL.txt").status_code == 404, "только .woff2"
    assert c.get("/fonts/..%2Fapp.py").status_code == 404
    assert c.get("/fonts/nope.woff2").status_code == 404
