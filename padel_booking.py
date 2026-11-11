#!/usr/bin/env python3
"""
Padel court booking automation for the Asteco community portal
(https://myportal.asteco.com).

The portal is a server-rendered (CodeIgniter) app. Authentication is:
  1. POST /login/checkLogin  with obfuscated username/password
  2. Server emails a 6-digit OTP and redirects to /login/checkOtp
  3. POST /login/verifyOtp   with the OTP + hidden fields (AJAX)
  4. Response is a redirect URL; following it establishes the session

Credentials are read from config.json (kept OUTSIDE this script, chmod 600).
The authenticated session (cookies) is persisted to session.json (chmod 600)
so the OTP is only needed once per session lifetime.

Usage:
  python3 padel_booking.py login-start             # submit credentials, trigger OTP email
  python3 padel_booking.py login-finish <OTP>      # verify OTP, save the session
  python3 padel_booking.py login-status            # show whether the saved session is valid
  python3 padel_booking.py explore                 # dump the authenticated booking page HTML
  python3 padel_booking.py window                  # show the portal's bookable dates
  python3 padel_booking.py slots 2026-09-26        # list available time slots for a date
  python3 padel_booking.py slots 2026-09-26 --from 18:00   # ...only from 6pm on
  python3 padel_booking.py book 2026-09-26 "18:00-19:00" [description] [--verbose]
  python3 padel_booking.py pick                    # interactive: browse all
  python3 padel_booking.py pick --from 18:00       #   ...highlight 6pm+ slots green
                                                   # available slots (the
                                                   # portal's 7-day window) and
                                                   # choose one
  python3 padel_booking.py mybookings             # list your portal bookings
  python3 padel_booking.py mybookings 2026-09-26  #   ...for one date
  python3 padel_booking.py cancel                 # interactive: pick one to
                                                   # cancel (future bookings)
  python3 padel_booking.py cancel 7938824         #   ...or cancel by booking-id
  python3 padel_booking.py autobook [date] [--dry-run]   # book a preferred slot now
  python3 padel_booking.py keepalive               # refresh the persisted session
  python3 padel_booking.py telegram                # interactive Telegram bot
                                                    # (also runs the background
                                                    # autobooking scheduler)

Auto-booking (runs inside the Telegram bot):
  Two facts drive it and must not be conflated. The portal's booking window is
  always 7 days wide (today .. today+6, config key booking_window_days) and is
  the HARD limit of what can be booked now. Which of those days are OPEN comes
  from the booking page itself: its "Check in date" <select> lists them, and a
  date that is not listed cannot be booked right now (the POST is rejected),
  even though the slot endpoint still answers for it. The scheduler therefore
  attempts every desired weekday whose date is inside the 7-day window: the
  dates the picker already offers first (freshest first), then the dates inside
  the window that the picker has not opened yet, which are NOT skipped - each
  one refreshes the portal every slot_retry_minutes (default 2 min) until the
  portal lists it AND a preferred slot is free, so a program started while the
  target day is still closed enters the same wait. Only a date beyond the 7-day
  window stops immediately, since waiting for it could never succeed. It keeps
  the session alive periodically and pushes Telegram notifications (booked /
  failed / no slot / not opened yet / outside window / OTP re-login needed).
  Start or stop it at any time with /startautobook and /stopautobook.
  All bot settings live in config.json (preferred_slots, slot_retry_minutes,
  booking_max_wait_minutes, keepalive_minutes, description).
  'preferred_slots' is a per-day map; its keys are the days to auto-book, e.g.
      "preferred_slots": {"Sunday": ["20:00", "19:00", "21:00"],
                          "Tuesday": ["20:00", "21:00"]}
  (a plain list is also accepted and applies to every day, in which case
  the days come from 'target_weekdays').
  'min_start_hour' (default "18:00") is the time cutoff: 'slots' lists only
  slots from that time on, while 'pick' shows all but highlights matching ones
  in green. Override per-run with '--from HH:MM' (e.g. '--from 00:00').
"""

import base64
import ctypes
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from curl_cffi import requests  # browser TLS fingerprint (impersonate) to pass Akamai
from bs4 import BeautifulSoup
from google_calendar import sync_booking, sync_cancellation

HERE = Path(__file__).resolve().parent
BASE_URL = "https://myportal.asteco.com"
# Default HTTP timeout (seconds) for every portal request. curl_cffi's
# Session defaults to 30s, which is too tight for this portal: when it is
# sluggish, calls such as the OTP login (POST /login/checkLogin) or the slow
# /booking/myBooking endpoint can exceed 30s and curl aborts with
# "curl: (28) Operation timed out after 30000 ms". We raise the session-wide
# default to 90s so every request that does not override it gets that.
PORTAL_TIMEOUT = 90
CONFIG_FILE = HERE / "config.json"
SESSION_FILE = HERE / "session.json"


# --------------------------------------------------------------------------- #
# Terminal colors (ANSI escape codes)
# --------------------------------------------------------------------------- #
# Colors are emitted only when stdout is an interactive terminal and the user
# hasn't opted out via NO_COLOR, so piped/log output stays clean.
_COLORS = {
    "grey":   "\033[90m",
    "red":    "\033[31m",
    "green":  "\033[32m",
    "strike": "\033[9m",
    "reset":  "\033[0m",
}
ANSI_STATE = {"ready": False}


def _ansi_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _enable_windows_ansi() -> None:
    """Enable VT (ANSI) processing on Windows consoles; no-op elsewhere."""
    if ANSI_STATE["ready"] or sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    ANSI_STATE["ready"] = True


def _paint(text: str, color: str) -> str:
    """Wrap *text* in an ANSI color; returns it unchanged when colors are off."""
    if not _ansi_enabled():
        return text
    _enable_windows_ansi()
    return f"{_COLORS[color]}{text}{_COLORS['reset']}"


# --------------------------------------------------------------------------- #
# Config / session persistence
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    """Load and validate config.json, exiting with a clear error if invalid."""
    if not CONFIG_FILE.exists():
        sys.exit(f"ERROR: config file not found at {CONFIG_FILE}")
    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    for key in ("email", "password"):
        if key not in cfg:
            sys.exit(f"ERROR: '{key}' missing from {CONFIG_FILE}")
    return cfg


def save_config(cfg: dict) -> None:
    """Write *cfg* back to config.json (used by the bot's /prefs command)."""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")


def new_session(restore: bool = True) -> requests.Session:
    """Create a Chrome-impersonating session, optionally restoring cookies."""
    # impersonate="chrome" presents a real Chrome TLS/HTTP2 fingerprint so the
    # portal's Akamai bot-detection grants a genuine authenticated session.
    # The session-wide default timeout is PORTAL_TIMEOUT (see module top); any
    # request that doesn't pass an explicit timeout inherits it.
    s = requests.Session(impersonate="chrome", timeout=PORTAL_TIMEOUT)
    s.headers.update({
        "Accept-Language": "en-US,en;q=0.9",
    })
    if restore:
        restore_cookies(s)
    return s


def restore_cookies(s: requests.Session) -> bool:
    """Load persisted cookies into the session. Returns True if any loaded."""
    if not SESSION_FILE.exists():
        return False
    try:
        data = json.loads(SESSION_FILE.read_text())
    except (OSError, ValueError):
        return False
    loaded = False
    for c in data.get("cookies", []):
        s.cookies.set(
            c["name"], c["value"],
            domain=c.get("domain", ".asteco.com"),
            path=c.get("path", "/"),
        )
        loaded = True
    return loaded


def save_session(s: requests.Session) -> None:
    """Persist the session's cookies to session.json (chmod 600)."""
    cookies = [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
        for c in s.cookies.jar
    ]
    SESSION_FILE.write_text(json.dumps(
        {"cookies": cookies, "saved_at": datetime.now().isoformat()}, indent=2))
    SESSION_FILE.chmod(0o600)


# --------------------------------------------------------------------------- #
# Shared HTTP session (connection reuse across actions)
# --------------------------------------------------------------------------- #
_SESSION: "requests.Session | None" = None   # long-lived keep-alive session
_SESSION_LOCK = threading.Lock()


def get_session() -> requests.Session:
    """Return the shared long-lived session, creating it on first use.

    Reusing one session (and its keep-alive connection) across actions
    avoids a fresh TLS/HTTP2 handshake + bot-detection round-trip on every
    command, and keeps the browser fingerprint consistent. Thread-safe.
    """
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            _SESSION = new_session()
        return _SESSION


def install_session(s: requests.Session) -> None:
    """Make *s* the shared session (e.g. the fresh one from a completed
    login), closing the previous one."""
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is not None and _SESSION is not s:
            try:
                _SESSION.close()
            except Exception:  # pylint: disable=broad-exception-caught
                pass
        _SESSION = s


def reset_session() -> None:
    """Discard the shared session (e.g. after a validation failure)."""
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is not None:
            try:
                _SESSION.close()
            except Exception:  # pylint: disable=broad-exception-caught
                pass
        _SESSION = None


# --------------------------------------------------------------------------- #
# HTTP timing (diagnostics) + booking-page meta cache
# --------------------------------------------------------------------------- #
_http_log = None  # callable(str) | None — set by the bot/loop/CLI


def set_http_log(fn) -> None:
    """Route per-request HTTP timing lines to *fn* (pass None to disable).

    Every portal GET/POST goes through ``_timed`` below, so the logs show
    exactly how long each request takes and whether the keep-alive connection
    is being reused (a reused connection should be markedly faster than the
    first request to a host).
    """
    global _http_log
    _http_log = fn


def _timed(method: str, url: str, call):
    """Run *call()* (one HTTP request) and log its wall-clock duration."""
    t0 = time.perf_counter()
    try:
        r = call()
    except Exception as e:  # pylint: disable=broad-exception-caught
        dt = time.perf_counter() - t0
        if _http_log:
            _http_log(f"[http] {method} {url} FAILED after {dt:.2f}s: {e}")
        raise
    dt = time.perf_counter() - t0
    status = getattr(r, "status_code", "?")
    if _http_log:
        _http_log(f"[http] {method} {url} -> {status} in {dt:.2f}s")
    return r


# The booking page's meta (unit_id / user_id / community_id / applicant_name /
# status) is stable for the life of a session — only the *slots* change, and
# those come from a separate POST. Caching the meta per session lets us skip
# the second booking-page GET that every action used to make (a login-check
# GET followed by a meta GET).
_META_CACHE = None  # (session, monotonic_ts, meta) | None
_META_TTL = 120     # seconds


def _meta_cached(s):
    """Return the cached meta for *s* if still fresh, else None."""
    if (_META_CACHE is not None and _META_CACHE[0] is s
            and (time.monotonic() - _META_CACHE[1]) < _META_TTL):
        return _META_CACHE[2]
    return None


def _meta_store(s, meta) -> None:
    global _META_CACHE
    _META_CACHE = (s, time.monotonic(), meta)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def encode(value: str) -> str:
    """Replicate the portal's JS encode() used for login credentials.

    JS:  btoa(btoa(str)) -> XOR each char code with 10 -> btoa()
    """
    s = base64.b64encode(value.encode("utf-8")).decode("ascii")
    s = base64.b64encode(s.encode("ascii")).decode("ascii")
    s = "".join(chr(ord(c) ^ 10) for c in s)
    return base64.b64encode(s.encode("latin-1")).decode("ascii")


def is_logged_in(s: requests.Session, cfg: dict) -> bool:
    """Return True if the current session is still authenticated.

    Checks the same page the portal itself lands on after a successful
    login (``GET /dashboard/index``): an authenticated session gets a 200,
    an expired one is redirected back to ``/login``. This is faster and
    more direct than the old booking-page probe
    (``/asset/assetbooking/{id}``) and mirrors what the browser does.
    ``cfg`` is kept in the signature for call-site compatibility.
    """
    url = f"{BASE_URL}/dashboard/index"
    r = _timed("GET", url,
               lambda: s.get(url, allow_redirects=True))
    return r.status_code == 200 and "/login" not in r.url.lower()


def _hidden_field(soup: BeautifulSoup, name: str) -> str:
    """Return the value of a hidden input with the given name, or ''."""
    el = soup.find("input", attrs={"name": name})
    if el is None:
        return ""
    value = el.get("value")
    return value if isinstance(value, str) else ""


PENDING_FILE = HERE / "pending_login.json"


def _save_cookies(s: requests.Session) -> list:
    return [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
        for c in s.cookies.jar
    ]


def _load_cookies(s: requests.Session, cookies: list) -> None:
    for c in cookies:
        s.cookies.set(
            c["name"], c["value"],
            domain=c.get("domain", ".asteco.com"),
            path=c.get("path", "/"),
        )


def login_start(cfg: dict) -> None:
    """Phase 1: submit credentials, receive the OTP email, stash pending state."""
    # Clean session: stale cookies from a previous (expired) session must not
    # be sent, or the server refuses to establish the new one.
    s = new_session(restore=False)

    # checkLogin -> redirects to /login/checkOtp (OTP emailed)
    # (each step goes through _timed so the log shows exactly which
    # request hangs or fails — see the _http_log wiring in main())
    _timed("GET", BASE_URL + "/", lambda: s.get(BASE_URL + "/"))
    r = _timed("POST", BASE_URL + "/login/checkLogin",
               lambda: s.post(BASE_URL + "/login/checkLogin", data={
                   "username": encode(cfg["email"]),
                   "password": encode(cfg["password"]),
               }))
    if "checkOtp" not in r.url:
        if is_logged_in(s, cfg):
            save_session(s)
            print("  -> Already authenticated. Session saved.")
            return
        raise RuntimeError(f"Login did not reach OTP step (url={r.url}, "
                           f"status={r.status_code})")

    # parse hidden fields from the OTP page
    soup = BeautifulSoup(r.text, "html.parser")
    otp_fields = {
        k: _hidden_field(soup, k)
        for k in ("id_user", "phone", "email", "first_name", "last_name",
                  "password", "username", "name_usr")
    }

    # stash the pending state (cookies + otp fields) so phase 2 can resume
    state = {
        "cookies": _save_cookies(s),
        "otp_fields": otp_fields,
        "created_at": datetime.now().isoformat(),
    }
    PENDING_FILE.write_text(json.dumps(state, indent=2))
    PENDING_FILE.chmod(0o600)

    print("  -> OTP sent to your email. Check your inbox.")
    print("  -> When ready, run: python3 padel_booking.py login-finish <OTP>")


