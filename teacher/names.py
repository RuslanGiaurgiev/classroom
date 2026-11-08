"""
Сопоставление имени файла с учениками.

Нормализация: нижний регистр, ё→е, латиница → кириллица (Masha → маша),
удаление цифр и знаков. Сравнение — нечёткое (rapidfuzz), но осторожное:
лучше положить файл в «Неразобранное», чем не тому ученику.

  • короткие варианты имени (до 4 букв: «Оля», «Маша») — только точное совпадение слова,
    иначе «Оля» совпала бы с «Коля»;
  • длинные — похожесть не ниже порога (по умолчанию 85 из 100);
  • вариант из нескольких слов («Маша К») — должны совпасть все слова подряд.
"""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

DEFAULT_THRESHOLD = 85
SHORT_LEN = 4

# Сначала многобуквенные сочетания (порядок важен: shch раньше sh)
_DIGRAPHS = [
    ("shch", "щ"), ("sch", "щ"), ("zh", "ж"), ("kh", "х"), ("ch", "ч"), ("sh", "ш"), ("ts", "ц"),
    ("yu", "ю"), ("ya", "я"), ("yo", "е"), ("ye", "е"), ("ju", "ю"), ("ja", "я"), ("jo", "е"),
    ("ck", "к"), ("ph", "ф"), ("ia", "ия"),
]
_SINGLE = {
    "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "х", "i": "и",
    "j": "й", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "q": "к", "r": "р",
    "s": "с", "t": "т", "u": "у", "v": "в", "w": "в", "x": "кс", "z": "з",
}
_VOWELS = set("aeiouy")


def translit(text: str) -> str:
    """Латиница → кириллица (кириллицу не трогает): masha → маша, alexey → алексей."""
    out = []
    i = 0
    t = text.lower()
    while i < len(t):
        for lat, cyr in _DIGRAPHS:
            if t.startswith(lat, i):
                out.append(cyr)
                i += len(lat)
                break
        else:
            ch = t[i]
            if ch == "y":
                # После гласной — «й» (Andrey), иначе — «ы»
                out.append("й" if i > 0 and t[i - 1] in _VOWELS else "ы")
            else:
                out.append(_SINGLE.get(ch, ch))
            i += 1
    return "".join(out)


def normalize(text: str) -> str:
    """«Маша_Иванова-2 (final).SB3» → «маша иванова финал …» без цифр и знаков."""
    # NFC: в именах файлов macOS «ё»/«й» хранятся как буква + отдельный значок
    text = unicodedata.normalize("NFC", text)
    t = translit(text.lower().replace("ё", "е"))
    t = t.replace("ё", "е")
    t = re.sub(r"[^а-яa-z]+", " ", t)  # всё, кроме букв (цифры, знаки, _) → пробел
    return " ".join(t.split())


def _soft(word: str) -> str:
    """Для сравнения убираем ь/ъ и выравниваем й/ы→и: «дарья»≈«дария», «софья»≈«софия»."""
    word = word.replace("ья", "ия").replace("ье", "ие").replace("ьи", "ии")
    return word.replace("ь", "").replace("ъ", "").replace("й", "и").replace("ы", "и")


def tokens(text: str) -> list[str]:
    return normalize(text).split()


def file_tokens(filename: str) -> list[str]:
    """Слова из имени файла (без расширения)."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return tokens(stem)


def _word_match(variant_word: str, file_word: str, threshold: int) -> bool:
    a, b = _soft(variant_word), _soft(file_word)
    if len(a) <= SHORT_LEN:
        return a == b
    return fuzz.ratio(a, b) >= threshold


def variant_matches(variant: str, words: list[str], threshold: int = DEFAULT_THRESHOLD) -> bool:
    v = tokens(variant)
    if not v or len(v) > len(words):
        return False
    for start in range(len(words) - len(v) + 1):
        if all(_word_match(v[k], words[start + k], threshold) for k in range(len(v))):
            return True
    return False


def match_students(filename: str, students: list[dict], threshold: int = DEFAULT_THRESHOLD) -> list[dict]:
    """
    Все ученики, чьё имя (или вариант написания) есть в имени файла.
    students: [{"id", "name", "aliases": [...]}]. Ровно один → сортируем, иначе — «Неразобранное».
    """
    words = file_tokens(filename)
    if not words:
        return []
    found = []
    for s in students:
        variants = [s["name"], *s.get("aliases", [])]
        if any(variant_matches(v, words, threshold) for v in variants):
            found.append(s)
    return found


def suggest_alias(filename: str, student_name: str) -> str:
    """
    Какое слово из имени файла предложить запомнить как вариант имени ученика
    (самое похожее на имя слово, иначе самое длинное).
    """
    words = file_tokens(filename)
    if not words:
        return ""
    name = _soft(tokens(student_name)[0]) if tokens(student_name) else ""
    return max(words, key=lambda w: (fuzz.ratio(_soft(w), name), len(w)))


# ================= Номер группы в названиях =================
# Группы называются «М6-1(2)», «М6-3», «7-1» и т.п.; дети пишут «Ваня6-1(2) проект».

_CODE_RE = re.compile(r"(?<!\d)(?<!\d[-–—_.])(\d{1,2})\s*[-–—_]\s*(\d{1,2})(?:\s*\(\s*(\d{1,2})\s*\))?(?!\d)")


def group_code(text: str) -> tuple[str, str, str] | None:
    """Код группы из текста: «Ваня6-1(2) проект» → ("6", "1", "2"); «М6-3» → ("6", "3", "")."""
    m = _CODE_RE.search(text or "")
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3) or ""


def looks_like_group_folder(name: str) -> bool:
    """Папка-группа: «М6-1(2)», «M7-1», «6-4», «Группа 6-1»."""
    return bool(re.fullmatch(r"[^\d]{0,12}\s*\d{1,2}\s*[-–—_]\s*\d{1,2}(\s*\(\s*\d{1,2}\s*\))?\s*", name or ""))


# Слова, которые не являются именами (в названиях работ)
_NOT_NAMES = {
    "проект", "проектик", "проекта", "project", "untitled", "без", "названия", "новый", "новая", "new", "копия", "copy",
    "tinkercad", "тинкер", "scratch", "скретч", "kodu", "codu", "игра", "game", "урок", "lesson", "test", "тест",
    "презентация", "файл", "file", "document", "документ", "my", "мой", "моя", "guest", "by", "version", "final",
    "домашка", "дз", "работа", "screen", "video", "видео", "image", "img", "photo", "фото",
}


def name_from_filename(filename: str) -> str | None:
    """
    Имя ребёнка из названия его работы: «Ваня6-1(2) проект.pptx» → «Ваня»,
    «КИРИЛЛ ПРОЕКТИК.sb3» → «Кирилл», «Masha_robot» → «Маша». None — имени не видно.
    """
    filename = unicodedata.normalize("NFC", filename)
    stem = filename.rsplit(".", 1)[0] if "." in filename.strip(".") else filename
    word = re.split(r"[^A-Za-zА-Яа-яЁё]+", stem.strip())
    word = next((w for w in word if w), "")
    if not 2 <= len(word) <= 20:
        return None
    if word.lower() in _NOT_NAMES:
        return None
    if re.fullmatch(r"[A-Za-z]+", word):
        word = translit(word)
    return word[:1].upper() + word[1:].lower()
