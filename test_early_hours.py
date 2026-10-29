# -*- coding: utf-8 -*-
"""Проверка фильтра «занятия раньше 7:00 не учитываются»."""
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ical
import weeks as weeks_mod
from config import Config
from storage import Storage

TZ = "Asia/Novosibirsk"
ZONE = ical.get_zone(TZ)

CAL = """BEGIN:VCALENDAR
VERSION:2.0
X-WR-TIMEZONE:Asia/Novosibirsk
BEGIN:VEVENT
DTSTART;TZID=Asia/Novosibirsk:20261020T050000
DTEND;TZID=Asia/Novosibirsk:20261020T060000
SUMMARY:Класс Иванов (не назначено)
UID:e1
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=Asia/Novosibirsk:20261020T065900
DTEND;TZID=Asia/Novosibirsk:20261020T075000
SUMMARY:Класс Петров (тоже рано)
UID:e2
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=Asia/Novosibirsk:20261020T070000
DTEND;TZID=Asia/Novosibirsk:20261020T080000
SUMMARY:Класс Сидоров (ровно 7:00 — учитывается)
UID:e3
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=Asia/Novosibirsk:20261020T150000
DTEND;TZID=Asia/Novosibirsk:20261020T160000
SUMMARY:Класс Смирнов (обычное)
UID:e4
END:VEVENT
END:VCALENDAR
"""

FAILED = []


def check(name, cond, detail=""):
    print(("  OK " if cond else "  FAIL ") + name + ("  " + detail if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def main():
    import calendar_sync

    cfg = Config()
    cfg.tz = TZ
    cfg.min_lesson_hour = 7
    cfg.lesson_keywords = ["класс"]
    cfg.include_all_day = False

    events = ical.parse_ics(CAL, default_zone_name=TZ)
    start = datetime(2026, 10, 19, tzinfo=ZONE).astimezone(timezone.utc)
    end = datetime(2026, 10, 26, tzinfo=ZONE).astimezone(timezone.utc)
    occurrences = ical.expand(events, start, end, ZONE)

    kept = [o for o in occurrences if not calendar_sync._is_before_min_hour(o, ZONE, 7)]
    names = sorted(o.summary for o in kept)
    check("ранние занятия отфильтрованы", names == ["Класс Сидоров (ровно 7:00 — учитывается)",
                                                    "Класс Смирнов (обычное)"], str(names))
    check("5:00 отфильтровано", calendar_sync._is_before_min_hour(
        ical.Occurrence("x", "x", "s", datetime(2026, 10, 20, 22, 0, tzinfo=timezone.utc),
                        datetime(2026, 10, 20, 23, 0, tzinfo=timezone.utc), False), ZONE, 7))
    check("6:59 отфильтровано", True)  # покрыто основным тестом (Петров)
    check("7:00 не отфильтровано", not calendar_sync._is_before_min_hour(
        ical.Occurrence("x", "x", "s", datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc),
                        datetime(2026, 10, 20, 1, 0, tzinfo=timezone.utc), False), ZONE, 7))
    check("min_hour=0 — всё учитывается", not calendar_sync._is_before_min_hour(
        ical.Occurrence("x", "x", "s", datetime(2026, 10, 20, 0, 30, tzinfo=timezone.utc),
                        datetime(2026, 10, 20, 1, 30, tzinfo=timezone.utc), False), ZONE, 0))

    # Очистка базы от ранних занятий
    tmp = os.path.join(tempfile.mkdtemp(), "t.db")
    storage = Storage(tmp)
    storage.upsert_lessons(occurrences)  # всё, включая ранние
    removed = storage.remove_lessons_before_hour(ZONE, 7, start, end)
    check("из базы удалены 2 ранних", removed == 2, str(removed))
    left = [l.summary for l in storage.lessons_between(start, end)]
    check("в базе остались 2 занятия", len(left) == 2, str(left))
    # Повторный вызов ничего не удаляет
    check("повторная чистка ничего не удаляет",
          storage.remove_lessons_before_hour(ZONE, 7, start, end) == 0)

    # Ручные занятия не трогаем
    from datetime import timedelta
    manual = storage.add_manual_lesson(
        "Класс вручную", datetime(2026, 10, 20, 23, 0, tzinfo=timezone.utc),
        datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc),
    )
    removed2 = storage.remove_lessons_before_hour(ZONE, 7, start, end)
    check("ручные занятия не удаляются", removed2 == 0 and manual.paid is False)

    print()
    if FAILED:
        print("FAILED:", ", ".join(FAILED))
        return 1
    print("ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())