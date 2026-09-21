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
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from curl_cffi import requests  # already a project dependency (no new packages)

from padel_booking import (
    login_start, login_finish, get_authenticated_session, new_session,
    is_logged_in, fetch_booking_page, parse_booking_meta, get_available_slots,
    submit_booking, verify_booking_created, record_booking,
    already_booked_successfully, fetch_my_bookings, cancel_booking,
    is_cancellable, is_cancelled, _fmt_booking, parse_date, to_api_date,
    preferred_slot_starts, pick_best_slot, keepalive,
)

HERE = Path(__file__).resolve().parent
BOT_API = "https://api.telegram.org"
STATE_FILE = HERE / "telegram_state.json"

# Windows consoles default to cp1252 and our Telegram texts contain emojis:
# make console printing replace unencodable characters instead of crashing
# (e.g. a daemon thread dying on a legacy console).
for _stream in (sys.stdout, sys.stderr):
    if _stream is None:
        continue
    try:
        _stream.reconfigure(errors="replace")
    except Exception:  # pylint: disable=broad-exception-caught
        pass


def _log(msg: str) -> None:
    """Print a local log line; safe under pythonw (no console)."""
    if sys.stdout is not None:
        print(msg, flush=True)


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
        _log(f"[telegram] {text}")
        sent = False
        for chat in self.chat_ids:
            try:
                self.api.send_message(chat, text)
                sent = True
            except TelegramError as e:
                _log(f"[telegram] notify to {chat} failed: {e}")
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


