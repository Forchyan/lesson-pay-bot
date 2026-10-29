"""Чтение расписания из Google Calendar по секретной ссылке в формате iCal (.ics).

Написано на стандартной библиотеке, без внешних зависимостей.

Что поддерживается:
  * обычные события и повторяющиеся (RRULE: DAILY / WEEKLY / MONTHLY / YEARLY,
    с INTERVAL, COUNT, UNTIL, BYDAY, BYMONTHDAY, BYMONTH);
  * исключения дат (EXDATE) и добавленные вручную (RDATE);
  * перенесённые на другое время занятия (RECURRENCE-ID) — заменяют занятие
    с тем же UID;
  * отменённые события (STATUS:CANCELLED) — пропускаются;
  * события «на весь день» (можно включать или выключать настройкой);
  * часовые пояса (TZID), а также время в UTC и «плавающее» время.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Iterator

from tzutil import get_zone

log = logging.getLogger(__name__)

UTC = timezone.utc

_DAY_CODES = {"MO": 1, "TU": 2, "WE": 3, "TH": 4, "FR": 5, "SA": 6, "SU": 7}


# ----------------------------------------------------------------------
# Разбор .ics
# ----------------------------------------------------------------------
def unfold(raw: str) -> list[str]:
    """Убирает переносы строк внутри свойств (стандарт iCal)."""
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = raw.split("\n")
    result: list[str] = []
    for line in lines:
        if line[:1] in (" ", "\t") and result:
            result[-1] += line[1:]
        else:
            result.append(line)
    return result


def unescape_text(value: str) -> str:
    """Раскодирует значение TEXT по правилам iCal."""
    value = value.replace("\\N", "\n").replace("\\n", "\n")
    value = value.replace("\\,", "\x00").replace("\\;", "\x01")
    value = value.replace("\\\\", "\\")
    return value.replace("\x00", ",").replace("\x01", ";")


def _split_property(line: str) -> tuple[str, dict[str, str], str] | None:
    """Разбирает строку 'DTSTART;TZID=X:20260101T100000' на части."""
    in_quotes = False
    colon = -1
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ":" and not in_quotes:
            colon = i
            break
    if colon < 0:
        return None
    head, value = line[:colon], line[colon + 1 :]
    parts = _split_unquoted(head, ";")
    name = parts[0].strip().upper()
    params: dict[str, str] = {}
    for chunk in parts[1:]:
        if "=" in chunk:
            key, val = chunk.split("=", 1)
            params[key.strip().upper()] = val.strip().strip('"')
    return name, params, value


def _split_unquoted(text: str, delimiter: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False
    for ch in text:
        if ch == '"':
            in_quotes = not in_quotes
            current.append(ch)
        elif ch == delimiter and not in_quotes:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


@dataclass
class RawEvent:
    """Событие из файла, до разворота повторов."""

    uid: str
    summary: str
    start: datetime | date | None
    end: datetime | date | None
    duration: timedelta | None
    all_day: bool
    tzid: str | None
    rrule: dict[str, str]
    exdates: list[datetime | date] = field(default_factory=list)
    rdates: list[datetime | date] = field(default_factory=list)
    recurrence_id: datetime | date | None = None
    cancelled: bool = False
    location: str = ""
    description: str = ""

    @property
    def is_recurring(self) -> bool:
        return bool(self.rrule)


def parse_ics(raw: str, default_zone_name: str = "UTC") -> list[RawEvent]:
    """Возвращает список событий (без разворота повторов)."""
    events: list[RawEvent] = []
    current: dict[str, object] | None = None
    calendar_zone: str | None = None

    for line in unfold(raw):
        if not line.strip():
            continue
        if line.upper().startswith("BEGIN:VEVENT"):
            current = {"exdates": [], "rdates": []}
            continue
        if line.upper().startswith("END:VEVENT"):
            if current is not None:
                event = _build_event(current, calendar_zone or default_zone_name)
                if event is not None:
                    events.append(event)
            current = None
            continue
        parsed = _split_property(line)
        if parsed is None:
            continue
        name, params, value = parsed

        if current is None:
            if name == "X-WR-TIMEZONE":
                calendar_zone = unescape_text(value).strip()
            continue

        _apply_property(current, name, params, value)

    return events


def _apply_property(
    current: dict[str, object], name: str, params: dict[str, str], value: str
) -> None:
    if name == "UID":
        current["uid"] = value.strip()
    elif name == "SUMMARY":
        current["summary"] = unescape_text(value).strip()
    elif name == "LOCATION":
        current["location"] = unescape_text(value).strip()
    elif name == "DESCRIPTION":
        current["description"] = unescape_text(value).strip()
    elif name == "STATUS":
        current["status"] = value.strip().upper()
    elif name == "DTSTART":
        start, is_date = parse_dt(value, params)
        current["start"] = start
        current["all_day"] = is_date
        if params.get("TZID"):
            current["tzid"] = params["TZID"]
    elif name == "DTEND":
        end, _ = parse_dt(value, params)
        current["end"] = end
    elif name == "DURATION":
        current["duration"] = parse_duration(value)
    elif name == "RRULE":
        current["rrule"] = parse_rrule(value)
    elif name == "EXDATE":
        current.setdefault("exdates", [])
        cast = current["exdates"]
        assert isinstance(cast, list)
        for chunk in _split_unquoted(value, ","):
            parsed, _ = parse_dt(chunk, params)
            if parsed is not None:
                cast.append(parsed)
    elif name == "RDATE":
        cast = current.setdefault("rdates", [])
        assert isinstance(cast, list)
        for chunk in _split_unquoted(value, ","):
            parsed, _ = parse_dt(chunk, params)
            if parsed is not None:
                cast.append(parsed)
    elif name == "RECURRENCE-ID":
        parsed, _ = parse_dt(value, params)
        current["recurrence_id"] = parsed
        if params.get("TZID"):
            current["recurrence_tzid"] = params["TZID"]


def _build_event(data: dict[str, object], default_zone_name: str) -> RawEvent | None:
    start = data.get("start")
    if start is None:
        return None
    tzid = data.get("tzid")
    return RawEvent(
        uid=str(data.get("uid") or ""),
        summary=str(data.get("summary") or ""),
        start=start,  # type: ignore[arg-type]
        end=data.get("end"),  # type: ignore[arg-type]
        duration=data.get("duration"),  # type: ignore[arg-type]
        all_day=bool(data.get("all_day")),
        tzid=str(tzid) if tzid else None,
        rrule=data.get("rrule") or {},  # type: ignore[arg-type]
        exdates=list(data.get("exdates") or []),  # type: ignore[arg-type]
        rdates=list(data.get("rdates") or []),  # type: ignore[arg-type]
        recurrence_id=data.get("recurrence_id"),  # type: ignore[arg-type]
        cancelled=str(data.get("status") or "").upper() == "CANCELLED",
        location=str(data.get("location") or ""),
        description=str(data.get("description") or ""),
    )


def parse_dt(value: str, params: dict[str, str]) -> tuple[datetime | date | None, bool]:
    """Разбирает значение даты/времени iCal. Возвращает (значение, «на весь день»)."""
    value = value.strip()
    if not value:
        return None, False

    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        try:
            return datetime.strptime(value, "%Y%m%d").date(), True
        except ValueError:
            return None, False

    zone: timezone | object | None = None
    if value.endswith("Z"):
        zone = UTC
        value = value[:-1]
    elif params.get("TZID"):
        zone = get_zone(params["TZID"])

    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
        try:
            naive = datetime.strptime(value, fmt)
            break
        except ValueError:
            continue
    else:
        return None, False

    if zone is not None:
        try:
            aware = naive.replace(tzinfo=zone)  # type: ignore[arg-type]
            return aware, False
        except Exception:
            return naive, False
    return naive, False  # «плавающее» время — трактуем как местное


def parse_duration(value: str) -> timedelta | None:
    """Разбирает DURATION вида 'PT1H30M' или 'P1D'."""
    match = re.fullmatch(
        r"(?P<sign>[+-])?P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?"
        r"(?:(?P<seconds>\d+)S)?)?",
        value.strip().upper(),
    )
    if not match:
        return None
    delta = timedelta(
        days=int(match.group("days") or 0),
        hours=int(match.group("hours") or 0),
        minutes=int(match.group("minutes") or 0),
        seconds=int(match.group("seconds") or 0),
    )
    return -delta if match.group("sign") == "-" else delta


def parse_rrule(value: str) -> dict[str, str]:
    """Разбирает RRULE в словарь."""
    rule: dict[str, str] = {}
    for chunk in _split_unquoted(value, ";"):
        if "=" in chunk:
            key, val = chunk.split("=", 1)
            rule[key.strip().upper()] = val.strip()
    return rule


# ----------------------------------------------------------------------
# Разворот повторов
# ----------------------------------------------------------------------
@dataclass
class Occurrence:
    """Конкретное занятие в конкретный день."""

    uid: str
    occurrence_id: str
    summary: str
    start_utc: datetime
    end_utc: datetime
    all_day: bool
    location: str = ""

    @property
    def key(self) -> str:
        """Устойчивый идентификатор: UID + время начала."""
        return f"{self.uid}|{self.start_utc.strftime('%Y%m%dT%H%M%SZ')}"

    def start_local(self, zone) -> datetime:
        return self.start_utc.astimezone(zone) if zone else self.start_utc

    def end_local(self, zone) -> datetime:
        return self.end_utc.astimezone(zone) if zone else self.end_utc


def _as_utc(value: datetime | date, zone) -> datetime:
    """Приводит дату/время события к UTC."""
    if isinstance(value, date) and not isinstance(value, datetime):
        naive = datetime.combine(value, time.min)
        return naive.replace(tzinfo=zone or UTC).astimezone(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=zone or UTC).astimezone(UTC)
    return value.astimezone(UTC)


def _local_naive(value: datetime | date, zone) -> tuple[datetime, bool]:
    """Локальное «настенное» время события (для проверки правил повторов)."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, time.min), True
    if value.tzinfo is None:
        return value, False
    return value.astimezone(zone or UTC).replace(tzinfo=None), False


