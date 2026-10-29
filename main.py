"""Точка входа: запуск бота.

Запуск:  python main.py

Бот работает через long polling (сам опрашивает Telegram). Дополнительно
поднимается крошечный HTTP-«маячок», чтобы хостинги вроде Render считали
процесс живым (они требуют, чтобы сервис слушал порт).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bot import Bot
from config import Config
from storage import Storage

HEALTH_PORT = int(os.environ.get("PORT", "10000"))


class _HealthHandler(BaseHTTPRequestHandler):
    """Отвечает 200 «ok» на любой запрос — маячок для хостинга."""

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_HEAD(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args) -> None:  # не спамим в лог
        pass


def run_health_server() -> None:
    """Фоновый HTTP-сервер, чтобы Render не перезапускал бота."""
    try:
        server = ThreadingHTTPServer(("0.0.0.0", HEALTH_PORT), _HealthHandler)
        logging.info("Health-сервер слушает порт %s", HEALTH_PORT)
        server.serve_forever()
    except Exception as exc:  # noqa: BLE001
        logging.warning("Health-сервер не поднялся (%s) — продолжаем без него", exc)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def main() -> int:
    cfg = Config()
    setup_logging(cfg.log_level)

    problems = cfg.validate()
    if problems:
        print("\n⚠️  Не хватает настроек:\n")
        for problem in problems:
            print(f"  • {problem}")
        print(
            "\nКак исправить:\n"
            "  1. Скопируйте файл .env.example в .env (в той же папке, что и main.py).\n"
            "  2. Откройте .env блокнотом и впишите значения BOT_TOKEN и ICAL_URL.\n"
            "  3. Запустите: python main.py\n"
        )
        return 1

    storage = Storage(
        db_path=cfg.db_path,
        supabase_url=cfg.supabase_url,
        supabase_key=cfg.supabase_key,
    )
    if cfg.supabase_url:
        print(f"База данных: Supabase ({cfg.supabase_url})")
    else:
        print(f"База данных: {storage.db_path}")
    if not cfg.ical_url:
        print("⚠️  ICAL_URL не задан — бот запустится, но календарь читать не сможет.")

    # Маячок здоровья — в фоне, чтобы не мешать боту.
    threading.Thread(target=run_health_server, daemon=True).start()

    bot = Bot(cfg, storage)
    print("Читаю календарь при запуске…")
    print(bot.sync())

    while True:
        try:
            bot.run()
        except KeyboardInterrupt:
            print("\nОстановлено пользователем.")
            return 0
        except Exception as exc:  # noqa: BLE001 — перезапуск вместо падения
            logging.error("Бот упал с ошибкой: %s", exc, exc_info=True)
            logging.info("Перезапуск через 15 секунд…")
            time.sleep(15)


if __name__ == "__main__":
    raise SystemExit(main())