def login_finish(cfg: dict, otp: str) -> requests.Session:
    """Phase 2: verify the OTP and establish the authenticated session."""
    if not PENDING_FILE.exists():
        raise RuntimeError("No pending login. Run 'login-start' first.")
    state = json.loads(PENDING_FILE.read_text())

    # Clean session (no stale session.json cookies), then apply the pending
    # cookies captured during login_start.
    s = new_session(restore=False)
    _load_cookies(s, state["cookies"])
    otp_fields = state["otp_fields"]

    # verifyOtp (AJAX) -> returns redirect URL, or '2' (expired) / '3' (invalid)
    r2 = _timed("POST", BASE_URL + "/login/verifyOtp",
                lambda: s.post(BASE_URL + "/login/verifyOtp",
                               data={"user_otp": otp, **otp_fields}))
    resp = r2.text.strip()
    if resp == "2":
        raise RuntimeError("OTP expired. Run 'login-start' to get a fresh OTP.")
    if resp == "3":
        raise RuntimeError("OTP invalid. Re-run 'login-start' and retry.")

    # follow the redirect to establish the session
    target = resp if resp.startswith("http") else BASE_URL + resp
    _timed("GET", target, lambda: s.get(target, allow_redirects=True))

    if not is_logged_in(s, cfg):
        raise RuntimeError("OTP accepted but session still not authenticated.")

    save_session(s)
    install_session(s)
    # A fresh login may be for a different account: reset the in-memory
    # booking state so the next action re-reads it for the current account.
    _STATE["meta"] = None
    _STATE["bookings"] = []
    PENDING_FILE.unlink(missing_ok=True)
    print("  -> Login successful. Session saved to session.json")
    return s


def get_authenticated_session(cfg: dict) -> requests.Session:
    """Return a valid authenticated session (shared, keep-alive), prompting
    to re-login if needed."""
    s = get_session()
    with _SESSION_LOCK:
        if is_logged_in(s, cfg):
            save_session(s)
            return s
    reset_session()
    raise RuntimeError(
        "Session missing/expired. Run 'login-start' then 'login-finish <OTP>' "
        "to authenticate before using slots/book.")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_login_start(cfg: dict) -> None:
    """CLI: start the two-step login (sends the OTP email)."""
    print("Starting login to", BASE_URL)
    login_start(cfg)


def cmd_login_finish(cfg: dict, args: list) -> None:
    """CLI: finish the two-step login with the OTP from the email."""
    if not args:
        sys.exit("ERROR: provide the OTP, e.g. login-finish 123456")
    login_finish(cfg, args[0].strip())


def cmd_login_status(cfg: dict) -> None:
    """CLI: report whether the saved session is still valid."""
    s = get_session()
    ok = is_logged_in(s, cfg)
    print("Session valid:", ok)
    if ok:
        save_session(s)
        print("Session refreshed/saved.")
    else:
        reset_session()
        print("Run 'login-start' then 'login-finish <OTP>' to authenticate.")


def cmd_explore(cfg: dict) -> None:
    """CLI: dump the authenticated booking page HTML for inspection."""
    _s, html = get_booking_page(cfg)
    out = HERE / "booking_page.html"
    out.write_text(html)
    print(f"Booking page dumped to {out} ({len(html)} chars)")


def to_api_date(d: str) -> str:
    """Convert a user-supplied date to the portal's 'd-m-yyyy' slot-API format."""
    dt = parse_date(d)
    return f"{dt.day}-{dt.month}-{dt.year}"


def parse_date(d: str) -> datetime:
    """Parse a user-supplied date (YYYY-MM-DD or DD/MM/YYYY); raise ValueError."""
    d = d.strip()
    # Tolerate ISO datetimes too (e.g. "2026-09-22T00:00:00"): keep the date part.
    d = d.split("T", 1)[0].strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(d, fmt)
        except ValueError:
            continue
    raise ValueError(f"Could not parse date '{d}'. Use YYYY-MM-DD or DD/MM/YYYY.")


def to_form_date(d: str) -> str:
    """Convert a user-supplied date to the form's 'M d, yyyy' format."""
    dt = parse_date(d)
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"


def fetch_booking_page(s: requests.Session, cfg: dict) -> str:
    """Fetch the booking page HTML, raising if the session has expired."""
    r = s.get(f"{BASE_URL}/asset/assetbooking/{cfg['asset_booking_id']}")
    if r.status_code != 200 or "/login" in r.url.lower():
        raise RuntimeError("Session expired. Re-login before booking.")
    return r.text


def parse_booking_meta(html: str) -> dict:
    """Extract the booking form's key values (unit, user, community, name)."""
    soup = BeautifulSoup(html, "html.parser")
    meta = {}
    sel = soup.find("select", id="id_unit")
    if sel:
        opts = [o for o in sel.find_all("option") if o.get("value")]
        if opts:
            meta["unit_id"] = opts[0].get("value")
            meta["unit_label"] = opts[0].get_text(strip=True)
    for key, attr in (("user_id", "user"), ("community_id", "community"),
                      ("applicant_name", "applicant_name")):
        el = soup.find("input", id=attr)
        if el and el.get("value"):
            meta[key] = el.get("value")
    el = soup.find("input", attrs={"name": "status"})
    if el and el.get("value"):
        meta["status"] = el.get("value")
    return meta


def get_booking_page(cfg: dict) -> tuple:
    """Fetch the booking page ONCE on the shared session (validates login).

    Returns (session, html). The single fetch doubles as the login check:
    ``fetch_booking_page`` raises RuntimeError when the session is expired.
    """
    s = get_session()
    try:
        html = fetch_booking_page(s, cfg)
    except RuntimeError:
        reset_session()
        raise
    with _SESSION_LOCK:
        save_session(s)
    return s, html


# --------------------------------------------------------------------------- #
# Session state (meta + bookings) — refreshed once at start / prefs-save
#
# The booking form's meta (unit_id, user_id, community_id, applicant_name,
# status) is stable per account. The user's existing bookings are fetched
# from the portal ONCE at program start or prefs-save (the single slow call,
# ~4.5s), then held in memory for the whole session. This eliminates the
# redundant booking-page GET and the slow mybookings call from every booking
# attempt: after the initial refresh, all booking logic runs against the
# in-memory state with no further mybookings calls.
# --------------------------------------------------------------------------- #
_STATE: dict = {"meta": None, "bookings": []}


def _fetch_my_bookings_fast(s: requests.Session) -> list:
    """Fetch the user's approved bookings via a bare POST (no prior GET).

    Confirmed: the portal's mybookings endpoint accepts a POST with just
    serach_val[id_service_req_status]=66 (Approved) and serach_val[page_num]=1
    and returns the filtered result set. The prior GET (which discovers the
    #searchForm fields) is unnecessary overhead (~5s).
    """
    data = {
        "serach_val[id_service_req_status]": "66",  # Approved
        "serach_val[page_num]": "1",
    }
    r = s.post(f"{BASE_URL}/booking/myBooking", data=data,
               allow_redirects=True, timeout=MYBOOKINGS_TIMEOUT)
    if "/login" in r.url.lower():
        raise RuntimeError("Session expired. Re-login before listing bookings.")
    return _parse_booking_rows(r.text)


def refresh_state(cfg: dict) -> None:
    """Fetch the booking meta + the user's existing bookings from the portal.

    This is the ONE slow call (booking-page GET ~1s + mybookings POST ~4.5s).
    It is called at program start and on prefs-save. After this, all booking
    logic runs against the in-memory state with no further mybookings calls.
    """
    s = get_session()
    # 1. Fetch the booking meta (from the booking page).
    try:
        html = fetch_booking_page(s, cfg)
        meta = parse_booking_meta(html)
    except RuntimeError:
        reset_session()
        raise
    with _SESSION_LOCK:
        save_session(s)
    _STATE["meta"] = meta
    # 2. Fetch the user's existing bookings (bare POST, no GET).
    bookings = _fetch_my_bookings_fast(s)
    _STATE["bookings"] = bookings
    # 3. Reconcile booked.json against the portal's actual bookings.
    #    If the user cancelled directly on the portal, remove the stale record.
    _reconcile_booked(bookings)


def get_booking_meta(cfg: dict) -> tuple:
    """Return (session, meta), fetching the meta if not yet in memory.

    The meta is stable per account, so it is fetched from the booking page
    only on the first call (or after a re-login). This removes the redundant
    booking-page GET from every slots/book/mybookings action.
    """
    if _STATE["meta"] is not None:
        return get_session(), _STATE["meta"]
    s = get_session()
    try:
        html = fetch_booking_page(s, cfg)
        meta = parse_booking_meta(html)
    except RuntimeError:
        reset_session()
        raise
    with _SESSION_LOCK:
        save_session(s)
    _STATE["meta"] = meta
    return s, meta


# --------------------------------------------------------------------------- #
# The booking window, as the portal itself defines it
#
# The portal renders a "Check in date" <select> on the booking page; the dates
# it offers ARE the bookable window (a date with no <option> cannot be booked:
# jQuery submits the selected option's value and the server validates it
# against that list, so a POSTed date outside it is rejected). The slot
# endpoint does NOT validate the date, which is why a date outside the window
# still returns slots — the failure only shows up once the booking is
# submitted. Everything that decides "can I book day X?" must therefore read
# this list instead of assuming a fixed today..today+6 window.
# --------------------------------------------------------------------------- #
_BOOKABLE_DATES_CACHE = None   # (session, monotonic_ts, dates) | None
_BOOKABLE_DATES_TTL = 60       # seconds — the window is re-read at most this often

# Full date formats accepted for an <option>. Day-first everywhere: the
# portal's own slot API uses d-m-yyyy (to_api_date) and its booking form
# "Nov 15, 2026" (to_form_date).
_BOOKABLE_DATE_FORMATS = (
    "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d %m %Y",
    "%d-%m-%y", "%d/%m/%y", "%d %b %Y", "%d %b, %Y", "%d %B %Y",
    "%b %d, %Y", "%b %d %Y", "%B %d, %Y", "%B %d %Y",
)
# Only for a select that is explicitly the date picker (day/month-only option
# lists). Never used for unrelated selects: their options, e.g. unit codes
# ("09 01"), would otherwise be read as dates.
_BOOKABLE_DAYMONTH_FORMATS = ("%d-%m", "%d/%m", "%d %m", "%d-%m-%y")

# Option texts that look like picker noise rather than dates.
_BOOKABLE_NON_DATES = {"", "today", "tomorrow", "select date", "select a date",
                       "choose date", "n/a", "none", "-"}

# Substrings (lowercase) identifying the date <select> by its name/id.
_BOOKABLE_DATE_SELECT_NAMES = ("check_in_date", "checkin_date", "check-in-date",
                               "check in date", "checkindate")


def _option_date(text: str, today, day_month_only: bool = False):
    """Parse one <option> value/text into a ``date``; None when not a date."""
    if text is None:
        return None
    t = " ".join(str(text).split())          # collapse whitespace/nbsp
    if t.lower().strip(".:") in _BOOKABLE_NON_DATES:
        return None
    for fmt in _BOOKABLE_DATE_FORMATS:
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    if day_month_only:
        for fmt in _BOOKABLE_DAYMONTH_FORMATS:
            try:
                parsed = datetime.strptime(t, fmt)
            except ValueError:
                continue
            # strptime leaves the year at 1900 when the option gives only day
            # and month; a booking window only ever holds today or later, so
            # anchor it on today's year and roll forward when needed.
            year = parsed.year if parsed.year > 1900 else today.year
            try:
                candidate = datetime(year, parsed.month, parsed.day).date()
            except ValueError:
                continue
            if candidate < today:
                try:
                    candidate = datetime(year + 1,
                                         parsed.month, parsed.day).date()
                except ValueError:
                    return None
            return candidate
    return None


def parse_booking_dates(html: str, today=None) -> list:
    """Extract the dates offered by the portal's "Check in date" picker.

    Returns a sorted, de-duplicated list of ``date`` objects, or ``[]`` when
    the page carries no parsable date picker — the caller must then fall back
    to the arithmetic window and never read "empty" as "nothing is bookable".

    Parsing is deliberately structure-agnostic (the markup is not pinned to a
    tested sample): a <select> whose name/id mentions the check-in date wins;
    otherwise the <select> offering the most parseable dates is used.
    """
    soup = BeautifulSoup(html, "html.parser")
    today = today or datetime.now().date()
    selects = soup.find_all("select")
    if not selects:
        return []

    def collect(sel, day_month_only: bool) -> list:
        found = []
        for opt in sel.find_all("option"):
            # A greyed-out option is the portal saying "not selectable yet"
            # (e.g. a date whose window has not opened). Counting it as
            # offered is what would let a booking be attempted for a day the
            # dropdown visibly refuses to give.
            if opt.has_attr("disabled"):
                continue
            for text in (opt.get("value"), opt.get_text(strip=True)):
                parsed = _option_date(text, today, day_month_only)
                if parsed:
                    found.append(parsed)
                    break
        return found

    named = [sel for sel in selects
             if any(tok in f"{sel.get('name') or ''} {sel.get('id') or ''}".lower()
                    for tok in _BOOKABLE_DATE_SELECT_NAMES)]
    if named:
        # The named date picker: day/month-only options are allowed here.
        dates = []
        for sel in named:
            dates.extend(collect(sel, True))
        if dates:
            return sorted(set(dates))
    # No date-named select: pick the select that looks most like a date list.
    best: list = []
    for sel in selects:
        found = collect(sel, False)
        if len(found) > len(best):
            best = found
    return sorted(set(best))



