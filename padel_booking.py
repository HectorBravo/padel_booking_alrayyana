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
  python3 padel_booking.py slots 2026-09-26        # list available time slots for a date
  python3 padel_booking.py slots 2026-09-26 --from 18:00   # ...only from 6pm on
  python3 padel_booking.py book 2026-09-26 "18:00-19:00" [description] [--verbose]
  python3 padel_booking.py pick                    # interactive: browse all
  python3 padel_booking.py pick --from 18:00       #   ...highlight 6pm+ slots green
                                                   # available slots (today ->
                                                   # today+6) and choose one
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
  The portal opens each day's bookings at midnight (00:00) for the date 6 days
  ahead. While `python3 padel_booking.py telegram` is running, a background
  scheduler books your target weekdays using that day's preferred slots in
  priority order, polling one request every 30s from the moment the window
  opens until it books (or 5h pass). It only fires on the day the target slot's
  window opens, keeps the session alive periodically, and pushes Telegram
  notifications (booked / failed / no slot / OTP re-login needed). Start or
  stop it at any time with /startautobook and /stopautobook.
  All bot settings live in config.json (preferred_slots, booking_open_hour,
  keepalive_minutes, description).
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
    s = requests.Session(impersonate="chrome")
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
    """Return True if the current session can reach the booking page."""
    r = s.get(f"{BASE_URL}/asset/assetbooking/{cfg['asset_booking_id']}",
              allow_redirects=True)
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
    s.get(BASE_URL + "/")
    r = s.post(BASE_URL + "/login/checkLogin", data={
        "username": encode(cfg["email"]),
        "password": encode(cfg["password"]),
    })
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
    r2 = s.post(BASE_URL + "/login/verifyOtp",
                data={"user_otp": otp, **otp_fields})
    resp = r2.text.strip()
    if resp == "2":
        raise RuntimeError("OTP expired. Run 'login-start' to get a fresh OTP.")
    if resp == "3":
        raise RuntimeError("OTP invalid. Re-run 'login-start' and retry.")

    # follow the redirect to establish the session
    target = resp if resp.startswith("http") else BASE_URL + resp
    s.get(target, allow_redirects=True)

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
    r = s.post(f"{BASE_URL}/booking/myBooking", data=data, allow_redirects=True)
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
    window_end = today + timedelta(days=6)
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


def cmd_slots(cfg: dict, args: list) -> None:
    """CLI: list the available slots for one date (optionally from a time on)."""
    min_start, args = resolve_min_start(cfg, args)
    if not args:
        sys.exit("Usage: slots <date> [--from HH:MM]   (e.g. slots 2026-09-26 --from 18:00)")
    api_date = to_api_date(args[0])
    s, meta = get_booking_meta(cfg)
    all_slots = get_available_slots(s, cfg, api_date, meta)
    slots = filter_slots_from(all_slots, min_start)
    print(f"\nAvailable slots for {args[0]}  (unit {meta.get('unit_label')}, "
          f"{cfg.get('attendees', 1)} attendees)")
    if min_start:
        print(f"  (filtered: from {min_start} onwards)")
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
        sync_booking(args[0], target["label"], cfg, description)
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
    r = s.post(f"{BASE_URL}/{post_url}", data=data, allow_redirects=True)
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
    r = s.get(MYBOOKINGS_URL, allow_redirects=True)
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

    today = datetime.now().date()
    last = today + timedelta(days=6)
    print(f"Bookable window: {today:%a %d %b %Y} .. {last:%a %d %b %Y}"
          f"   (unit {meta.get('unit_label')})")
    if min_start:
        print(f"  (green: slots from {min_start} onwards)")
    print("-" * 62)

    all_slots = []
    for i in range(7):
        d = today + timedelta(days=i)
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


