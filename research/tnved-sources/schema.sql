-- Схема для загрузки tnved_codes.csv (ТН ВЭД ЕАЭС, действующая редакция).
-- Загрузка без ручной правки:
--   sqlite3 tnved_test.db < schema.sql
--   sqlite3 tnved_test.db ".mode csv" ".import --skip 1 tnved_codes.csv tnved_codes_import" \
--     "INSERT INTO tnved_codes SELECT code, CAST(level AS INTEGER), NULLIF(parent_code,''), name_short, name_full, notes, explanatory_notes, unit, edition_date, source_id, pending_change FROM tnved_codes_import;" \
--     "DROP TABLE tnved_codes_import;"
-- Колонки импорта задаются таблицей tnved_codes_import (все TEXT).
CREATE TABLE IF NOT EXISTS tnved_codes_import (
    code TEXT, level TEXT, parent_code TEXT, name_short TEXT, name_full TEXT,
    notes TEXT, explanatory_notes TEXT, unit TEXT, edition_date TEXT, source_id TEXT, pending_change TEXT
);
CREATE TABLE IF NOT EXISTS tnved_codes (
    code TEXT PRIMARY KEY,                         -- 2/4/6/8/9/10 знаков, как напечатано в номенклатуре
    level INTEGER NOT NULL CHECK (level IN (2,4,6,8,9,10)),
    parent_code TEXT REFERENCES tnved_codes(code), -- ближайший вышестоящий узел, у которого есть код
    name_short TEXT NOT NULL,                      -- собственное наименование узла
    name_full TEXT NOT NULL,                       -- наименование позиции — уровни через « — » — наименование узла
    notes TEXT NOT NULL DEFAULT '',                -- примечания к группе (level=2), у ФНС обрезаны до 4000 символов, здесь полные
    explanatory_notes TEXT NOT NULL DEFAULT '',    -- пояснения к ТН ВЭД (в этом наборе не заполнены, см. REPORT.md)
    unit TEXT NOT NULL DEFAULT '',                 -- доп. единица измерения, пусто = нет
    edition_date TEXT NOT NULL DEFAULT '',         -- ГГГГ-ММ-ДД, дата из имени файла группы; пусто = в имени даты нет
    source_id TEXT NOT NULL,                       -- id из sources.csv
    pending_change TEXT NOT NULL DEFAULT ''        -- изменение, вступающее в силу позже 2026-10-23 (пусто = не выявлено)
);
CREATE INDEX IF NOT EXISTS idx_tnved_codes_parent ON tnved_codes(parent_code);
CREATE INDEX IF NOT EXISTS idx_tnved_codes_level ON tnved_codes(level);