# --------------------------------------------------------------------------- #
# Interactive bot
# --------------------------------------------------------------------------- #
class PadelBot:
    """Interactive Telegram bot: login/OTP, slots, book, my-bookings, cancel."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.token, self.chat_ids = get_telegram_cfg(cfg)
        if not self.token or not self.chat_ids:
            sys.exit("ERROR: set 'telegram_bot_token' and 'telegram_chat_ids' "
                     "in config.json first (see TELEGRAM.md).")
        self.api = TelegramAPI(self.token)
        self.state = load_state()
        self.pending: dict = {}   # chat_id -> in-progress flow state
        self._stop = threading.Event()   # set to ask the poll worker to exit
        self._fatal: str | None = None   # worker's fatal-error message

    def run(self) -> None:
        """Validate the token, publish the command menu, then long-poll.

        The blocking getUpdates call runs on a daemon worker thread: on
        Windows a Ctrl+C is only delivered to the main thread once it
        returns to Python bytecode, and libcurl holds it for the whole
        poll window (up to 50s) otherwise. Keeping the main thread in
        short, interruptible sleeps makes Ctrl+C stop the bot at once.
        """
        try:
            me = self.api.get_me()
        except TelegramError as e:
            sys.exit(f"ERROR: {e}")
        _log(f"[telegram] bot @{me['username']} ready; "
              f"allowed chats: {self.chat_ids}")
        _log("[telegram] stop the bot with Ctrl+C")
        commands = [
            {"command": "start", "description": "Show the command list"},
            {"command": "help", "description": "Show the command list"},
            {"command": "login", "description": "Start a fresh OTP login"},
            {"command": "status", "description": "Check the saved session"},
            {"command": "slots", "description": "List free slots (optional: date)"},
            {"command": "book", "description": "Pick a date + slot to book"},
            {"command": "mybookings", "description": "List your bookings"},
            {"command": "cancel", "description": "Cancel one of your bookings"},
            {"command": "autobook",
             "description": "Book a preferred slot (optional: date)"},
        ]
        try:
            self.api.set_my_commands(commands)
        except TelegramError as e:
            _log(f"[telegram] warning: command menu not set: {e}")
        worker = threading.Thread(target=self._poll_loop, daemon=True)
        worker.start()
        try:
            while worker.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            _log("\n[telegram] Ctrl+C — shutting down…")
            self._stop.set()
            worker.join(timeout=3)
            _log("[telegram] stopped. Bye!")
            return
        if self._fatal:
            sys.exit(f"ERROR: {self._fatal}")

    def _poll_loop(self) -> None:
        """Worker thread: long-poll for updates until stopped or fatal."""
        while not self._stop.is_set():
            try:
                updates = self.api.get_updates(
                    offset=self.state.get("offset"), timeout=50)
            except TelegramError as e:
                if e.code == 409:
                    self._fatal = ("409 Conflict — another instance of this "
                                   "bot is already polling (e.g. the daemon "
                                   "or another terminal). Stop it, then "
                                   "retry.")
                    return
                _log(f"[telegram] polling error: {e} — retrying in 5s")
                time.sleep(5)
                continue
            for u in updates:
                if self._stop.is_set():
                    return
                try:
                    self._handle(u)
                except Exception as e:  # pylint: disable=broad-exception-caught
                    _log(f"[telegram] error handling an update: {e}")
            if updates:
                self.state["offset"] = updates[-1]["update_id"] + 1
                save_state(self.state)

    def _handle(self, u: dict) -> None:
        """Route one update to its handler; reject non-whitelisted chats."""
        if "message" in u:
            chat_id = u["message"].get("chat", {}).get("id")
        elif "callback_query" in u:
            chat_id = u["callback_query"].get("from", {}).get("id")
        else:
            return
        if chat_id not in self.chat_ids:
            if "message" in u:
                try:
                    self.api.send_message(chat_id, "⛔ This bot is private.")
                except TelegramError as e:
                    _log(f"[telegram] reply failed: {e}")
            return
        if "message" in u:
            self._on_message(u["message"])
        elif "callback_query" in u:
            self._on_callback(u["callback_query"])

    def _send(self, chat_id: int, text: str,
              reply_markup: dict | None = None) -> None:
        """Send a message to *chat_id*; log failures instead of raising."""
        try:
            self.api.send_message(chat_id, text, reply_markup)
        except TelegramError as e:
            _log(f"[telegram] send to {chat_id} failed: {e}")

    # ---- text commands ----------------------------------------------------- #
    def _on_message(self, m: dict) -> None:
        """Handle a text message: a pending OTP first, then /commands."""
        chat_id = m["chat"]["id"]
        text = (m.get("text") or "").strip()

        # Pending OTP flow: the next message should be the code from email.
        if self.pending.get(chat_id, {}).get("action") == "otp":
            digits = re.sub(r"\D", "", text)
            if 4 <= len(digits) <= 8:
                self.pending.pop(chat_id, None)
                try:
                    login_finish(self.cfg, digits)
                    self._send(chat_id, "✅ Login successful — session saved.")
                except Exception as e:  # pylint: disable=broad-exception-caught
                    self._send(chat_id, f"❌ Login failed: {e}\n\n"
                                        f"Send /login to try again.")
            else:
                self._send(chat_id, "That doesn't look like the OTP code. "
                                    "Reply with the 6-digit code from your "
                                    "email (or /login to restart).")
            return

        if not text.startswith("/"):
            return

        parts = text.split()
        cmd = parts[0].split("@")[0].lower()   # strip any @botname, lowercase
        args = parts[1:]
        if cmd in ("/start", "/help"):
            self._on_help(chat_id)
        elif cmd == "/login":
            self._on_login(chat_id)
        elif cmd == "/status":
            self._on_status(chat_id)
        elif cmd == "/slots":
            self._on_slots(chat_id, args)
        elif cmd == "/book":
            self._on_book(chat_id)
        elif cmd == "/mybookings":
            self._on_mybookings(chat_id)
        elif cmd == "/cancel":
            self._on_cancel(chat_id)
        elif cmd == "/autobook":
            self._on_autobook(chat_id, args)
        else:
            self._send(chat_id, f"Unknown command {cmd}\n\n/help for the list.")

    def _on_login(self, chat_id: int) -> None:
        """Start the two-step login, then wait for the OTP code here."""
        try:
            login_start(self.cfg)
        except Exception as e:  # pylint: disable=broad-exception-caught
            self._send(chat_id, f"❌ Could not start login: {e}")
            return
        self.pending[chat_id] = {"action": "otp"}
        self._send(chat_id, "📧 OTP sent to your email.\n"
                            "Reply here with the 6-digit code to finish "
                            "the login.")

    def _on_status(self, chat_id: int) -> None:
        s = new_session()
        ok = is_logged_in(s, self.cfg)
        if ok:
            self._send(chat_id, "✅ Logged in — session is valid.")
        else:
            self._send(chat_id, "❌ Not logged in (or session expired).\n"
                                "Send /login to start a fresh login.")

    def _on_help(self, chat_id: int) -> None:
        self._send(chat_id,
                   "Padel booking bot — commands:\n\n"
                   "/login — start a fresh OTP login\n"
                   "/status — check whether the saved session is valid\n"
                   "/slots [date] — list free slots (or pick a date)\n"
                   "/book — pick a date, then a slot, to book\n"
                   "/mybookings — list your portal bookings\n"
                   "/cancel — cancel one of your bookings\n"
                   "/autobook [date] — book a preferred slot (default: +6d)\n"
                   "/start, /help — show this help")

    def _on_slots(self, chat_id: int, args: list) -> None:
        if args:
            try:
                date = parse_date(args[0])
            except ValueError as e:
                self._send(chat_id, f"❌ {e}")
                return
            self._show_slots(chat_id, date)
        else:
            self._show_date_keyboard(chat_id, "Pick a date:")

    def _show_date_keyboard(self, chat_id: int, intro: str) -> None:
        """Inline keyboard with one button per day, today through today+6."""
        today = datetime.now().date()
        buttons = []
        for i in range(7):
            d = today + timedelta(days=i)
            buttons.append([{"text": f"{d:%a %d %b}",
                             "callback_data": f"date:{d.isoformat()}"}])
        self._send(chat_id, intro, {"inline_keyboard": buttons})

    def _show_slots(self, chat_id: int, date: datetime) -> None:
        try:
            s = get_authenticated_session(self.cfg)
            html = fetch_booking_page(s, self.cfg)
            meta = parse_booking_meta(html)
            slots = get_available_slots(s, self.cfg,
                                        to_api_date(date.strftime("%Y-%m-%d")),
                                        meta)
        except RuntimeError as e:
            self._send(chat_id, f"⚠️ {e}\n\nSend /login to start a fresh "
                                f"login.")
            return
        if not slots:
            self._send(chat_id, f"No free slots on {date:%a %d %b}.")
            self._show_date_keyboard(chat_id, "Pick another date:")
            return
        self.pending[chat_id] = {"action": "pick_slot",
                                 "date": date.strftime("%Y-%m-%d"),
                                 "slots": slots}
        buttons = [[{"text": sl["label"], "callback_data": f"slot:{i}"}]
                   for i, sl in enumerate(slots)]
        self._send(chat_id, f"Free slots on {date:%a %d %b} — tap one to "
                            f"book:", {"inline_keyboard": buttons})

    def _on_book(self, chat_id: int) -> None:
        self._show_date_keyboard(chat_id, "Pick a date to book:")

    def _on_mybookings(self, chat_id: int) -> None:
        try:
            s = get_authenticated_session(self.cfg)
            bookings = fetch_my_bookings(s)
        except RuntimeError as e:
            self._send(chat_id, f"⚠️ {e}\n\nSend /login to start a fresh "
                                f"login.")
            return
        # Only show live bookings — hide cancelled/rejected ones.
        bookings = [b for b in bookings if not is_cancelled(b)]
        if not bookings:
            self._send(chat_id, "No upcoming bookings.")
            return
        text = ("Your bookings:\n\n"
                + "\n".join(f"{i + 1}. {_fmt_booking(b)}"
                            for i, b in enumerate(bookings)))
        rows = [[{"text": f"❌ Cancel #{i + 1}",
                  "callback_data": f"cx:{b['details_id']}"}]
                for i, b in enumerate(bookings) if is_cancellable(b)]
        self._send(chat_id, text, {"inline_keyboard": rows} if rows else None)

    def _on_cancel(self, chat_id: int) -> None:
        try:
            s = get_authenticated_session(self.cfg)
            bookings = fetch_my_bookings(s)
        except RuntimeError as e:
            self._send(chat_id, f"⚠️ {e}\n\nSend /login to start a fresh "
                                f"login.")
            return
        cancellable = [(i, b) for i, b in enumerate(bookings)
                       if is_cancellable(b)]
        if not cancellable:
            self._send(chat_id, "Nothing to cancel (no bookings, or none are "
                                "within the cancellation window).")
            return
        text = ("Cancellable bookings:\n\n"
                + "\n".join(f"{i + 1}. {_fmt_booking(b)}"
                            for i, b in cancellable))
        rows = [[{"text": f"❌ Cancel #{i + 1}",
                  "callback_data": f"cx:{b['details_id']}"}]
                for i, b in cancellable]
        self._send(chat_id, text, {"inline_keyboard": rows})

    def _on_autobook(self, chat_id: int, args: list) -> None:
        if args:
            try:
                date = parse_date(args[0])
            except ValueError as e:
                self._send(chat_id, f"❌ {e}")
                return
        else:
            date = datetime.now() + timedelta(days=6)
        date_str = date.strftime("%Y-%m-%d")
        if already_booked_successfully(date_str):
            self._send(chat_id, f"Already booked {date:%a %d %b} "
                                f"successfully (booked.json) — not booking "
                                f"again.")
            return
        try:
            s = get_authenticated_session(self.cfg)
            html = fetch_booking_page(s, self.cfg)
            meta = parse_booking_meta(html)
            slots = get_available_slots(s, self.cfg, to_api_date(date_str),
                                        meta)
        except RuntimeError as e:
            self._send(chat_id, f"⚠️ {e}\n\nSend /login to start a fresh "
                                f"login.")
            return
        preferred = preferred_slot_starts(self.cfg)
        best = pick_best_slot(slots, preferred)
        if best is None:
            self._send(chat_id, f"None of your preferred slots "
                                f"({', '.join(preferred)}) are free on "
                                f"{date:%a %d %b}.\n\nFree slots:\n"
                                + "\n".join(f"  {sl['label']}" for sl in slots)
                                + "\n\nUse /book to pick one manually.")
            return
        self.pending[chat_id] = {"action": "confirm", "date": date_str,
                                 "slot": best}
        self._send(chat_id, f"Autobook best match: {best['label']} on "
                            f"{date:%a %d %b}?",
                   self._confirm_book_keyboard())

    # ---- inline keyboards / callbacks ------------------------------------- #
    def _confirm_book_keyboard(self) -> dict:
        return {"inline_keyboard": [[
            {"text": "✅ Yes, book", "callback_data": "bk:yes"},
            {"text": "❌ No", "callback_data": "bk:no"},
        ]]}

    def _on_callback(self, cq: dict) -> None:
        chat_id = cq["from"]["id"]
        data = cq.get("data") or ""
        try:
            self.api.answer_callback_query(cq["id"], "…")
        except TelegramError as e:
            _log(f"[telegram] answerCallbackQuery failed: {e}")
        if chat_id not in self.chat_ids:
            return
        if data.startswith("date:"):
            date = datetime.strptime(data[5:], "%Y-%m-%d")
            self._show_slots(chat_id, date)
        elif data.startswith("slot:"):
            st = self.pending.get(chat_id, {})
            if st.get("action") != "pick_slot":
                self._send(chat_id, "Flow expired — start again with /book.")
                return
            try:
                slot = st["slots"][int(data[5:])]
            except (ValueError, IndexError):
                self._send(chat_id, "Flow expired — start again with /book.")
                return
            st["action"] = "confirm"
            st["slot"] = slot
            self._send(chat_id, f"Book {slot['label']} on {st['date']}?",
                       self._confirm_book_keyboard())
        elif data == "bk:yes":
            st = self.pending.get(chat_id, {})
            if st.get("action") != "confirm":
                self._send(chat_id, "Nothing to confirm — start with /book.")
                return
            self._do_book(chat_id, st["date"], st["slot"])
        elif data == "bk:no":
            self.pending.pop(chat_id, None)
            self._send(chat_id, "Aborted — nothing was booked.")
        elif data.startswith("cx:"):
            self.pending[chat_id] = {"action": "confirm_cancel",
                                     "cancel_id": data[3:]}
            self._send(chat_id, "Cancel this booking?",
                       {"inline_keyboard": [[
                           {"text": "✅ Yes, cancel",
                            "callback_data": "cxc:yes"},
                           {"text": "❌ Keep it", "callback_data": "cxc:no"},
                       ]]})
        elif data == "cxc:yes":
            st = self.pending.get(chat_id, {})
            if st.get("action") != "confirm_cancel":
                self._send(chat_id, "Nothing to cancel — see /mybookings.")
                return
            try:
                s = get_authenticated_session(self.cfg)
                ok, msg = cancel_booking(s, st["cancel_id"])
            except RuntimeError as e:
                self._send(chat_id, f"⚠️ {e}")
                return
            self.pending.pop(chat_id, None)
            if ok:
                self._send(chat_id, "✅ Booking cancelled.")
            else:
                self._send(chat_id, f"❌ Cancel failed: {msg}")
        elif data == "cxc:no":
            self.pending.pop(chat_id, None)
            self._send(chat_id, "Kept — booking untouched.")
        else:
            self._send(chat_id, "Unknown action — start again with /help.")

    def _do_book(self, chat_id: int, date_str: str, slot: dict) -> None:
        self._send(chat_id, "⏳ Submitting booking…")
        try:
            s = get_authenticated_session(self.cfg)
            html = fetch_booking_page(s, self.cfg)
            meta = parse_booking_meta(html)
            submit_booking(s, self.cfg, meta, date_str, slot,
                           self.cfg.get("description", "Padel booking"))
            ok = verify_booking_created(s, date_str, slot["start"])
        except RuntimeError as e:
            self.pending.pop(chat_id, None)
            self._send(chat_id, f"⚠️ {e}\n\nSend /login to start a fresh "
                                f"login.")
            return
        except Exception as e:  # pylint: disable=broad-exception-caught
            self.pending.pop(chat_id, None)
            self._send(chat_id, f"❌ Booking error: {e}")
            return
        record_booking(date_str, slot["label"], ok)
        self.pending.pop(chat_id, None)
        if ok:
            self._send(chat_id, f"✅ BOOKED: {slot['label']} on {date_str}\n\n"
                                f"Verified — it now appears in your "
                                f"'My Bookings'.")
        else:
            self._send(chat_id, f"❌ Booking FAILED for {slot['label']} on "
                                f"{date_str}.\nThe portal did not create it "
                                f"(check the daemon/terminal log for the "
                                f"portal's error).")


# --------------------------------------------------------------------------- #
# Daemon integration: notifications + automatic OTP re-login
# --------------------------------------------------------------------------- #
def _wait_for_otp(api: TelegramAPI, chat_ids: list, deadline: float,
                  reprompt_every: int = 300) -> str | None:
    """Poll updates until a whitelisted user replies with a numeric code.

    Returns the digits, or None when *deadline* (a ``time.time()`` value)
    passes. Tracks its own update offset so a restart mid-wait never
    re-reads an old code.
    """
    offset: int | None = None
    last_prompt = time.time()
    while time.time() < deadline:
        try:
            updates = api.get_updates(offset=offset, timeout=25)
        except TelegramError as e:
            if e.code == 409:
                _log("[telegram] 409 while waiting for OTP: another poller "
                      "is active; retrying")
                time.sleep(10)
            else:
                _log(f"[telegram] poll error while waiting for OTP: {e}")
                time.sleep(5)
            continue
        if updates:
            offset = updates[-1]["update_id"] + 1
        for u in updates:
            m = u.get("message") or {}
            if m.get("chat", {}).get("id") not in chat_ids:
                continue
            digits = re.sub(r"\D", "", m.get("text") or "")
            if 4 <= len(digits) <= 8:
                return digits
        if time.time() - last_prompt >= reprompt_every:
            last_prompt = time.time()
            for chat in chat_ids:
                try:
                    api.send_message(chat, "⏳ Still waiting for the OTP "
                                           "code from your email…")
                except TelegramError:
                    pass
    return None


def relogin_via_telegram(cfg: dict, max_attempts: int = 3,
                         wait_seconds: int = 900) -> bool:
    """Automatic re-login for the daemon, driven through Telegram.

    Triggers a fresh OTP email, asks the whitelisted user(s) for the code in
    chat, completes the login and verifies the session with a keep-alive.
    Returns True only when the session is valid afterwards. Never raises.
    """
    token, chat_ids = get_telegram_cfg(cfg)
    if not token or not chat_ids:
        return False
    api = TelegramAPI(token)
    for attempt in range(1, max_attempts + 1):
        try:
            login_start(cfg)
        except Exception as e:  # pylint: disable=broad-exception-caught
            _log(f"[telegram] could not trigger the OTP email: {e}")
            return False
        for chat in chat_ids:
            try:
                api.send_message(chat, "⚠️ Session expired — I triggered a "
                                       "fresh login.\n📧 Check your email "
                                       "and reply here with the 6-digit OTP "
                                       "code.")
            except TelegramError as e:
                _log(f"[telegram] ask to {chat} failed: {e}")
        otp = _wait_for_otp(api, chat_ids, time.time() + wait_seconds)
        if not otp:
            _log("[telegram] no OTP received in time; will retry on the "
                  "next keep-alive failure")
            return False
        try:
            login_finish(cfg, otp)
        except Exception as e:  # pylint: disable=broad-exception-caught
            _log(f"[telegram] OTP attempt {attempt}/{max_attempts} failed: "
                  f"{e}")
            continue
        try:
            keepalive(cfg)   # verify the session is really valid
        except Exception as e:  # pylint: disable=broad-exception-caught
            _log(f"[telegram] login finished but session still invalid: "
                  f"{e}")
            continue
        for chat in chat_ids:
            try:
                api.send_message(chat, "✅ Session restored — the daemon is "
                                       "healthy again.")
            except TelegramError:
                pass
        return True
    return False