def _rrule_days(rule: dict[str, str]) -> set[int]:
    days: set[int] = set()
    for token in (rule.get("BYDAY") or "").split(","):
        token = token.strip().upper()
        if not token:
            continue
        code = token[-2:]
        if code in _DAY_CODES:
            days.add(_DAY_CODES[code])
    return days


def _monthly_byday_index(token: str) -> int | None:
    match = re.fullmatch(r"([+-]?\d+)([A-Z]{2})", token.strip().upper())
    if not match:
        return None
    return int(match.group(1))


def _nth_weekday_of_month(year: int, month: int, weekday: int, nth: int) -> date | None:
    if nth > 0:
        first = date(year, month, 1)
        offset = (weekday - first.isoweekday()) % 7
        day = 1 + offset + (nth - 1) * 7
    else:
        if month == 12:
            nxt = date(year + 1, 1, 1)
        else:
            nxt = date(year, month + 1, 1)
        last = nxt - timedelta(days=1)
        offset = (last.isoweekday() - weekday) % 7
        day = last.day - offset + (nth + 1) * 7
    try:
        candidate = date(year, month, day)
    except ValueError:
        return None
    return candidate if candidate.month == month else None


def _matches(day: date, rule: dict[str, str], anchor: date) -> bool:
    """Подходит ли календарный день под правило повтора."""
    freq = (rule.get("FREQ") or "").upper()
    interval = int(rule.get("INTERVAL") or 1) if (rule.get("INTERVAL") or "1").isdigit() else 1
    if interval < 1:
        interval = 1

    bymonth = {
        int(x) for x in (rule.get("BYMONTH") or "").split(",") if x.strip().isdigit()
    }
    if bymonth and day.month not in bymonth:
        return False

    bymonthday = {
        abs(int(x)) for x in (rule.get("BYMONTHDAY") or "").split(",") if x.strip().lstrip("+-").isdigit()
    }
    byday = _rrule_days(rule)

    if freq == "DAILY":
        delta = (day - anchor).days
        if delta < 0 or delta % interval:
            return False
        if byday and day.isoweekday() not in byday:
            return False
        if bymonthday and day.day not in bymonthday:
            return False
        return True

    if freq == "WEEKLY":
        # Недели считаем от понедельника недели первого занятия.
        anchor_monday = anchor - timedelta(days=anchor.isoweekday() - 1)
        day_monday = day - timedelta(days=day.isoweekday() - 1)
        weeks = (day_monday - anchor_monday).days // 7
        if weeks < 0 or weeks % interval:
            return False
        weekdays = byday or {anchor.isoweekday()}
        return day.isoweekday() in weekdays

    if freq == "MONTHLY":
        months = (day.year - anchor.year) * 12 + (day.month - anchor.month)
        if months < 0 or months % interval:
            return False
        tokens = [t.strip().upper() for t in (rule.get("BYDAY") or "").split(",") if t.strip()]
        if tokens:
            for token in tokens:
                code = token[-2:]
                if code not in _DAY_CODES or day.isoweekday() != _DAY_CODES[code]:
                    continue
                nth = _monthly_byday_index(token)
                if nth is None:
                    return True
                target = _nth_weekday_of_month(day.year, day.month, _DAY_CODES[code], nth)
                if target == day:
                    return True
            return False
        days = bymonthday or {anchor.day}
        return day.day in days

    if freq == "YEARLY":
        years = day.year - anchor.year
        if years < 0 or years % interval:
            return False
        if bymonthday or byday:
            if bymonth and day.month not in bymonth:
                return False
            if bymonthday and day.day not in bymonthday:
                return False
            if byday and day.isoweekday() not in byday:
                return False
            return True
        return day.month == anchor.month and day.day == anchor.day

    # Неизвестная частота — считаем событие одиночным.
    return day == anchor


