"""
Тесты tnved_classifier.py на изолированной тестовой БД (НЕ app/tnved.db) —
обычный pytest-тест в процессе, изоляция DATA_DIR не нужна (classify_batch
не трогает DATA_DIR/auth), см. docstring conftest.py. AI_PROVIDER=gemini
выставлен в conftest.py, поэтому monkeypatch.setattr(gemini, "chat", ...)
подхватывается ai_client.chat/tnved_classifier прозрачно (PEP 562 __getattr__
в ai_client.py).
"""
import json
import os
import sqlite3
import tempfile

import gemini
import tnved_classifier as tc

_FIXTURE_CODES = [
    # code, name, group_code, group_name, heading_code, heading_name, valid_from
    ("6101209000", "куртки (включая лыжные), ветровки, штормовки",
     "61", "ПРЕДМЕТЫ ОДЕЖДЫ", "6101", "ПАЛЬТО, ПОЛУПАЛЬТО, НАКИДКИ, ПЛАЩИ", "2017-01-01"),
    ("6204611000", "брюки и бриджи из хлопка",
     "62", "ПРЕДМЕТЫ ОДЕЖДЫ ТКАНЫЕ", "6204", "КОСТЮМЫ, КОМПЛЕКТЫ, ЖАКЕТЫ, БРЮКИ", "2017-01-01"),
    ("9405420012", "светодиодные светильники",
     "94", "МЕБЕЛЬ; ОСВЕЩЕНИЕ", "9405", "СВЕТИЛЬНИКИ И ОСВЕТИТЕЛЬНОЕ ОБОРУДОВАНИЕ", "2022-01-01"),
]


