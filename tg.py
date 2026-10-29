"""Минимальный клиент Telegram Bot API (только стандартная библиотека).

Реализовано ровно то, что нужно боту: приём обновлений длинным опросом,
отправка и редактирование сообщений, инлайн-кнопки, меню команд.
"""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"


class TelegramError(RuntimeError):
    def __init__(self, message: str, error_code: int | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


class Telegram:
    """Тонкая обёртка над HTTP-методами Bot API."""

    def __init__(self, token: str, timeout: int = 65) -> None:
        self.token = token
        self.timeout = timeout
        self._base = f"{API_ROOT}/bot{token}"

    # ------------------------------------------------------------------
    def _call(self, method: str, payload: dict | None = None, timeout: int | None = None) -> dict:
        data = json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base}/{method}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            description = body
            try:
                parsed = json.loads(body)
                description = parsed.get("description") or body
            except Exception:
                pass
            raise TelegramError(
                f"Telegram API {method}: HTTP {exc.code} — {description}", exc.code
            ) from exc
        except (urllib.error.URLError, socket.timeout) as exc:
            raise TelegramError(f"Telegram API {method}: сеть недоступна ({exc})") from exc

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TelegramError(f"Telegram API {method}: некорректный ответ") from exc
        if not parsed.get("ok"):
            raise TelegramError(
                f"Telegram API {method}: {parsed.get('description', 'ошибка')}",
                parsed.get("error_code"),
            )
        return parsed.get("result") or {}

    # ------------------------------------------------------------------
    def get_me(self) -> dict:
        return self._call("getMe", timeout=20)

    def get_updates(self, offset: int | None, timeout: int = 50) -> list[dict]:
        payload: dict = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload, timeout=timeout + 15)
        return result if isinstance(result, list) else []

    def send_message(
        self,
        chat_id: int,
        text: str,
        keyboard: dict | None = None,
        disable_notification: bool = False,
        parse_mode: str | None = None,
    ) -> dict:
        payload: dict = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
            "disable_notification": disable_notification,
        }
        if keyboard is not None:
            payload["reply_markup"] = keyboard
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return self._call("sendMessage", payload)

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        keyboard: dict | None = None,
    ) -> dict | None:
        payload: dict = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if keyboard is not None:
            payload["reply_markup"] = keyboard
        try:
            return self._call("editMessageText", payload)
        except TelegramError as exc:
            # «message is not modified» — обычная ситуация, ничего делать не нужно.
            if exc.error_code == 400 and "not modified" in str(exc):
                return None
            raise

    def answer_callback_query(
        self, callback_id: str, text: str = "", show_alert: bool = False
    ) -> None:
        try:
            self._call(
                "answerCallbackQuery",
                {"callback_query_id": callback_id, "text": text, "show_alert": show_alert},
                timeout=20,
            )
        except TelegramError as exc:
            log.debug("answerCallbackQuery не прошёл: %s", exc)

    def delete_message(self, chat_id: int, message_id: int) -> None:
        """Удаляет сообщение бота. Ошибки молча игнорируются — это некритично."""
        try:
            self._call(
                "deleteMessage",
                {"chat_id": chat_id, "message_id": message_id},
                timeout=20,
            )
        except TelegramError as exc:
            log.debug("deleteMessage не прошёл: %s", exc)

    def set_my_commands(self, commands: list[tuple[str, str]]) -> None:
        self._call(
            "setMyCommands",
            {"commands": [{"command": c, "description": d} for c, d in commands]},
            timeout=20,
        )

    def delete_webhook(self, drop_pending: bool = False) -> None:
        """Гарантирует, что режим длинного опроса не конфликтует с webhook."""
        try:
            self._call("deleteWebhook", {"drop_pending_updates": drop_pending}, timeout=20)
        except TelegramError as exc:
            log.warning("deleteWebhook не прошёл: %s", exc)

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        try:
            self._call("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=20)
        except TelegramError as exc:
            log.debug("sendChatAction не прошёл: %s", exc)


# ----------------------------------------------------------------------
# Помощники для клавиатур
# ----------------------------------------------------------------------
def inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    """rows: [[('Текст','callback_data'), ...], ...]"""
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row] for row in rows
        ]
    }