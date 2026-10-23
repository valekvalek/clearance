"""Классификатор v2: вычисляемая уверенность, обратная связь, конвейер с подменой LLM."""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import gemini
import tnved_classifier as tc
from tnved import confidence as C
from tnved import feedback as F
from tnved import headings as H
from tnved import v2
from test_tnved_classifier import _fixture

CFG = C.load_config()
CFG3 = {**CFG, "votes": {**CFG["votes"], "runs": 3}}  # голосование выключено в конфиге, но код проверяем


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

    monkeypatch.setattr(gemini, "chat", fake_chat)  # не ai_client: см. __getattr__ в ai_client.py
    monkeypatch.setattr(gemini, "API_KEY", "test")
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

    monkeypatch.setattr(gemini, "chat", fake_chat)  # не ai_client: см. __getattr__ в ai_client.py
    monkeypatch.setattr(gemini, "API_KEY", "test")
    res = tc.classify_batch([{"product_name": "светодиодные светильники"}], pipeline="v2",
                            use_synonyms=False, use_cache=False)[0]
    assert res["code"] == "" and res["needs_review"]


def test_vote_majority_agreement_and_tiebreak(monkeypatch):
    answers = iter([{"heading": "8536", "missing_attributes": ["a"]},
                    {"heading": "8504", "missing_attributes": ["b"]},
                    {"heading": "8536", "missing_attributes": []}])
    monkeypatch.setattr(v2, "_chat_json", lambda *a, **k: next(answers))
    r = v2._vote("s", "u", "heading", {"8536", "8504"}, {**CFG3, "votes": {**CFG3["votes"], "decision": "majority"}})
    assert r["answer"] == "8536" and abs(r["share"] - 2 / 3) < 1e-9
    assert r["missing"] == ["a"], "недостающие — из одного прогона за выбранный ответ"
    assert v2._agreement(1.0, 1.0, 3) == 1.0
    assert v2._agreement(1 / 3, 1 / 3, 3) == 0.0
    assert v2._agreement(1.0, 1.0, 1) is None


def test_vote_ignores_answers_outside_valid_and_survives_failures(monkeypatch):
    answers = iter([None, {"heading": "9999"}, {"heading": "8536"}])
    monkeypatch.setattr(v2, "_chat_json", lambda *a, **k: next(answers))
    r = v2._vote("s", "u", "heading", {"8536"}, CFG3)
    assert r["counts"] == {"": 1, "8536": 1} and r["answer"] == ""  # ничья -> ранний ответ («не определено»)
    monkeypatch.setattr(v2, "_chat_json", lambda *a, **k: None)
    assert v2._vote("s", "u", "heading", {"8536"}, CFG3) is None


def test_feedback_exact_by_original_name_and_article(monkeypatch):
    path = _fixture(monkeypatch)
    conn = sqlite3.connect(path)
    F.record(conn, "Насос погружной", "8413702100", article="AB-1234", name_original="Water pump DC24")
    ex = F.exact_any(conn, [tc.normalize("Другой перевод"), tc.normalize("Water pump DC24")], "")
    assert ex and ex[0] == "8413702100", "находится по исходному названию"
    assert F.exact_any(conn, [tc.normalize("Неизвестно")], tc.normalize("ab-1234"))[0] == "8413702100"
    assert F.exact_any(conn, [tc.normalize("Неизвестно")], tc.normalize("БА")) is None, "неинформативный артикул"
    F.record(conn, "Другой товар", "9999999999", article="AB-1234")
    assert F.exact_any(conn, [], tc.normalize("AB-1234")) is None, "артикул с двумя кодами не используется"


def test_similar_uses_all_names_and_brand_bonus(monkeypatch):
    path = _fixture(monkeypatch)
    conn = sqlite3.connect(path)
    F.record(conn, "Светильник фонтанный подводный", "9405420039", brand="Rexin")
    F.record(conn, "Светильник настольный", "9405210013", brand="Other")
    sim = F.similar(conn, ["Перевод без совпадений", "Подводный светильник"], 2, brand="Rexin")
    assert sim[0]["code"] == "9405420039"


def test_vote_anchor_decides_others_only_affect_share(monkeypatch):
    answers = iter([{"heading": "8504"}, {"heading": "8536"}, {"heading": "8536"}])
    monkeypatch.setattr(v2, "_chat_json", lambda *a, **k: next(answers))
    r = v2._vote("s", "u", "heading", {"8536", "8504"}, {**CFG3, "votes": {**CFG3["votes"], "decision": "anchor"}})
    assert r["answer"] == "8504" and abs(r["share"] - 1 / 3) < 1e-9


def test_single_run_makes_one_call(monkeypatch):
    calls = []
    monkeypatch.setattr(v2, "_chat_json", lambda *a, **k: calls.append(1) or {"heading": "8536"})
    r = v2._vote("s", "u", "heading", {"8536"}, CFG)
    assert CFG["votes"]["runs"] == 1 and len(calls) == 1 and r["answer"] == "8536"


def test_duplicate_missing_names_do_not_force_refusal(monkeypatch):
    """Один пробел, названный на трёх шагах по-разному, считается один раз (было: отказ)."""
    path = _fixture(monkeypatch)
    H.build(path)

    def fake_chat(messages, **kw):
        sp = messages[0]["content"]
        if "помогаешь таможенному брокеру" in sp:
            return json.dumps([{"id": 0, "what": "светильник", "missing_attributes": ["назначение", "материал"], "heading_hints": ["9405"]}])
        if "Товарная позиция уже выбрана" in sp:
            return json.dumps({"code": "9405420012", "rule_ref": "ОПИ 1", "reason": "x",
                               "missing_attributes": ["Материал изготовления", "точное назначение"]})
        return json.dumps({"heading": "9405", "rule_ref": "ОПИ 1", "reason": "x", "missing_attributes": ["Область применения"]})

    monkeypatch.setattr(gemini, "chat", fake_chat)
    monkeypatch.setattr(gemini, "API_KEY", "test")
    res = tc.classify_batch([{"product_name": "светодиодные светильники"}], pipeline="v2",
                            use_synonyms=False, use_cache=False)[0]
    assert res["missing_attributes"] == ["назначение", "материал"]
    assert res["signals"]["missing"] == 2
    assert res["code"] == "9405420012", res["reason"]


def test_heading_search_maps_household_words_to_tnved_terms():
    from tnved import headings as H
    terms = H._query_terms("Катушка припоя, разъём IP68")
    assert "пайк" in terms and "соединител" in terms
    assert "пайк" not in H._query_terms("Контроллер для светодиодов")
    assert "схем" in H._query_terms("Печатная плата в сборе") and "схем" in H._query_terms("PCB")
    assert "схем" not in H._query_terms("Платье женское")


def test_weak_precedents_are_dropped():
    prec = [{"name": "Разъем соединительный подводный", "code": "8536699008", "n": 6, "sim": 0.12},
            {"name": "разъемы 5-пиновые (папа)", "code": "8536699008", "n": 1, "sim": 0.5}]
    lim = C.load_config()["limits"]
    assert [p["sim"] for p in v2._relevant_precedents(prec, lim)] == [0.5]
