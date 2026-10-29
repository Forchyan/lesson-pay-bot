"""Конфигурация бота.

Все параметры читаются из переменных окружения. Дополнительно, если рядом с кодом
лежит файл .env, он подхватывается автоматически — удобно при запуске на своём
компьютере. Переменные окружения имеют приоритет над .env.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Простой разбор файла .env (строки ВИД=значение, # — комментарий)."""
    if not path.is_file():
        return
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in raw.replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(BASE_DIR / ".env")


def _get(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_bool(name: str, default: bool = False) -> bool:
    raw = _get(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "да", "on", "y")


def _default_db_path() -> str:
    """На хостинге пишем базу в /app/data (там данные переживают перезапуск)."""
    shared = Path("/app/data")
    if shared.is_dir():
        return str(shared / "bot.db")
    return str(BASE_DIR / "data" / "bot.db")


class Config:
    """Все настройки бота в одном месте."""

    def __init__(self) -> None:
        # --- обязательное ---
        self.bot_token: str = _get("BOT_TOKEN")

        # --- доступ и календарь ---
        self.ical_url: str = _get("ICAL_URL")
        self.lesson_keywords: list[str] = [
            k.strip() for k in _get("LESSON_KEYWORDS").split(",") if k.strip()
        ]
        self.include_all_day: bool = _get_bool("INCLUDE_ALL_DAY", False)

        # Занятия, стоящие в календаре раньше этого часа, не учитываются:
        # это «ещё не назначенные» — их потом переносят на нормальное время.
        # 0 или 24 — не ограничивать.
        self.min_lesson_hour: int = _get_int("MIN_LESSON_HOUR", 7)

        # Удалять старые сообщения бота, чтобы чат не зарастал.
        self.cleanup_messages: bool = _get_bool("CLEANUP_MESSAGES", True)

        # --- время ---
        self.tz: str = _get("TZ", "Asia/Novosibirsk")
        self.week_start: int = _get_int("WEEK_START", 1)  # 1=понедельник ... 7=воскресенье

        # --- сколько недель тянуть из календаря ---
        self.history_weeks: int = _get_int("HISTORY_WEEKS", 8)
        self.future_weeks: int = _get_int("FUTURE_WEEKS", 8)

        # --- синхронизация ---
        self.sync_interval_min: int = _get_int("SYNC_INTERVAL_MIN", 15)

        # --- автоматические итоги недели ---
        self.summary_enabled: bool = _get_bool("SUMMARY_ENABLED", True)
        self.summary_weekday: int = _get_int("SUMMARY_WEEKDAY", 7)  # 1=Пн ... 7=Вс
        self.summary_time: str = _get("SUMMARY_TIME", "20:00")

        # --- прочее ---
        self.currency: str = _get("CURRENCY", "₽")
        self.owner_chat_id: int = _get_int("OWNER_CHAT_ID", 0)
        self.db_path: str = _get("DB_PATH") or _default_db_path()
        self.log_level: str = _get("LOG_LEVEL", "INFO").upper()

        # --- облачная база Supabase (необязательно) ---
        # Если заданы — данные живут в облаке и переживают любые перезапуски.
        self.supabase_url: str = _get("SUPABASE_URL")
        self.supabase_key: str = _get("SUPABASE_KEY")

    # ------------------------------------------------------------------
    def summary_hhmm(self) -> tuple[int, int]:
        """Время отправки итогов в виде (часы, минуты)."""
        raw = self.summary_time.replace(".", ":").strip()
        try:
            hh, mm = raw.split(":")[:2]
            hours, minutes = int(hh), int(mm)
        except (ValueError, AttributeError):
            return 20, 0
        if not (0 <= hours <= 23 and 0 <= minutes <= 59):
            return 20, 0
        return hours, minutes

    def validate(self) -> list[str]:
        """Список проблем в настройках (пустой, если всё хорошо)."""
        problems: list[str] = []
        if not self.bot_token:
            problems.append(
                "Не задан BOT_TOKEN — токен бота от @BotFather (переменная окружения BOT_TOKEN)."
            )
        elif ":" not in self.bot_token:
            problems.append("BOT_TOKEN выглядит неправильно: в нём должно быть двоеточие.")
        if not (1 <= self.week_start <= 7):
            self.week_start = 1
        if not (1 <= self.summary_weekday <= 7):
            self.summary_weekday = 7
        if self.history_weeks < 0:
            self.history_weeks = 0
        if self.future_weeks < 0:
            self.future_weeks = 0
        if self.sync_interval_min < 1:
            self.sync_interval_min = 15
        if not (0 <= self.min_lesson_hour <= 24):
            self.min_lesson_hour = 0
        return problems