def _bookable_dates_cached(s) -> list | None:
    """Cached window dates for session *s* if still fresh, else None."""
    if (_BOOKABLE_DATES_CACHE is not None and _BOOKABLE_DATES_CACHE[0] is s
            and (time.monotonic() - _BOOKABLE_DATES_CACHE[1])
            < _BOOKABLE_DATES_TTL):
        return _BOOKABLE_DATES_CACHE[2]
    return None


def _bookable_dates_store(s, dates: list) -> None:
    global _BOOKABLE_DATES_CACHE
    _BOOKABLE_DATES_CACHE = (s, time.monotonic(), dates)


def _dump_unreadable_picker(html: str) -> None:
    """Save the booking page when its date picker reads EMPTY (diagnostic).

    An empty picker is what makes ``run_booking_race`` wait forever on a date
    the browser clearly offers (fail-closed: it cannot prove the day bookable),
    so the markup that broke parsing has to be inspectable. Written next to the
    script like the other diag dumps; failure to write must never affect the
    booking path.
    """
    out = HERE / "diag_booking_page.html"
    try:
        out.write_text(html)
        out.chmod(0o600)
        print(f"{ts_prefix()} [bot] ⚠️ 'Check in date' picker unreadable — page "
              f"dumped to {out} ({len(html)} chars); check its <select> markup",
              flush=True)
    except Exception as e:  # pylint: disable=broad-exception-caught
        print(f"{ts_prefix()} [bot] picker-dump failed (non-fatal): {e}",
              flush=True)


def portal_bookable_dates(cfg: dict, force: bool = False) -> list:
    """The dates the portal currently offers for booking ("Check in date").

    Re-reads the booking page at most once per ``_BOOKABLE_DATES_TTL`` unless
    ``force`` is set. Raises RuntimeError when the session has expired. An
    EMPTY list means "unknown" (page has no parsable date picker) — callers
    must fall back to the arithmetic window, not treat it as "nothing bookable".
    """
    cached = _bookable_dates_cached(get_session())
    if cached is not None and not force:
        return cached
    s = get_session()
    try:
        html = fetch_booking_page(s, cfg)
    except RuntimeError:
        reset_session()
        raise
    dates = parse_booking_dates(html)
    if not dates:
        # An empty picker means "unknown", and every decision point then falls
        # back to arithmetic / refuses to POST (fail-closed). That is also what
        # makes the bot sit and wait for a date the browser clearly offers — so
        # dump the page once per refresh to make a parsing break visible.
        _dump_unreadable_picker(html)
    with _SESSION_LOCK:
        save_session(s)
    _bookable_dates_store(s, dates)
    return dates


# The 7-day rolling window and the "Check in date" picker are NOT the same
# thing, and conflating them is what caused a date to be offered and then
# rejected. The window is always 7 days wide (today .. today+6); the picker
# says which of those days the portal has opened so far. Therefore:
#   * a day OUTSIDE the window can never be booked now  -> stop, never wait;
#   * a day INSIDE it that the picker omits is just not open yet -> keep
#     refreshing (the window slides forward) until it opens and a slot is free.
BOOKING_WINDOW_DAYS = 7        # default window width, config: booking_window_days

# Default cap on ONE wait for a date to open and a preferred slot to free up,
# in minutes (config: booking_max_wait_minutes). It bounds a single
# run_booking_race attempt, not the program: the autobooking scheduler
# re-attempts as its window triggers fire. 720 = 12h, long enough to cover an
# overnight wait for the portal's window to slide onto a target day.
BOOKING_MAX_WAIT_MINUTES = 720


def booking_window_days(cfg: dict) -> int:
    """Width of the portal's rolling window in days (default 7)."""
    try:
        days = int(cfg.get("booking_window_days", BOOKING_WINDOW_DAYS))
    except (TypeError, ValueError):
        return BOOKING_WINDOW_DAYS
    return max(1, days)


def rolling_window_dates(now, days: int = 7) -> list:
    """The portal's rolling window: ``days`` consecutive dates from today.

    This is the HARD limit of what can be booked right now; it doubles as the
    fallback view for callers that could not read the picker.
    """
    today = now.date() if isinstance(now, datetime) else now
    return [today + timedelta(days=i) for i in range(max(1, days))]


def window_limit(cfg: dict, now) -> tuple:
    """``(first, last)`` date of the rolling window — the arithmetic bounds.

    These are the widest bounds the portal could ever accept; the picker is a
    subset of them and is what proves a date bookable (see ``run_booking_race``
    step 2, which requires the target to appear in the picker). Never treat the
    arithmetic bound alone as permission to POST.
    """
    dates = rolling_window_dates(now, booking_window_days(cfg))
    return dates[0], dates[-1]


def in_booking_window(cfg: dict, day, now) -> bool:
    """True when *day* lies inside the portal's rolling window right now."""
    day = day.date() if isinstance(day, datetime) else day
    first, last = window_limit(cfg, now)
    return first <= day <= last


def window_label(dates: list) -> str:
    """Human-readable window, e.g. ``Wed 11 Nov .. Sat 14 Nov 2026``."""
    if not dates:
        return ""
    first, last = dates[0], dates[-1]
    if len(dates) == 1:
        return f"{first:%a %d %b %Y}"
    return f"{first:%a %d %b} .. {last:%a %d %b %Y}"


def bookable_window_note(cfg: dict) -> str:
    """``(portal window: Wed 11 Nov .. Sat 14 Nov 2026)`` or ``""``.

    Never raises — a failed/expired lookup simply yields no note, so the
    message still makes sense when the window cannot be read.
    """
    try:
        dates = portal_bookable_dates(cfg)
    except RuntimeError:
        return ""
    except Exception:  # pylint: disable=broad-exception-caught
        return ""
    if not dates:
        return ""
    return f" (portal window: {window_label(dates)}, {len(dates)} days)"



def get_bookings() -> list:
    """Return the in-memory list of the user's existing bookings."""
    return _STATE["bookings"]


def is_day_booked(target_date) -> bool:
    """True if the user already has an approved (non-cancelled) booking for
    *target_date* (a ``datetime.date``)."""
    for b in _STATE["bookings"]:
        if b.get("from_dt") is None:
            continue
        if b["from_dt"].date() == target_date and not is_cancelled(b):
            return True
    return False


def count_active_bookings(target_date) -> int:
    """Count the user's active (non-cancelled) bookings within the current
    7-day booking window (today through today+6).

    The portal allows a maximum of 3 active bookings within the 7-day
    booking window. Bookings outside that window (past or beyond 7 days)
    do not count toward the limit.
    """
    today = datetime.now().date()
    window_start = today
    window_end = today + timedelta(days=BOOKING_WINDOW_DAYS - 1)
    count = 0
    for b in _STATE["bookings"]:
        if b.get("from_dt") is None or is_cancelled(b):
            continue
        d = b["from_dt"].date()
        if window_start <= d <= window_end:
            count += 1
    return count


MAX_TOTAL_BOOKINGS = 3  # portal limit: max 3 active bookings per 7-day window


def _slots_endpoint(s: requests.Session, cfg: dict, api_date: str,
                    meta: dict | None = None) -> tuple[list, str]:
    """Call the portal's slot endpoint. Returns (slots, error_text).

    *slots* is the list of free slots (empty when the day is already booked
    or has no free slots). *error_text* is the text of the 'alert-danger'
    box the portal returns when a day is already booked (e.g. '...this
    amenity can only be booked once a day'), or '' when absent.
    """
    meta = meta or {}
    serach_val = {
        "date": api_date,
        "id_asset": cfg["asset_booking_id"],
        "is_paid": "N",
        "user": meta.get("user_id", cfg.get("user_id", "")),
        "id_community": meta.get("community_id", cfg.get("community_id", "")),
        "attendees": str(cfg.get("attendees", 1)),
        "id_unit": meta.get("unit_id", cfg.get("unit_id", "")),
        "clsId": "",
    }
    # jQuery serializes the nested object as serach_val[key]=value
    data = {f"serach_val[{k}]": v for k, v in serach_val.items()}
    r = s.post(BASE_URL + "/ajaxctrl/getAmenityBookingSlot", data=data)
    soup = BeautifulSoup(r.text, "html.parser")
    slots = []
    for inp in soup.find_all("input", attrs={"name": "check_in_time"}):
        label = inp.find_next("label")
        raw_val = inp.get("value")
        val = raw_val if isinstance(raw_val, str) else ""
        parts = val.split("_")
        start = parts[0] if parts else None
        end = parts[1] if len(parts) > 1 else None
        # Prefer a clean 24-hour label (e.g. "14:00-15:00") built from the
        # slot value; fall back to the portal's own label text if unavailable.
        label_text = f"{start}-{end}" if (start and end) else (
            label.get_text(strip=True) if label else val)
        slots.append({
            "value": val,
            "label": label_text,
            "start": start,
            "end": end,
            "slot_id": parts[-1] if parts else None,
        })
    error = _alert_text(r, "danger")
    return slots, error


def get_available_slots(s: requests.Session, cfg: dict, api_date: str,
                        meta: dict | None = None) -> list:
    """Call the portal's slot endpoint and return the list of free slots."""
    return _slots_endpoint(s, cfg, api_date, meta)[0]


def parse_from_filter(args: list):
    """Extract an optional '--from HH:MM' from args.

    Returns (min_start_or_None, remaining_args).
    """
    min_start = None
    rest = []
    i = 0
    while i < len(args):
        if args[i] == "--from" and i + 1 < len(args):
            min_start = args[i + 1]
            i += 2
        else:
            rest.append(args[i])
            i += 1
    return min_start, rest


def _start_minutes(start) -> int:
    """Convert 'HH:MM' to minutes since midnight, or -1 if invalid/missing."""
    try:
        h, m = str(start).split(":")
        return int(h) * 60 + int(m)
    except (AttributeError, ValueError):
        return -1


def filter_slots_from(slots: list, min_start) -> list:
    """Keep only slots whose start time is >= min_start ('HH:MM').

    If min_start is empty/invalid, return the slots unchanged.
    """
    if not min_start:
        return slots
    cutoff = _start_minutes(min_start)
    if cutoff < 0:
        return slots
    return [sl for sl in slots
            if (mins := _start_minutes(sl.get("start"))) >= 0 and mins >= cutoff]


def _slot_matches(slot: dict, min_start) -> bool:
    """True if the slot starts at/after min_start ('HH:MM'); False if no filter."""
    if not min_start:
        return False
    cutoff = _start_minutes(min_start)
    if cutoff < 0:
        return False
    return _start_minutes(slot.get("start")) >= cutoff


def resolve_min_start(cfg: dict, args: list):
    """Return (min_start, remaining_args).

    A CLI '--from HH:MM' overrides the config 'min_start_hour' default.
    min_start is None when no filter applies.
    """
    min_start, rest = parse_from_filter(args)
    if min_start is None:
        min_start = cfg.get("min_start_hour") or None
    return min_start, rest


def pop_flag(args: list, flag: str):
    """Remove `flag` from args (if present). Returns (present, remaining)."""
    present = flag in args
    rest = [a for a in args if a != flag]
    return present, rest


def cmd_window(cfg: dict, _args: list) -> None:
    """CLI: print the bookable window exactly as the portal advertises it.

    Reads the "Check in date" option list on the booking page — those dates
    are the only ones the portal will accept, so this is the ground truth the
    bot must follow (e.g. it may stop at today+5 rather than today+6).
    """
    try:
        dates = portal_bookable_dates(cfg, force=True)
    except RuntimeError as e:
        sys.exit(str(e))
    if not dates:
        print("No 'Check in date' option list could be read from the booking "
              "page (the markup may have changed) — the bot falls back to the "
              "today..today+6 window.")
        sys.exit(1)
    today = datetime.now().date()
    days = booking_window_days(cfg)
    first, last = window_limit(cfg, datetime.now())
    print(f"Portal rolling window: {first:%a %d %b} .. {last:%a %d %b %Y} "
          f"({days} days)")
    print(f"Opened by 'Check in date': {window_label(dates)} "
          f"({len(dates)} of {days} days)")
    for d in rolling_window_dates(datetime.now(), days):
        offset = (d - today).days
        marker = "today" if offset == 0 else f"in {offset} day(s)"
        state = "bookable" if d in dates else "NOT OPEN YET"
        print(f"  {d:%a %d %b %Y}   ({marker})   {state}")