def expand(
    events: Iterable[RawEvent],
    window_start_utc: datetime,
    window_end_utc: datetime,
    default_zone,
    max_iterations: int = 40000,
) -> list[Occurrence]:
    """Разворачивает события в конкретные занятия внутри окна [start, end)."""
    events = list(events)
    overrides: dict[str, RawEvent] = {}
    base_events: list[RawEvent] = []
    for event in events:
        if event.recurrence_id is not None and event.uid:
            zone = get_zone(event.tzid) if event.tzid else default_zone
            marker = _as_utc(event.recurrence_id, zone).strftime("%Y%m%dT%H%M%SZ")
            if event.cancelled:
                overrides[f"{event.uid}|{marker}"] = event
            else:
                overrides[f"{event.uid}|{marker}"] = event
        else:
            base_events.append(event)

    occurrences: dict[str, Occurrence] = {}

    for event in base_events:
        zone = get_zone(event.tzid) if event.tzid else default_zone
        start_utc = _as_utc(event.start, zone)  # type: ignore[arg-type]
        end_utc = _compute_end(event, zone, start_utc)
        duration = end_utc - start_utc

        if not event.is_recurring:
            if event.cancelled:
                continue
            if window_start_utc <= start_utc < window_end_utc:
                occurrence = Occurrence(
                    uid=event.uid,
                    occurrence_id=f"{event.uid}|single",
                    summary=event.summary,
                    start_utc=start_utc,
                    end_utc=end_utc,
                    all_day=event.all_day,
                    location=event.location,
                )
                occurrences[occurrence.key] = occurrence
            continue

        rule = event.rrule
        anchor_local, anchor_is_date = _local_naive(event.start, zone)  # type: ignore[arg-type]
        anchor_date = anchor_local.date()

        until_utc = None
        if rule.get("UNTIL"):
            parsed, _ = parse_dt(rule["UNTIL"], {})
            if parsed is not None:
                until_utc = _as_utc(parsed, zone)
        count_limit = int(rule["COUNT"]) if (rule.get("COUNT") or "").isdigit() else None

        exclusions = {
            _as_utc(d, zone).strftime("%Y%m%dT%H%M%SZ") for d in event.exdates
        }

        # Границы перебора: от первого занятия до конца окна (или UNTIL).
        hard_end = window_end_utc
        if until_utc is not None and until_utc + duration < hard_end:
            hard_end = until_utc + duration
        scan_start = anchor_date
        scan_end = hard_end.astimezone(zone).date() if hard_end > start_utc else anchor_date
        # Небольшой запас вперёд на случай часовых поясов.
        scan_end = scan_end + timedelta(days=2)

        matches: list[tuple[datetime, datetime]] = []
        iterations = 0
        day = scan_start
        while day <= scan_end and iterations < max_iterations:
            iterations += 1
            if _matches(day, rule, anchor_date):
                local_dt = datetime.combine(day, anchor_local.timetz())
                if anchor_is_date or event.all_day:
                    local_dt = datetime.combine(day, time.min)
                aware = local_dt.replace(tzinfo=zone or UTC)
                occurrence_start = aware.astimezone(UTC)
                if until_utc is not None and occurrence_start > until_utc:
                    break
                matches.append((occurrence_start, occurrence_start + duration))
            day += timedelta(days=1)

        if count_limit is not None:
            matches = matches[:count_limit]

        for occurrence_start, occurrence_end in matches:
            marker = occurrence_start.strftime("%Y%m%dT%H%M%SZ")
            override = overrides.get(f"{event.uid}|{marker}")
            if override is not None:
                continue  # перенесённое/отменённое занятие добавим отдельно
            if marker in exclusions:
                continue
            if window_start_utc <= occurrence_start < window_end_utc:
                occurrence = Occurrence(
                    uid=event.uid,
                    occurrence_id=f"{event.uid}|{marker}",
                    summary=event.summary,
                    start_utc=occurrence_start,
                    end_utc=occurrence_end,
                    all_day=event.all_day or anchor_is_date,
                    location=event.location,
                )
                occurrences[occurrence.key] = occurrence

        # RDATE — дополнительные занятия, добавленные вручную.
        for extra in event.rdates:
            extra_utc = _as_utc(extra, zone)
            if window_start_utc <= extra_utc < window_end_utc:
                occurrence = Occurrence(
                    uid=event.uid,
                    occurrence_id=f"{event.uid}|{extra_utc.strftime('%Y%m%dT%H%M%SZ')}",
                    summary=event.summary,
                    start_utc=extra_utc,
                    end_utc=extra_utc + duration,
                    all_day=event.all_day,
                    location=event.location,
                )
                occurrences[occurrence.key] = occurrence

    # Перенесённые занятия и отмены.
    for key, override in overrides.items():
        if override.cancelled or override.summary == "" and override.start is None:
            continue
        if override.start is None:
            continue
        zone = get_zone(override.tzid) if override.tzid else default_zone
        start_utc = _as_utc(override.start, zone)
        end_utc = _compute_end(override, zone, start_utc)
        if not (window_start_utc <= start_utc < window_end_utc):
            continue
        occurrence = Occurrence(
            uid=override.uid,
            occurrence_id=f"{override.uid}|{start_utc.strftime('%Y%m%dT%H%M%SZ')}",
            summary=override.summary,
            start_utc=start_utc,
            end_utc=end_utc,
            all_day=override.all_day,
            location=override.location,
        )
        occurrences[occurrence.key] = occurrence

    result = sorted(occurrences.values(), key=lambda o: o.start_utc)
    return result


def _compute_end(event: RawEvent, zone, start_utc: datetime) -> datetime:
    if isinstance(event.end, (datetime, date)) and event.end is not None:
        end_utc = _as_utc(event.end, zone)
        if end_utc > start_utc:
            return end_utc
    if event.duration:
        return start_utc + event.duration
    # По умолчанию считаем занятие часовым — только для отображения времени.
    return start_utc + timedelta(hours=1 if not event.all_day else 0)


# ----------------------------------------------------------------------
# Фильтр «что считается занятием»
# ----------------------------------------------------------------------
def normalize(text: str) -> str:
    """Приводит текст к виду, удобному для поиска ключевых слов."""
    lowered = text.strip().lower().replace("ё", "е")
    return unicodedata.normalize("NFKC", lowered)


def is_lesson(summary: str, keywords: list[str]) -> bool:
    """Подходит ли событие под ключевые слова."""
    if not keywords:
        return True
    haystack = normalize(summary)
    return any(normalize(word) in haystack for word in keywords)