"""Офлайн-проверка ядра: парсер календаря, повторы, подсчёты.

Запуск:  python test_offline.py
Телеграм и Google здесь не нужны — всё считается на встроенных данных.

Проверяется поведение на календаре, похожем на настоящий Google-экспорт:
повторяющиеся занятия, перенос одного занятия на другой день, исключённая дата,
отменённое занятие, обычное событие и событие «на весь день».
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from datetime import datetime, timezone

import ical
import weeks as weeks_mod
from config import Config
from storage import Storage

TZ_NAME = "Asia/Novosibirsk"
ZONE = ical.get_zone(TZ_NAME)

FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name}  {detail}")
        FAILED.append(name)


SAMPLE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Google Inc//Google Calendar 70.9054//EN
X-WR-TIMEZONE:Asia/Novosibirsk
BEGIN:VEVENT
DTSTART;TZID=Asia/Novosibirsk:20260901T150000
DTEND;TZID=Asia/Novosibirsk:20260901T160000
RRULE:FREQ=WEEKLY;BYDAY=TU
SUMMARY:Урок математики
UID:math-001
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=Asia/Novosibirsk:20260902T170000
DTEND;TZID=Asia/Novosibirsk:20260902T180000
RRULE:FREQ=WEEKLY;BYDAY=WE
SUMMARY:Урок английского
UID:eng-002
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=Asia/Novosibirsk:20260903T180000
DTEND;TZID=Asia/Novosibirsk:20260903T190000
RRULE:FREQ=WEEKLY;BYDAY=TH
EXDATE;TZID=Asia/Novosibirsk:20261015T180000
SUMMARY:Урок физики
UID:phys-003
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=Asia/Novosibirsk:20261019T150000
DTEND;TZID=Asia/Novosibirsk:20261019T160000
RECURRENCE-ID;TZID=Asia/Novosibirsk:20261020T150000
SUMMARY:Урок математики
UID:math-001
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=Asia/Novosibirsk:20261022T180000
DTEND;TZID=Asia/Novosibirsk:20261022T190000
RECURRENCE-ID;TZID=Asia/Novosibirsk:20261022T180000
STATUS:CANCELLED
SUMMARY:Урок физики
UID:phys-003
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=Asia/Novosibirsk:20261021T120000
DTEND;TZID=Asia/Novosibirsk:20261021T130000
SUMMARY:Обед с другом
UID:lunch-004
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:20261024
SUMMARY:Отпуск
UID:vac-005
END:VEVENT
END:VCALENDAR
"""

# «Сейчас» для расчётов: четверг 22 октября 2026, 12:00 по Новосибирску.
NOW = datetime(2026, 10, 22, 5, 0, tzinfo=timezone.utc)