def cmd_slots(cfg: dict, args: list) -> None:
    """CLI: list the available slots for one date (optionally from a time on)."""
    min_start, args = resolve_min_start(cfg, args)
    if not args:
        sys.exit("Usage: slots <date> [--from HH:MM]   (e.g. slots 2026-09-26 --from 18:00)")
    api_date = to_api_date(args[0])
    want_date = parse_date(args[0]).date()
    try:
        offered = portal_bookable_dates(cfg)
    except Exception:  # pylint: disable=broad-exception-caught
        # get_booking_meta() below reports an expired session properly.
        offered = []
    s, meta = get_booking_meta(cfg)
    all_slots = get_available_slots(s, cfg, api_date, meta)
    slots = filter_slots_from(all_slots, min_start)
    print(f"\nAvailable slots for {args[0]}  (unit {meta.get('unit_label')}, "
          f"{cfg.get('attendees', 1)} attendees)")
    if min_start:
        print(f"  (filtered: from {min_start} onwards)")
    first, last = window_limit(cfg, datetime.now())
    if want_date < first or want_date > last:
        # Outside the rolling window: no slot endpoint answer can ever be
        # booked, the form has no option for such a date.
        print(f"  ⚠️ {want_date:%a %d %b %Y} is OUTSIDE the portal's "
              f"{booking_window_days(cfg)}-day window "
              f"({first:%a %d %b} .. {last:%a %d %b %Y}) — it cannot be "
              f"booked at all right now.")
    elif offered and want_date not in offered:
        # Inside the window but the picker has not opened it yet: it becomes
        # bookable when the window slides forward (autobook waits for that).
        print(f"  ⚠️ {want_date:%a %d %b %Y} is inside the portal's window "
              f"but its 'Check in date' does not offer it yet — the portal "
              f"lists {window_label(offered)} ({len(offered)} of "
              f"{booking_window_days(cfg)} days).")
    print("-" * 46)
    if not slots:
        if all_slots:
            reason = (f"no slots from {min_start} onwards "
                      f"(earliest is {all_slots[0]['label']})")
        else:
            reason = "fully booked"
        print(f"  No slots available ({reason}).")
    for sl in slots:
        print(f"  {sl['label']:<22}  [{sl['value']}]")
    print("-" * 46)
    shown = len(slots)
    total = len(all_slots)
    if min_start and shown != total:
        print(f"  {shown} of {total} slot(s) available (filtered from {min_start}).")
    else:
        print(f"  {shown} slot(s) available.")


def cmd_book(cfg: dict, args: list) -> None:
    """CLI: book a specific slot for a date (asks for confirmation first)."""
    verbose, args = pop_flag(args, "--verbose")
    if len(args) < 2:
        sys.exit("Usage: book <date> <slot-label-or-value> [description] [--verbose]")
    api_date = to_api_date(args[0])
    want = args[1]
    description = args[2] if len(args) > 2 else cfg.get("description",
                                                        "Padel booking")

    try:
        offered = portal_bookable_dates(cfg)
    except Exception:  # pylint: disable=broad-exception-caught
        # get_booking_meta() below reports an expired session properly.
        offered = []
    want_date = parse_date(args[0]).date()
    first, last = window_limit(cfg, datetime.now())
    if want_date < first or want_date > last:
        # Outside the rolling window: the booking form has no option for it.
        sys.exit(f"NOT BOOKABLE AT ALL: {want_date:%a %d %b %Y} is outside "
                 f"the portal's {booking_window_days(cfg)}-day window "
                 f"({first:%a %d %b} .. {last:%a %d %b %Y}).")
    if offered and want_date not in offered:
        # Inside the window but not opened yet: a manual submit now would be
        # rejected, while autobooking waits 2 min at a time for that opening.
        sys.exit(f"NOT OPEN YET: {want_date:%a %d %b %Y} is inside the "
                 f"portal's window but its 'Check in date' does not offer "
                 f"it; the portal lists {window_label(offered)}.")

    s, meta = get_booking_meta(cfg)
    slots = get_available_slots(s, cfg, api_date, meta)
    if not slots:
        sys.exit("No slots available for that date.")

    target = None
    for sl in slots:
        if want in (sl["label"], sl["value"], sl["slot_id"]):
            target = sl
            break
    if not target:
        print("Available slots:")
        for sl in slots:
            print(f"  {sl['label']}  [{sl['value']}]")
        sys.exit(f"Slot '{want}' not found among the available slots.")

    print(f"Booking {api_date}  {target['label']}  (unit {meta.get('unit_label')})")
    confirm = input("Confirm booking? [y/N] ")
    if confirm.strip().lower() not in ("y", "yes"):
        print("Aborted.")
        return

    r = submit_booking(s, cfg, meta, args[0], target, description,
                       verbose=verbose)
    ok = verify_booking_created(s, args[0], target["start"])
    record_booking(args[0], target["label"], ok)
    print("Response status:", r.status_code, "final url:", r.url)
    print(f"[diag] verify_booking_created: {ok}")
    if ok:
        print("Booking CONFIRMED - it now appears in your 'My Bookings'.")
        # Sync Google Calendar (non-fatal)
        try:
            if sync_booking(args[0], target["label"], cfg, description):
                print("📅 Event created on Google Calendar.")
            else:
                print("⚠️ Booking confirmed, but the Google Calendar "
                      "event could not be created.")
        except Exception as e:  # pylint: disable=broad-exception-caught
            print(f"⚠️ Google Calendar sync failed (non-fatal): {e}")
    else:
        err = _booking_error(r)
        if err:
            print(f"Booking FAILED: {err}")
        else:
            print("Booking FAILED: the portal did not create the booking "
                  "(no error message, and it is not in 'My Bookings').")
        print("(No booking was made. Check the slot is still free and retry.)")
        print("NOTE: the portal allows only ONE booking per day. If you already "
              "have a booking on this date, it silently blocks every other "
              "slot - cancel it first (see 'mybookings' / 'cancel') to book a "
              "different time.")


def submit_booking(s: requests.Session, cfg: dict, meta: dict, date_str: str,
                   slot: dict, description: str, verbose: bool = False
                   ) -> requests.Response:
    """POST the booking form. Returns the raw response.

    The portal silently ignores a booking POST that does not include the
    submit-button field ('submit=submit') - a real browser form always sends
    it, and its absence is exactly what causes the HTTP-200-but-no-booking
    silent failure. It must therefore be part of the payload.

    With verbose=True it logs the submitted payload, the HTTP status / final
    URL, any alert-danger / alert-success text, and saves the full response
    body to diag_book_response.html for offline inspection.
    """
    payload = {
        "status": meta.get("status", "66"),
        "is_paid_var": "N",
        "id_community_asset": cfg["asset_booking_id"],
        "id_community": meta.get("community_id", ""),
        "id_unit": meta.get("unit_id", ""),
        "applicant_name": meta.get("applicant_name", ""),
        "attendees": str(cfg.get("attendees", 1)),
        "check_in_date": to_form_date(date_str),
        "check_in_time": slot["value"],
        "description": description,
        "tnc_assest": "on",
        # The form's submit button (<button name="submit" value="submit">).
        # Without this the portal accepts the POST (HTTP 200) but creates no
        # booking and shows no error - so it must always be sent.
        "submit": "submit",
    }
    if verbose:
        print(f"[diag] POST {BASE_URL}/asset/assetbooking/{cfg['asset_booking_id']}")
        for k, v in payload.items():
            print(f"[diag]   {k} = {v!r}")
    r = s.post(f"{BASE_URL}/asset/assetbooking/{cfg['asset_booking_id']}",
               data=payload, allow_redirects=True)
    if verbose:
        print(f"[diag] HTTP {r.status_code}   final url: {r.url}")
        print(f"[diag] alert-danger : {_alert_text(r, 'danger') or '(none)'}")
        print(f"[diag] alert-success: {_alert_text(r, 'success') or '(none)'}")
        out = HERE / "diag_book_response.html"
        out.write_text(r.text)
        out.chmod(0o600)
        print(f"[diag] response body saved -> {out} ({len(r.text)} chars)")
    return r


def submit_booking_fast(s: requests.Session, cfg: dict, meta: dict,
                        date_str: str, slot: dict,
                        description: str = "Padel booking") -> int:
    """POST the booking form WITHOUT waiting for the full HTML response.

    The portal generates the 'My Bookings' HTML page (~4-5s) before sending
    the response body. We only need the status code to know the request was
    accepted. Verification is done separately via the slots endpoint.

    Returns the HTTP status code (200 = accepted, 302 = redirect).
    """
    payload = {
        "status": meta.get("status", "66"),
        "is_paid_var": "N",
        "id_community_asset": cfg["asset_booking_id"],
        "id_community": meta.get("community_id", ""),
        "id_unit": meta.get("unit_id", ""),
        "applicant_name": meta.get("applicant_name", ""),
        "attendees": str(cfg.get("attendees", 1)),
        "check_in_date": to_form_date(date_str),
        "check_in_time": slot["value"],
        "description": description,
        "tnc_assest": "on",
        "submit": "submit",
    }
    r = s.post(f"{BASE_URL}/asset/assetbooking/{cfg['asset_booking_id']}",
               data=payload, allow_redirects=False, stream=True)
    status = r.status_code
    r.close()  # discard the body — we verify via slots endpoint instead
    return status


def cancel_booking_fast(s: requests.Session, details_id: str) -> tuple[bool, int]:
    """Cancel a booking WITHOUT waiting for the full HTML response.

    Returns (ok, status_code). Verification is done via the slots endpoint.
    """
    r = s.post(CANCEL_URL, data={
        "id_asset_booking": details_id,
        "booking_cancel_value": "1",
        "submit": "submit",
    }, allow_redirects=False, stream=True)
    status = r.status_code
    r.close()
    ok = status in (200, 302, 303)
    return ok, status


def _booking_error(r: requests.Response) -> str:
    """Return the text of a Bootstrap 'alert-danger' box in the response, or ''.

    A failed booking submission re-renders the page with such a box, e.g.
    'Booking slot has already been scheduled.'
    """
    m = re.search(r'<div class="[^"]*alert-danger[^"]*">(.*?)</div>',
                  r.text, re.S)
    if m:
        return BeautifulSoup(m.group(1), "html.parser").get_text(" ", strip=True)
    return ""

# --------------------------------------------------------------------------- #
# Booking memory (prevents double-booking after a daemon restart)
# --------------------------------------------------------------------------- #
BOOKED_FILE = HERE / "booked.json"


def load_booked() -> list:
    """Load the booking-memory list from booked.json ([] if missing/invalid)."""
    if BOOKED_FILE.exists():
        try:
            data = json.loads(BOOKED_FILE.read_text())
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []
    return []


def record_booking(date_str: str, slot_label: str, success: bool) -> None:
    """Append a booking attempt (with outcome) to booked.json."""
    recs = load_booked()
    recs.append({
        "date": date_str,
        "slot": slot_label,
        "success": bool(success),
        "at": datetime.now().isoformat(),
    })
    BOOKED_FILE.write_text(json.dumps(recs, indent=2))
    BOOKED_FILE.chmod(0o600)


def already_booked_successfully(date_str: str) -> bool:
    """True if a booking for this date already succeeded (see booked.json)."""
    return any(r.get("date") == date_str and r.get("success")
               for r in load_booked())


def remove_booking_record(date_str: str) -> None:
    """Remove all booking records for *date_str* from booked.json."""
    recs = load_booked()
    filtered = [r for r in recs if r.get("date") != date_str]
    if len(filtered) != len(recs):
        BOOKED_FILE.write_text(json.dumps(filtered, indent=2))
        BOOKED_FILE.chmod(0o600)


def _reconcile_booked(portal_bookings: list) -> None:
    """Remove stale booked.json entries that no longer have an active booking.

    Compares local records against the portal's actual booking list. If a date
    in booked.json has no corresponding non-cancelled booking in the portal,
    the record is removed (the user likely cancelled directly on the portal).
    """
    recs = load_booked()
    if not recs:
        return
    active_dates = set()
    for b in portal_bookings:
        if is_cancelled(b):
            continue
        dt = b.get("from_dt")
        if dt is not None:
            active_dates.add(dt.strftime("%Y-%m-%d"))
    filtered = [r for r in recs if r.get("date") in active_dates]
    if len(filtered) != len(recs):
        BOOKED_FILE.write_text(json.dumps(filtered, indent=2))
        BOOKED_FILE.chmod(0o600)


# --------------------------------------------------------------------------- #
# My bookings / cancellation
# --------------------------------------------------------------------------- #
MYBOOKINGS_URL = f"{BASE_URL}/booking/myBooking"
CANCEL_URL = f"{BASE_URL}/booking/cancelAmenityBooking"
# The mybookings endpoint is the portal's slowest call (~4.5s). It uses the
# same raised timeout as the rest of the portal session (see PORTAL_TIMEOUT).
MYBOOKINGS_TIMEOUT = PORTAL_TIMEOUT


def _parse_booking_dt(x: str):
    """Parse the portal's 'September 26, 2026  18:00' datetime, or None."""
    try:
        return datetime.strptime(re.sub(r"\s+", " ", x), "%B %d, %Y %H:%M")
    except ValueError:
        return None


def _parse_booking_rows(html: str) -> list:
    """Parse one 'My Bookings' page (or AJAX fragment) into booking dicts.

    Each dict has: booking_id, details_id, name, community, unit, from_str,
    to_str, from_dt, to_dt, status.  'details_id' is the id used by the
    portal's cancel endpoint (found in the row's 'View' link).
    """
    soup = BeautifulSoup(html, "html.parser")
    tbl = soup.find("table")
    bookings = []
    if not tbl:
        return bookings
    for tr in tbl.find_all("tr")[1:]:
        cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True))
                 for c in tr.find_all("td")]
        if len(cells) < 8:
            continue
        m = re.search(r"bookingDetails/(\d+)", str(tr))
        if not m:
            continue
        bookings.append({
            "booking_id": cells[0],
            "details_id": m.group(1),
            "name": cells[2],
            "community": cells[3],
            "unit": cells[4],
            "from_str": cells[5],
            "to_str": cells[6],
            "from_dt": _parse_booking_dt(cells[5]),
            "to_dt": _parse_booking_dt(cells[6]),
            "status": cells[7].split(" ")[0],
        })
    return bookings