def _build_fixture_db(path: str):
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE tnved_codes (
        code TEXT PRIMARY KEY, name TEXT NOT NULL,
        group_code TEXT NOT NULL, group_name TEXT NOT NULL DEFAULT '',
        heading_code TEXT NOT NULL, heading_name TEXT NOT NULL DEFAULT '',
        valid_from TEXT NOT NULL
    );
    CREATE VIRTUAL TABLE tnved_fts USING fts5(
        code UNINDEXED, name, group_name, heading_name
    );
    """)
    conn.executemany(
        "INSERT INTO tnved_codes VALUES (?,?,?,?,?,?,?)", _FIXTURE_CODES)
    conn.executemany(
        "INSERT INTO tnved_fts(code,name,group_name,heading_name) VALUES (?,?,?,?)",
        [(c[0], c[1], c[3], c[5]) for c in _FIXTURE_CODES],
    )
    conn.commit()
    conn.close()


def _fixture(monkeypatch):
    path = os.path.join(tempfile.mkdtemp(prefix="tnved_test_"), "tnved.db")
    _build_fixture_db(path)
    monkeypatch.setattr(tc, "DB_PATH", path)
    return path


def test_normalize():
    assert tc.normalize("  Куртка,  Мужская!! ") == "куртка мужская"
    assert tc.normalize("") == ""
    assert tc.normalize(None) == ""


def test_fts_candidates_finds_plural_form(monkeypatch):
    """Запрос в ед. числе ("куртка") должен найти запись во мн. числе
    ("куртки") — проверка эвристики усечения окончаний (_stem)."""
    _fixture(monkeypatch)
    conn = tc._connect()
    cands = tc._fts_candidates(conn, "куртка")
    assert any(c["code"] == "6101209000" for c in cands)


def test_fts_candidates_uses_material_hint(monkeypatch):
    """Материал передаётся в текст FTS-поиска вместе с названием (см.
    classify_batch: search_text = f"{name} {material}") — проверяем на
    уровне самого поиска, что добавка материала находит кандидата, которого
    общее название не находит."""
    _fixture(monkeypatch)
    conn = tc._connect()
    assert tc._fts_candidates(conn, "изделие") == []
    cands = tc._fts_candidates(conn, "изделие хлопок")
    assert any(c["code"] == "6204611000" for c in cands)


def test_classify_batch_passes_material_to_search(monkeypatch):
    """classify_batch должен реально использовать item['material'] при
    поиске кандидатов, а не только название товара."""
    _fixture(monkeypatch)

    def _fake_chat(messages, **kw):
        text = messages[-1]["content"]
        assert "Материал: хлопок" in text
        assert "6204611000" in text  # кандидат найден благодаря материалу
        return json.dumps([{"id": 0, "code": "6204611000",
                            "confidence": "high", "reason": "test"}])
    monkeypatch.setattr(gemini, "chat", _fake_chat)
    monkeypatch.setattr(gemini, "API_KEY", "test")

    result = tc.classify_batch([
        {"product_name": "изделие", "description": None, "material": "хлопок"},
    ])[0]
    assert result["code"] == "6204611000"


def test_classify_no_candidates_no_llm_call(monkeypatch):
    """Абсурдный ввод без кандидатов в справочнике — LLM НЕ вызывается
    вообще (угадывать запрещено), needs_review=true, код пуст."""
    _fixture(monkeypatch)

    def _fail_if_called(*a, **kw):
        raise AssertionError("LLM не должна вызываться без кандидатов")
    monkeypatch.setattr(gemini, "chat", _fail_if_called)

    result = tc.classify("asdfgh")
    assert result["code"] == ""
    assert result["needs_review"] is True
    assert result["confidence"] == "low"


def test_classify_picks_valid_candidate(monkeypatch):
    _fixture(monkeypatch)

    def _fake_chat(messages, **kw):
        text = messages[-1]["content"]
        assert "6101209000" in text  # кандидат реально дошёл до промпта
        return json.dumps([{"id": 0, "code": "6101209000",
                            "confidence": "high", "reason": "test"}])
    monkeypatch.setattr(gemini, "chat", _fake_chat)
    monkeypatch.setattr(gemini, "API_KEY", "test")

    result = tc.classify("куртка мужская зимняя")
    assert result["code"] == "6101209000"
    assert result["confidence"] == "high"
    assert result["needs_review"] is False
    assert "куртки" in result["description"]


def test_classify_rejects_code_outside_db(monkeypatch):
    """LLM вернула код, которого нет в справочнике вовсе — код НЕ
    принимается (шаг 3 валидации), needs_review=true."""
    _fixture(monkeypatch)

    def _fake_chat(messages, **kw):
        return json.dumps([{"id": 0, "code": "0000000000",
                            "confidence": "high", "reason": "выдумано"}])
    monkeypatch.setattr(gemini, "chat", _fake_chat)
    monkeypatch.setattr(gemini, "API_KEY", "test")

    result = tc.classify("куртка мужская зимняя")
    assert result["code"] == ""
    assert result["needs_review"] is True


def test_classify_llm_says_not_determined(monkeypatch):
    _fixture(monkeypatch)

    def _fake_chat(messages, **kw):
        return json.dumps([{"id": 0, "code": None, "confidence": "low",
                            "reason": "нет подходящего"}])
    monkeypatch.setattr(gemini, "chat", _fake_chat)
    monkeypatch.setattr(gemini, "API_KEY", "test")

    result = tc.classify("куртка мужская зимняя")
    assert result["code"] == ""
    assert result["needs_review"] is True


def test_classify_batch_caches_and_skips_second_llm_call(monkeypatch):
    """Повторный classify_batch с тем же названием НЕ должен снова вызывать
    LLM — только из кэша."""
    _fixture(monkeypatch)
    calls = {"n": 0}

    def _fake_chat(messages, **kw):
        calls["n"] += 1
        return json.dumps([{"id": 0, "code": "9405420012",
                            "confidence": "high", "reason": "test"}])
    monkeypatch.setattr(gemini, "chat", _fake_chat)
    monkeypatch.setattr(gemini, "API_KEY", "test")

    r1 = tc.classify_batch([{"product_name": "светодиодный светильник", "description": None}])
    assert calls["n"] == 1
    assert r1[0]["code"] == "9405420012"

    r2 = tc.classify_batch([{"product_name": "светодиодный светильник", "description": None}])
    assert calls["n"] == 1, "второй вызов должен был взять результат из кэша"
    assert r2[0]["code"] == "9405420012"


def test_classify_batch_does_not_cache_infra_failure(monkeypatch):
    """Инфраструктурный сбой LLM (исключение) НЕ кэшируется — повторный
    вызов после восстановления должен снова попытаться классифицировать."""
    _fixture(monkeypatch)
    calls = {"n": 0}

    def _flaky_chat(messages, **kw):
        calls["n"] += 1
        if calls["n"] <= 3:  # 3 попытки в _classify_llm_batch — все проваливаем
            raise RuntimeError("временная сетевая ошибка")
        return json.dumps([{"id": 0, "code": "6204611000",
                            "confidence": "medium", "reason": "ok"}])
    monkeypatch.setattr(gemini, "chat", _flaky_chat)
    monkeypatch.setattr(gemini, "API_KEY", "test")

    r1 = tc.classify_batch([{"product_name": "брюки хлопковые", "description": None}])
    assert r1[0]["code"] == ""
    assert r1[0]["needs_review"] is True

    conn = tc._connect()
    cached = conn.execute("SELECT * FROM classify_cache").fetchall()
    assert cached == [], "сбой не должен попадать в кэш"

    r2 = tc.classify_batch([{"product_name": "брюки хлопковые", "description": None}])
    assert r2[0]["code"] == "6204611000"


def test_classify_empty_name():
    result = tc.classify("")
    assert result["code"] == ""
    assert result["needs_review"] is True
