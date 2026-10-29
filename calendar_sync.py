"""Синхронизация расписания: скачивает .ics из Google Calendar и пишет занятия в базу."""

from __future__ import annotations

import gzip
import logging
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone

import ical
from storage import Storage
from tzutil import get_zone

log = logging.getLogger(__name__)

USER_AGENT = "lesson-pay-bot/1.0 (+icalsync)"
_TIMEOUT = 30


class IcalError(RuntimeError):
    """Не удалось получить или разобрать календарь."""


def fetch_ical(url: str) -> str:
    """Скачивает содержимое .ics по секретной ссылке."""
    if not url:
        raise IcalError("Не задана ссылка на календарь (ICAL_URL).")
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/calendar, text/plain, */*",
            "Accept-Encoding": "gzip, deflate",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            raw = response.read()
            encoding = (response.headers.get("Content-Encoding") or "").lower()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise IcalError(
                "Google отказал в доступе к календарю. Проверьте, что ссылка скопирована"
                " целиком и в настройках календаря открыт доступ «по ссылке»."
            ) from exc
        if exc.code == 404:
            raise IcalError("Календарь по ссылке не найден — возможно, ссылка устарела.") from exc
        raise IcalError(f"Календарь вернул ошибку HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise IcalError(f"Не удалось подключиться к календарю: {exc.reason}") from exc

    if "gzip" in encoding:
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    elif "deflate" in encoding:
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            try:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            except zlib.error:
                pass

    for codec in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
        try:
            return raw.decode(codec)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def window_bounds(cfg, now_utc: datetime | None = None) -> tuple[datetime, datetime, object]:
    """Границы окна синхронизации и часовой пояс пользователя."""
    zone = get_zone(cfg.tz) or timezone.utc
    now = now_utc or datetime.now(timezone.utc)
    local_now = now.astimezone(zone)
    today = local_now.date()

    # Начало текущей недели
    offset = (today.isoweekday() - cfg.week_start) % 7
    week_start = today - timedelta(days=offset)

    start = datetime.combine(week_start, datetime.min.time()).replace(tzinfo=zone)
    start -= timedelta(weeks=cfg.history_weeks)
    end = datetime.combine(week_start, datetime.min.time()).replace(tzinfo=zone)
    end += timedelta(weeks=cfg.future_weeks + 1)

    return start.astimezone(timezone.utc), end.astimezone(timezone.utc), zone


def _is_before_min_hour(occurrence, zone, min_hour: int) -> bool:
    """Занятие стоит раньше минимального часа — «ещё не назначено»."""
    if min_hour <= 0 or min_hour > 24:
        return False
    local = occurrence.start_local(zone)
    return local.hour < min_hour


def sync(storage: Storage, cfg, now_utc: datetime | None = None) -> dict:
    """Скачивает календарь, фильтрует занятия и записывает их в базу.

    Возвращает краткий отчёт о том, что произошло.
    """
    raw = fetch_ical(cfg.ical_url)
    zone = get_zone(cfg.tz) or timezone.utc
    events = ical.parse_ics(raw, default_zone_name=cfg.tz)

    window_start, window_end, zone = window_bounds(cfg, now_utc)

    lessons: list[ical.Occurrence] = []
    skipped_all_day = 0
    skipped_early = 0
    for occurrence in ical.expand(events, window_start, window_end, zone):
        if not ical.is_lesson(occurrence.summary, cfg.lesson_keywords):
            continue
        if occurrence.all_day and not cfg.include_all_day:
            skipped_all_day += 1
            continue
        if _is_before_min_hour(occurrence, zone, cfg.min_lesson_hour):
            skipped_early += 1
            continue
        lessons.append(occurrence)

    added = storage.upsert_lessons(lessons)
    # Вычищаем «неразобранные» занятия, попавшие в базу раньше фильтра
    # (например, вы перенесли занятие на время до 7 утра — из учёта оно уйдёт).
    removed_early = storage.remove_lessons_before_hour(
        zone, cfg.min_lesson_hour, window_start, window_end
    )

    if not cfg.lesson_keywords:
        note = "ключевые слова не заданы — берутся все события"
    else:
        note = "ключевые слова: " + ", ".join(cfg.lesson_keywords)

    return {
        "events_total": len(events),
        "lessons_found": len(lessons),
        "added": added,
        "skipped_all_day": skipped_all_day,
        "skipped_early": skipped_early,
        "removed_early": removed_early,
        "note": note,
        "window_start": window_start,
        "window_end": window_end,
    }