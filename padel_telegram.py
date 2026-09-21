#!/usr/bin/env python3
"""
Telegram integration for the padel booking automation.

Talks to the Telegram Bot API (https://core.telegram.org/bots/api) using
curl_cffi (no extra dependencies). Used by:
  - `python padel_booking.py telegram`  (interactive bot, PadelBot)
  - `python padel_booking.py daemon`    (notifications + OTP re-login)

Config keys (config.json, gitignored):
  "telegram_bot_token": "123456:ABC..."   # from @BotFather
  "telegram_chat_ids":  [123456789]       # allowed user chat ids (whitelist)
"""

import json
from pathlib import Path

from curl_cffi import requests  # already a project dependency (no new packages)

HERE = Path(__file__).resolve().parent
BOT_API = "https://api.telegram.org"
STATE_FILE = HERE / "telegram_state.json"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class TelegramError(Exception):
    """A Bot API failure: network problem or an `ok: false` response."""

    def __init__(self, code: int, description: str) -> None:
        super().__init__(f"Telegram error {code}: {description}")
        self.code = code
        self.description = description

    def __str__(self) -> str:
        return f"Telegram error {self.code}: {self.description}"


# --------------------------------------------------------------------------- #
# Bot API client
# --------------------------------------------------------------------------- #
class TelegramAPI:
    """Minimal synchronous client for the Telegram Bot API (curl_cffi)."""

    def __init__(self, token: str) -> None:
        self.token = token

    def _call(self, method: str, **params) -> dict:
        """POST /bot<token>/<method> and return the `result` payload.

        Raises TelegramError on network failures, non-JSON bodies, or
        `ok: false` responses.
        """
        url = f"{BOT_API}/bot{self.token}/{method}"
        # getUpdates long-polls for up to `timeout` seconds; add a margin
        # (min 65s total) so a slow reply is never cut off by the socket.
        timeout = max(65, int(params.get("timeout", 0)) + 20)
        try:
            r = requests.post(url, json=params, timeout=timeout)
        except Exception as e:  # pylint: disable=broad-exception-caught
            raise TelegramError(0, str(e)) from e
        try:
            body = r.json()
        except Exception as e:  # pylint: disable=broad-exception-caught
            raise TelegramError(
                r.status_code, f"non-JSON response from Bot API: {e}") from e
        if body.get("ok") is not True:
            raise TelegramError(
                int(body.get("error_code", r.status_code)),
                str(body.get("description", f"HTTP {r.status_code}")))
        return body["result"]

    def get_me(self) -> dict:
        """Return the bot's user info (also validates the token)."""
        return self._call("getMe")

    def send_message(self, chat_id: int, text: str,
                     reply_markup: dict | None = None) -> dict:
        """Send a text message to *chat_id*.

        *reply_markup* is an inline-keyboard dict; it is serialized to the
        JSON string the Bot API expects (omitted entirely when None).
        """
        if reply_markup is None:
            return self._call("sendMessage", chat_id=chat_id, text=text)
        return self._call("sendMessage", chat_id=chat_id, text=text,
                          reply_markup=json.dumps(reply_markup))

    def get_updates(self, offset: int | None = None, timeout: int = 50) -> list:
        """Long-poll for new updates (messages + callback queries)."""
        params = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", **params)

    def answer_callback_query(self, callback_query_id: str,
                              text: str | None = None) -> dict:
        """Acknowledge a callback query (optional alert *text*)."""
        if text is None:
            return self._call("answerCallbackQuery",
                              callback_query_id=callback_query_id)
        return self._call("answerCallbackQuery",
                          callback_query_id=callback_query_id, text=text)

    def set_my_commands(self, commands: list) -> dict:
        """Set the bot's command menu (list of BotCommand dicts)."""
        return self._call("setMyCommands", commands=commands)


# --------------------------------------------------------------------------- #
# Config / notifications
# --------------------------------------------------------------------------- #
def get_telegram_cfg(cfg: dict) -> tuple:
    """Extract (bot_token, chat_ids) from a config dict; empty when unset."""
    token = str(cfg.get("telegram_bot_token", "") or "").strip()
    chat_ids = [int(c) for c in (cfg.get("telegram_chat_ids") or [])]
    return token, chat_ids


class TelegramNotifier:
    """Fire-and-forget notifier: broadcasts a text to all whitelisted chats."""

    def __init__(self, cfg: dict) -> None:
        token, chat_ids = get_telegram_cfg(cfg)
        self.api = TelegramAPI(token) if token else None
        self.chat_ids = chat_ids

    @property
    def enabled(self) -> bool:
        """True when a token and at least one chat id are configured."""
        return self.api is not None and bool(self.chat_ids)

    def notify(self, text: str) -> bool:
        """Send *text* to every configured chat; never raises.

        Logs the text locally first, then attempts each chat in turn.
        Returns True when at least one send succeeded.
        """
        if not self.enabled:
            return False
        print(f"[telegram] {text}", flush=True)
        sent = False
        for chat in self.chat_ids:
            try:
                self.api.send_message(chat, text)
                sent = True
            except TelegramError as e:
                print(f"[telegram] notify to {chat} failed: {e}", flush=True)
        return sent


# --------------------------------------------------------------------------- #
# State persistence
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    """Read telegram_state.json; returns {} when missing or corrupt."""
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:  # pylint: disable=broad-exception-caught
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict) -> None:
    """Write telegram_state.json (indent=2, UTF-8, chmod 600)."""
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    STATE_FILE.chmod(0o600)