def bookable_targets(targets: set, now: datetime, open_hour: int) -> list:
    """For each target weekday, find the immediately closer (next)
    occurrence and check whether the portal's booking window has opened.

    The portal opens a day's slots at ``open_hour`` on the day that is 6
    days before the target day.  A target day is bookable once ``now`` is
    at/after that moment.  Returns the list of bookable target dates in
    chronological order.

    Note: we deliberately do **not** filter by ``already_booked_successfully``
    here — that notification belongs in ``book_day()`` so the user sees a
    consistent message for every desired day.
    """
    today = now.date()
    result = []
    for target_weekday in sorted(targets):
        # Find the next occurrence of this weekday
        days_ahead = (target_weekday - today.weekday() + 7) % 7
        # days_ahead == 0 means today itself is the target weekday.
        # We keep it as 0 so candidate == today; the portal window check
        # below will decide whether today's slots are available.
        candidate = today + timedelta(days=days_ahead)

        # Check if portal booking window has opened
        open_dt = datetime.combine(
            candidate - timedelta(days=6),
            datetime.min.time().replace(hour=open_hour))
        if now >= open_dt:
            result.append(candidate)
    # First priority: the bookable day whose window opened most recently.
    # This gives the autobook a head start on the freshest slots.
    # After that, remaining days are checked in chronological order.
    result.sort()  # Sort chronologically first

    if not result:
        return result

    # Find the day with the most recent open time
    newest = max(result, key=lambda d: datetime.combine(
        d - timedelta(days=6),
        datetime.min.time().replace(hour=open_hour)))

    others = [d for d in result if d != newest]
    return [newest] + others


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
                     wait_for_open: bool = True, timeout: int = 18000,
                     max_attempts: int = 1, dry_run: bool = False):
    """Poll the slot endpoint and book the best preferred slot as soon as it
    appears. Keeps trying until it succeeds, `max_attempts` is reached, or
    `timeout` seconds elapse (whichever comes first).

    By default ``max_attempts=1``: the bot checks once and stops immediately
    if no preferred slot is available (instead of polling for hours). Increase
    ``max_attempts`` to keep retrying (e.g. to catch a cancellation that frees
    a slot), with each retry 30s apart.

    Returns (slot, all_slots, response_or_None). With dry_run=True it
    identifies the slot but does NOT submit the booking.
    """
    open_hour = int(cfg.get("booking_open_hour", 0))
    now = datetime.now()
    open_dt = now.replace(hour=open_hour, minute=0, second=0, microsecond=0)

    # Pre-warm the shared session + booking page (lowers the booking
    # latency): one fetch that also validates the session.
    s, meta = get_booking_meta(cfg)

    # If this is the day the target opens and we are before the open time,
    # wait until a couple of seconds before it, then start polling.
    if (wait_for_open
            and target.date() == (now + timedelta(days=6)).date()
            and now < open_dt):
        while datetime.now() < open_dt - timedelta(seconds=3):
            time.sleep(0.3)

    api_date = f"{target.day}-{target.month}-{target.year}"
    deadline = datetime.now() + timedelta(seconds=timeout)
    last_slots = []
    attempt = 0
    while datetime.now() < deadline and attempt < max_attempts:
        attempt += 1
        slots = get_available_slots(s, cfg, api_date, meta)
        last_slots = slots
        best = pick_best_slot(slots, preferred)
        if best:
            if dry_run:
                return best, slots, None
            description = cfg.get("description", "Padel booking")
            status = submit_booking_fast(s, cfg, meta,
                                         target.strftime("%Y-%m-%d"),
                                         best, description)
            return best, slots, status
        if attempt < max_attempts:
            print(f"{ts_prefix()} [bot] no preferred slot yet (attempt "
                  f"{attempt}/{max_attempts}); retrying in 30s...", flush=True)
            time.sleep(30)
    return None, last_slots, None


