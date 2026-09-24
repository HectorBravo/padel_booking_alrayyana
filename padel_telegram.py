#!/usr/bin/env python3
"""
Telegram integration for the padel booking automation.

Talks to the Telegram Bot API (https://core.telegram.org/bots/api) using
curl_cffi (no extra dependencies). Used by:
  - `python padel_booking.py telegram`  (interactive bot, PadelBot, which
    also runs the background autobooking scheduler)

Config keys (config.json, gitignored):
  "telegram_bot_token": "123456:ABC..."   # from @BotFather
  "telegram_chat_ids":  [123456789]       # allowed user chat ids (whitelist)
"""

import html
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
    is_cancellable, is_cancelled, _booking_info, parse_date, to_api_date,
    preferred_slots_for_day, pick_best_slot, run_autobook_loop,
    save_config, WEEKDAY_NAME, ts_prefix,
)

HERE = Path(__file__).resolve().parent
BOT_API = "https://api.telegram.org"
STATE_FILE = HERE / "telegram_state.json"

# Valid weekday names (lowercase) for the /prefs editor, e.g. "monday".."sunday".
_DAY_NAMES = set(WEEKDAY_NAME.values())

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
    """Print a timestamped local log line; safe under pythonw (no console)."""
    if sys.stdout is not None:
        print(f"{ts_prefix()} {msg}", flush=True)


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
                     reply_markup: dict | None = None,
                     parse_mode: str | None = None) -> dict:
        """Send a text message to *chat_id*.

        *reply_markup* is an inline-keyboard dict; it is serialized to the
        JSON string the Bot API expects (omitted entirely when None).
        *parse_mode* is the Bot API parse mode (e.g. "HTML"); omitted when
        None so plain-text messages are unaffected.
        """
        params = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            params["reply_markup"] = json.dumps(reply_markup)
        if parse_mode is not None:
            params["parse_mode"] = parse_mode
        return self._call("sendMessage", **params)

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
        # Background autobooking scheduler (started on boot, can be toggled
        # with /startautobook and /stopautobook).
        self._sched_stop = threading.Event()
        self._sched_thread: threading.Thread | None = None

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
            {"command": "mybookings",
             "description": "List your bookings (cancel from here)"},
            {"command": "autobook",
             "description": "Book a preferred slot (optional: date)"},
            {"command": "startautobook",
             "description": "Start the background autobooking"},
            {"command": "stopautobook",
             "description": "Stop the background autobooking"},
            {"command": "prefs",
             "description": "Set days & preferred slots for autobooking"},
        ]
        try:
            self.api.set_my_commands(commands)
        except TelegramError as e:
            _log(f"[telegram] warning: command menu not set: {e}")
        # Start the background autobooking scheduler (keep-alive + booking
        # race) so `python padel_booking.py telegram` also autobooks and
        # pushes Telegram notifications (booked / failed / OTP expiry).
        # It can be toggled at runtime with /startautobook & /stopautobook.
        self._start_autobook()
        self._notify("🤖 Padel bot started — autobooking is active.\n"
                     "I'll book your preferred slot automatically when the "
                     "window opens and let you know right here.")
        worker = threading.Thread(target=self._poll_loop, daemon=True)
        worker.start()
        try:
            while worker.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            _log("\n[telegram] Ctrl+C — shutting down…")
            self._sched_stop.set()
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
              reply_markup: dict | None = None,
              parse_mode: str | None = None) -> None:
        """Send a message to *chat_id*; log failures instead of raising."""
        try:
            self.api.send_message(chat_id, text, reply_markup, parse_mode)
        except TelegramError as e:
            _log(f"[telegram] send to {chat_id} failed: {e}")

    # ---- text commands ----------------------------------------------------- #
    def _on_message(self, m: dict) -> None:
        """Handle a text message: a pending OTP first, then /commands."""
        chat_id = m["chat"]["id"]
        text = (m.get("text") or "").strip()

        # Pending OTP flow: the next message should be the code from email.
        # If a background autobook re-login is waiting on this code, signal it.
        if self.pending.get(chat_id, {}).get("action") == "otp":
            digits = re.sub(r"\D", "", text)
            st = self.pending.get(chat_id, {})
            waiter = st.get("_waiter")
            state = st.get("_state")
            if 4 <= len(digits) <= 8:
                self.pending.pop(chat_id, None)
                try:
                    login_finish(self.cfg, digits)
                    self._send(chat_id, "✅ Login successful — session saved.")
                    if state is not None:
                        state["ok"] = True
                    if waiter is not None:
                        waiter.set()
                except Exception as e:  # pylint: disable=broad-exception-caught
                    self._send(chat_id, f"❌ Login failed: {e}\n\n"
                                        f"Send /login to try again.")
                    if state is not None:
                        state["ok"] = False
                        state["error"] = str(e)
                    if waiter is not None:
                        waiter.set()
            else:
                self._send(chat_id, "That doesn't look like the OTP code. "
                                    "Reply with the 6-digit code from your "
                                    "email (or /login to restart).")
            return

        # Pending /prefs "add slot" prompt: the next message is the slot time.
        if self.pending.get(chat_id, {}).get("action") == "prefs_addslot":
            st = self.pending.get(chat_id, {})
            day_num = st.get("day")
            if text.lower() in ("/prefs", "back", "cancel"):
                st["action"] = "prefs"
                st.pop("day", None)
                self._render_prefs_menu(chat_id)
            else:
                self._prefs_add_slot(chat_id, day_num, text)
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
        elif cmd == "/autobook":
            self._on_autobook(chat_id, args)
        elif cmd == "/startautobook":
            self._on_startautobook(chat_id)
        elif cmd == "/stopautobook":
            self._on_stopautobook(chat_id)
        elif cmd == "/prefs":
            self._on_prefs(chat_id)
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
                   "/mybookings — list your bookings (cancel from here)\n"
                   "/autobook [date] — book a preferred slot (default: +6d)\n"
                   "/startautobook — start the background autobooking\n"
                   "/stopautobook — stop the background autobooking\n"
                   "/prefs — set the days & preferred slots for autobooking\n"
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
            # Ask the portal for Approved bookings only (server-side filter),
            # so we fetch far fewer rows/pages. The client-side filter below
            # stays as a safety net.
            bookings = fetch_my_bookings(s, status="Approved")
        except RuntimeError as e:
            self._send(chat_id, f"⚠️ {e}\n\nSend /login to start a fresh "
                                f"login.")
            return
        # Only show live bookings — hide cancelled/rejected ones (so every
        # entry here is already approved; no need to show the status tag).
        bookings = [b for b in bookings if not is_cancelled(b)]
        if not bookings:
            self._send(chat_id, "No upcoming bookings.")
            return
        # Most-distant-future first; past bookings sink to the bottom.
        bookings = sorted(bookings,
                          key=lambda b: b["from_dt"] or datetime.min,
                          reverse=True)
        now = datetime.now()
        lines = []
        for i, b in enumerate(bookings, 1):
            info = html.escape(_booking_info(b))
            if b["from_dt"] is not None and b["from_dt"] < now:
                lines.append(f"{i}. <s>{info}</s>")
            else:
                lines.append(f"{i}. {info}")
        text = "Your bookings:\n\n" + "\n".join(lines)
        rows = [[{"text": f"❌ Cancel {b['from_dt']:%a %d %b %Y} {b['from_dt']:%H:%M} - {b['to_dt']:%H:%M}",
                  "callback_data": f"cx:{b['details_id']}"}]
                for i, b in enumerate(bookings, 1) if is_cancellable(b)]
        self._send(chat_id, text, {"inline_keyboard": rows} if rows else None,
                   parse_mode="HTML")

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
        preferred = preferred_slots_for_day(self.cfg, date.strftime("%A"))
        if not preferred:
            self._send(chat_id, f"No preferred slots are configured for "
                                f"{date:%A}. Add them to 'preferred_slots' "
                                f"in config.json, or use /book to pick one "
                                f"manually.")
            return
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
        elif data.startswith("pf:"):
            self._on_prefs_callback(chat_id, data)
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

    # ---- background autobooking (keep-alive + booking race) -------------- #
    def _notify(self, text: str) -> None:
        """Broadcast a notification to all whitelisted chats (never raises)."""
        for chat in self.chat_ids:
            try:
                self.api.send_message(chat, text)
            except TelegramError as e:
                _log(f"[telegram] notify {chat} failed: {e}")

    def _sched_log(self, msg: str) -> None:
        _log(f"[autobook] {msg}")

    def _relogin(self) -> bool:
        """Restore an expired session using the bot's own OTP capture.

        Triggers a fresh OTP email, asks the user to reply with the code in
        chat (captured by the normal poll loop — no second getUpdates poller,
        so no 409 conflict), and blocks until the code arrives or it times
        out. Returns True only when the login finished successfully.
        """
        try:
            login_start(self.cfg)
        except Exception as e:  # pylint: disable=broad-exception-caught
            self._notify(f"❌ Could not start re-login: {e}")
            return False
        event = threading.Event()
        state = {"ok": False, "error": None}
        for chat in self.chat_ids:
            self.pending[chat] = {"action": "otp", "_waiter": event,
                                  "_state": state}
        self._notify("⚠️ Session expired — I triggered a fresh login.\n"
                     "📧 Check your email and reply here with the 6-digit "
                     "OTP code.")
        if not event.wait(timeout=900):
            for chat in self.chat_ids:
                st = self.pending.get(chat, {})
                if st.get("action") == "otp" and st.get("_waiter") is event:
                    self.pending.pop(chat, None)
            self._notify("⏳ No OTP received in time; will retry on the "
                         "next pass.")
            return False
        if state["ok"]:
            self._notify("✅ Session restored — autobooking is healthy "
                         "again.")
        else:
            self._notify(f"❌ Re-login failed: "
                         f"{state.get('error') or 'unknown'}\n"
                         f"Send /login to try again.")
        return state["ok"]

    def _autobook_running(self) -> bool:
        """True when the background autobooking scheduler is alive."""
        return (self._sched_thread is not None
                and self._sched_thread.is_alive())

    def _start_autobook(self) -> bool:
        """Start the background autobooking scheduler.

        Returns True if it was started, False if it is already running.
        """
        if self._autobook_running():
            return False
        self._sched_stop.clear()
        self._sched_thread = threading.Thread(target=self._autobook_loop,
                                              daemon=True)
        self._sched_thread.start()
        return True

    def _stop_autobook(self) -> bool:
        """Stop the background autobooking scheduler.

        Returns True if a running scheduler was asked to stop, False if it
        was not running.
        """
        if not self._autobook_running():
            return False
        self._sched_stop.set()
        return True

    def _autobook_loop(self) -> None:
        """Background autobooking: keep the session alive and book the
        target day when its window opens. Notifies via Telegram on
        success/failure and on OTP expiry."""
        _log("[autobook] autobooking scheduler started "
             "(keep-alive + booking race)")
        try:
            run_autobook_loop(self.cfg, self._sched_log,
                              notify=self._notify, relogin=self._relogin,
                              dry_run=False, stop_event=self._sched_stop)
        except Exception as e:  # pylint: disable=broad-exception-caught
            _log(f"[autobook] scheduler stopped with error: {e}")

    def _on_startautobook(self, chat_id: int) -> None:
        if self._start_autobook():
            self._send(chat_id, "✅ Autobooking started — I'll book your "
                                "preferred slot when the window opens.")
        else:
            self._send(chat_id, "Autobooking is already running.")

    def _on_stopautobook(self, chat_id: int) -> None:
        if self._stop_autobook():
            self._send(chat_id, "⏹ Autobooking stopped. Send /startautobook "
                                "to resume.")
        else:
            self._send(chat_id, "Autobooking is not running.")

    # ---- /prefs — edit target days & preferred slots ----------------------- #
    def _on_prefs(self, chat_id: int) -> None:
        """Open the interactive preferences editor (days + per-day slots)."""
        self.pending[chat_id] = {"action": "prefs",
                                 "slots": self._prefs_initial_slots()}
        self._render_prefs_menu(chat_id)

    def _prefs_initial_slots(self) -> dict:
        """The current preferred_slots as a per-day dict (lowercase keys).

        Handles both the recommended per-day dict and the legacy flat-list
        form (whose days come from ``target_weekdays``).
        """
        slots = self.cfg.get("preferred_slots")
        result = {}
        if isinstance(slots, dict):
            for k, v in slots.items():
                day = str(k).strip().lower()
                if day in _DAY_NAMES:
                    result[day] = list(v) if isinstance(v, list) else []
        elif isinstance(slots, list) and slots:
            days = self.cfg.get("target_weekdays", ["Sunday", "Tuesday"])
            for k in days:
                day = str(k).strip().lower()
                if day in _DAY_NAMES:
                    result[day] = list(slots)
        return result

    def _render_prefs_menu(self, chat_id: int) -> None:
        slots = self.pending[chat_id]["slots"]
        lines = ["⚙️ Autobook preferences", ""]
        lines.append("Days to book (tap a day to edit its slots):")
        for n in range(7):
            name = WEEKDAY_NAME[n]
            if name in slots and slots[name]:
                lines.append(f"  ✅ {name.capitalize()} — "
                             f"{', '.join(slots[name])}")
            elif name in slots:
                lines.append(f"  ⚠️ {name.capitalize()} — (no slots yet)")
            else:
                lines.append(f"  ⬜ {name.capitalize()}")
        lines.append("")
        lines.append("Saved changes apply to the background autobooking.")
        text = "\n".join(lines)

        def day_btn(n: int) -> dict:
            name = WEEKDAY_NAME[n]
            enabled = name in slots
            return {"text": ("✅ " if enabled else "⬜ ") + name.capitalize(),
                    "callback_data": f"pf:day:{n}"}

        rows = [
            [day_btn(0), day_btn(1), day_btn(2)],
            [day_btn(3), day_btn(4), day_btn(5)],
            [day_btn(6)],
            [{"text": "💾 Save", "callback_data": "pf:save"},
             {"text": "❌ Cancel", "callback_data": "pf:cancel"}],
        ]
        self._send(chat_id, text, {"inline_keyboard": rows})

    def _render_prefs_day(self, chat_id: int, day_num: int) -> None:
        slots = self.pending[chat_id]["slots"]
        name = WEEKDAY_NAME[day_num]
        day_slots = slots.get(name, [])
        lines = [f"📅 {name.capitalize()} — preferred slots (top = tried first)",
                 ""]
        if day_slots:
            lines.extend(f"  {i + 1}. {t}" for i, t in enumerate(day_slots))
        else:
            lines.append("  (no slots yet — add one below)")
        lines.append("")
        lines.append("Tap a slot to remove it.")
        text = "\n".join(lines)

        enabled = name in slots
        rows = [[{"text": ("🔄 Disable " if enabled else "🔄 Enable ")
                          + name.capitalize(),
                  "callback_data": f"pf:toggle:{day_num}"}]]
        for i in range(0, len(day_slots), 3):
            chunk = day_slots[i:i + 3]
            rows.append([
                {"text": f"❌ {t}", "callback_data": f"pf:rm:{day_num}:{i + j}"}
                for j, t in enumerate(chunk)
            ])
        rows.append([{"text": "➕ Add slot",
                      "callback_data": f"pf:add:{day_num}"}])
        rows.append([{"text": "⬅ Back to days", "callback_data": "pf:back"}])
        self._send(chat_id, text, {"inline_keyboard": rows})

    def _prefs_toggle_day(self, chat_id: int, day_num: int) -> None:
        st = self.pending[chat_id]
        slots = st["slots"]
        name = WEEKDAY_NAME[day_num]
        if name in slots:
            del slots[name]
            st["action"] = "prefs"
            st.pop("day", None)
            self._render_prefs_menu(chat_id)
        else:
            slots[name] = ["20:00"]
            st["action"] = "prefs_day"
            st["day"] = day_num
            self._render_prefs_day(chat_id, day_num)

    def _prefs_remove_slot(self, chat_id: int, day_num: int, idx: int) -> None:
        slots = self.pending[chat_id]["slots"]
        name = WEEKDAY_NAME[day_num]
        day_slots = slots.get(name, [])
        if 0 <= idx < len(day_slots):
            day_slots.pop(idx)
        self._render_prefs_day(chat_id, day_num)

    def _prefs_add_slot_prompt(self, chat_id: int, day_num: int) -> None:
        st = self.pending[chat_id]
        st["action"] = "prefs_addslot"
        st["day"] = day_num
        name = WEEKDAY_NAME[day_num].capitalize()
        self._send(chat_id,
                   f"Type the start time to add to {name} (e.g. 20:00), "
                   f"or send /prefs to go back.")

    def _prefs_add_slot(self, chat_id: int, day_num: int, time_str: str) -> None:
        st = self.pending[chat_id]
        slots = st["slots"]
        name = WEEKDAY_NAME[day_num]
        t = time_str.strip()
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", t):
            self._send(chat_id, "That doesn't look like a time "
                                "(use HH:MM, e.g. 20:00).")
            st["action"] = "prefs_addslot"
            st["day"] = day_num
            return
        h, m = t.split(":")
        t = f"{int(h):02d}:{m}"
        if name not in slots:
            slots[name] = []
        if t not in slots[name]:
            slots[name].append(t)
        st["action"] = "prefs_day"
        st["day"] = day_num
        self._render_prefs_day(chat_id, day_num)

    def _prefs_save(self, chat_id: int) -> None:
        st = self.pending.get(chat_id, {})
        if st.get("action") not in ("prefs", "prefs_day", "prefs_addslot"):
            self._send(chat_id, "No preferences flow in progress — send /prefs.")
            return
        slots = {d: s for d, s in st.get("slots", {}).items() if s}
        self.cfg["preferred_slots"] = slots
        try:
            save_config(self.cfg)
        except OSError as e:
            self._send(chat_id, f"⚠️ Could not save config: {e}")
            return
        self.pending.pop(chat_id, None)
        days = ", ".join(d.capitalize() for d in slots) or "(none)"
        self._send(chat_id,
                   f"✅ Preferences saved.\nDays to book: {days}\n"
                   f"The background autobooking will use these going forward.")

    def _prefs_cancel(self, chat_id: int) -> None:
        self.pending.pop(chat_id, None)
        self._send(chat_id, "Cancelled — no changes were saved.")

    def _on_prefs_callback(self, chat_id: int, data: str) -> None:
        """Route the /prefs inline-button callbacks (pf:*)."""
        parts = data.split(":")
        action = parts[1]
        st = self.pending.get(chat_id, {})
        if st.get("action") not in ("prefs", "prefs_day", "prefs_addslot"):
            self._send(chat_id, "No preferences flow in progress — send /prefs.")
            return
        try:
            if action == "day":
                day_num = int(parts[2])
                st["action"] = "prefs_day"
                st["day"] = day_num
                self._render_prefs_day(chat_id, day_num)
            elif action == "toggle":
                self._prefs_toggle_day(chat_id, int(parts[2]))
            elif action == "rm":
                self._prefs_remove_slot(chat_id, int(parts[2]), int(parts[3]))
            elif action == "add":
                self._prefs_add_slot_prompt(chat_id, int(parts[2]))
            elif action == "back":
                st["action"] = "prefs"
                st.pop("day", None)
                self._render_prefs_menu(chat_id)
            elif action == "save":
                self._prefs_save(chat_id)
            elif action == "cancel":
                self._prefs_cancel(chat_id)
        except (ValueError, IndexError):
            self._send(chat_id, "Invalid button — send /prefs to start again.")
