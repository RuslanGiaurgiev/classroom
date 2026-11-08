"""Импорт групп из папки, несколько групп одновременно, номер группы в названии файла."""

import unicodedata
from pathlib import Path

import pytest

from teacher.importer import scan_folder
from teacher.names import group_code, looks_like_group_folder, name_from_filename
from tests.test_integration import teacher, ui  # noqa: F401
from tests.test_names import receive


@pytest.mark.parametrize("filename, name, code", [
    ("Ваня6-1(2) проект.pptx", "Ваня", ("6", "1", "2")),
    ("Тихон6-1(2) проект v02, by Guest.Kodu2", "Тихон", ("6", "1", "2")),
    ("Дамир Проект Codu.Kodu2", "Дамир", None),
    ("КИРИЛЛ ПРОЕКТИК.sb3", "Кирилл", None),
    ("Слава6-4.zip", "Слава", ("6", "4", "")),
    ("Masha_robot.sb3", "Маша", None),
    ("Tinkercad.zip", None, None),
    ("Видео 2026-05-02 19-13-50.mp4", "Видео" if False else None, ("19", "13", "")),
])
def test_name_and_code(filename, name, code):
    assert name_from_filename(filename) == name
    if code is not None:
        assert group_code(filename) == code


def test_date_is_not_group_code():
    assert group_code("Edge 2026-05-02") is None


def test_group_folders():
    assert looks_like_group_folder("М6-1(2)") and looks_like_group_folder("M7-1") and looks_like_group_folder("6-4")
    assert not looks_like_group_folder("Tinkercad") and not looks_like_group_folder("Проекты Детей")


def make_tree(root: Path):
    files = ["М6-1/Ваня6-1 проект.pptx", "М6-1/Оля6-1.sb3", "М6-1(2)/Ваня6-1(2) проект.pptx",
             "М6-1(2)/Стёпа6-1(2) проект/src/x.java", "М6-1(2)/~$Ваня6-1(2) проект.pptx", "М6-1(2)/.DS_Store",
             "М7-1/Tinkercad.zip", "М7-1/КИРИЛЛ ПРОЕКТИК.sb3", "М7-1/Кирилл тинкер (3).stl", "Разное/Петя.sb3"]
    for f in files:
        # macOS хранит «ё» в разложенном виде — проверяем и это
        p = root / unicodedata.normalize("NFD", f)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")


def test_scan_folder(tmp_path):
    make_tree(tmp_path)
    got = {g["name"]: [s["name"] for s in g["students"]] for g in scan_folder(tmp_path)}
    got = {unicodedata.normalize("NFC", k): v for k, v in got.items()}
    assert got == {"М6-1": ["Ваня", "Оля"], "М6-1(2)": ["Ваня", "Стёпа"], "М7-1": ["Кирилл"]}


async def test_import_and_two_groups_at_once(teacher, tmp_path):
    make_tree(tmp_path / "src")
    _, scan = await ui(teacher, "POST", "/ui/api/import/scan", json={"path": str(tmp_path / "src")})
    assert len(scan["groups"]) == 3 and not any(g["exists"] for g in scan["groups"])
    status, r = await ui(teacher, "POST", "/ui/api/import/apply", json={"groups": scan["groups"]})
    assert status == 200 and r["groups"] == 3 and r["students"] == 5
    # Повторный импорт ничего не дублирует
    _, r2 = await ui(teacher, "POST", "/ui/api/import/apply", json={"groups": scan["groups"]})
    assert r2 == {"groups": 0, "students": 0, "errors": []}

    _, proj = await ui(teacher, "GET", "/ui/api/projects")
    ids = {unicodedata.normalize("NFC", g["name"]): g["id"] for g in proj["groups"]}
    # Сейчас занимаются две группы
    await ui(teacher, "PUT", "/ui/api/settings", json={"active_groups": [ids["М6-1"], ids["М6-1(2)"]]})

    f1 = await receive(teacher, "1", "Ваня6-1(2) робот.sb3", b"a")    # номер группы решает, какой Ваня
    assert f1["status"] == "sorted" and "/М6-1(2)/Ваня/" in unicodedata.normalize("NFC", f1["project_path"]).replace("\\", "/")
    f2 = await receive(teacher, "2", "Ваня6-1 кот.sb3", b"b")
    assert f2["status"] == "sorted" and "/М6-1/Ваня/" in unicodedata.normalize("NFC", f2["project_path"]).replace("\\", "/")
    f3 = await receive(teacher, "3", "Ваня кот.sb3", b"c")              # без номера, Вани в обеих группах
    assert f3["status"] == "unsorted"
    f4 = await receive(teacher, "4", "Стёпа игра.sb3", b"d")            # Стёпа только в 6-1(2)
    assert f4["status"] == "sorted"
    f5 = await receive(teacher, "5", "Кирилл.sb3", b"e")                # 7-1 сейчас не занимается
    assert f5["status"] == "unsorted"
    f6 = await receive(teacher, "5", "Кирилл7-1.sb3", b"f")             # но номер группы в названии — находим
    assert f6["status"] == "sorted"
