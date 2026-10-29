"""Работа с часовыми поясами.

Основной путь — стандартный модуль zoneinfo (нужна база IANA: на Linux она есть
всегда, на Windows помогает пакет tzdata). Если базы нет, используем встроенную
таблицу постоянных смещений — для России и ближайших стран этого достаточно,
потому что перехода на летнее время там давно нет.
"""

from __future__ import annotations

from datetime import timedelta, timezone, tzinfo

# Аварийные смещения (часы от UTC) на случай, если базы часовых поясов нет.
_FALLBACK_OFFSETS: dict[str, int] = {
    "utc": 0,
    "gmt": 0,
    "europe/london": 0,
    "europe/berlin": 1,
    "europe/paris": 1,
    "europe/warsaw": 1,
    "europe/kaliningrad": 2,
    "europe/kiev": 2,
    "europe/kyiv": 2,
    "europe/minsk": 3,
    "europe/moscow": 3,
    "europe/istanbul": 3,
    "europe/samara": 4,
    "asia/yerevan": 4,
    "asia/baku": 4,
    "asia/tbilisi": 4,
    "asia/dubai": 4,
    "asia/yekaterinburg": 5,
    "asia/tashkent": 5,
    "asia/dushanbe": 5,
    "asia/ashgabat": 5,
    "asia/almaty": 5,
    "asia/omsk": 6,
    "asia/bishkek": 6,
    "asia/novosibirsk": 7,
    "asia/krasnoyarsk": 7,
    "asia/barnaul": 7,
    "asia/tomsk": 7,
    "asia/novokuznetsk": 7,
    "asia/irkutsk": 8,
    "asia/ust-kira": 9,
    "asia/yakutsk": 9,
    "asia/chita": 9,
    "asia/vladivostok": 10,
    "asia/magadan": 11,
    "asia/sakhalin": 11,
    "asia/kamchatka": 12,
    "asia/anadyr": 12,
}


def get_zone(name: str | None) -> tzinfo | None:
    """Часовой пояс по имени. None, если имя пустое и распознать не удалось."""
    if not name:
        return None
    clean = name.strip().strip('"')
    if not clean:
        return None
    if clean.upper() in ("UTC", "GMT", "Z"):
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(clean)
    except Exception:  # нет базы tzdata (частый случай на Windows) или имя неизвестно
        pass
    hours = _FALLBACK_OFFSETS.get(clean.lower())
    if hours is not None:
        return timezone(timedelta(hours=hours), clean)
    # Последняя попытка: смещение вида "+07:00" / "+0700" / "-03:00"
    text = clean.replace(" ", "")
    sign = 1
    if text.startswith("+"):
        text = text[1:]
    elif text.startswith("-"):
        sign = -1
        text = text[1:]
    digits = text.replace(":", "")
    if len(digits) == 4 and digits.isdigit():
        hh, mm = int(digits[:2]), int(digits[2:])
        if hh <= 23 and mm <= 59:
            return timezone(sign * timedelta(hours=hh, minutes=mm), clean)
    return None