def _search_form_fields(soup) -> dict:
    """Collect the My Bookings search-form fields as an ordered {id: value}.

    The portal's pagination JS posts every #searchForm input/select (keyed by
    its id) as a 'serach_val' array, so we mirror exactly those fields.
    """
    form = soup.find(id="searchForm")
    fields = {}
    if not form:
        return fields
    for el in form.find_all(["input", "select", "textarea"]):
        el_id = el.get("id")
        if not el_id:
            continue
        if el.name == "select":
            # jQuery .val() on a <select> is the selected option's value
            # (first option when none is marked selected).
            opt = el.find("option", selected=True) or el.find("option")
            val = opt.get("value", "") if opt else ""
        else:
            val = el.get("value") or ""
        fields[el_id] = val
    return fields


def _total_pages(html: str) -> int:
    """Read 'Page 1 of N' from the pagination markup (1 when absent).

    The portal only renders the pagination block when there are 2+ pages, so
    an absent marker means a single page -- which is the correct default.
    """
    norm = html.replace("&nbsp;", " ")
    m = re.search(r"of\s*<i>\s*<b>\s*(\d+)", norm)
    return max(1, int(m.group(1))) if m else 1


def _status_value(soup, status_name: str):
    """Resolve a status name (e.g. 'Approved') to the portal's numeric value
    in the #id_service_req_status dropdown. Returns None when not found, in
    which case the caller should fall back to an unfiltered fetch."""
    sel = soup.find(id="id_service_req_status")
    if not sel:
        return None
    target = status_name.strip().lower()
    for opt in sel.find_all("option"):
        if opt.get_text(strip=True).lower() == target:
            return opt.get("value")
    return None


def _fetch_mybookings_page(s: requests.Session, post_url: str,
                           fields: dict, page_num: int,
                           status_val: str | None = None) -> str:
    """POST one page of 'My Bookings' exactly like the portal's JS does.

    The portal's jqAppClass.methodPost() issues:
        POST {base}/{post_url}   body: serach_val[<field_id>]=<value> ...
    and renders the returned HTML fragment in place of the table.

    When status_val is given, the Status dropdown filter is applied
    server-side, reducing the result set (and page count).
    """
    data = {f"serach_val[{k}]": (v or "") for k, v in fields.items()}
    data["serach_val[page_num]"] = str(page_num)
    if status_val is not None:
        data["serach_val[id_service_req_status]"] = status_val
    r = s.post(f"{BASE_URL}/{post_url}", data=data, allow_redirects=True,
               timeout=MYBOOKINGS_TIMEOUT)
    return r.text


def fetch_my_bookings(s: requests.Session, status: str | None = None) -> list:
    """Fetch 'My Bookings', following the portal's pagination.

    status: optional portal Status filter, e.g. 'Approved'. When set, the
            portal filters server-side (fewer results / pages). All pages of
            the (filtered) result are still fetched, in the portal's order
            (newest first).

    Page 1 is a GET (session check + to discover the #searchForm fields);
    pages 2..N -- and page 1 when a filter is applied -- are the AJAX POST
    the browser uses (see _fetch_mybookings_page).
    """
    r = s.get(MYBOOKINGS_URL, allow_redirects=True, timeout=MYBOOKINGS_TIMEOUT)
    if "/login" in r.url.lower():
        raise RuntimeError("Session expired. Re-login before listing bookings.")
    soup = BeautifulSoup(r.text, "html.parser")
    fields = _search_form_fields(soup)
    post_url = fields.get("post_url") or "booking/myBooking"
    status_val = _status_value(soup, status) if status else None

    # Page 1 results: a filtered fetch must come from the POST (the GET
    # always loads the unfiltered default); otherwise reuse the GET response.
    if status_val is not None:
        page1_html = _fetch_mybookings_page(s, post_url, fields, 1, status_val)
    else:
        page1_html = r.text
    total = min(_total_pages(page1_html), 50)  # safety cap
    bookings = _parse_booking_rows(page1_html)

    for page in range(2, total + 1):
        try:
            html = _fetch_mybookings_page(s, post_url, fields, page, status_val)
        except Exception:  # pylint: disable=broad-exception-caught
            break  # keep the pages we have rather than fail the whole listing
        page_rows = _parse_booking_rows(html)
        if not page_rows:
            break  # empty page: nothing further to fetch
        bookings.extend(page_rows)

    # De-duplicate (pages are disjoint, but guard against any overlap).
    seen = set()
    unique = []
    for b in bookings:
        key = b.get("details_id") or b.get("booking_id")
        if key in seen:
            continue
        seen.add(key)
        unique.append(b)
    return unique


def verify_booking_created(s: requests.Session, date_str: str,
                           slot_start: str) -> bool:
    """Confirm a booking was ACTUALLY created by checking 'My Bookings'.

    This is the ground-truth success check. The portal's booking POST can
    succeed OR fail silently (HTTP 200, page re-rendered, no alert) without
    creating a booking, so the response body alone cannot be trusted. The
    only reliable signal is whether the booking appears in 'My Bookings'.

    Returns True when a booking matching (date, start time) is present.
    """
    try:
        target_date = parse_date(date_str).date()
    except ValueError:
        return False
    try:
        # Approved-only (id_service_req_status=66): a booking we just made is
        # 'Approved', and the filter keeps this to a single page (~4.5s)
        # instead of the multi-page unfiltered fetch (~15s). This is only the
        # slow fallback -- the fast path is the slots endpoint's 'booked once
        # a day' alert checked by the caller.
        bookings = fetch_my_bookings(s, status="Approved")
    except Exception:  # pylint: disable=broad-exception-caught
        return False
    for b in bookings:
        if b.get("from_dt") is None:
            continue
        if (b["from_dt"].date() == target_date
                and b["from_dt"].strftime("%H:%M") == slot_start):
            return True
    return False


def _alert_text(r: requests.Response, kind: str) -> str:
    """Return the text of a Bootstrap 'alert-<kind>' box, or '' if absent."""
    m = re.search(rf'<div class="[^"]*alert-{kind}[^"]*">(.*?)</div>',
                  r.text, re.S)
    if m:
        return BeautifulSoup(m.group(1), "html.parser").get_text(" ", strip=True)
    return ""


def cancel_booking(s: requests.Session, details_id: str):
    """Cancel a booking by its details id. Returns (ok, message).

    The portal's cancel is a plain POST to /booking/cancelAmenityBooking with
    'id_asset_booking' + 'booking_cancel_value=1' (the on-page confirm() is
    client-side only).  Success is signalled by an 'alert-success' box, a
    failure by an 'alert-danger' box.
    """
    r = s.post(CANCEL_URL, data={
        "id_asset_booking": details_id,
        "booking_cancel_value": "1",
        "submit": "submit",
    }, allow_redirects=True)
    err = _alert_text(r, "danger")
    if err:
        return False, err
    ok_msg = _alert_text(r, "success")
    if ok_msg:
        return True, ok_msg
    if r.status_code in (200, 302, 303):
        return True, "Booking cancelled (no explicit confirmation message)."
    return False, f"HTTP {r.status_code}"


def is_cancelled(b: dict) -> bool:
    """True when the portal marks the booking as cancelled/rejected."""
    return b.get("status", "").lower() in ("reject", "rejected",
                                           "cancelled", "canceled")


def is_cancellable(b: dict, now: datetime | None = None) -> bool:
    """A booking can be cancelled if it's still active and starts in the future.

    Already-rejected/cancelled bookings are not cancellable.
    """
    now = now or datetime.now()
    if is_cancelled(b):
        return False
    return b["from_dt"] is not None and b["from_dt"] > now


def _booking_info(b: dict) -> str:
    """Date/time/name portion of a booking line (no status, no unit)."""
    return (f"{b['from_dt']:%a %d %b %Y} {b['from_dt']:%H:%M}-{b['to_dt']:%H:%M}"
            f"   {b['name']}")


def _booking_label(b: dict) -> str:
    """Simplified status label: 'approved' or 'cancelled'."""
    return "cancelled" if is_cancelled(b) else "approved"


def _fmt_booking(b: dict) -> str:
    return f"{_booking_info(b)}   [{_booking_label(b)}]"


def _booking_state(b: dict, now: datetime) -> str:
    """Classify a booking for color: 'past' (grey), 'cancelled' (red), or
    'active' (green). Past (start time already passed) takes priority."""
    if b["from_dt"] is not None and b["from_dt"] < now:
        return "past"
    if _booking_label(b) == "cancelled":
        return "cancelled"
    return "active"


def cmd_mybookings(cfg: dict, args: list) -> None:
    """List the user's bookings from the portal's 'My Bookings' page.

    Optionally filter to a single date, e.g. 'mybookings 2026-09-26'.
    Bookings are listed newest to oldest (most distant future first, past
    bookings sink to the bottom); each shows its status
    (approved/cancelled) and is colored: grey + strikethrough if in the
    past, red if cancelled, green if approved.
    """
    s = get_authenticated_session(cfg)
    bookings = fetch_my_bookings(s)
    if not bookings:
        print("No bookings found.")
        return

    date_filter = None
    positional = [a for a in args if not a.startswith("--")]
    if positional:
        try:
            date_filter = parse_date(positional[0]).date()
        except ValueError:
            sys.exit(f"Could not parse date '{positional[0]}'. "
                     f"Use YYYY-MM-DD or DD/MM/YYYY.")

    now = datetime.now()
    if date_filter:
        shown = [b for b in bookings
                 if b["from_dt"] and b["from_dt"].date() == date_filter]
        if not shown:
            print(f"No bookings on {date_filter:%Y-%m-%d}.")
            return
        print(f"Your bookings on {date_filter:%a %d %b %Y} ({len(shown)}):")
    else:
        shown = bookings
        print(f"Your bookings ({len(shown)}):")
    shown = sorted(shown, key=lambda b: b["from_dt"] or datetime.min,
                   reverse=True)
    print("-" * 78)
    for i, b in enumerate(shown, 1):
        idx = f"  [{i:>2}] "
        info = _booking_info(b)
        label = _booking_label(b)
        state = _booking_state(b, now)
        if state == "past":
            print(_paint(_paint(idx + info + f"   [{label}]", "grey"),
                         "strike"))
        elif state == "cancelled":
            print(idx + info + _paint(f"   [{label}]", "red"))
        else:
            print(idx + info + _paint(f"   [{label}]", "green"))
    print()
    print("To cancel one:")
    print("  python3 padel_booking.py cancel                 # interactive")
    print("  python3 padel_booking.py cancel <booking-id>    # direct")


def cmd_cancel(cfg: dict, args: list) -> None:
    """Cancel one of the user's bookings.

    With no argument, lists the cancellable (future) bookings and lets you
    pick a number, then confirms.  With an argument, target a specific
    booking by booking-id, details-id, or date (YYYY-MM-DD / DD/MM/YYYY).
    """
    s = get_authenticated_session(cfg)
    bookings = fetch_my_bookings(s, status="Approved")
    if not bookings:
        print("No bookings found.")
        return
    now = datetime.now()

    target = None
    positional = [a for a in args if not a.startswith("--")]
    if positional:
        key = positional[0]
        for b in bookings:
            if key in (b["booking_id"], b["details_id"]):
                target = b
                break
        if target is None:
            try:
                d = parse_date(key).date()
            except ValueError:
                sys.exit(f"Booking '{key}' not found. Use a booking-id, "
                         f"details-id, or a date.")
            matches = [b for b in bookings
                       if b["from_dt"] and b["from_dt"].date() == d]
            if len(matches) == 1:
                target = matches[0]
            elif len(matches) > 1:
                print(f"Multiple bookings on {d:%Y-%m-%d}; specify a booking-id:")
                for i, b in enumerate(matches, 1):
                    print(f"  [{i}] {b['from_dt']:%H:%M}-{b['to_dt']:%H:%M}"
                          f"  id={b['booking_id']}")
                sys.exit(1)
            else:
                sys.exit(f"No booking on {d:%Y-%m-%d}.")
        if target is None:
            sys.exit(f"Booking '{key}' not found.")
    else:
        cancellable = [b for b in bookings if is_cancellable(b, now)]
        if not cancellable:
            print("No cancellable (future) bookings found. Current bookings:")
            for b in bookings:
                print(f"  {_fmt_booking(b)}")
            return
        print("Cancellable bookings:")
        for i, b in enumerate(cancellable, 1):
            print(f"  [{i:>2}] {_fmt_booking(b)}   id={b['booking_id']}")
        raw = input("\nEnter booking number to cancel ('q' to quit): ").strip()
        if not raw or raw.lower() in ("q", "quit", "cancel"):
            print("Aborted. Nothing cancelled.")
            return
        if not raw.isdigit() or not 1 <= int(raw) <= len(cancellable):
            print("Invalid selection. Nothing cancelled.")
            return
        target = cancellable[int(raw) - 1]

    print("\nYou are about to CANCEL:")
    print(f"  {_fmt_booking(target)}")
    if not is_cancellable(target, now):
        print("  WARNING: this slot's start time is in the past; the portal")
        print("           may refuse to cancel it.")
    confirm = input("Confirm cancellation? This cannot be undone. [y/N] ")
    if confirm.strip().lower() not in ("y", "yes"):
        print("Aborted. Nothing cancelled.")
        return

    ok, msg = cancel_booking(s, target["details_id"])
    if ok:
        print(f"Cancelled: {msg}")
        if target.get("from_dt") and target.get("to_dt"):
            _d = target["from_dt"].strftime("%Y-%m-%d")
            _s = f"{target['from_dt']:%H:%M}-{target['to_dt']:%H:%M}"
            sync_cancellation(_d, _s, cfg)
    else:
        print(f"FAILED to cancel: {msg}")
        sys.exit(1)


