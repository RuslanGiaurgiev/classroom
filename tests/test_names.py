"""Этап 4: нормализация и сопоставление имён, раскладка по папкам, «Неразобранное»."""

import hashlib
from pathlib import Path

import pytest

from teacher.names import match_students, normalize, suggest_alias, translit
from tests.test_integration import teacher, ui  # noqa: F401


@pytest.mark.parametrize("src, expected", [
    ("Masha", "маша"), ("Alexey", "алексей"), ("Artyom", "артем"), ("Zhenya", "женя"), ("Ksyusha", "ксюша"),
    ("Yana", "яна"), ("Mikhail", "михаил"), ("Shchukin", "щукин"), ("Maria", "мария"),
])
def test_translit(src, expected):
    assert translit(src) == expected


def test_normalize():
    assert normalize("Маша_Ёлкина-2 (final).SB3") == "маша елкина финал сб"   # латиница тоже переводится
    assert normalize("___123___") == ""


STUDENTS = [
    {"id": 1, "name": "Маша", "aliases": []},
    {"id": 2, "name": "Миша", "aliases": []},
    {"id": 3, "name": "Оля", "aliases": []},
    {"id": 4, "name": "Александр", "aliases": ["Саша"]},
    {"id": 5, "name": "Софья", "aliases": []},
    {"id": 6, "name": "Маша К", "aliases": []},
]


@pytest.mark.parametrize("filename, ids", [
    ("masha_kot.sb3", [1]),
    ("МАША.sb3", [1]),
    ("Маша и Миша.sb3", [1, 2]),          # двое — в «Неразобранное»
    ("kolya.sb3", []),                    # «Коля» не должен совпасть с «Оля»
    ("Оля-3.png", [3]),
    ("Aleksandr game.sb3", [4]),          # опечатка в длинном имени — нечёткое совпадение
    ("sasha robot.sb3", [4]),             # по варианту имени
    ("sofia.sb3", [5]), ("Sofya1.sb3", [5]),
    ("Маша К робот.sb3", [1, 6]),
    ("проект.sb3", []), ("123.sb3", []),
])
def test_match(filename, ids):
    assert [s["id"] for s in match_students(filename, STUDENTS)] == ids


def test_suggest_alias():
    assert suggest_alias("Mashunya_robot_3.sb3", "Маша") == "машуня"


async def add_group(teacher, name, students_text):
    _, g = await ui(teacher, "POST", "/ui/api/groups", json={"name": name})
    _, r = await ui(teacher, "POST", "/ui/api/students/bulk", json={"group_id": g["id"], "text": students_text})
    return g["id"], r


async def receive(teacher, laptop, name, data):
    """Имитировать полученный и проверенный файл (как после /complete)."""
    root = teacher.projects_root() / "Входящие" / "2026-11-08" / f"Ноут-{laptop}"
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_bytes(data)
    fid = teacher.db.add_received(laptop=laptop, sha256=hashlib.sha256(data).hexdigest(), size=len(data), name=name,
                                  rel_path=name, folder_key="downloads", lesson_date="2026-11-08",
                                  inbox_path=str(root / name))
    await teacher.sorter.sort_new(fid)
    return teacher.db.one("SELECT * FROM received WHERE id=?", (fid,))


async def test_sort_into_student_folder_and_unsorted(teacher):
    gid, r = await add_group(teacher, "Пн 10:00", "Маша, Машенька\nМиша\nОля")
    assert r == {"added": 3, "errors": []}

    f = await receive(teacher, "1", "Masha_kot.sb3", b"1")
    assert f["status"] == "sorted"
    assert f["project_path"].replace("\\", "/").endswith("Проекты/Пн 10.00/Маша/2026-11-08/Masha_kot.sb3")
    assert Path(f["project_path"]).read_bytes() == b"1" and Path(f["inbox_path"]).exists()   # архив на месте

    # Тот же файл с тем же именем от другого ноутбука — суффикс, без перезаписи
    f2 = await receive(teacher, "2", "Masha_kot.sb3", b"2")
    assert f2["project_path"].replace("\\", "/").endswith("Masha_kot (2).sb3")

    u = await receive(teacher, "3", "Маша и Миша.sb3", b"3")
    assert u["status"] == "unsorted" and "/Неразобранное/2026-11-08/" in u["project_path"].replace("\\", "/")
    u2 = await receive(teacher, "3", "Mashunya.sb3", b"4")
    assert u2["status"] == "unsorted"

    # Назначаем одним кликом и запоминаем написание
    masha = [s for s in teacher.db.students() if s["name"] == "Маша"][0]
    status, res = await ui(teacher, "POST", f"/ui/api/files/{u2['id']}/assign", json={"student_id": masha["id"]})
    assert status == 200 and res["suggest_alias"] == "машуня"
    moved = teacher.db.one("SELECT * FROM received WHERE id=?", (u2["id"],))
    assert moved["status"] == "sorted" and "/Маша/" in moved["project_path"].replace("\\", "/") and Path(moved["project_path"]).exists()
    assert not (teacher.projects_root() / "Неразобранное" / "2026-11-08" / "Mashunya.sb3").exists()

    await ui(teacher, "PUT", f"/ui/api/students/{masha['id']}", json={"aliases": masha["aliases"] + ["машуня"]})
    f3 = await receive(teacher, "1", "mashunya_2.sb3", b"5")
    assert f3["status"] == "sorted" and "/Маша/" in f3["project_path"].replace("\\", "/")


async def test_active_group_limits_matching(teacher):
    g1, _ = await add_group(teacher, "Группа А", "Маша")
    g2, _ = await add_group(teacher, "Группа Б", "Маша")
    assert (await receive(teacher, "1", "masha.sb3", b"a"))["status"] == "unsorted"   # две Маши
    await ui(teacher, "PUT", "/ui/api/settings", json={"active_group": g2})
    f = await receive(teacher, "1", "masha2.sb3", b"b")
    assert f["status"] == "sorted" and "/Группа Б/Маша/" in f["project_path"].replace("\\", "/")


async def test_student_validation(teacher):
    gid, r = await add_group(teacher, "Г", "Маша\nМаша\nМа/ша")
    assert r["added"] == 1 and len(r["errors"]) == 2
    status, _ = await ui(teacher, "POST", "/ui/api/groups", json={"name": "Г"})
    assert status == 400
