"""Классификатор v2: вычисляемая уверенность, обратная связь, конвейер с подменой LLM."""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import tnved_classifier as tc
from tnved import confidence as C
from tnved import feedback as F
from tnved import headings as H
from tnved import v2
from test_tnved_classifier import _fixture

CFG = C.load_config()


def test_confidence_levels_and_abstain():
    hi = C.compute({"rank": 1, "hint": True, "gap": 1.0, "precedent": True, "missing": 0}, CFG)
    assert hi["level"] == "high" and not hi["abstain"]
    lo = C.compute({"rank": None, "hint": False, "gap": 0.0, "precedent": False, "missing": 0}, CFG)
    assert lo["level"] == "low" and lo["abstain"]
    many = C.compute({"rank": 1, "hint": True, "gap": 1.0, "precedent": True, "missing": 4}, CFG)
    assert many["abstain"], "слишком много недостающих атрибутов -> отказ"


def test_feedback_exact_similar_and_conflict(monkeypatch):
    path = _fixture(monkeypatch)
    conn = sqlite3.connect(path)
    F.record(conn, "Куртка мужская зимняя", "6101000000")
    F.record(conn, "куртка мужская", "6101000000")
    F.record(conn, "Спорный товар", "1111111111")
    F.record(conn, "Спорный товар", "2222222222")
    assert F.exact(conn, tc.normalize("куртка мужская")) == ("6101000000", 1)
    assert F.exact(conn, tc.normalize("Спорный товар")) is None, "два кода -> не короткое замыкание"
    sim = F.similar(conn, "куртка женская зимняя")
    assert sim and sim[0]["code"] == "6101000000"
    assert not F.similar(conn, "куртка мужская", exclude_codes={"6101000000"})


def test_pipeline_with_fake_llm(monkeypatch):
    path = _fixture(monkeypatch)
    H.build(path)
    calls = []

    def fake_chat(messages, **kw):
        calls.append(messages[0]["content"][:20])
        sys_prompt = messages[0]["content"]
        if "помогаешь таможенному брокеру" in sys_prompt:
            return json.dumps([{"id": 0, "what": "светильник", "missing_attributes": [], "heading_hints": ["9405"]}])
        if "Товарная позиция уже выбрана" in sys_prompt:
            return json.dumps({"code": "9405420012", "rule_ref": "ОПИ 1", "reason": "тест", "missing_attributes": []})
        return json.dumps({"heading": "9405", "rule_ref": "ОПИ 1", "reason": "тест", "missing_attributes": []})

    monkeypatch.setattr(tc.ai_client, "chat", fake_chat)
    res = tc.classify_batch([{"product_name": "светодиодные светильники"}], pipeline="v2",
                            use_synonyms=False)[0]
    assert res["code"] == "9405420012" and res["heading"] == "9405"
    assert res["rule_ref"] == "ОПИ 1" and res["confidence"] in ("high", "medium", "low")
    assert res["missing_attributes"] == []
    n = len(calls)
    again = tc.classify_batch([{"product_name": "светодиодные светильники"}], pipeline="v2",
                              use_synonyms=False)[0]
    assert len(calls) == n, "v2 кэшируется отдельно и повторно LLM не вызывает"
    assert again["code"] == "9405420012" and again["rule_ref"] == "ОПИ 1"


def test_pipeline_rejects_code_outside_candidates(monkeypatch):
    path = _fixture(monkeypatch)
    H.build(path)

    def fake_chat(messages, **kw):
        if "помогаешь таможенному брокеру" in messages[0]["content"]:
            return json.dumps([{"id": 0, "what": "светильник", "missing_attributes": [], "heading_hints": []}])
        if "Товарная позиция уже выбрана" in messages[0]["content"]:
            return json.dumps({"code": "0000000000", "rule_ref": "ОПИ 1", "reason": "x", "missing_attributes": []})
        return json.dumps({"heading": "9405", "rule_ref": "ОПИ 1", "reason": "x", "missing_attributes": []})

    monkeypatch.setattr(tc.ai_client, "chat", fake_chat)
    res = tc.classify_batch([{"product_name": "светодиодные светильники"}], pipeline="v2",
                            use_synonyms=False, use_cache=False)[0]
    assert res["code"] == "" and res["needs_review"]