# --------------------------------------------------------------------------- #
# Interactive manual booking (browse the whole bookable window, then choose)
# --------------------------------------------------------------------------- #
def cmd_pick(cfg: dict, args: list) -> None:
    """List every available slot from today through the last bookable day
    (today + 6), let the user pick one or more (max one per day), then book.

    A '--from HH:MM' filter (or the config 'min_start_hour') highlights the
    matching slots (starting at/after that time) in green; the rest are shown
    in normal color. Nothing is hidden.
    """
    verbose, args = pop_flag(args, "--verbose")
    min_start, args = resolve_min_start(cfg, args)
    s, meta = get_booking_meta(cfg)

    # The bookable window comes from the portal's "Check in date" picker; the
    # today..today+6 arithmetic is only the fallback for a page that gives no
    # parsable picker (see rolling_window_dates).
    try:
        window_dates = portal_bookable_dates(cfg)
    except Exception:  # pylint: disable=broad-exception-caught
        window_dates = []
    if not window_dates:
        window_dates = rolling_window_dates(datetime.now(),
                                            booking_window_days(cfg))
    print(f"Bookable window (from the portal): {window_label(window_dates)}"
          f"   ({len(window_dates)} of {booking_window_days(cfg)} days, "
          f"unit {meta.get('unit_label')})")
    if min_start:
        print(f"  (green: slots from {min_start} onwards)")
    print("-" * 62)

    all_slots = []
    for d in window_dates:
        api_date = f"{d.day}-{d.month}-{d.year}"
        for sl in get_available_slots(s, cfg, api_date, meta):
            all_slots.append({"date": d, "slot": sl})

    if not all_slots:
        print("No slots available in the bookable window.")
        sys.exit(1)

    print(f"Available slots ({len(all_slots)}):")
    for i, item in enumerate(all_slots, 1):
        line = f"  [{i:>2}] {item['date']:%a %d %b %Y}   {item['slot']['label']}"
        if _slot_matches(item["slot"], min_start):
            print(_paint(line, "green"))
        else:
            print(line)
    print()

    raw = input("Enter slot number(s) to book (e.g. '3' or '1,5'; 'q' to quit): ")
    raw = raw.strip()
    if not raw or raw.lower() in ("q", "quit", "cancel"):
        print("Aborted. Nothing booked.")
        return

    nums = [int(x) for x in re.split(r"[,\s]+", raw) if x.strip().isdigit()]
    if not nums:
        print("No valid slot number entered. Nothing booked.")
        return

    selections, seen_days = [], set()
    for num in nums:
        if num < 1 or num > len(all_slots):
            print(f"  ! [{num}] out of range (1-{len(all_slots)}); skipped.")
            continue
        item = all_slots[num - 1]
        if item["date"] in seen_days:
            print(f"  ! [{num}] {item['date']:%a %d %b} skipped - one slot per day.")
            continue
        seen_days.add(item["date"])
        selections.append(item)

    if not selections:
        print("Nothing valid to book.")
        return

    print("\nYou are about to book:")
    for item in selections:
        print(f"  {item['date']:%a %d %b %Y}   {item['slot']['label']}")
    confirm = input("Confirm booking? [y/N] ")
    if confirm.strip().lower() not in ("y", "yes"):
        print("Aborted. Nothing booked.")
        return

    description = cfg.get("description", "Padel booking")
    for item in selections:
        date_str = item["date"].strftime("%Y-%m-%d")
        r = submit_booking(s, cfg, meta, date_str, item["slot"], description,
                           verbose=verbose)
        ok = verify_booking_created(s, date_str, item["slot"]["start"])
        record_booking(date_str, item["slot"]["label"], ok)
        if ok:
            status = "OK (confirmed in My Bookings)"
        else:
            err = _booking_error(r) or ("not created - portal gave no error, "
                                        "check My Bookings")
            status = f"FAILED ({err})"
        print(f"  {item['date']:%a %d %b} {item['slot']['label']}: {status}"
              f"  (HTTP {r.status_code})")
    print("Done.")


# --------------------------------------------------------------------------- #
# Auto-booking bot
# --------------------------------------------------------------------------- #
WEEKDAY_NUM = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
               "friday": 4, "saturday": 5, "sunday": 6}
WEEKDAY_NAME = {v: k for k, v in WEEKDAY_NUM.items()}  # 0-6 -> "monday" ...


def target_weekday_set(cfg: dict) -> set:
    """The configured target weekdays as a set of 0-6 (Mon-Sun) numbers.

    When ``preferred_slots`` is a per-day dict (the recommended form) the
    target days are simply its keys, so no separate ``target_weekdays`` is
    needed. When ``preferred_slots`` is a legacy flat list (which carries no
    day information), the days come from ``target_weekdays`` (default
    ``["Sunday", "Tuesday"]``).
    """
    slots = cfg.get("preferred_slots")
    if isinstance(slots, dict) and slots:
        names = list(slots.keys())
    else:
        names = cfg.get("target_weekdays", ["Sunday", "Tuesday"])
    return {WEEKDAY_NUM[str(n).strip().lower()] for n in names}


def preferred_slots_for_day(cfg: dict, day_name: str) -> list:
    """Preferred slot start times (HH:MM) for *day_name*, in priority order.

    Supports two config forms for ``preferred_slots``:
      * per-day dict (recommended):
            {"Sunday": ["20:00", "19:00", "21:00"],
             "Tuesday": ["20:00", "21:00"]}
      * legacy flat list (applies to every day):
            ["20:00", "21:00", "19:00"]

    Day names match case-insensitively. Returns ``[]`` when the day has no
    configured slots (the caller should then skip booking that day).
    """
    slots = cfg.get("preferred_slots")
    if slots is None:
        return ["20:00", "21:00", "19:00"]
    if isinstance(slots, dict):
        lowered = {str(k).strip().lower(): v for k, v in slots.items()}
        value = lowered.get(day_name.strip().lower(), [])
        return list(value) if isinstance(value, list) else []
    # Legacy flat list: the same slots for every day.
    return list(slots)


def bookable_targets(targets: set, now: datetime, dates: list | None = None,
                     window_days: int = BOOKING_WINDOW_DAYS) -> list:
    """The target days inside the portal's 7-day window, in attempt order.

    Candidacy is the HARD limit — the rolling window (today .. today+6). Every
    target weekday has exactly one occurrence in it, and that day is a
    candidate **whether or not** the "Check in date" picker lists it yet: a
    target day the portal has not opened is not skipped, it is attempted
    through ``run_booking_race``, which refreshes every ``slot_retry_minutes``
    until the portal opens it. Days outside the window are not returned —
    those can never be booked now and waiting for them would be pointless.

    The picker only decides the ORDER:

      * days it already offers come first, freshest first (its last date is
        the one that just opened, so its slots are the least contested);
      * days inside the window that it does not list yet come last, nearest
        first, so the day that will open soonest is waited for first.

    Note: ``already_booked_successfully`` is deliberately not filtered here —
    that notification belongs in ``book_day()`` so the user sees one
    consistent message for every desired day.
    """
    today = now.date()
    window_days = max(1, int(window_days))
    candidates = []
    for target_weekday in sorted(targets):
        days_ahead = (target_weekday - today.weekday() + 7) % 7
        # One occurrence per weekday inside the window.
        if days_ahead < window_days:
            candidates.append(today + timedelta(days=days_ahead))
    if not candidates:
        return []

    if not dates:
        # Picker unreadable: freshest first, as the old arithmetic did.
        freshest = max(candidates)
        return [freshest] + sorted(d for d in candidates if d != freshest)

    opened = [d for d in candidates if d in dates]
    pending = sorted(d for d in candidates if d not in dates)
    if not opened:
        return pending
    freshest = max(opened)
    return ([freshest]
            + sorted(d for d in opened if d != freshest)
            + pending)


def prefs_signature(cfg: dict) -> str:
    """A stable string identifying the current booking preferences, so the
    loop can detect when /prefs (or a config.json edit) adds a new day or
    time slot and react by (re)attempting those bookings."""
    return json.dumps(
        {"preferred_slots": cfg.get("preferred_slots"),
         "target_weekdays": cfg.get("target_weekdays")},
        sort_keys=True, default=str)


def pick_best_slot(slots: list, preferred: list):
    """Return the first available slot matching the preference order."""
    for pref in preferred:
        for sl in slots:
            if sl["start"] == pref:
                return sl
    return None


def keepalive(cfg: dict) -> requests.Session:
    """Validate + refresh the persisted session (extends server-side lifetime)."""
    s = get_authenticated_session(cfg)
    save_session(s)
    return s


def ts_prefix() -> str:
    """Return the current local time as a log-line prefix, e.g.
    ``[25/09/2026 01:12:23]`` (brackets included)."""
    return datetime.now().strftime("[%d/%m/%Y %H:%M:%S]")


def run_booking_race(cfg: dict, target: datetime, preferred: list,
                     wait_for_open: bool = True, timeout: int | None = None,
                     max_attempts: int | None = None, dry_run: bool = False,
                     retry_minutes: int | None = None, stop=None,
                     on_wait=None, on_retry=None):
    """Poll the portal and book the best preferred slot when one is bookable.

    Two facts are read on every refresh and must NOT be conflated:

      * the portal's rolling window (today .. today+6, ``booking_window_days``)
        is the HARD limit — a day outside it can never be booked now, so the
        race STOPS (``outside_window``) rather than waiting forever;
      * the "Check in date" option list says which days INSIDE that window the
        portal has opened so far. A target day that lies in the window but is
        not listed yet is not a failure: the window simply has not slid to it,
        so the race keeps refreshing every ``slot_retry_minutes`` (default 2
        min) until the portal lists the day AND a preferred slot is free —
        which is what a program started while the target day is still closed
        needs. The slot endpoint is deliberately not queried for such a day:
        it answers for closed dates, which is what used to produce a booking
        the portal then rejected.

    Outcomes (4th returned value):
      * ``slot_found``     — day IS listed by the picker AND a preferred slot
        free: booked (or identified, in dry-run) and the race stopped.
      * ``not_yet_open``   — never positively confirmed bookable within the
        bound (the picker does not list the date yet, or could not be read):
        no booking was ever attempted. This is also the outcome when the
        picker's last option has not reached the target: the window simply has
        not slid that far, so the scheduler re-attempts it later.
      * ``no_slot``        — the day WAS listed by the picker, but no preferred
        slot was free within the bound.
      * ``outside_window`` — before today's date: stopped as of now.
      * ``stopped``        — ``stop`` event set (keeps ``/stopautobook``
        responsive during a long wait).

    Booking is FAIL-CLOSED: the slot endpoint does not validate dates, so it
    answers for days the "Check in date" form has no option for. A POST is
    therefore only ever made when that picker positively lists the target; an
    empty/unreadable picker means "unknown", which keeps the race waiting and
    never authorises a submission.

    ``timeout`` bounds the whole loop and defaults to
    ``booking_max_wait_minutes`` (720 = 12h); ``max_attempts`` (default:
    unlimited) caps the refreshes instead, so a single check is ``max_attempts=1``.

    Returns (slot, all_slots, status_or_None, outcome). With dry_run=True the
    slot is only identified, no booking is submitted. ``stop`` is an optional
    ``threading.Event`` checked before every refresh, so a long wait cannot
    delay ``/stopautobook`` (outcome ``"stopped"``). ``on_wait`` is an optional
    no-arg callback run before every refresh — the autobooking loop uses it to
    keep the session alive during waits that can span hours. ``on_retry`` is an
    optional callback taking the wait reason (one string); it fires **only on
    the first refresh** of an attempt, i.e. once the race has decided to wait
    rather than book now. The race never repeats it, so a caller can use it for
    one "I am trying, but this day is not available yet" notice without having
    to de-duplicate hours of identical messages itself.
    """
    open_hour = int(cfg.get("booking_open_hour", 0))
    if retry_minutes is None:
        retry_minutes = int(cfg.get("slot_retry_minutes", 2))
    retry_secs = max(1, int(retry_minutes)) * 60
    if timeout is None:
        timeout = max(retry_secs, int(cfg.get("booking_max_wait_minutes",
                                              BOOKING_MAX_WAIT_MINUTES)) * 60)
    now = datetime.now()

    # Pre-warm the shared session + booking meta (lowers the booking latency):
    # one fetch that also validates the session.
    s, meta = get_booking_meta(cfg)

    # Micro-optimisation: if this is the day at the far end of the rolling
    # window and the portal opens it at ``booking_open_hour`` (a convention,
    # not a fact we rely on), sleep until a few seconds before that hour so the
    # first refresh happens right when the day may open. Skipped entirely when
    # booking_open_hour is unset/invalid-ish (0 keeps polling immediately).
    if open_hour and wait_for_open:
        _, window_last = window_limit(cfg, now)
        open_dt = now.replace(hour=open_hour, minute=0, second=0,
                              microsecond=0)
        if target.date() == window_last and now < open_dt:
            while datetime.now() < open_dt - timedelta(seconds=3):
                time.sleep(0.3)

    api_date = f"{target.day}-{target.month}-{target.year}"
    target_date = target.date()
    deadline = datetime.now() + timedelta(seconds=timeout)
    last_slots: list = []
    attempt = 0
    ever_opened = False
    while True:
        attempt += 1

        # Stop honours /stopautobook: a long refresh wait must not delay it.
        if stop is not None and stop.is_set():
            return None, last_slots, None, "stopped"

        # (1) HARD limit: never before today. The picker's own last date is the
        #     real ceiling when it could be read (it can stop short of today+6),
        #     but an UNREADABLE picker must not pretend to know the ceiling —
        #     that would refuse a date only because parsing failed.
        first, arith_last = window_limit(cfg, datetime.now())
        offered = portal_bookable_dates(cfg)
        if target_date < first:
            return None, [], None, "outside_window"
        if offered and target_date > max(offered):
            # The picker was read and demonstrably does not reach this date.
            return None, [], None, "not_yet_open"

        # (2) Is the date bookable? Only a POSITIVE confirmation counts: the
        #     target must actually appear in the picker. An empty picker means
        #     "unknown", and unknown is NOT permission to POST — the slot
        #     endpoint answers for dates the form has no option for, which is
        #     precisely how a booking gets submitted and rejected with HTTP 303.
        opened = bool(offered) and target_date in offered
        if opened:
            ever_opened = True

            # (3) Are its slots open? Only asked once the picker offers the day.
            slots = get_available_slots(s, cfg, api_date, meta)
            last_slots = slots
            best = pick_best_slot(slots, preferred)
            if best:
                if dry_run:
                    return best, slots, None, "slot_found"
                description = cfg.get("description", "Padel booking")
                status = submit_booking_fast(s, cfg, meta,
                                             target.strftime("%Y-%m-%d"),
                                             best, description)
                return best, slots, status, "slot_found"

        # (4) Nothing bookable yet -> the same 2-minute refresh process.
        if (max_attempts is not None and attempt >= max_attempts
                or datetime.now() >= deadline):
            if not ever_opened:
                return None, [], None, "not_yet_open"
            return None, last_slots, None, "no_slot"
        reason = ("the portal has not opened this date yet" if not opened
                  else "no preferred slot is free")
        shown_last = max(offered) if offered else arith_last
        print(f"{ts_prefix()} [bot] {target:%a %d %b} is inside the portal's "
              f"window (offered so far up to {shown_last:%a %d %b}"
              f"{'' if offered else ', picker unreadable'}) but {reason}; "
              f"refreshing in {retry_secs // 60} min...", flush=True)
        if on_retry is not None and attempt == 1:
            # One notice per attempt (the caller decides what to do with it,
            # e.g. a single Telegram heads-up); a failing callback must never
            # abort the wait.
            try:
                on_retry(reason)
            except Exception as e:  # pylint: disable=broad-exception-caught
                print(f"{ts_prefix()} [bot] retry notice failed (non-fatal): "
                      f"{e}", flush=True)
        if on_wait is not None:
            # Long waits must not starve the session: refresh it before sleeping
            # so a hours-long wait cannot end with an expired session.
            try:
                on_wait()
            except Exception as e:  # pylint: disable=broad-exception-caught
                print(f"{ts_prefix()} [bot] wait refresh failed (non-fatal): "
                      f"{e}", flush=True)
        time.sleep(retry_secs)


