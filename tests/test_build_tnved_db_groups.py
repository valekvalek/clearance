"""Разбор TNVED2 (группы) в build_tnved_db: формат раздел|группа|наименование|примечания|с|по|."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import build_tnved_db as b

_LINES = [
    "036|20220811|0000|",
    "01|01|ЖИВЫЕ ЖИВОТНЫЕ|1. примечание|01.01.2002|31.12.2006|",
    "01|01|ЖИВЫЕ ЖИВОТНЫЕ|1. примечание новое|23.08.2012||",
    "20|94|МЕБЕЛЬ; ЛАМПЫ|1. примечание|01.01.2012||",
]


def test_groups_keyed_by_group_not_section():
    groups = {code[2:]: v[0] for code, v in b._parse_level(_LINES, 2).items()}
    assert groups == {"01": "ЖИВЫЕ ЖИВОТНЫЕ", "94": "МЕБЕЛЬ; ЛАМПЫ"}
