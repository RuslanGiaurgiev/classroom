"""
Адрес или поиск?

Учитель вводит в одно поле и адреса, и запросы:
    scratch.mit.edu        → https://scratch.mit.edu
    раскраски динозавры    → поиск в Яндексе (с семейным фильтром)
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import quote_plus

SEARCH_ENGINES = {
    # family=yes — семейный поиск Яндекса (без «взрослого» контента)
    "yandex": ("Яндекс", "https://yandex.ru/search/?family=yes&text={q}"),
    # safe=active — безопасный поиск Google
    "google": ("Google", "https://www.google.com/search?safe=active&q={q}"),
}
DEFAULT_ENGINE = "yandex"

# «что-то.что-то», последняя часть — буквы (домен: .ru, .com, .рф, .online …)
_DOMAIN_RE = re.compile(r"^[\w-]+(\.[\w-]+)*\.[^\W\d_]{2,}(:\d{1,5})?([/?#].*)?$")


def looks_like_url(text: str) -> bool:
    t = text.strip()
    if not t or " " in t:
        return False
    if re.match(r"^https?://", t, re.IGNORECASE):
        return True
    host = re.split(r"[/:?#]", t, maxsplit=1)[0]
    try:
        ipaddress.IPv4Address(host)
        return True
    except ValueError:
        pass
    return host.lower() == "localhost" or bool(_DOMAIN_RE.match(t))


def to_url(text: str, engine: str = DEFAULT_ENGINE) -> str:
    """Превратить строку учителя в адрес: сайт как есть, остальное — поиск."""
    t = text.strip()
    if looks_like_url(t):
        return t if re.match(r"^https?://", t, re.IGNORECASE) else "https://" + t
    _, template = SEARCH_ENGINES.get(engine, SEARCH_ENGINES[DEFAULT_ENGINE])
    return template.format(q=quote_plus(t))
