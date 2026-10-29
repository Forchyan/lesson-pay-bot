"""Недели и подсчёты: группировка занятий, итоги, статистика."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from storage import LessonRow, Storage
from tzutil import get_zone

_MONTHS_GEN = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
_WEEKDAYS_SHORT = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def week_start_for(day: date, week_start: int = 1) -> date:
    """Понедельник (или другой выбранный день) недели, в которую попадает дата."""
    offset = (day.isoweekday() - week_start) % 7
    return day - timedelta(days=offset)


def week_bounds(start: date, zone) -> tuple[datetime, datetime]:
    """Границы недели в UTC [начало, конец)."""
    begin = datetime.combine(start, datetime.min.time()).replace(tzinfo=zone)
    end = begin + timedelta(days=7)
    return begin.astimezone(timezone.utc), end.astimezone(timezone.utc)


def week_label(start: date) -> str:
    """Например: '13–19 октября'."""
    end = start + timedelta(days=6)
    if start.month == end.month:
        return f"{start.day}–{end.day} {_MONTHS_GEN[end.month - 1]}"
    return f"{start.day} {_MONTHS_GEN[start.month - 1]} – {end.day} {_MONTHS_GEN[end.month - 1]}"


def day_label(moment: datetime) -> str:
    """Например: 'Пн 13.10'."""
    return f"{_WEEKDAYS_SHORT[moment.isoweekday() - 1]} {moment.day:02d}.{moment.month:02d}"


def time_label(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def format_money(amount: float, currency: str = "₽") -> str:
    """1234.0 -> '1 234 ₽'."""
    if abs(amount - round(amount)) < 0.005:
        text = f"{int(round(amount)):,}".replace(",", " ")
    else:
        text = f"{amount:,.2f}".replace(",", " ").replace(".", ",")
    return f"{text} {currency}".strip()


@dataclass
class LessonView:
    """Занятие, готовое к показу: время уже в местной зоне, цена подставлена."""

    row: LessonRow
    start_local: datetime
    price: float | None
    price_source: str | None

    @property
    def key(self) -> str:
        return self.row.key

    @property
    def summary(self) -> str:
        return self.row.summary or "Занятие"

    @property
    def paid(self) -> bool:
        return self.row.paid


@dataclass
class WeekStats:
    total: int = 0
    paid: int = 0
    unpaid: int = 0
    planned: int = 0  # ещё не наступили
    amount_total: float = 0.0
    amount_paid: float = 0.0
    amount_unpaid: float = 0.0
    amount_planned: float = 0.0
    no_price: int = 0
    lessons: list[LessonView] = field(default_factory=list)

    @property
    def amount_expected(self) -> float:
        """Сколько всего должны за проведённые занятия."""
        return self.amount_paid + self.amount_unpaid

    @property
    def amount_week(self) -> float:
        """Все деньги за неделю, включая ещё не наступившие занятия."""
        return self.amount_paid + self.amount_unpaid + self.amount_planned


class Ledger:
    """Высокоуровневые операции: недели, итоги, статистика."""

    def __init__(self, storage: Storage, cfg) -> None:
        self.storage = storage
        self.cfg = cfg

    @property
    def zone(self):
        return get_zone(self.cfg.tz) or timezone.utc

    # ------------------------------------------------------------------
    def lessons_of_week(self, week_start: date) -> list[LessonView]:
        start_utc, end_utc = week_bounds(week_start, self.zone)
        rows = self.storage.lessons_between(start_utc, end_utc)
        result: list[LessonView] = []
        for row in rows:
            price, source = self.storage.lesson_price(row)
            result.append(
                LessonView(
                    row=row,
                    start_local=row.start_utc.astimezone(self.zone),
                    price=price,
                    price_source=source,
                )
            )
        return result

    def week_stats(self, week_start: date, now_utc: datetime | None = None) -> WeekStats:
        now = now_utc or datetime.now(timezone.utc)
        local_now = now.astimezone(self.zone)
        stats = WeekStats()
        for lesson in self.lessons_of_week(week_start):
            stats.lessons.append(lesson)
            stats.total += 1
            future = lesson.start_local > local_now
            if future:
                stats.planned += 1
            elif lesson.paid:
                stats.paid += 1
            else:
                stats.unpaid += 1

            if lesson.price is None:
                stats.no_price += 1
            else:
                stats.amount_total += lesson.price
                if lesson.paid:
                    stats.amount_paid += lesson.price
                elif future:
                    stats.amount_planned += lesson.price
                else:
                    stats.amount_unpaid += lesson.price
        return stats

    # ------------------------------------------------------------------
    def current_week_start(self, now_utc: datetime | None = None) -> date:
        now = now_utc or datetime.now(timezone.utc)
        return week_start_for(now.astimezone(self.zone).date(), self.cfg.week_start)

    def week_start_by_offset(self, offset: int, now_utc: datetime | None = None) -> date:
        """offset = 0 — текущая неделя, -1 — предыдущая, +1 — следующая."""
        return self.current_week_start(now_utc) + timedelta(weeks=offset)

    def weeks_with_lessons(self) -> list[date]:
        """Все недели, по которым есть занятия (для истории)."""
        span = self.storage.week_span()
        if span is None:
            return []
        first, last = span
        first_d = first.astimezone(self.zone).date()
        last_d = last.astimezone(self.zone).date()
        start = week_start_for(first_d, self.cfg.week_start)
        end = week_start_for(last_d, self.cfg.week_start)
        weeks: list[date] = []
        current = start
        while current <= end:
            weeks.append(current)
            current += timedelta(days=7)
        return weeks

    # ------------------------------------------------------------------
    def summary_by_student(self, week_start: date) -> list[tuple[str, WeekStats]]:
        """Разбивка недели по ученикам (по названию занятия)."""
        grouped: dict[str, list[LessonView]] = {}
        for lesson in self.lessons_of_week(week_start):
            grouped.setdefault(lesson.summary, []).append(lesson)

        result: list[tuple[str, WeekStats]] = []
        local_now = datetime.now(timezone.utc).astimezone(self.zone)
        for name, lessons in grouped.items():
            stats = WeekStats(lessons=lessons, total=len(lessons))
            for lesson in lessons:
                future = lesson.start_local > local_now
                if future:
                    stats.planned += 1
                elif lesson.paid:
                    stats.paid += 1
                else:
                    stats.unpaid += 1
                if lesson.price is None:
                    stats.no_price += 1
                else:
                    stats.amount_total += lesson.price
                    if lesson.paid:
                        stats.amount_paid += lesson.price
                    elif future:
                        stats.amount_planned += lesson.price
                    else:
                        stats.amount_unpaid += lesson.price
            result.append((name, stats))
        result.sort(key=lambda item: item[1].amount_unpaid, reverse=True)
        return result

    def totals_for_period(self, weeks: list[date]) -> dict:
        """Сводка по нескольким неделям сразу."""
        total = paid = unpaid = planned = 0
        amount_paid = amount_unpaid = amount_planned = 0.0
        no_price = 0
        for week in weeks:
            stats = self.week_stats(week)
            total += stats.total
            paid += stats.paid
            unpaid += stats.unpaid
            planned += stats.planned
            amount_paid += stats.amount_paid
            amount_unpaid += stats.amount_unpaid
            amount_planned += stats.amount_planned
            no_price += stats.no_price
        return {
            "weeks": len(weeks),
            "total": total,
            "paid": paid,
            "unpaid": unpaid,
            "planned": planned,
            "amount_paid": amount_paid,
            "amount_unpaid": amount_unpaid,
            "amount_planned": amount_planned,
            "no_price": no_price,
        }