def cmd_autobook(cfg: dict, args: list) -> None:
    """One-shot: book a target date now, waiting 2 min at a time while the
    portal has not opened that date yet (bounded by
    ``booking_max_wait_minutes``).

    With no date given the target is the freshest date the portal offers, or —
    if its picker cannot be read — the last day of the rolling window.
    """
    dry_run = "--dry-run" in args
    date_str = next((a for a in args if not a.startswith("--")), None)
    if date_str:
        target = parse_date(date_str)
    else:
        # Never assume today+6: use the portal's own view of the window.
        try:
            offered = portal_bookable_dates(cfg)
        except Exception:  # pylint: disable=broad-exception-caught
            offered = []
        if offered:
            target = datetime.combine(offered[-1], datetime.min.time())
        else:
            target = datetime.combine(
                window_limit(cfg, datetime.now())[1], datetime.min.time())

    preferred = preferred_slots_for_day(cfg, target.strftime("%A"))
    target_str = target.strftime("%Y-%m-%d")
    tag = " [DRY RUN - no booking will be made]" if dry_run else ""
    print(f"Auto-booking {target:%a %d %b %Y}{tag}")
    if not preferred:
        print(f"  No preferred slots configured for {target:%A}; "
              f"nothing to book.")
        return
    print(f"  prefs: {', '.join(preferred)}")
    if not dry_run and already_booked_successfully(target_str):
        print(f"Already booked {target:%a %d %b} successfully (see "
              f"booked.json). Not booking again.")
        return
    retry = int(cfg.get("slot_retry_minutes", 2))
    maxw = int(cfg.get("booking_max_wait_minutes", BOOKING_MAX_WAIT_MINUTES))
    best, slots, r, outcome = run_booking_race(
        cfg, target, preferred, dry_run=dry_run)
    if outcome == "outside_window":
        # Beyond the rolling window: it can never be booked now, so waiting
        # for it would be pointless.
        print(f"OUTSIDE THE PORTAL WINDOW: {target:%a %d %b %Y} is not inside "
              f"the portal's {booking_window_days(cfg)}-day rolling window "
              f"(today .. today+{booking_window_days(cfg) - 1}).")
        note = bookable_window_note(cfg).strip()
        print(f"  {note or 'Portal window could not be read.'}")
        sys.exit(2)
    if outcome == "not_yet_open":
        # Inside the window but the picker never opened the date while we
        # waited: nothing was attempted, and nothing would have succeeded.
        print(f"NOT OPEN YET: {target:%a %d %b %Y} is inside the portal's "
              f"{booking_window_days(cfg)}-day window, but its 'Check in date' "
              f"never offered it while we refreshed every {retry} min for "
              f"{maxw} min. Nothing was attempted.")
        print("  The autobooking scheduler books it as soon as the portal "
              "opens that date.")
        sys.exit(3)
    if best is None:
        print(f"STOPPED: the portal opened {target:%a %d %b %Y} but none of "
              f"the preferred slots was free after refreshing every {retry} "
              f"min (up to {maxw} min).")
        print(f"  Slots seen for that date ({len(slots)}):")
        for sl in slots:
            print(f"    {sl['label']}  [{sl['value']}]")
        note = bookable_window_note(cfg).strip()
        print(f"  {note or 'Portal window could not be read.'}")
        sys.exit(1)
    print(f"Selected slot: {best['label']}  [{best['value']}]")
    if dry_run:
        print("DRY RUN: no booking was submitted.")
        return
    s = get_authenticated_session(cfg)
    ok = verify_booking_created(s, target_str, best["start"])
    record_booking(target_str, best["label"], ok)
    if r is not None:
        print("Response status:", r)
    if ok:
        print("Booking CONFIRMED - it now appears in your 'My Bookings'.")
        # Sync Google Calendar (non-fatal)
        try:
            if sync_booking(target_str, best["label"], cfg,
                            cfg.get("description", "Padel booking")):
                print("📅 Event created on Google Calendar.")
            else:
                print("⚠️ Booking confirmed, but the Google Calendar "
                      "event could not be created.")
        except Exception as e:  # pylint: disable=broad-exception-caught
            print(f"⚠️ Google Calendar sync failed (non-fatal): {e}")
    else:
        print(f"Booking FAILED: the portal did not create the booking "
              f"(HTTP {r})")


def cmd_keepalive(cfg: dict, _args: list) -> None:
    """CLI: refresh and re-save the persisted session."""
    keepalive(cfg)
    print("Keep-alive OK. Session refreshed and saved.")


def cmd_telegram(cfg: dict, _args: list) -> None:
    """CLI: run the interactive Telegram bot (long polling)."""
    from padel_telegram import PadelBot  # local import keeps the CLI light
    try:
        PadelBot(cfg).run()
    except KeyboardInterrupt:
        # Ctrl+C before/while the poll worker is starting up (e.g. during
        # the initial getMe). run() handles the steady-state case itself.
        print(f"\n{ts_prefix()} [telegram] stopped.", flush=True)


