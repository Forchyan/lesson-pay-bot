"""Логика телеграм-бота: недели, отметки об оплате, цены, итоги.

Здесь нет «тяжёлых» библиотек — только модули из комплекта Python.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

import calendar_sync
import weeks as weeks_mod
from config import Config
from storage import Storage
from tg import Telegram, TelegramError, inline_keyboard

log = logging.getLogger(__name__)

HELP_TEXT = """Что я умею

📅 Показываю занятия из вашего Google-календаря по неделям:
   прошлая, текущая и следующая.

💰 Вы отмечаете кнопкой, какие занятия оплачены — я всё запоминаю.

📊 Считаю итоги: сколько проведено, сколько оплачено,
   сколько ещё ждём и на какую сумму.

Цены задаются один раз для каждого занятия:
«Математика 1500» — и дальше я подставляю её сам.

Команды (можно нажимать кнопки под сообщениями):
/неделя — текущая неделя
/прошлая — предыдущая неделя
/следующая — следующая неделя
/итоги — итоги недели + разбивка по ученикам
/статистика — сводка за всё время
/цены — список цен
/цена Математика 1500 — задать цену
/обновить — заново прочитать календарь
/помощь — эта подсказка"""

_COMMANDS = [
    ("week", "текущая неделя"),
    ("prev", "прошлая неделя"),
    ("next", "следующая неделя"),
    ("sum", "итоги недели"),
    ("stats", "статистика"),
    ("prices", "список цен"),
    ("sync", "обновить из календаря"),
    ("help", "помощь"),
]

# Синонимы команд: и латиница, и русские слова, и с «/», и без.
_ALIASES = {
    "start": "start",
    "help": "help",
    "помощь": "help",
    "хелп": "help",
    "week": "week",
    "неделя": "week",
    "текущая": "week",
    "prev": "week_prev",
    "last": "week_prev",
    "прошлая": "week_prev",
    "предыдущая": "week_prev",
    "next": "week_next",
    "следующая": "week_next",
    "sum": "summary",
    "итоги": "summary",
    "итог": "summary",
    "stat": "stats",
    "stats": "stats",
    "статистика": "stats",
    "цены": "prices",
    "prices": "prices",
    "price": "price",
    "цена": "price",
    "sync": "sync",
    "обновить": "sync",
    "обнови": "sync",
}

_PLAIN_COMMANDS = {
    "неделя", "итоги", "статистика", "цены", "помощь", "прошлая",
    "следующая", "обновить", "обнови",
}

# «Математика 1500», «Математика = 1500 ₽», «математика: 1 500 руб»
_PRICE_RE = re.compile(
    r"^(?P<name>[^0-9]{2,60}?)\s*(?:=|:|-|—|за|стоит)?\s*"
    r"(?P<amount>\d[\d\s]{0,9}(?:[.,]\d{1,2})?)\s*"
    r"(?:₽|р\.?|руб\.?|рублей)?\s*$",
    re.IGNORECASE,
)


def _short_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:20]


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


class Bot:
    """Телеграм-бот целиком: приём обновлений, состояние, отрисовка."""

    def __init__(self, cfg: Config, storage: Storage) -> None:
        self.cfg = cfg
        self.storage = storage
        self.tg = Telegram(cfg.bot_token)
        self.ledger = weeks_mod.Ledger(storage, cfg)
        self.currency = cfg.currency

        self._lock = threading.Lock()
        self._hash_index: dict[str, str] = {}
        self._view_week: dict[int, date] = {}   # chat_id -> какая неделя показана
        self._view_day: dict[int, date | None] = {}  # chat_id -> открытый день (None = вся неделя)
        self._view_msg: dict[int, int] = {}     # chat_id -> id сообщения-недели
        self._service_msg: dict[int, int] = {}  # chat_id -> id последнего служебного сообщения
        self._pending: dict[int, dict] = {}     # chat_id -> ожидаемое действие
        self._last_sync: datetime | None = None
        self._last_sync_error: str = ""
        self._current_week_for_render: date | None = None
        self._view_week_hint: date | None = None
        self._rebuild_hash_index()

    # ------------------------------------------------------------------
    # Индекс «короткий код -> ключ занятия» (чтобы кнопки влезали в лимит Telegram)
    # ------------------------------------------------------------------
    def _rebuild_hash_index(self) -> None:
        index: dict[str, str] = {}
        for key in self.storage.all_lesson_keys():
            index[_short_hash(key)] = key
        with self._lock:
            self._hash_index = index

    def _key_by_hash(self, code: str) -> str | None:
        with self._lock:
            found = self._hash_index.get(code)
        if found:
            return found
        self._rebuild_hash_index()
        with self._lock:
            return self._hash_index.get(code)

    # ------------------------------------------------------------------
    # Синхронизация с календарём
    # ------------------------------------------------------------------
    def sync(self, notify_chat: int | None = None) -> str:
        """Читает календарь. Возвращает текст отчёта для пользователя."""
        if not self.cfg.ical_url:
            return (
                "⚠️ Ссылка на календарь не задана.\n\n"
                "Добавьте её в файл .env в строку ICAL_URL=... и перезапустите бота."
            )
        try:
            report = calendar_sync.sync(self.storage, self.cfg)
        except calendar_sync.IcalError as exc:
            self._last_sync_error = str(exc)
            log.warning("Синхронизация не удалась: %s", exc)
            return f"⚠️ Не удалось прочитать календарь.\n\n{exc}"
        except Exception as exc:  # noqa: BLE001 — показываем пользователю понятный текст
            self._last_sync_error = str(exc)
            log.exception("Неожиданная ошибка синхронизации")
            return f"⚠️ Ошибка при чтении календаря: {exc}"

        self._last_sync = datetime.now(timezone.utc)
        self._last_sync_error = ""
        self._rebuild_hash_index()

        local = self._last_sync.astimezone(self.ledger.zone)
        return (
            f"✅ Календарь прочитан в {local.strftime('%H:%M')}\n"
            f"Занятий найдено: {report['lessons_found']}\n"
            f"Новых добавлено: {report['added']}\n"
            f"В календаре всего событий: {report['events_total']}\n"
            f"({report['note']})"
        )

    def _auto_sync_loop(self) -> None:
        while True:
            try:
                self.sync()
            except Exception:
                log.exception("Ошибка фоновой синхронизации")
            time.sleep(max(60, self.cfg.sync_interval_min * 60))

    def _summary_loop(self) -> None:
        """Раз в сутки проверяет, не пора ли отправить итоги недели."""
        while True:
            try:
                self._maybe_send_summary()
            except Exception:
                log.exception("Ошибка проверки итогов недели")
            time.sleep(300)

    def _owner_chat(self) -> int:
        """Кому отправлять уведомления: из настроек или запомненный хозяин."""
        if self.cfg.owner_chat_id:
            return self.cfg.owner_chat_id
        stored = self.storage.get_setting("owner_chat_id")
        try:
            return int(stored) if stored else 0
        except ValueError:
            return 0

    def _maybe_send_summary(self) -> None:
        if not self.cfg.summary_enabled:
            return
        chat_id = self._owner_chat()
        if not chat_id:
            return
        local_now = datetime.now(timezone.utc).astimezone(self.ledger.zone)
        if local_now.isoweekday() != self.cfg.summary_weekday:
            return
        hours, minutes = self.cfg.summary_hhmm()
        scheduled = local_now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
        if local_now < scheduled:
            return
        week_start = self.ledger.current_week_start()
        marker = week_start.isoformat()
        if self.storage.summary_sent(marker):
            return
        text = self.render_summary(week_start)
        try:
            self.tg.send_message(chat_id, "🔔 Итоги недели\n\n" + text)
            self.storage.mark_summary_sent(marker)
            log.info("Итоги недели за %s отправлены", marker)
        except TelegramError as exc:
            log.warning("Не удалось отправить итоги: %s", exc)

    # ------------------------------------------------------------------
    # Уборка старых сообщений: в чате живёт одно сообщение-неделя
    # и одно служебное, старые удаляются.
    # ------------------------------------------------------------------
    def _cleanup_service(self, chat_id: int) -> None:
        if not self.cfg.cleanup_messages:
            return
        old = self._service_msg.pop(chat_id, None)
        if old:
            self.tg.delete_message(chat_id, int(old))

    def _send_service(self, chat_id: int, text: str) -> None:
        """Служебное сообщение (итоги, цены и т.п.): одно вместо кучи."""
        self._cleanup_service(chat_id)
        message = self.tg.send_message(chat_id, text)
        message_id = message.get("message_id")
        if message_id:
            self._service_msg[chat_id] = int(message_id)

    # ------------------------------------------------------------------
    # Отрисовка недели
    # ------------------------------------------------------------------
    def render_week(self, week_start: date, day: date | None = None,
                    now_utc: datetime | None = None):
        """Возвращает (текст, клавиатуру): день или вся неделя целиком."""
        now = now_utc or datetime.now(timezone.utc)
        self._current_week_for_render = week_start
        self._view_week_hint = week_start
        stats = self.ledger.week_stats(week_start, now)
        title = weeks_mod.week_label(week_start)
        current = self.ledger.current_week_start(now)

        if week_start < current:
            head = f"📅 Неделя {title} (прошлая)"
        elif week_start > current:
            head = f"📅 Неделя {title} (следующая)"
        else:
            head = f"📅 Неделя {title} (текущая)"

        if not stats.lessons:
            return (
                f"{head}\n\nЗанятий за эту неделю нет.\n\n"
                "Если занятия есть в календаре — нажмите «🔄 Обновить» или проверьте,"
                " что в названии есть ключевое слово."
            ), self._nav_keyboard(week_start)

        if day is not None:
            return self._render_day(head, day, stats, now)
        return self._render_week_overview(head, stats, now)

    # ------------------------------------------------------------------
    def _render_day(self, head: str, day: date,
                    stats: weeks_mod.WeekStats, now: datetime):
        """Показ одного дня: занятия + кнопки отметок."""
        day_lessons = [l for l in stats.lessons if l.start_local.date() == day]
        day_title = weeks_mod.day_label(datetime.combine(day, datetime.min.time()))
        if not day_lessons:
            return (
                f"{head}\n\n{day_title} — занятий нет.",
                self._day_nav_keyboard(day),
            )

        zone = self.ledger.zone
        local_now = now.astimezone(zone)
        lines = [f"{head} → {day_title}", ""]

        day_stats = weeks_mod.WeekStats(lessons=day_lessons, total=len(day_lessons))
        for lesson in day_lessons:
            mark = self._lesson_mark(lesson, now)
            price_text = (
                weeks_mod.format_money(lesson.price, self.currency)
                if lesson.price is not None
                else "цена не задана"
            )
            lines.append(
                f"   {mark} {weeks_mod.time_label(lesson.start_local)}  "
                f"{lesson.summary} — {price_text}"
            )
            future = lesson.start_local > local_now
            if lesson.paid:
                day_stats.paid += 1
            elif future:
                day_stats.planned += 1
            else:
                day_stats.unpaid += 1
            if lesson.price is None:
                day_stats.no_price += 1
            else:
                day_stats.amount_total += lesson.price
                if lesson.paid:
                    day_stats.amount_paid += lesson.price
                elif future:
                    day_stats.amount_planned += lesson.price
                else:
                    day_stats.amount_unpaid += lesson.price

        lines.append("")
        lines.append("—" * 24)
        lines.append(self._stats_line(day_stats))

        return "\n".join(lines).strip(), self._day_lesson_keyboard(
            day_lessons, day, now
        )

    # ------------------------------------------------------------------
    def _render_week_overview(self, head: str,
                              stats: weeks_mod.WeekStats, now: datetime):
        """Компактная неделя: одна строка на день."""
        zone = self.ledger.zone
        local_now = now.astimezone(zone)

        by_day: dict[date, list[weeks_mod.LessonView]] = {}
        for lesson in stats.lessons:
            by_day.setdefault(lesson.start_local.date(), []).append(lesson)

        lines = [head, ""]

        for day, day_lessons in sorted(by_day.items()):
            day_title = weeks_mod.day_label(
                datetime.combine(day, datetime.min.time())
            )
            marks = "".join(self._lesson_mark(l, now) for l in day_lessons)
            unpaid = sum(
                1 for l in day_lessons if not l.paid and l.start_local <= local_now
            )
            suffix = f" ждём {unpaid}" if unpaid else ""
            lines.append(f"  {day_title}  {marks}{suffix}")

        lines.append("")
        lines.append("—" * 24)
        lines.append(self._stats_line(stats))
        lines.append("")
        lines.append("👇 Нажмите день, чтобы открыть занятия")

        keyboard = self._week_days_keyboard(by_day, now)
        return "\n".join(lines).strip(), keyboard

    def _week_days_keyboard(self, by_day: dict, now: datetime) -> dict:
        """Кнопки-дни: «Пн 27.10 ✅✅⭕» — по две в ряд."""
        local_now = now.astimezone(self.ledger.zone)
        today = local_now.date()
        rows: list[list[tuple[str, str]]] = []
        row: list[tuple[str, str]] = []

        for day in sorted(by_day.keys()):
            day_lessons = by_day[day]
            day_title = weeks_mod.day_label(datetime.combine(day, datetime.min.time()))
            marks = "".join(self._lesson_mark(l, now) for l in day_lessons)
            label = f"{day_title} {marks}".strip()
            if day == today:
                label = f"▶️ {label}"
            row.append((label[:36], f"d:{day.isoformat()}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

        has_unpaid = any(
            not l.paid and l.start_local <= local_now
            for lessons in by_day.values() for l in lessons
        )
        if has_unpaid:
            week_start = self._view_week_hint or self.ledger.current_week_start()
            rows.append(
                [
                    ("✅ Всё оплачено", f"pa:{week_start.isoformat()}"),
                    ("⭕ Снять отметки", f"pu:{week_start.isoformat()}"),
                ]
            )
        rows.append([("💰 Задать цены", "prices")])
        rows.append(
            [
                ("◀️ Прошлая", "n:-1"),
                ("Текущая", "n:0"),
                ("Следующая ▶️", "n:1"),
            ]
        )
        rows.append(
            [
                ("📊 Итоги недели", f"sum:{self._keyboard_week().isoformat()}"),
                ("🔄 Обновить", "sync"),
            ]
        )
        return inline_keyboard(rows)

    def _keyboard_week(self) -> date:
        """Неделя для кнопок текущего экрана."""
        return self._current_week_for_render or self.ledger.current_week_start()

    def _day_lesson_keyboard(self, day_lessons: list, day: date,
                             now: datetime) -> dict:
        """Кнопки занятий конкретного дня + возврат к неделе."""
        week_start = self._current_week_for_render or self.ledger.current_week_start()
        rows: list[list[tuple[str, str]]] = []
        for lesson in day_lessons:
            mark = self._lesson_mark(lesson, now)
            label = (
                f"{mark} {weeks_mod.time_label(lesson.start_local)} · {lesson.summary}"
            )
            rows.append([(label[:60], f"t:{_short_hash(lesson.key)}")])
        if day_lessons:
            rows.append(
                [
                    ("✅ День оплачен", f"da:{day.isoformat()}"),
                    ("⭕ Снять за день", f"du:{day.isoformat()}"),
                ]
            )
        rows.append([("◀️ К неделе", "w:0")])
        rows.append(
            [
                ("◀️ Прошлая", "n:-1"),
                ("Текущая", "n:0"),
                ("Следующая ▶️", "n:1"),
            ]
        )
        rows.append(
            [
                ("📊 Итоги недели", f"sum:{week_start.isoformat()}"),
                ("🔄 Обновить", "sync"),
            ]
        )
        return inline_keyboard(rows)

    def _day_nav_keyboard(self, day: date) -> dict:
        """Кнопки для дня без занятий."""
        week_start = self._current_week_for_render or self.ledger.current_week_start()
        return inline_keyboard(
            [
                [("◀️ К неделе", "w:0")],
                [("◀️ Прошлая", "n:-1"), ("Текущая", "n:0"), ("Следующая ▶️", "n:1")],
                [("📊 Итоги недели", f"sum:{week_start.isoformat()}"), ("🔄 Обновить", "sync")],
            ]
        )
    def _stats_line(self, stats: weeks_mod.WeekStats) -> str:
        conducted = stats.paid + stats.unpaid
        parts = [f"проведено {conducted}", f"оплачено ✅ {stats.paid}"]
        if stats.unpaid:
            parts.append(f"ждём ⭕ {stats.unpaid}")
        if stats.planned:
            parts.append(f"впереди 🕗 {stats.planned}")
        first = "Занятия: " + " · ".join(parts)

        money: list[str] = []
        if stats.amount_paid:
            money.append(f"получено {weeks_mod.format_money(stats.amount_paid, self.currency)}")
        if stats.amount_unpaid:
            money.append(f"ждём {weeks_mod.format_money(stats.amount_unpaid, self.currency)}")
        if stats.amount_planned:
            money.append(f"впереди на {weeks_mod.format_money(stats.amount_planned, self.currency)}")
        if stats.amount_week:
            money.append(f"всего за неделю {weeks_mod.format_money(stats.amount_week, self.currency)}")
        second = "Деньги: " + " · ".join(money) if money else "Деньги: цены ещё не заданы"
        if stats.no_price:
            second += f"\n(у {stats.no_price} {_plural(stats.no_price, 'занятия', 'занятий', 'занятий')} цена не задана)"
        return first + "\n" + second

    def _lesson_mark(self, lesson: weeks_mod.LessonView, now_utc: datetime) -> str:
        """✅ оплачено, 🕗 ещё не наступило, ⭕ проведено и не оплачено."""
        if lesson.paid:
            return "✅"
        if lesson.start_local > now_utc.astimezone(self.ledger.zone):
            return "🕗"
        return "⭕"

    def _week_keyboard(self, stats: weeks_mod.WeekStats, week_start: date) -> dict:
        now = datetime.now(timezone.utc)
        rows: list[list[tuple[str, str]]] = []
        for lesson in stats.lessons:
            mark = self._lesson_mark(lesson, now)
            label = (
                f"{mark} {weeks_mod.day_label(lesson.start_local)} "
                f"{weeks_mod.time_label(lesson.start_local)} · {lesson.summary}"
            )
            rows.append([(label[:60], f"t:{_short_hash(lesson.key)}")])

        if stats.lessons:
            rows.append(
                [
                    ("✅ Всё оплачено", f"pa:{week_start.isoformat()}"),
                    ("⭕ Снять отметки", f"pu:{week_start.isoformat()}"),
                ]
            )
            rows.append([("💰 Задать цены", "prices")])

        rows.append(
            [
                ("◀️ Прошлая", "n:-1"),
                ("Текущая", "n:0"),
                ("Следующая ▶️", "n:1"),
            ]
        )
        rows.append(
            [
                ("📊 Итоги недели", f"sum:{week_start.isoformat()}"),
                ("🔄 Обновить", "sync"),
            ]
        )
        return inline_keyboard(rows)

    def _nav_keyboard(self, week_start: date) -> dict:
        return inline_keyboard(
            [
                [("◀️ Прошлая", "n:-1"), ("Текущая", "n:0"), ("Следующая ▶️", "n:1")],
                [
                    ("📊 Итоги недели", f"sum:{week_start.isoformat()}"),
                    ("🔄 Обновить", "sync"),
                ],
            ]
        )

    # ------------------------------------------------------------------
    # Итоги
    # ------------------------------------------------------------------
    def render_summary(self, week_start: date, now_utc: datetime | None = None) -> str:
        now = now_utc or datetime.now(timezone.utc)
        stats = self.ledger.week_stats(week_start, now)
        title = weeks_mod.week_label(week_start)
        lines = [f"📊 Итоги недели {title}", ""]

        if not stats.total:
            lines.append("Занятий за эту неделю не было.")
            return "\n".join(lines)

        lessons_word = _plural(stats.total, "занятие", "занятия", "занятий")
        lines.append(f"Всего в расписании: {stats.total} {lessons_word}")
        lines.append(f"Проведено: {stats.paid + stats.unpaid}")
        lines.append(f"Оплачено: ✅ {stats.paid}")
        lines.append(f"Не оплачено: ⭕ {stats.unpaid}")
        if stats.planned:
            lines.append(f"Ещё впереди: 🕗 {stats.planned}")
        lines.append("")

        if stats.amount_paid or stats.amount_unpaid or stats.amount_planned:
            lines.append(f"Получено: {weeks_mod.format_money(stats.amount_paid, self.currency)}")
            if stats.amount_unpaid:
                lines.append(
                    f"Ждём получить: {weeks_mod.format_money(stats.amount_unpaid, self.currency)}"
                )
            if stats.amount_planned:
                lines.append(
                    f"Впереди (ещё не прошли): "
                    f"{weeks_mod.format_money(stats.amount_planned, self.currency)}"
                )
            lines.append(f"Итого за неделю: {weeks_mod.format_money(stats.amount_week, self.currency)}")
        else:
            lines.append("Цены пока не заданы — суммы не считаю.")
        if stats.no_price:
            lines.append(
                f"⚠️ У {stats.no_price} {_plural(stats.no_price, 'занятия', 'занятий', 'занятий')} цена не задана."
            )

        by_student = self.ledger.summary_by_student(week_start)
        if len(by_student) > 1:
            lines.append("")
            lines.append("По занятиям:")
            for name, item in by_student:
                conducted = item.paid + item.unpaid
                if item.amount_total:
                    money = (
                        f" — {weeks_mod.format_money(item.amount_paid, self.currency)}"
                        f" из {weeks_mod.format_money(item.amount_total, self.currency)}"
                    )
                else:
                    money = ""
                if item.unpaid:
                    flag = "⭕"
                elif item.planned:
                    flag = "🕗"
                else:
                    flag = "✅"
                lines.append(
                    f"  {flag} {name}: проведено {conducted}, "
                    f"оплачено {item.paid}"
                    + (f", впереди {item.planned}" if item.planned else "")
                    + money
                )
        return "\n".join(lines)

    def render_full_stats(self) -> str:
        weeks = self.ledger.weeks_with_lessons()
        if not weeks:
            return "Пока нет данных — сначала прочитайте календарь командой /обновить."
        current = self.ledger.current_week_start()
        now = datetime.now(timezone.utc)

        lines = ["📈 Статистика", ""]

        groups = [
            ("Прошлая неделя", [current - timedelta(weeks=1)]),
            ("Текущая неделя", [current]),
            ("Следующая неделя", [current + timedelta(weeks=1)]),
        ]
        for title, group in groups:
            totals = self.ledger.totals_for_period(group)
            if not totals["total"]:
                lines.append(f"{title}: занятий нет")
                continue
            lines.append(
                f"{title} ({weeks_mod.week_label(group[0])}): "
                f"{totals['total']} {_plural(totals['total'], 'занятие', 'занятия', 'занятий')}, "
                f"оплачено {totals['paid']}, ждём {totals['unpaid']}"
            )

        past_weeks = [w for w in weeks if w < current]
        if past_weeks:
            totals = self.ledger.totals_for_period(past_weeks)
            lines.append("")
            lines.append(f"Всего за прошлые недели ({totals['weeks']} нед.):")
            lines.append(
                f"  занятий: {totals['total']}, оплачено: {totals['paid']}, "
                f"не оплачено: {totals['unpaid']}"
            )
            if totals["amount_paid"] or totals["amount_unpaid"]:
                lines.append(
                    f"  получено: {weeks_mod.format_money(totals['amount_paid'], self.currency)}, "
                    f"ждём: {weeks_mod.format_money(totals['amount_unpaid'], self.currency)}"
                )

        # Долги по всем неделям до текущей
        debt_total = 0.0
        debt_count = 0
        debt_lines: list[str] = []
        for week in past_weeks:
            stats = self.ledger.week_stats(week, now)
            if stats.unpaid:
                debt_count += stats.unpaid
                debt_total += stats.amount_unpaid
                money = (
                    f" ({weeks_mod.format_money(stats.amount_unpaid, self.currency)})"
                    if stats.amount_unpaid
                    else ""
                )
                debt_lines.append(
                    f"  {weeks_mod.week_label(week)}: {stats.unpaid} "
                    f"{_plural(stats.unpaid, 'занятие', 'занятия', 'занятий')}{money}"
                )
        if debt_lines:
            money_total = (
                f" на {weeks_mod.format_money(debt_total, self.currency)}" if debt_total else ""
            )
            lines.append("")
            lines.append(
                f"⭕ Не оплачено всего: {debt_count} "
                f"{_plural(debt_count, 'занятие', 'занятия', 'занятий')}{money_total}"
            )
            lines.extend(debt_lines[:12])
            if len(debt_lines) > 12:
                lines.append(f"  … и ещё {len(debt_lines) - 12}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Цены
    # ------------------------------------------------------------------
    def render_prices(self) -> str:
        rules = self.storage.price_rules()
        lines = ["💰 Цены занятий", ""]
        if rules:
            for rule in rules:
                lines.append(f"  • {rule.label} — {weeks_mod.format_money(rule.amount, self.currency)}")
        else:
            lines.append("Пока ничего не задано.")
        lines.append("")
        lines.append("Чтобы задать цену, отправьте сообщение вида:")
        lines.append("«Математика 1500» — или нажмите /цена")
        return "\n".join(lines)

    def render_price_picker(self) -> tuple[str, dict]:
        """Кнопки со всеми названиями занятий, для которых можно задать цену."""
        rows: list[list[tuple[str, str]]] = []
        names = self._distinct_names()
        if not names:
            return (
                "Названий занятий пока нет — сначала прочитайте календарь (/обновить).",
                inline_keyboard([[("🔄 Обновить", "sync")]]),
            )
        for name in names[:40]:
            amount, _ = self.storage.find_price(name)
            mark = f" — {weeks_mod.format_money(amount, self.currency)}" if amount else ""
            rows.append([(f"{name}{mark}"[:60], f"pr:{_short_hash(name)}")])
        text = (
            "💰 Выберите занятие, для которого нужно задать цену.\n"
            "После нажатия просто пришлите число, например 1500."
        )
        return text, inline_keyboard(rows)

    def _distinct_names(self) -> list[str]:
        return self.storage.distinct_summaries()

    # ------------------------------------------------------------------
    # Обработка сообщений
    # ------------------------------------------------------------------
    def handle_update(self, update: dict) -> None:
        try:
            if "message" in update:
                self.handle_message(update["message"])
            elif "callback_query" in update:
                self.handle_callback(update["callback_query"])
        except TelegramError as exc:
            log.warning("Ошибка Telegram: %s", exc)
        except Exception:
            log.exception("Ошибка обработки обновления")

    def handle_message(self, message: dict) -> None:
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        if chat.get("type") != "private":
            self.tg.send_message(chat_id, "Я работаю только в личном чате.")
            return

        text = (message.get("text") or "").strip()
        if not text:
            self._send_service(chat_id, "Пришлите текст — например «Математика 1500» или /неделя.")
            return

        if not self._is_allowed(chat_id):
            self.tg.send_message(
                chat_id,
                "Извините, этот бот настроен для другого пользователя.\n"
                f"Ваш ID: {chat_id}",
            )
            log.info("Отклонено сообщение от chat_id=%s", chat_id)
            return

        self._remember_owner(chat_id)

        command, argument = self._parse_command(text)
        if command:
            self._run_command(chat_id, command, argument)
            return

        # Свободный текст: сначала «ожидание суммы», потом «Название 1500».
        pending = self._pending.pop(chat_id, None)
        if pending and pending.get("action") == "set_price":
            amount = self._parse_amount(text)
            if amount is None:
                self._pending[chat_id] = pending
                self._send_service(chat_id, "Не понял сумму. Пришлите просто число, например 1500.")
                return
            name = pending["name"]
            self.storage.set_price_rule(name, amount, name)
            self.tg.send_message(
                chat_id,
                f"✅ Запомнил: «{name}» — {weeks_mod.format_money(amount, self.currency)}.\n\n"
                + self.sync_and_render_current(chat_id),
            )
            return

        match = _PRICE_RE.match(text)
        if match:
            amount = self._parse_amount(match.group("amount"))
            name = match.group("name").strip(" .:-—")
            if amount is not None and len(name) >= 2:
                self.storage.set_price_rule(name, amount, name)
                self._send_service(
                    chat_id,
                    f"✅ Запомнил: «{name}» — {weeks_mod.format_money(amount, self.currency)}.",
                )
                self.send_week(
                    chat_id,
                    self._view_week.get(chat_id) or self.ledger.current_week_start(),
                )
                return

        self._send_service(
            chat_id,
            "Не понял сообщение. 🙂\n\n"
            "Показать неделю — /неделя\n"
            "Задать цену — напишите, например: Математика 1500\n"
            "Все команды — /помощь",
        )

    def _is_allowed(self, chat_id: int) -> bool:
        """Бот отвечает только своему хозяину.

        Если OWNER_CHAT_ID задан в настройках — только ему. Если не задан, то
        хозяином становится первый, кто написал боту; дальше доступ у него одного.
        """
        if self.cfg.owner_chat_id:
            return chat_id == self.cfg.owner_chat_id
        stored = self.storage.get_setting("owner_chat_id")
        return not stored or int(stored) == chat_id

    def _remember_owner(self, chat_id: int) -> None:
        if self.cfg.owner_chat_id:
            return
        if not self.storage.get_setting("owner_chat_id"):
            self.storage.set_setting("owner_chat_id", str(chat_id))
            log.info("Бот закреплён за chat_id=%s", chat_id)

    def _parse_command(self, text: str) -> tuple[str | None, str]:
        lowered = text.lower()
        if lowered.startswith("/"):
            body = lowered[1:].split("@", 1)[0].strip()
            parts = body.split(maxsplit=1)
            word = parts[0] if parts else ""
            argument = text[1:].split(" ", 1)[1].strip() if " " in text else ""
            if word in ("start", "begin"):
                return "start", argument
            if word == "цена" or word == "price" or word == "цены":
                return ("prices" if not argument else "price"), argument
            return _ALIASES.get(word), argument
        plain = lowered.split(maxsplit=1)
        first = plain[0] if plain else ""
        if first in _PLAIN_COMMANDS:
            argument = text.split(" ", 1)[1].strip() if " " in text else ""
            return _ALIASES.get(first), argument
        return None, ""

    def _parse_amount(self, text: str) -> float | None:
        cleaned = text.replace(" ", "").replace("\u00a0", "").replace(",", ".")
        cleaned = cleaned.replace("₽", "").replace("руб.", "").replace("руб", "").replace("р.", "")
        cleaned = cleaned.strip(" .=:;")
        try:
            value = float(cleaned)
        except ValueError:
            return None
        if value < 0 or value > 10_000_000:
            return None
        return value

    def _run_command(self, chat_id: int, command: str | None, argument: str) -> None:
        if command in (None, ""):
            self._send_service(chat_id, "Не знаю такой команды. /помощь")
            return

        if command == "start":
            self.tg.send_message(chat_id, "Привет! 👋\n\n" + HELP_TEXT)
            self.send_week(chat_id, self.ledger.current_week_start())
            return

        if command == "help":
            self._send_service(chat_id, HELP_TEXT)
            return

        if command == "week":
            self.send_week(chat_id, self.ledger.current_week_start())
            return

        if command == "week_prev":
            self._shift_week(chat_id, -1)
            return

        if command == "week_next":
            self._shift_week(chat_id, +1)
            return

        if command == "summary":
            week = self._view_week.get(chat_id) or self.ledger.current_week_start()
            self._send_service(chat_id, self.render_summary(week))
            return

        if command == "stats":
            self._send_service(chat_id, self.render_full_stats())
            return

        if command == "prices":
            if argument:
                self._set_price_from_argument(chat_id, argument)
                return
            text, keyboard = self.render_price_picker()
            self._cleanup_service(chat_id)
            message = self.tg.send_message(chat_id, text, keyboard)
            message_id = message.get("message_id")
            if message_id:
                self._service_msg[chat_id] = int(message_id)
            return

        if command == "price":
            self._set_price_from_argument(chat_id, argument)
            return

        if command == "sync":
            self._send_service(chat_id, self.sync())
            self.send_week(chat_id, self.ledger.current_week_start())
            return

        self._send_service(chat_id, "Не знаю такой команды. /помощь")

    def _set_price_from_argument(self, chat_id: int, argument: str) -> None:
        match = _PRICE_RE.match(argument.strip())
        if not match:
            self._send_service(
                chat_id,
                "Формат: /цена Математика 1500\n\n"
                "Можно и просто сообщением: «Математика 1500»",
            )
            return
        amount = self._parse_amount(match.group("amount"))
        name = match.group("name").strip(" .:-—")
        if amount is None or len(name) < 2:
            self._send_service(chat_id, "Не разобрал. Пример: /цена Математика 1500")
            return
        self.storage.set_price_rule(name, amount, name)
        self.tg.send_message(
            chat_id,
            f"✅ «{name}» — {weeks_mod.format_money(amount, self.currency)}.",
        )
        self.send_week(chat_id, self._view_week.get(chat_id) or self.ledger.current_week_start())

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Экраны: неделя и день живут в одном сообщении
    # ------------------------------------------------------------------
    def _show_screen(self, chat_id: int, week_start: date, day: date | None,
                     message_id: int | None = None) -> None:
        """Показывает экран (неделя или день) — редактирует существующее
        сообщение или шлёт новое, удаляя старое."""
        text, keyboard = self.render_week(week_start, day)
        self._view_week[chat_id] = week_start
        self._view_day[chat_id] = day

        if message_id is None:
            message_id = self._view_msg.get(chat_id)
        if message_id:
            # Экран меняется на месте — старое сообщение заменяется новым.
            try:
                self.tg.edit_message_text(chat_id, int(message_id), text, keyboard)
                return
            except TelegramError:
                pass  # сообщение могло устареть — пересоздадим
        # Новое сообщение-экран: старое удаляем, чат не зарастает.
        self._delete_view(chat_id)
        message = self.tg.send_message(chat_id, text, keyboard)
        message_id = message.get("message_id")
        if message_id:
            self._view_msg[chat_id] = int(message_id)

    def _delete_view(self, chat_id: int) -> None:
        if not self.cfg.cleanup_messages:
            return
        old = self._view_msg.pop(chat_id, None)
        if old:
            self.tg.delete_message(chat_id, int(old))

    def send_week(self, chat_id: int, week_start: date) -> None:
        self._show_screen(chat_id, week_start, None)

    def _open_day(self, chat_id: int, day: date, message_id: int | None) -> None:
        week_start = weeks_mod.week_start_for(day, self.cfg.week_start)
        self._show_screen(chat_id, week_start, day, message_id)

    def _shift_week(self, chat_id: int, delta: int, message_id: int | None = None) -> None:
        base = self._view_week.get(chat_id) or self.ledger.current_week_start()
        target = base + timedelta(weeks=delta)
        if target > self.ledger.current_week_start() + timedelta(weeks=self.cfg.future_weeks):
            self._send_service(chat_id, "Дальше календарь не читается — сначала /обновить.")
            return
        self._show_screen(chat_id, target, None, message_id)
    def sync_and_render_current(self, chat_id: int) -> str:
        """Обновляет календарь и возвращает текст текущей недели."""
        self.sync()
        week = self._view_week.get(chat_id) or self.ledger.current_week_start()
        return self.render_week(week)[0]

    # ------------------------------------------------------------------
    # Кнопки
    # ------------------------------------------------------------------
    def handle_callback(self, query: dict) -> None:
        data = (query.get("data") or "").strip()
        callback_id = query.get("id") or ""
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        if chat_id is None:
            return

        if not self._is_allowed(chat_id):
            self.tg.answer_callback_query(callback_id, "Бот настроен для другого пользователя", True)
            return
        self._remember_owner(chat_id)

        try:
            if data.startswith("t:"):
                self._toggle_lesson(chat_id, callback_id, data[2:])
                return
            if data.startswith("d:"):
                day = date.fromisoformat(data[2:])
                self.tg.answer_callback_query(callback_id)
                self._open_day(chat_id, day, message_id)
                return
            if data == "w:0":
                self.tg.answer_callback_query(callback_id)
                week = self._view_week.get(chat_id) or self.ledger.current_week_start()
                self._show_screen(chat_id, week, None, message_id)
                return
            if data.startswith("n:"):
                delta = int(data[2:])
                self.tg.answer_callback_query(callback_id)
                self._shift_week(chat_id, delta, message_id)
                return
            if data.startswith("pa:") or data.startswith("pu:"):
                paid = data.startswith("pa:")
                week_start = date.fromisoformat(data[3:])
                lessons = self.ledger.lessons_of_week(week_start)
                self.storage.mark_all_paid([lesson.key for lesson in lessons], paid)
                self._view_week[chat_id] = week_start
                self._view_day.pop(chat_id, None)
                self._refresh_view(chat_id, message_id, callback_id,
                    "Отметил всю неделю" if paid else "Снял отметки за неделю")
                return
            if data.startswith("da:") or data.startswith("du:"):
                paid = data.startswith("da:")
                day = date.fromisoformat(data[3:])
                week_start = weeks_mod.week_start_for(day, self.cfg.week_start)
                lessons = [
                    l for l in self.ledger.lessons_of_week(week_start)
                    if l.start_local.date() == day
                ]
                self.storage.mark_all_paid([lesson.key for lesson in lessons], paid)
                self._view_week[chat_id] = week_start
                self._view_day[chat_id] = day
                self._refresh_view(chat_id, message_id, callback_id,
                    "Отметил день" if paid else "Снял отметки за день")
                return
            if data.startswith("sum:"):
                week_start = date.fromisoformat(data[4:])
                self.tg.answer_callback_query(callback_id)
                self._send_service(chat_id, self.render_summary(week_start))
                return
            if data.startswith("pr:"):
                self._ask_price_for(chat_id, callback_id, data[3:])
                return
            if data == "prices":
                text, keyboard = self.render_price_picker()
                self.tg.answer_callback_query(callback_id)
                self._cleanup_service(chat_id)
                message = self.tg.send_message(chat_id, text, keyboard)
                message_id = message.get("message_id")
                if message_id:
                    self._service_msg[chat_id] = int(message_id)
                return
            if data == "sync":
                self.tg.answer_callback_query(callback_id, "Читаю календарь…")
                report = self.sync()
                self._refresh_view(chat_id, message_id, callback_id, None)
                self._send_service(chat_id, report)
                return
        except TelegramError as exc:
            log.warning("Ошибка при обработке кнопки: %s", exc)
            self.tg.answer_callback_query(callback_id, "Не получилось, попробуйте ещё раз", True)
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка обработки кнопки")
            self.tg.answer_callback_query(callback_id, f"Ошибка: {exc}", True)
            return

        self.tg.answer_callback_query(callback_id, "Кнопка устарела — откройте /неделя")
    def _toggle_lesson(self, chat_id: int, callback_id: str, code: str) -> None:
        key = self._key_by_hash(code)
        if not key:
            self.tg.answer_callback_query(callback_id, "Занятие не найдено — обновите /неделя", True)
            return
        lesson = self.storage.lesson(key)
        if lesson is None:
            self.tg.answer_callback_query(callback_id, "Занятие не найдено", True)
            return
        new_state = not lesson.paid
        updated = self.storage.set_paid(key, new_state)
        price, source = self.storage.lesson_price(updated) if updated else (None, None)

        if new_state:
            if price is None:
                note = "Отмечено как оплаченное. Цена не задана — пришлите, например: «Математика 1500»"
            else:
                note = f"Оплачено ✅ {weeks_mod.format_money(price, self.currency)}"
        else:
            note = "Отметка об оплате снята"

        self.tg.answer_callback_query(callback_id, note)
        self._refresh_view(chat_id, self._view_msg.get(chat_id), None, None)

    def _ask_price_for(self, chat_id: int, callback_id: str, code: str) -> None:
        names = self._distinct_names()
        name = next((n for n in names if _short_hash(n) == code), None)
        if not name:
            self.tg.answer_callback_query(callback_id, "Не нашёл название", True)
            return
        self._pending[chat_id] = {"action": "set_price", "name": name}
        self.tg.answer_callback_query(callback_id)
        current, _ = self.storage.find_price(name)
        hint = (
            f"\nСейчас задано: {weeks_mod.format_money(current, self.currency)}."
            if current
            else ""
        )
        self._send_service(
            chat_id,
            f"💰 Сколько стоит «{name}»?{hint}\nПришлите число, например 1500.",
        )

    def _refresh_view(
        self, chat_id: int, message_id: int | None, callback_id: str | None, toast: str | None
    ) -> None:
        week_start = self._view_week.get(chat_id) or self.ledger.current_week_start()
        day = self._view_day.get(chat_id)
        text, keyboard = self.render_week(week_start, day)
        if message_id is None:
            message_id = self._view_msg.get(chat_id)
        if message_id:
            try:
                self.tg.edit_message_text(chat_id, int(message_id), text, keyboard)
            except TelegramError as exc:
                log.debug("Не удалось обновить сообщение: %s", exc)
        else:
            self._show_screen(chat_id, week_start, day)
        if callback_id and toast:
            self.tg.answer_callback_query(callback_id, toast)
    # ------------------------------------------------------------------
    def run(self) -> None:
        """Бесконечный цикл приёма обновлений."""
        me = self.tg.get_me()
        log.info(
            "Бот запущен: @%s (id=%s)", me.get("username") or "?", me.get("id") or "?"
        )
        self.tg.delete_webhook()
        try:
            self.tg.set_my_commands(_COMMANDS)
        except TelegramError as exc:
            log.warning("Не удалось установить меню команд: %s", exc)

        # Синхронизация в фоне и автопроверка итогов.
        threading.Thread(target=self._auto_sync_loop, daemon=True).start()
        threading.Thread(target=self._summary_loop, daemon=True).start()

        offset: int | None = None
        while True:
            try:
                updates = self.tg.get_updates(offset)
            except TelegramError as exc:
                log.warning("Опрос Telegram не удался: %s", exc)
                time.sleep(5)
                continue
            for update in updates:
                offset = int(update["update_id"]) + 1
                self.handle_update(update)