def cmd_autobook(cfg: dict, args: list) -> None:
    """One-shot: book a target date now (or wait for midnight if it opens
    today)."""
    dry_run = "--dry-run" in args
    date_str = next((a for a in args if not a.startswith("--")), None)
    target = parse_date(date_str) if date_str \
        else datetime.now() + timedelta(days=6)

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
    best, slots, r = run_booking_race(cfg, target, preferred, dry_run=dry_run)
    if best is None:
        print("FAILED: none of the preferred slots were available.")
        print(f"  Slots seen for that date ({len(slots)}):")
        for sl in slots:
            print(f"    {sl['label']}  [{sl['value']}]")
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

      1. Program start  -> book ALL desired days whose window is already open
         (catches up on days missed while the bot was not running, and books
         the day whose window just opened, not only the nearest one).
      2. Prefs change   -> when /prefs (or config.json) adds a new day or a new
         time slot, (re)attempt every desired day that is bookable now.
      3. New day opens  -> when a new calendar day starts and its window
         (for today+6) opens at ``booking_open_hour``, book ONLY that new day.

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
        t0 = time.time()
        try:
            best, slots, r = run_booking_race(
                cfg, datetime.combine(target, datetime.min.time()),
                preferred, wait_for_open=False, timeout=18000,
                max_attempts=1, dry_run=dry_run)
            t_race = time.time() - t0
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
                    sync_booking(target_str, best["label"], cfg,
                                 cfg.get("description", "Padel booking"))
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
            log(f"*** FAILED: no preferred slot available "
                f"({len(slots)} slots seen).")
            notify_(f"❌ No preferred slot available for {target:%a %d %b} "
                    f"({len(slots)} slots seen).")
            return False
        except Exception as e:  # pylint: disable=broad-exception-caught
            log(f"*** ERROR during booking: {e}")
            notify_(f"❌ Booking error: {e}")
            return False

    last_keepalive = time.time()
    started = False           # whether the initial catch-up has been done
    last_open_date = None     # day whose window-opening was last processed
    last_prefs_sig = None     # last seen preferences signature
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
        open_hour = int(cfg.get("booking_open_hour", 0))

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
        # Three triggers:
        #   (1) Program start            -> book ALL desired (bookable) days.
        #   (2) Prefs changed (a new day
        #       or time slot added)      -> book ALL desired (bookable) days.
        #   (3) A new day's window opens
        #       (a new calendar day)     -> book ONLY that new day (today+6).
        #
        # ``last_open_date`` is only advanced once the window for (today+6)
        # is actually open (``now >= open_dt``), so a day whose window opens
        # later on the startup day is still picked up by trigger (3).
        prefs_sig = prefs_signature(cfg)
        open_dt = datetime.combine(
            now.date(), datetime.min.time().replace(hour=open_hour))
        window_open = now >= open_dt
        if not started:
            # (1) Program start: catch up on every desired day whose window
            # is already open - including the one that just opened - instead
            # of only the nearest day.
            log("Startup: attempting to book all desired days...")
            for target in bookable_targets(targets, now, open_hour):
                book_day(target, now)
            started = True
            last_prefs_sig = prefs_sig
            if window_open:
                last_open_date = now.date()
        elif prefs_sig != last_prefs_sig:
            # (2) Preferences changed (e.g. /prefs added a day or a time
            # slot): refresh the in-memory state (the ONE slow call), then
            # (re)attempt every desired day that is bookable now.
            log("Preferences changed: refreshing state and booking...")
            try:
                refresh_state(cfg)
            except Exception as e:  # pylint: disable=broad-exception-caught
                log(f"WARNING: state refresh after prefs change failed: {e}.")
            for target in bookable_targets(targets, now, open_hour):
                book_day(target, now)
            last_prefs_sig = prefs_sig
            if window_open:
                last_open_date = now.date()
        elif last_open_date is None or now.date() > last_open_date:
            # (3) A new calendar day started (or today's window has not been
            # processed yet): book ONLY the new day (today+6), once its
            # window is open.
            if window_open:
                new_day = now.date() + timedelta(days=6)
                if (new_day.weekday() in targets
                        and not already_booked_successfully(
                            new_day.strftime("%Y-%m-%d"))):
                    log(f"New booking window opened for "
                        f"{new_day:%a %d %b %Y}. "
                        f"Waiting for portal to sync...")
                    # The portal opens slots at 00:00, but the remote server
                    # time may not be synced.  Wait up to 60 minutes for the
                    # portal to actually make the new day's slots available.
                    api_date = (f"{new_day.day}-{new_day.month}-"
                                f"{new_day.year}")
                    for attempt in range(1, 361):
                        try:
                            # The slots endpoint needs the booking meta
                            # (user_id / community_id / unit_id) to return
                            # slots; those values are only known from the
                            # booking page, NOT from config.json. Passing
                            # meta=None (as before) sent empty
                            # user/community/unit fields, so the portal
                            # always returned 0 slots and this loop could
                            # never see the new day's slots. get_booking_meta()
                            # reuses the meta cached at startup, so after the
                            # first call this is a cheap (no-network) lookup
                            # and matches what run_booking_race() uses.
                            _s, _meta = get_booking_meta(cfg)
                            _slots = get_available_slots(
                                _s, cfg, api_date, _meta)
                            if _slots:
                                log(f"  Portal has slots for "
                                    f"{new_day:%a %d %b} "
                                    f"(attempt {attempt}). "
                                    f"Proceeding...")
                                break
                        except Exception:  # pylint: disable=broad-exception-caught
                            pass
                        if attempt < 360:
                            log(f"  Waiting for portal to open slots for "
                                f"{new_day:%a %d %b} "
                                f"(attempt {attempt}/360)...")
                            time.sleep(10)
                    log(f"Booking {new_day:%a %d %b %Y}...")
                    book_day(new_day, now)
                last_open_date = now.date()
            # else: window not open yet - retry on the next pass.

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
    commands = ("login-start", "login-finish", "login-status", "explore", "slots",
                "book", "pick", "mybookings", "cancel", "autobook",
                "keepalive", "telegram", "google-auth", "google-test",
                "google-list-calendars")
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        print(__doc__)
        sys.exit(1)
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