def run_autobook_loop(cfg: dict, log, *, notify=None, relogin=None,
                      dry_run: bool = False, stop_event=None) -> None:
    """Resident autobooking loop: keep the session alive and book preferred
    slots. It reacts to three events:

      1. Program start  -> attempt EVERY desired day inside the portal's
         rolling window, in one process: the days the "Check in date" picker
         already offers first (freshest first), then the days inside the window
         it has not opened yet — those are NOT skipped, each one refreshes the
         portal every ``slot_retry_minutes`` until it opens and a preferred
         slot is free (bounded by ``booking_max_wait_minutes``).
      2. Prefs change   -> (re)attempt every desired day inside the window.
      3. The portal starts offering a NEW date (its window slid forward) ->
         attempt only those new days.

    There is no midnight/``today+6`` arithmetic any more: the window width
    comes from ``booking_window_days`` (default 7) and which of those days are
    open comes from the portal itself.

    Shared by the standalone daemon and the Telegram bot (which runs it in a
    background thread so `python padel_booking.py telegram` also autobooks).

    log(msg)           -- required; where loop events are written
    notify(text)       -- optional; broadcast a notification (e.g. Telegram)
    relogin() -> bool  -- optional; restore an expired session (may block
                          until the OTP is provided or it times out)
    dry_run            -- identify the slot but do not submit the booking
    stop_event         -- optional threading.Event; stop the loop when set
    """
    # NOTE: ``targets``, ``keepalive_secs`` and ``open_hour`` are re-read on
    # every pass of the loop (below) so that config changes made at runtime
    # (e.g. the bot's /prefs command) apply without a restart.

    def notify_(text: str) -> None:
        if notify is None:
            return
        try:
            notify(text)
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    def ensure_session() -> bool:
        """Return True when the session is valid; otherwise try re-login."""
        try:
            keepalive(cfg)
            return True
        except Exception as e:  # pylint: disable=broad-exception-caught
            if relogin is None:
                return False
            log(f"Session invalid ({e}); asking for a fresh OTP...")
            try:
                ok = relogin()
            except Exception as e2:  # pylint: disable=broad-exception-caught
                log(f"re-login raised: {e2}")
                return False
            if ok:
                log("Session restored via OTP login.")
            else:
                log("Re-login failed; will retry on the next pass.")
            return ok

    def book_day(target, now) -> bool:
        """Attempt to book the preferred slot for a single target day.

        Returns True when a booking was submitted (or identified in dry-run),
        False otherwise (no preferred slot, session invalid, or an error).
        """
        target_str = target.strftime("%Y-%m-%d")
        preferred = preferred_slots_for_day(
            cfg, WEEKDAY_NAME[target.weekday()])
        if not preferred:
            log(f"Window open but no preferred slots are configured for "
                f"{target:%A}; skipping.")
            return False
        if not ensure_session():
            log("*** SKIPPED booking: session invalid and re-login failed; "
                "will retry on the next pass.")
            notify_("❌ Booking skipped: session expired and automatic "
                    "re-login failed. Send /login in the bot when you're "
                    "ready.")
            return False
        # Fast path (no network): if we already booked this day successfully
        # (local memory), skip entirely.
        if already_booked_successfully(target_str):
            log(f"*** {target:%a %d %b %Y} is already booked (per booked.json); "
                f"skipping.")
            notify_(f"ℹ️ {target:%a %d %b} is already booked.")
            return True
        # Fast path (no network): if the user already has a booking for this
        # day (per the in-memory My Bookings list, refreshed once at
        # start/prefs-save), skip entirely — no slots endpoint call needed.
        if is_day_booked(target):
            log(f"*** {target:%a %d %b %Y} is already booked (per My Bookings); "
                f"skipping.")
            notify_(f"ℹ️ {target:%a %d %b} is already booked.")
            return True
        # Fast path (no network): the portal allows max 3 active bookings
        # within the 7-day booking window. If we already have 3, the slots
        # endpoint will return 0 slots for ANY day in that window — skip.
        active_count = count_active_bookings(target)
        if active_count >= MAX_TOTAL_BOOKINGS:
            log(f"*** {target:%a %d %b %Y}: booking limit reached "
                f"({active_count}/{MAX_TOTAL_BOOKINGS} in 7-day window); "
                f"skipping.")
            notify_(f"ℹ️ {target:%a %d %b}: max "
                    f"{MAX_TOTAL_BOOKINGS} bookings in this window.")
            return True
        log(f"*** Booking {target:%a %d %b %Y} "
            f"(prefs: {', '.join(preferred)}) "
            f"{'[DRY RUN]' if dry_run else ''}...")
        # One Telegram heads-up per day per attempt: the race reports only its
        # first wait, so a 12h wait cannot flood the chat. The final result
        # (booked / no slot / not opened yet) is reported separately below.
        def on_retry(reason: str) -> None:
            notify_(f"⏳ Trying to book {target:%a %d %b}: {reason}. "
                    f"Refreshing every {int(cfg.get('slot_retry_minutes', 2))} "
                    f"min — I will report the result here.")

        t0 = time.time()
        try:
            best, slots, r, outcome = run_booking_race(
                cfg, datetime.combine(target, datetime.min.time()),
                preferred, wait_for_open=False, dry_run=dry_run,
                stop=stop_event, on_wait=lambda: keepalive(cfg),
                on_retry=on_retry)
            t_race = time.time() - t0
            # A future day the picker does not list yet is NOT a failure: it is
            # simply not open, and trigger (3) re-attempts it as soon as the
            # portal starts offering it. Only a past date is truly unbookable.
            if outcome in ("outside_window", "not_yet_open"):
                # Callers pass either a date or a datetime; normalise before
                # comparing so neither type can raise here.
                target_day = getattr(target, "date", lambda: target)()
                if target_day < window_limit(cfg, now)[0]:
                    log(f"*** {target:%a %d %b %Y} is in the past; nothing to "
                        f"book.")
                    notify_(f"ℹ️ {target:%a %d %b} is in the past — nothing to "
                            f"book.")
                else:
                    log(f"*** {target:%a %d %b %Y} is not offered by the "
                        f"portal's 'Check in date' picker yet (its window has "
                        f"not opened); no booking attempted. Refreshed every "
                        f"{int(cfg.get('slot_retry_minutes', 2))} min for up to "
                        f"{int(cfg.get('booking_max_wait_minutes', BOOKING_MAX_WAIT_MINUTES))} "
                        f"min, and it is re-tried whenever the portal opens it.")
                    notify_(f"ℹ️ {target:%a %d %b}: the portal has not opened "
                            f"it yet — I will book it as soon as it opens.")
                return False
            if outcome == "stopped":
                log(f"*** Race for {target:%a %d %b} interrupted "
                    f"(stop requested).")
                return False
            if best:
                if dry_run:
                    log(f"*** DRY RUN: would book {best['label']} "
                        f"[{best['value']}] (no booking made). "
                        f"[race: {t_race:.2f}s]")
                    return True
                # Verify via the slots endpoint (~0.3s): if the booking
                # succeeded, the day now has 0 free slots. This replaces the
                # slow mybookings round-trip entirely.
                s = get_session()
                meta = get_booking_meta(cfg)[1]
                api_date = f"{target.day}-{target.month}-{target.year}"
                verify_slots, _ = _slots_endpoint(s, cfg, api_date, meta)
                t_verify = time.time() - t0
                ok = len(verify_slots) == 0
                record_booking(target_str, best["label"], ok)
                timing = (f"[race: {t_race:.2f}s | "
                          f"verify: {t_verify - t_race:.2f}s | "
                          f"total: {t_verify:.2f}s]")
                if ok:
                    log(f"*** BOOKED {best['label']} [{best['value']}] "
                        f"(confirmed: 0 slots remaining) {timing}")
                    notify_(f"✅ BOOKED: {target:%a %d %b} {best['label']}")
                    # Sync Google Calendar (non-fatal)
                    try:
                        if sync_booking(target_str, best["label"], cfg,
                                        cfg.get("description",
                                                "Padel booking")):
                            log("Google Calendar event created.")
                            notify_("📅 Event created on Google Calendar.")
                        else:
                            log("Google Calendar event could not be "
                                "created (disabled or error).")
                            notify_("⚠️ Booking confirmed, but the "
                                    "Google Calendar event could not "
                                    "be created.")
                    except Exception as e:  # pylint: disable=broad-exception-caught
                        log(f"Google Calendar sync failed (non-fatal): {e}")
                        notify_("⚠️ Booking confirmed, but the "
                                "Google Calendar event could not "
                                "be created.")
                    # Refresh the in-memory bookings list so /mybookings
                    # shows the new slot and count_active_bookings is
                    # accurate for the next attempt.
                    try:
                        refresh_state(cfg)
                        log("Bookings cache refreshed after autobook.")
                    except Exception as e2:  # pylint: disable=broad-exception-caught
                        log(f"Cache refresh after autobook failed (non-fatal): {e2}")
                    return True
                err = (f"booking not confirmed (slots still available, "
                       f"HTTP {r})" if r else
                       "booking not confirmed (slots still available)")
                log(f"*** FAILED to book {best['label']} [{best['value']}]: "
                    f"{err} {timing}")
                notify_(f"❌ Booking FAILED for {target:%a %d %b %Y} "
                        f"{best['label']}.\n{err}")
                return False
            # No preferred slot was available. The day is fully booked by
            # someone else (0 slots + no existing booking of ours = slot
            # taken).
            # Safety net: if the in-memory bookings list is empty/stale, do
            # a one-time mybookings check to see if we actually already have
            # a booking for this day.
            if len(slots) == 0 and not _STATE["bookings"]:
                log(f"*** WARNING: 0 slots but in-memory bookings list is "
                    f"empty — doing one-time mybookings check...")
                try:
                    s_chk = get_session()
                    fresh = _fetch_my_bookings_fast(s_chk)
                    _STATE["bookings"] = fresh
                    log(f"*** Refreshed bookings list: {len(fresh)} "
                        f"booking(s).")
                    if is_day_booked(target):
                        log(f"*** {target:%a %d %b %Y} IS already booked "
                            f"(confirmed via mybookings).")
                        notify_(f"ℹ️ {target:%a %d %b} is already booked.")
                        return True
                except Exception as e2:  # pylint: disable=broad-exception-caught
                    log(f"*** mybookings safety-net check failed: {e2}")
            log(f"*** FAILED: {target:%a %d %b %Y} IS offered by the portal "
                f"but none of the preferred slots was free after refreshing "
                f"every {int(cfg.get('slot_retry_minutes', 2))} min "
                f"({len(slots)} slots seen).")
            notify_(f"❌ No slot free for {target:%a %d %b} "
                    f"({len(slots)} slots seen).")
            return False
        except Exception as e:  # pylint: disable=broad-exception-caught
            log(f"*** ERROR during booking: {e}")
            notify_(f"❌ Booking error: {e}")
            return False

    last_keepalive = time.time()
    started = False           # whether the initial catch-up has been done
    last_window = None        # portal dates already processed (see trigger 3)
    last_prefs_sig = None     # last seen preferences signature
    # Re-attempt cadence for desired days the picker does not list yet, so a
    # day waiting to be opened is retried without hammering the portal.
    retry_wait_secs = max(60, int(cfg.get("slot_retry_minutes", 2)) * 60)
    last_pending_try = 0.0
    targets = target_weekday_set(cfg)

    # --- initial state refresh (the ONE slow call) -------------------------
    # Fetch the booking meta + the user's existing bookings from the portal.
    # This is the single slow call (~5.5s) that is done ONCE at program start.
    # After this, all booking logic runs against the in-memory state with no
    # further mybookings calls.
    try:
        refresh_state(cfg)
        log(f"State refreshed: {len(_STATE['bookings'])} existing bookings "
            f"loaded.")
    except Exception as e:  # pylint: disable=broad-exception-caught
        log(f"WARNING: initial state refresh failed: {e}. "
            f"Booking will still work, but 'already booked' checks will "
            f"fall back to the slots endpoint.")
    while not (stop_event is not None and stop_event.is_set()):
        now = datetime.now()

        # Re-read the config each pass so runtime changes (e.g. the bot's
        # /prefs command) apply without a restart.
        try:
            targets = target_weekday_set(cfg)
        except KeyError as e:
            log(f"WARNING: unknown day name {e} in preferred_slots; "
                "keeping the previous target days.")
        keepalive_secs = int(cfg.get("keepalive_minutes", 20)) * 60
        window_days = booking_window_days(cfg)

        # --- keep-alive ---------------------------------------------------
        if time.time() - last_keepalive >= keepalive_secs:
            try:
                keepalive(cfg)
                log("Keep-alive OK (session refreshed).")
                # Reconcile booked.json against the portal's real bookings
                # (catches cancellations made directly on the portal).
                try:
                    s = get_session()
                    bookings = _fetch_my_bookings_fast(s)
                    _STATE["bookings"] = bookings
                    _reconcile_booked(bookings)
                except Exception:
                    pass  # non-fatal
            except Exception as e:  # pylint: disable=broad-exception-caught
                log(f"WARNING: keep-alive failed: {e}. "
                    f"Session may be expired - re-login needed!")
                if relogin is not None:
                    ensure_session()
            last_keepalive = time.time()

        # --- booking -------------------------------------------------------
        # Three triggers, all driven by the window the PORTAL advertises (the
        # "Check in date" option list, re-read at most once per TTL):
        #   (1) Program start  -> attempt EVERY desired day inside the rolling
        #       window: the days the picker already offers first (freshest
        #       first), then the days inside the window it has not opened yet,
        #       which are waited for (2 min at a time) rather than skipped.
        #   (2) Prefs changed  -> (re)attempt every desired day inside the
        #       window, same order.
        #   (3) The window slid forward or the portal started offering a NEW
        #       date -> attempt ONLY those new days.
        #
        # There is no +6 arithmetic here: the window WIDTH comes from
        # booking_window_days (7) and which of those days are OPEN comes from
        # the portal. A desired weekday whose date is still outside the window
        # is not attempted (it can never be booked now); one that is inside but
        # unopened IS attempted, by waiting.
        prefs_sig = prefs_signature(cfg)
        try:
            # Cached: at most one page fetch per _BOOKABLE_DATES_TTL, so this
            # is cheap inside the 0.5s loop.
            window_dates = portal_bookable_dates(cfg)
        except Exception as e:  # pylint: disable=broad-exception-caught
            log(f"WARNING: window lookup failed: {e}. Falling back to the "
                f"arithmetic window.")
            window_dates = []
        candidates = bookable_targets(targets, now, window_dates or None,
                                      window_days)
        if not window_dates:
            # Picker unreadable: fall back to the hard rolling window so the
            # messages below still describe what the portal can accept.
            window_dates = rolling_window_dates(now, window_days)
        fresh = ([d for d in candidates if last_window is not None
                  and d not in last_window] if last_window else [])
        # A desired day inside the rolling window that the picker does not list
        # yet must NOT be forgotten: it is exactly the "the server has not
        # opened the 17th yet" case, and the point of autobooking is to keep
        # trying until it does. last_window holds the picker's dates, so such a
        # day would never look "fresh"; re-arm it on its own schedule instead.
        pending_now = [d for d in candidates if d not in window_dates]
        due = bool(pending_now) and (
            time.time() - last_pending_try >= retry_wait_secs)
        if not started:
            # (1) Program start: catch up on every desired day inside the
            # window, so a day the portal has not opened yet enters the same
            # refresh-every-2-min process instead of being skipped.
            not_open = [d for d in candidates if d not in window_dates]
            log("Startup: attempting to book all desired days "
                f"(portal window {window_label(window_dates)})...")
            if not_open:
                log(f"Startup: portal has not opened "
                    f"{window_label(not_open)} yet — waiting for them "
                    f"(refreshing every {int(cfg.get('slot_retry_minutes', 2))} "
                    f"min, up to {int(cfg.get('booking_max_wait_minutes', BOOKING_MAX_WAIT_MINUTES))} "
                    f"min each).")
            for target in candidates:
                book_day(target, now)
            started = True
            last_prefs_sig = prefs_sig
            last_window = set(window_dates)
        elif prefs_sig != last_prefs_sig:
            # (2) Preferences changed (e.g. /prefs added a day or a time
            # slot): refresh the in-memory state (the ONE slow call), then
            # (re)attempt every desired day the portal offers now.
            log("Preferences changed: refreshing state and booking...")
            try:
                refresh_state(cfg)
            except Exception as e:  # pylint: disable=broad-exception-caught
                log(f"WARNING: state refresh after prefs change failed: {e}.")
            for target in candidates:
                book_day(target, now)
            last_prefs_sig = prefs_sig
            last_window = set(window_dates)
        elif fresh or due:
            # (3) Either the rolling window slid so that a new target date is
            # now inside it, the portal started offering dates it did not offer
            # before, or a desired day is still waiting for its window to open:
            # attempt those (an unopened one is waited for).
            todo = sorted(set(fresh) | (set(pending_now) if due else set()))
            names = ", ".join(d.strftime("%a %d %b") for d in todo)
            log(f"Booking {names} "
                f"(portal window {window_label(window_dates)}) — waiting while "
                f"the portal has not opened them yet...")
            if pending_now:
                last_pending_try = time.time()
            for d in todo:
                if not already_booked_successfully(
                        d.strftime("%Y-%m-%d")):
                    book_day(d, now)
            last_window = set(window_dates)
        # else: nothing new to do - the loop keeps the session alive and
        # waits for the portal's window to change.

        time.sleep(0.5)


def _cmd_google_auth(cfg: dict, args: list) -> None:
    """CLI: run the Google Calendar OAuth flow."""
    from google_calendar import run_google_auth
    positional = [a for a in args if not a.startswith("--")]
    cid = positional[0] if len(positional) >= 1 else ""
    csec = positional[1] if len(positional) >= 2 else ""
    run_google_auth(cfg, cid, csec)


def _cmd_google_test(cfg: dict) -> None:
    """CLI: test the Google Calendar connection."""
    from google_calendar import run_google_test
    run_google_test(cfg)


def _cmd_google_list_calendars(cfg: dict) -> None:
    """CLI: list all accessible Google calendars."""
    from google_calendar import run_google_list_calendars
    run_google_list_calendars(cfg)


def main() -> None:
    """Parse the command line and dispatch to the matching command."""
    commands = ("login-start", "login-finish", "login-status", "explore",
                "window", "slots", "book", "pick", "mybookings", "cancel",
                "autobook", "keepalive", "telegram", "google-auth",
                "google-test", "google-list-calendars")
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        print(__doc__)
        sys.exit(1)
    # Per-request timing for every portal call (login, session check, ...):
    # `[http] GET https://... -> 200 in 1.32s` / `... FAILED after 90.02s: ...`
    set_http_log(lambda m: print(f"{ts_prefix()} {m}", flush=True))
    # Confirms which timeout the deployed image actually uses (was the T6 fix
    # picked up by this container?).
    print(f"{ts_prefix()} [start] portal={BASE_URL} "
          f"session timeout={PORTAL_TIMEOUT}s", flush=True)
    cfg = load_config()
    cmd = sys.argv[1]
    args = sys.argv[2:]
    if cmd == "login-start":
        cmd_login_start(cfg)
    elif cmd == "login-finish":
        cmd_login_finish(cfg, args)
    elif cmd == "login-status":
        cmd_login_status(cfg)
    elif cmd == "explore":
        cmd_explore(cfg)
    elif cmd == "window":
        cmd_window(cfg, args)
    elif cmd == "slots":
        cmd_slots(cfg, args)
    elif cmd == "book":
        cmd_book(cfg, args)
    elif cmd == "pick":
        cmd_pick(cfg, args)
    elif cmd == "mybookings":
        cmd_mybookings(cfg, args)
    elif cmd == "cancel":
        cmd_cancel(cfg, args)
    elif cmd == "autobook":
        cmd_autobook(cfg, args)
    elif cmd == "keepalive":
        cmd_keepalive(cfg, args)
    elif cmd == "telegram":
        cmd_telegram(cfg, args)
    elif cmd == "google-auth":
        _cmd_google_auth(cfg, args)
    elif cmd == "google-test":
        _cmd_google_test(cfg)
    elif cmd == "google-list-calendars":
        _cmd_google_list_calendars(cfg)


if __name__ == "__main__":
    main()
