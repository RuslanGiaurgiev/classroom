"""Ученики, записанные полностью (Фамилия Имя Отчество), и резервные копии. Все имена вымышленные."""

import sqlite3

import pytest

from teacher.backup import make_backup
from teacher.names import match_students, name_variants

GROUP = [
    {"id": 1, "name": "Петров Илья Сергеевич", "aliases": []},
    {"id": 2, "name": "Зайцев Тимофей Олегович", "aliases": []},
    {"id": 3, "name": "Зайцев Тихон Олегович", "aliases": []},
    {"id": 4, "name": "Белова Нина Викторовна", "aliases": []},
    {"id": 5, "name": "Лисина Варвара", "aliases": []},
    {"id": 6, "name": "Карим", "aliases": []},
]


def test_patronymic_not_used():
    assert name_variants("Зайцев Тихон Олегович")[1:] == ["заицев", "тихон"] or \
        name_variants("Зайцев Тихон Олегович")[1:] == ["зайцев", "тихон"]


@pytest.mark.parametrize("filename, ids", [
    ("Илья6-1 проект.sb3", [1]),
    ("Петров кот.sb3", [1]),
    ("Тихон6-2 проект.pptx", [3]),
    ("Тимофей.sb3", [2]),
    ("Зайцев игра.sb3", [2, 3]),          # фамилия общая — в «Неразобранное», не ошибаемся
    ("Нина проект.sb3", [4]),
    ("Варя проект.zip", []),               # «Варя» ≠ «Варвара»: добавьте вариант имени
    ("Варвара проект.zip", [5]),
    ("Карим.sb3", [6]),
    ("Олегович.sb3", []),                  # отчество не используется
])
def test_full_names(filename, ids):
    assert [s["id"] for s in match_students(filename, GROUP)] == ids


def test_backup_and_rotation(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    c = sqlite3.connect(data / "teacher.sqlite3")
    c.execute("CREATE TABLE students(name TEXT)")
    c.execute("INSERT INTO students VALUES ('Маша')")
    c.commit()
    (data / "state.json").write_text("{}")
    root = tmp_path / "backups"
    for i in range(4):
        (root / f"2000-01-0{i + 1}_00-00-00").mkdir(parents=True)
    dest = make_backup(data, root, keep=3)
    assert sqlite3.connect(dest / "teacher.sqlite3").execute("SELECT name FROM students").fetchall() == [("Маша",)]
    assert (dest / "state.json").exists()
    assert len(list(root.iterdir())) == 3 and dest.exists()
