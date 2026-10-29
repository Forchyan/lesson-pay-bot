"""Демо: показывает, как выглядят сообщения бота, без Telegram и без Google.

Запуск:  python demo.py
Используются вымышленные занятия, чтобы вы просто увидели внешний вид.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone

import ical
import weeks as weeks_mod
from config import Config
from storage import Storage

TZ_NAME = "Asia/Novosibirsk"
ZONE = ical.get_zone(TZ_NAME)
# «Сейчас» — четверг, 22 октября 2026, 12:00 по Новосибирску.
NOW = datetime(2026, 10, 22, 5, 0, tzinfo=timezone.utc)

DEMO_CALENDAR = """BEGIN:VCALENDAR
VERSION:2.0
X-WR-TIMEZONE:Asia/Novosibirsk
BEGIN:VEVENT
DTSTART;TZID=Asia/Novosibirsk:20260901T150000
DTEND;TZID=Asia/Novosibirsk:20260901T160000
RRULE:FREQ=WEEKLY;BYDAY=TU
SUMMARY:Математика (Аня)
UID:d-1
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=Asia/Novosibirsk:20260902T170000
DTEND;TZID=Asia/Novosibirsk:20260902T180000
RRULE:FREQ=WEEKLY;BYDAY=WE
SUMMARY:Физика (Игорь)
UID:d-2
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=Asia/Novosibirsk:20260903T180000
DTEND;TZID=Asia/Novosibirsk:20260903T190000
RRULE:FREQ=WEEKLY;BYDAY=TH
SUMMARY:Английский (Соня)
UID:d-3
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=Asia/Novosibirsk:20260904T160000
DTEND;TZID=Asia/Novosibirsk:20260904T170000
RRULE:FREQ=WEEKLY;BYDAY=FR
SUMMARY:Математика (Аня)
UID:d-4
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=Asia/Novosibirsk:20261024T120000
DTEND;TZID=Asia/Novosibirsk:20261024T130000
SUMMARY:Стрижка
UID:d-5
END:VEVENT
END:VCALENDAR
"""


def rule(title: str) -> None:
    print("\n" + "═" * 62)
    print(f"  {title}")
    print("═" * 62)


def show(text: str) -> None:
    print(text)
    print()


def main() -> int:
    cfg = Config()
    cfg.tz = TZ_NAME
    cfg.week_start = 1
    cfg.currency = "₽"
    cfg.lesson_keywords = ["математика", "физика", "английский"]

    storage = Storage(os.path.join(tempfile.mkdtemp(), "demo.db"))
    zone = ZONE
    start = datetime(2026, 9, 1, tzinfo=zone).astimezone(timezone.utc)
    end = datetime(2026, 11, 10, tzinfo=zone).astimezone(timezone.utc)

    events = ical.parse_ics(DEMO_CALENDAR, default_zone_name=TZ_NAME)
    lessons = [
        occurrence
        for occurrence in ical.expand(events, start, end, zone)
        if ical.is_lesson(occurrence.summary, cfg.lesson_keywords)
    ]
    storage.upsert_lessons(lessons)

    storage.set_price_rule("Математика (Аня)", 1500)
    storage.set_price_rule("Физика (Игорь)", 2000)
    storage.set_price_rule("Английский (Соня)", 1200)

    ledger = weeks_mod.Ledger(storage, cfg)
    from bot import Bot

    bot = Bot.__new__(Bot)
    bot.cfg = cfg
    bot.storage = storage
    bot.ledger = ledger
    bot.currency = "₽"
    bot._hash_index = {}
    bot._view_week = {}
    bot._view_day = {}
    bot._view_msg = {}
    bot._service_msg = {}
    bot._pending = {}
    bot._lock = threading.Lock()
    bot._current_week_for_render = None
    bot._view_week_hint = None

    current = ledger.current_week_start(NOW)
    prev_week = current - timedelta(weeks=1)
    next_week = current + timedelta(weeks=1)

    print()
    print("  ДЕМО-ПОКАЗ: как выглядит бот в Telegram")
    print(f"  (внутренние часы демо: {NOW.astimezone(zone).strftime('%d.%m.%Y %H:%M')}, Новосибирск)")
    print("  Занятия взяты вымышленные, чтобы показать внешний вид.")

    # Отмечаем часть занятий оплаченными, как это сделал бы пользователь.
    prev_lessons = ledger.lessons_of_week(prev_week)
    for lesson in prev_lessons:
        storage.set_paid(lesson.key, True)
    current_lessons = ledger.lessons_of_week(current)
    if current_lessons:
        storage.set_paid(current_lessons[0].key, True)

    rule("1. Что бот пишет в ответ на /неделя (текущая неделя)")
    text, keyboard = bot.render_week(current, None, NOW)
    show(text + "\n\n" + _keyboard_preview(keyboard))

    rule("2. Прошлая неделя (/прошлая) — здесь видно отметки об оплате")
    text, keyboard = bot.render_week(prev_week, None, NOW)
    show(text + "\n\n" + _keyboard_preview(keyboard))

    rule("3. Следующая неделя (/следующая)")
    text, keyboard = bot.render_week(next_week, None, NOW)
    show(text + "\n\n" + _keyboard_preview(keyboard))

    rule("4. Итоги недели (/итоги)")
    show(bot.render_summary(prev_week, NOW))

    rule("5. Общая статистика (/статистика)")
    show(bot.render_full_stats())

    rule("6. Сколько стоит занятие (/цены)")
    show(bot.render_prices())
    picker_text, picker_kb = bot.render_price_picker()
    show(picker_text + "\n\n" + _keyboard_preview(picker_kb))

    rule("7. Как задать цену")
    print("Нажимаете кнопку с названием занятия и присылаете число.")
    print("Или просто пишете сообщением:\n")
    print("   Вы:   Математика (Аня) 1500")
    print("   Бот:  ✅ Запомнил: «Математика (Аня)» — 1 500 ₽.")
    print()
    print("Дальше бот сам подставляет эту цену во все подсчёты.")
    print()

    rule("8. Что происходит при нажатии на занятие")
    print("Под сообщением недели у каждого занятия есть кнопка:")
    print()
    print("   ⭕ Ср 28.10 17:00 · Физика (Игорь)")
    print()
    print("Один тап — и занятие помечено оплаченным, всплывает подсказка")
    print("«Оплачено ✅ 2 000 ₽», а сообщение тут же перерисовывается:")
    print("отметка меняется на ✅, а суммы внизу пересчитываются.")
    print()

    rule("9. Напоминание об итогах")
    print("Каждое воскресенье в 20:00 бот сам присылает:")
    print()
    print("   🔔 Итоги недели")
    print()
    print(bot.render_summary(current, NOW).replace("\n", "\n   "))
    print()
    print("  Демо закончилось. Настоящий бот работает так же, только")
    print("  берёт занятия из вашего Google-календаря.")
    print()
    return 0


def _keyboard_preview(keyboard: dict) -> str:
    """Рисует кнопки так, как они примерно выглядят в Telegram."""
    lines = ["      ┌──────────────── кнопки ────────────────┐"]
    for row in keyboard.get("inline_keyboard", []):
        labels = [button["text"] for button in row]
        lines.append("      │ " + "  │  ".join(labels))
    lines.append("      └────────────────────────────────────────┘")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())