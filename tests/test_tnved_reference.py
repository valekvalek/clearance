"""Эталонная база (tnved/reference.py): поиск похожих товаров по названию, описанию и материалу
ДО обращения к модели; повторы строк базы не убираются и работают как голоса."""
import os
import sqlite3

import gemini
import tnved_classifier as tc
from tnved import reference as R
from test_tnved_classifier import _build_fixture_db, _fixture

ROWS = (
    [("Светодиодный уличный светильник ISL-SL07 50W", "алюминий", "уличное освещение", "9405420039")] * 5
    + [("Светодиодный прожектор XD-30-0106", "алюминий", "угол луча 20", "9405420019")]
    + [("Круглый светильник LD IST70", "пластик", "", "9405420039")] * 3
    + [("Паяльная паста Sn63Pb37, 500 г", "олово", "", "3810100000")] * 2
)


def test_tokens_ignore_homoglyphs_and_digit_letter_splits():
    assert R.tokens("Контроллер К-8000С") == R.tokens("Контроллер K-8000C")
    assert R.tokens("паста 500г") == R.tokens("паста 500 г")
    assert R.tokens("Ссылка на описание") == frozenset()


def test_lookup_votes_by_similarity_and_keeps_duplicates():
    idx = R.Index(ROWS)
    assert sum(e["n"] for e in idx.entries) == len(ROWS), "повторы не теряются, объединяются со счётчиком"
    r = idx.lookup(["Светодиодный уличный светильник ISL-SL07 50W"], "", "алюминий")
    assert r["code"] == "9405420039" and r["level"] == "high" and r["support"] >= 5
    weak = idx.lookup(["Станок токарный"], "", "")
    assert weak["level"] == "none"


def test_exclude_name_keys_simulates_new_product():
    idx = R.Index(ROWS)
    name = "Паяльная паста Sn63Pb37, 500 г"
    r = idx.lookup([name], "", "олово", exclude_name_keys={tc.normalize(name)})
    assert r["code"] != "3810100000" or r["level"] in ("low", "none")


def test_classify_batch_uses_reference_before_llm(monkeypatch):
    path = _fixture(monkeypatch)
    conn = sqlite3.connect(path)
    conn.executescript(R._SCHEMA)
    conn.executemany("INSERT INTO tnved_reference(name, material, description, brand, code) VALUES (?,?,?,?,?)",
                     [(n, m, d, "", c) for n, m, d, c in
                      [("Светодиодные светильники уличные ISL-SL07", "алюминий", "", "9405420012")] * 4])
    conn.commit()
    conn.close()
    R._INDEX_CACHE.clear()

    def _fail_if_called(*a, **kw):
        raise AssertionError("модель не должна вызываться: код есть в эталонной базе")
    monkeypatch.setattr(gemini, "chat", _fail_if_called)
    out = tc.classify_batch([{"product_name": "Светодиодные светильники уличные ISL-SL07",
                              "material": "алюминий", "description": None}], use_cache=False)[0]
    assert out["code"] == "9405420012" and out["confidence"] == "high" and not out["needs_review"]
    assert "эталонные" in out["reason"] and out["reference"]["support"] == 4
    R._INDEX_CACHE.clear()


def test_reference_training_export_keeps_duplicates_drops_conflicts(tmp_path):
    from tnved import make_training as mt
    p = tmp_path / "ref.csv"
    p.write_text(
        '"Торговая марка","Наименование","Материал","Описание","Спец. пошлина %","Код ТНВЭД"\n'
        '"нет","Паста","олово","Ссылка на описание","","3810100000"\n'
        '"нет","Паста","олово","Ссылка на описание","","3810100000"\n'
        '"нет","Поводок","","","","4201000000"\n'
        '"нет","Поводок","","","","8536699008"\n'
        '"нет","Без кода","","","",""\n', encoding="utf-8")
    ex = mt.build_reference_examples(str(p))
    assert len(ex) == 2, "повторы сохранены, противоречие и строка без кода исключены"
    assert ex[0]["user"] == "Название: Паста\nМатериал: олово", "заглушки не попадают в текст"
    assert ex[0]["response"] == '{"heading": "3810", "code": "3810100000"}'