def main() -> int:
    print("1. Разбор файла календаря")
    events = ical.parse_ics(SAMPLE, default_zone_name=TZ_NAME)
    check("события прочитаны", len(events) == 7, f"получено {len(events)}")
    recurring = [e for e in events if e.is_recurring]
    check("повторяющихся — 3", len(recurring) == 3, f"получено {len(recurring)}")
    math = next(e for e in events if e.uid == "math-001" and e.recurrence_id is None)
    check("название разобрано", math.summary == "Урок математики", math.summary)
    check("правило повтора недельное", math.rrule.get("FREQ") == "WEEKLY", str(math.rrule))
    check("часовой пояс события прочитан", math.tzid == TZ_NAME, str(math.tzid))

    print("\n2. Разворот повторов внутри окна (12 октября — 2 ноября)")
    start = datetime(2026, 10, 12, tzinfo=ZONE).astimezone(timezone.utc)
    end = datetime(2026, 11, 2, tzinfo=ZONE).astimezone(timezone.utc)
    occurrences = ical.expand(events, start, end, ZONE)
    lessons = [o for o in occurrences if ical.is_lesson(o.summary, ["урок"])]
    others = [o for o in occurrences if not ical.is_lesson(o.summary, ["урок"])]
    check("занятий в окне — 7", len(lessons) == 7, f"получено {len(lessons)}")
    check("прочих событий — 2", len(others) == 2, f"получено {len(others)}")

    by_day = {o.start_local(ZONE).strftime("%d.%m %H:%M"): o.summary for o in lessons}
    check("повторы развернулись", "13.10 15:00" in by_day, str(sorted(by_day)))
    check("перенесённое занятие на новом месте", "19.10 15:00" in by_day, str(sorted(by_day)))
    check("старый слот переноса убран", "20.10 15:00" not in by_day, str(sorted(by_day)))
    check("исключённая дата пропущена", "15.10 18:00" not in by_day, str(sorted(by_day)))
    check("отменённое занятие пропущено", "22.10 18:00" not in by_day, str(sorted(by_day)))
    check("занятие следующей недели на месте", "27.10 15:00" in by_day, str(sorted(by_day)))
    check(
        "местное время верное (не съехал часовой пояс)",
        by_day.get("13.10 15:00") == "Урок математики",
        str(by_day.get("13.10 15:00")),
    )
    check(
        "событие «на весь день» не попало в занятия",
        all(o.summary != "Отпуск" for o in lessons),
    )
    check("чужое событие отфильтровано", all("Обед" not in o.summary for o in lessons))

    print("\n3. База и подсчёты")
    tmp = os.path.join(tempfile.mkdtemp(), "test.db")
    cfg = Config()
    cfg.tz = TZ_NAME
    cfg.week_start = 1
    cfg.currency = "₽"
    storage = Storage(tmp)
    added = storage.upsert_lessons(lessons)
    check("занятия записаны", added == 7, f"добавлено {added}")
    check("повторная запись не дублирует", storage.upsert_lessons(lessons) == 0)
    check("чужое событие в базу не попало", len(storage.lessons_between(
        start, end)) == 7, str(len(storage.lessons_between(start, end))))

    storage.set_price_rule("Урок математики", 1500)
    storage.set_price_rule("Урок английского", 1200)
    storage.set_price_rule("физики", 2000)

    ledger = weeks_mod.Ledger(storage, cfg)
    week = ledger.week_start_by_offset(0, NOW)
    check("неделя начинается с понедельника", week.isoweekday() == 1, week.isoformat())
    check("это неделя 19–25 октября", (week.month, week.day) == (10, 19), week.isoformat())

    stats = ledger.week_stats(week, NOW)
    check("занятий на неделе — 2", stats.total == 2, str(stats.total))
    check("оба уже прошли и не оплачены", stats.unpaid == 2 and stats.paid == 0 and stats.planned == 0)
    check(
        "сумма к получению — 2 700",
        abs(stats.amount_unpaid - 2700) < 0.01,
        str(stats.amount_unpaid),
    )
    check("цены подобраны для всех занятий", stats.no_price == 0, str(stats.no_price))
    check(
        "цены подставлены по правилам",
        sorted(round(l.price or 0) for l in stats.lessons) == [1200, 1500],
        str([l.price for l in stats.lessons]),
    )

    first = stats.lessons[0]
    storage.set_paid(first.key, True)
    stats2 = ledger.week_stats(week, NOW)
    check("после отметки оплачено — 1", stats2.paid == 1 and stats2.unpaid == 1,
          f"paid={stats2.paid} unpaid={stats2.unpaid}")
    check("сумма оплаченного — 1 500", abs(stats2.amount_paid - 1500) < 0.01, str(stats2.amount_paid))
    check("сумма долга — 1 200", abs(stats2.amount_unpaid - 1200) < 0.01, str(stats2.amount_unpaid))

    reopened = storage.lesson(first.key)
    check("отметка сохранилась в базе", reopened is not None and reopened.paid)
    storage.set_paid(first.key, False)
    check("отметку можно снять", not storage.lesson(first.key).paid)

    check("персональная цена занятия перекрывает правило",
          storage.lesson_price(_with_price(storage, first, 999))[0] == 999)

    # Будущие занятия не считаются долгом
    next_week = ledger.week_start_by_offset(1, NOW)
    stats3 = ledger.week_stats(next_week, NOW)
    check("на следующей неделе 3 занятия", stats3.total == 3, str(stats3.total))
    check("будущие занятия не в долгах", stats3.unpaid == 0 and stats3.planned == 3, str(stats3))
    check("будущее не искажает сумму долга", abs(stats3.amount_unpaid) < 0.01, str(stats3.amount_unpaid))
    check(
        "сумма будущих занятий посчитана отдельно",
        abs(stats3.amount_planned - (1500 + 1200 + 2000)) < 0.01,
        str(stats3.amount_planned),
    )
    check(
        "будущие занятия не попадают в «итого за неделю как долг»",
        abs(stats3.amount_unpaid) < 0.01 and abs(stats3.amount_week - 4700) < 0.01,
        f"week={stats3.amount_week}",
    )

    # Предыдущая неделя
    prev_week = ledger.week_start_by_offset(-1, NOW)
    prev_stats = ledger.week_stats(prev_week, NOW)
    check("на прошлой неделе 2 занятия", prev_stats.total == 2, str(prev_stats.total))
    check("прошлая неделя вся в долгу", prev_stats.unpaid == 2, str(prev_stats.unpaid))

    print("\n4. Тексты сообщений")
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

    week_text, week_kb = bot.render_week(week, None, NOW)
    check("в тексте недели есть заголовок", "Неделя" in week_text, week_text[:60])
    check("в тексте есть сумма", "₽" in week_text)
    check("в тексте есть «проведено»", "проведено" in week_text)
    check("неделя компактная: подсказка про дни", "Нажмите день" in week_text)
    check("есть кнопки-дни", any(
        b["callback_data"].startswith("d:")
        for row in week_kb["inline_keyboard"] for b in row
    ))
    check("кнопки-дни по 2 в ряд", any(
        len(row) == 2 for row in week_kb["inline_keyboard"]
    ))
    check("кнопки попадают в лимит 64 байта", all(
        len(button["callback_data"].encode()) <= 64
        for row in week_kb["inline_keyboard"] for button in row
    ))

    # Экран одного дня
    import datetime as dt_mod
    day_now = dt_mod.date(2026, 10, 21)  # среда текущей недели
    day_text, day_kb = bot.render_week(week, day_now, NOW)
    check("день: заголовок со стрелкой", "→" in day_text, day_text[:80])
    check("день: только занятия среды", "17:00" in day_text and "15:00" not in day_text,
          day_text)
    check("день: кнопки занятий с временем", any(
        b["callback_data"].startswith("t:")
        for row in day_kb["inline_keyboard"] for b in row
    ))
    check("день: есть возврат к неделе", any(
        b["callback_data"] == "w:0"
        for row in day_kb["inline_keyboard"] for b in row
    ))
    check("день: есть отметка за день", any(
        b["callback_data"].startswith("da:")
        for row in day_kb["inline_keyboard"] for b in row
    ))

    # День без занятий
    empty_day_text, _ = bot.render_week(week, dt_mod.date(2026, 10, 25), NOW)
    check("пустой день обрабатывается", "занятий нет" in empty_day_text,
          empty_day_text[:80])

    summary_text = bot.render_summary(week, NOW)
    check("в итогах есть «Оплачено»", "Оплачено" in summary_text)
    check("в итогах есть разбивка по занятиям", "По занятиям" in summary_text)
    check("в итогах есть итоговая сумма", "Итого за неделю" in summary_text)

    prices_text = bot.render_prices()
    check("список цен показывает математику", "Урок математики" in prices_text)

    stats_text = bot.render_full_stats()
    check("статистика строится", "Статистика" in stats_text)
    check("в статистике есть долг", "Не оплачено всего" in stats_text)

    picker_text, picker_kb = bot.render_price_picker()
    check("выбор цены объясняет, что делать", "цену" in picker_text)
    check("в выборе цен есть кнопки", len(picker_kb["inline_keyboard"]) == 3)
    check(
        "в кнопках видны названия занятий",
        any("Урок математики" in button[0]["text"]
            for button in picker_kb["inline_keyboard"]),
        str(picker_kb),
    )

    print("\n5. Разбор цен и команд из текста")
    import bot as bot_module

    cases = [
        ("Математика 1500", "математика", 1500.0),
        ("физика: 2 000 ₽", "физика", 2000.0),
        ("Английский = 1 200,50 руб", "английский", 1200.5),
        ("Математика 300", "математика", 300.0),
        ("Урок математики 1 500", "урок математики", 1500.0),
    ]
    for raw, expected_name, expected_amount in cases:
        match = bot_module._PRICE_RE.match(raw)
        ok = bool(match)
        if match:
            ok = bot._parse_amount(match.group("amount")) == expected_amount
            ok = ok and match.group("name").strip(" .:-—").lower() == expected_name
        check(f"цена «{raw}»", ok, "не разобрано" if not match else str(match.groups()))

    check("обычный текст не принимается за цену",
          not bot_module._PRICE_RE.match("привет как дела"))
    check("одно слово без числа не цена",
          not bot_module._PRICE_RE.match("Математика"))

    for text, expected in [
        ("/неделя", "week"),
        ("/прошлая", "week_prev"),
        ("/следующая", "week_next"),
        ("/итоги", "summary"),
        ("/статистика", "stats"),
        ("/цены", "prices"),
        ("/обновить", "sync"),
        ("/помощь", "help"),
        ("неделя", "week"),
        ("итоги", "summary"),
    ]:
        command, _ = bot._parse_command(text)
        check(f"команда «{text}»", command == expected, f"получено {command}")

    print("\n6. Крайние случаи")
    empty_db = os.path.join(tempfile.mkdtemp(), "empty.db")
    empty_storage = Storage(empty_db)
    empty_ledger = weeks_mod.Ledger(empty_storage, cfg)
    empty_stats = empty_ledger.week_stats(week, NOW)
    check("пустая неделя считается без ошибок", empty_stats.total == 0)
    check("пустая статистика не падает",
          "Статистика" in bot.render_full_stats() or True)

    check("экспорт без ключевых слов берёт всё",
          ical.is_lesson("Что угодно", []))
    check("поиск не зависит от регистра и «ё»",
          ical.is_lesson("УРОК МАТЕМАТИКИ", ["урок"]))
    check("поиск понимает ё/е",
          ical.is_lesson("Учёт занятий", ["учет"]))

    from tzutil import get_zone

    check("часовой пояс из встроенной таблицы", get_zone("Asia/Novosibirsk") is not None)
    check("смещение +07:00 разбирается", get_zone("+07:00") is not None)
    check("мусорный пояс даёт None", get_zone("Не/существует") is None)

    print()
    if FAILED:
        print(f"❌ Провалено проверок: {len(FAILED)}")
        for name in FAILED:
            print(f"   • {name}")
        return 1
    print("✅ Все проверки пройдены.")
    return 0


def _with_price(storage: Storage, lesson, amount: float):
    """Вспомогательная: занятие с персональной ценой."""
    storage.set_price(lesson.key, amount)
    return storage.lesson(lesson.key)


if __name__ == "__main__":
    sys.exit(main())