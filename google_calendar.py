#!/usr/bin/env python3
"""
Google Calendar sync for the padel booking automation.

Creates calendar events when bookings are confirmed and deletes them
when bookings are cancelled.  Uses OAuth 2.0 with a stored refresh token
so no browser interaction is needed after the initial `google-auth` run.

CLI commands (via padel_booking.py):
  python3 padel_booking.py google-auth [client_id] [client_secret]
  python3 padel_booking.py google-test
"""

import json
import sys
import time
import webbrowser
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode

from curl_cffi import requests

HERE = Path(__file__).resolve().parent
EVENTS_FILE = HERE / "google_events.json"
CONFIG_FILE = HERE / "config.json"

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
SCOPE = "https://www.googleapis.com/auth/calendar"


class GoogleCalendarError(Exception):
    """A Google Calendar API or OAuth failure."""
    pass


class GoogleCalendar:
    """Minimal client for the Google Calendar API (curl_cffi)."""

    def __init__(self, cfg: dict):
        gc = cfg.get("google_calendar", {})
        self.enabled = gc.get("enabled", False)
        self.email = gc.get("email", "")
        self.calendar_name = gc.get("calendar", "")
        self.client_id = gc.get("client_id", "")
        self.client_secret = gc.get("client_secret", "")
        self.refresh_token = gc.get("refresh_token", "")
        self.timezone = gc.get("timezone", "Asia/Riyadh")
        self._access_token: str | None = None
        self._token_expires_at: float = 0
        self._resolved_calendar_id: str | None = None

    def _resolve_calendar_id(self) -> str:
        """Resolve calendar name/ID to the actual calendar ID."""
        if self._resolved_calendar_id:
            return self._resolved_calendar_id
        if not self.calendar_name:
            self._resolved_calendar_id = self.email
            return self._resolved_calendar_id
        # If it looks like an email or a raw calendar ID, use it directly
        if "@" in self.calendar_name or "-" in self.calendar_name:
            self._resolved_calendar_id = self.calendar_name
            return self._resolved_calendar_id
        # Otherwise, resolve by name
        calendars = self.list_calendars()
        for cal in calendars:
            if cal.get("summary", "").lower() == self.calendar_name.lower():
                self._resolved_calendar_id = cal["id"]
                return self._resolved_calendar_id
        raise GoogleCalendarError(
            f"Calendar '{self.calendar_name}' not found. "
            f"Available: {[c.get('summary') for c in calendars]}")

    @property
    def calendar_id(self) -> str:
        return self._resolve_calendar_id()

    def _get_access_token(self) -> str:
        """Return a valid access token, refreshing if needed."""
        now = time.time()
        if self._access_token and now < self._token_expires_at - 60:
            return self._access_token
        if not self.refresh_token:
            raise GoogleCalendarError("No refresh_token configured")
        resp = requests.post(
            TOKEN_URL,
            data={
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
            },
            impersonate="chrome",
            timeout=30,
        )
        if resp.status_code != 200:
            raise GoogleCalendarError(
                f"Token refresh failed: {resp.status_code} {resp.text}")
        data = resp.json()
        self._access_token = data["access_token"]
        self._token_expires_at = now + data.get("expires_in", 3600)
        return self._access_token

    def _headers(self) -> dict:
        token = self._get_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def create_event(self, summary: str, start_iso: str, end_iso: str,
                     description: str = "") -> str:
        """Create a calendar event.  Returns the new event ID."""
        event: dict = {
            "summary": summary,
            "start": {"dateTime": start_iso, "timeZone": self.timezone},
            "end": {"dateTime": end_iso, "timeZone": self.timezone},
        }
        if description:
            event["description"] = description
        resp = requests.post(
            f"{CALENDAR_API}/calendars/{self.calendar_id}/events",
            json=event,
            headers=self._headers(),
            impersonate="chrome",
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            raise GoogleCalendarError(
                f"Create event failed: {resp.status_code} {resp.text}")
        return resp.json()["id"]

    def delete_event(self, event_id: str) -> None:
        """Delete a calendar event by ID."""
        resp = requests.delete(
            f"{CALENDAR_API}/calendars/{self.calendar_id}/events/{event_id}",
            headers=self._headers(),
            impersonate="chrome",
            timeout=30,
        )
        if resp.status_code not in (200, 204):
            raise GoogleCalendarError(
                f"Delete event failed: {resp.status_code} {resp.text}")

    def list_calendars(self) -> list:
        """List all accessible calendars.  Returns list of {id, summary}."""
        resp = requests.get(
            f"{CALENDAR_API}/users/me/calendarList",
            headers=self._headers(),
            impersonate="chrome",
            timeout=30,
        )
        if resp.status_code != 200:
            raise GoogleCalendarError(
                f"List calendars failed: {resp.status_code} {resp.text}")
        return resp.json().get("items", [])


def load_event_ids() -> dict:
    if EVENTS_FILE.exists():
        try:
            data = json.loads(EVENTS_FILE.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            pass
    return {}


def save_event_ids(ids: dict) -> None:
    EVENTS_FILE.write_text(json.dumps(ids, indent=2))
    try:
        EVENTS_FILE.chmod(0o600)
    except OSError:
        pass


def event_key(date_str: str, slot_label: str) -> str:
    return f"{date_str}_{slot_label}"


def _build_iso_times(date_str: str, slot_label: str,
                     timezone: str) -> tuple:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    parts = slot_label.split("-")
    if len(parts) != 2:
        raise ValueError(f"Can't parse slot label: {slot_label}")
    tz = ZoneInfo(timezone)
    start_dt = datetime.fromisoformat(
        f"{date_str}T{parts[0]}:00").replace(tzinfo=tz)
    end_dt = datetime.fromisoformat(
        f"{date_str}T{parts[1]}:00").replace(tzinfo=tz)
    return start_dt.isoformat(), end_dt.isoformat()


def get_setup_status(cfg: dict) -> tuple:
    """Check Google Calendar config and return (is_ready, message).

    If not ready, message contains setup instructions with URLs.
    """
    gc = cfg.get("google_calendar", {})
    needs_creds = not gc.get("client_id") or not gc.get("client_secret")
    needs_token = not gc.get("refresh_token")
    needs_email = not gc.get("email")

    if not (needs_creds or needs_token or needs_email):
        return True, ""

    lines = ["📅 Google Calendar is not configured yet.", ""]
    step = 1

    if needs_creds:
        lines.append(f"{step}️⃣ Create OAuth credentials:")
        lines.append("   🔗 https://console.cloud.google.com/apis/credentials")
        lines.append("   • Create a project")
        lines.append("   • Enable 'Google Calendar API':")
        lines.append("     🔗 https://console.cloud.google.com/apis/library/calendar.json")
        lines.append("   • Create OAuth 2.0 Client ID → type: Web application")
        lines.append("   • Copy the Client ID and Client Secret")
        lines.append("")
        step += 1

        lines.append(f"{step}️⃣ Set authorized redirect URI:")
        lines.append("   🔗 https://console.cloud.google.com/auth/clients")
        lines.append("   • Add: https://padel-booking.destr0.com/callback")
        lines.append("")
        step += 1

        lines.append(f"{step}️⃣ Add yourself as a test user:")
        lines.append("   🔗 https://console.cloud.google.com/auth/audience")
        lines.append("   • Click 'Add users' and enter your Google email")
        lines.append("")
        step += 1

    lines.append(f"{step}️⃣ Set your Google email & calendar in config.json:")
    lines.append('   "google_calendar": { "email": "you@gmail.com", "calendar": "Social" }')
    lines.append("   (calendar = the name of the calendar to use, e.g. 'Social')")
    lines.append("")
    step += 1

    if needs_token:
        lines.append(f"{step}️⃣ Run the auth flow (opens browser, one-time):")
        lines.append("   python padel_booking.py google-auth <CLIENT_ID> <CLIENT_SECRET>")
        lines.append("")
        step += 1

    lines.append("After setup, verify with:")
    lines.append("   python padel_booking.py google-test")

    return False, "\n".join(lines)


def generate_auth_url(client_id: str, redirect_uri: str) -> str:
    """Build the Google OAuth authorization URL."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def start_callback_server(port: int = 8010) -> tuple:
    """Start the OAuth callback server in a daemon thread.

    Returns (result_dict, server).  The result_dict will contain
    'code' or 'error' once the callback is received.
    """
    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/callback":
                params = parse_qs(parsed.query)
                if "code" in params:
                    result["code"] = params["code"][0]
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(
                        b"<html><body style='font-family:sans-serif;"
                        b"text-align:center;padding:50px;'>"
                        b"<h2 style='color:green;'>Authorized!</h2>"
                        b"<p>Close this tab and go back to Telegram."
                        b"</p></body></html>")
                elif "error" in params:
                    result["error"] = params["error"][0]
                    self.send_response(400)
                    self.end_headers()
                else:
                    self.send_response(400)
                    self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, fmt, *args):
            pass

    try:
        server = HTTPServer(("0.0.0.0", port), Handler)
    except OSError as e:
        raise GoogleCalendarError(f"Could not start server on port {port}: {e}")

    def _handle():
        server.handle_request()

    t = threading.Thread(target=_handle, daemon=True)
    t.start()
    return result, server


def exchange_code_for_tokens(code: str, client_id: str,
                             client_secret: str,
                             redirect_uri: str) -> dict:
    """Exchange an authorization code for tokens.  Returns the token dict."""
    resp = requests.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        impersonate="chrome",
        timeout=30,
    )
    if resp.status_code != 200:
        raise GoogleCalendarError(
            f"Token exchange failed: {resp.status_code} {resp.text}")
    return resp.json()


def sync_booking(date_str: str, slot_label: str, cfg: dict,
                 description: str = "") -> bool:
    """Create a Google Calendar event for a confirmed booking (non-fatal).

    Returns True if the event was created, False otherwise.
    """
    gc = GoogleCalendar(cfg)
    if not gc.enabled:
        return False
    try:
        start_iso, end_iso = _build_iso_times(date_str, slot_label, gc.timezone)
        event_id = gc.create_event("Padel", start_iso, end_iso, description)
        ids = load_event_ids()
        ids[event_key(date_str, slot_label)] = event_id
        save_event_ids(ids)
        print("[google-calendar] event created")
        return True
    except Exception as e:
        print(f"[google-calendar] sync failed: {e}")
        return False


def sync_cancellation(date_str: str, slot_label: str, cfg: dict) -> bool:
    """Delete the Google Calendar event for a cancelled booking (non-fatal).

    Returns True if the event was deleted, False otherwise.
    """
    gc = GoogleCalendar(cfg)
    if not gc.enabled:
        return False
    key = event_key(date_str, slot_label)
    ids = load_event_ids()
    event_id = ids.get(key)
    if not event_id:
        print(f"[google-calendar] no event found for {key}")
        return False
    try:
        gc.delete_event(event_id)
        del ids[key]
        save_event_ids(ids)
        print("[google-calendar] event deleted")
        return True
    except Exception as e:
        print(f"[google-calendar] delete failed: {e}")
        return False


def _save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")


def run_google_auth(cfg: dict, client_id: str = "",
                    client_secret: str = "") -> None:
    """Run the OAuth 2.0 flow to obtain a refresh token and save it to config."""
    gc_cfg = cfg.get("google_calendar", {})
    cid = client_id or gc_cfg.get("client_id", "")
    csec = client_secret or gc_cfg.get("client_secret", "")

    if not cid or not csec:
        print("ERROR: google_calendar.client_id and client_secret must be set.")
        print("Setup: Go to https://console.cloud.google.com/apis/credentials")
        print("  1. Create a project  2. Enable 'Google Calendar API'")
        print("  3. Create OAuth 2.0 Client ID (Desktop type)")
        print("Then run: python padel_booking.py google-auth <cid> <csec>")
        sys.exit(1)

    email = gc_cfg.get("email", "") or cfg.get("email", "")
    if not email:
        print("ERROR: Set 'google_calendar.email' in config.json")
        sys.exit(1)

    port = 8010
    redirect_uri = "https://padel-booking.destr0.com/callback"
    auth_params = {
        "client_id": cid, "redirect_uri": redirect_uri,
        "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent",
    }
    auth_url = f"{AUTH_URL}?{urlencode(auth_params)}"
    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/callback":
                params = parse_qs(parsed.query)
                if "code" in params:
                    result["code"] = params["code"][0]
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(
                        b"<html><body style='font-family:sans-serif;"
                        b"text-align:center;padding:50px;'>"
                        b"<h2 style='color:green;'>Authorized!</h2>"
                        b"<p>Close this tab and go back to the terminal."
                        b"</p></body></html>")
                elif "error" in params:
                    result["error"] = params["error"][0]
                    self.send_response(400)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    err = params["error"][0].encode()
                    self.wfile.write(
                        b"<html><body style='font-family:sans-serif;"
                        b"text-align:center;padding:50px;'>"
                        b"<h2 style='color:red;'>Authorization failed</h2>"
                        b"<p>" + err + b"</p></body></html>")
                else:
                    self.send_response(400)
                    self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, fmt, *args):
            pass

    try:
        server = HTTPServer(("0.0.0.0", port), Handler)
    except OSError as e:
        print(f"ERROR: Could not start local server on port {port}: {e}")
        sys.exit(1)

    print("Opening browser for Google authorization...")
    print(f"If it doesn't open, visit:\n  {auth_url}")
    webbrowser.open(auth_url)
    print("Waiting for authorization (2 min timeout)...")

    def _handle():
        server.handle_request()

    t = threading.Thread(target=_handle, daemon=True)
    t.start()
    t.join(timeout=120)
    server.server_close()

    if "error" in result:
        print(f"ERROR: Google authorization failed: {result['error']}")
        sys.exit(1)
    if "code" not in result:
        print("ERROR: No authorization code received (timeout or cancelled).")
        sys.exit(1)

    code = result["code"]
    print("Authorization code received. Exchanging for tokens...")

    resp = requests.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": cid,
            "client_secret": csec,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        impersonate="chrome",
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"ERROR: Token exchange failed: {resp.status_code} {resp.text}")
        sys.exit(1)

    data = resp.json()
    refresh_token = data.get("refresh_token")
    access_token = data.get("access_token", "")

    if not refresh_token:
        print("WARNING: No refresh_token in response.")
        print("Try revoking access at: https://myaccount.google.com/permissions")
        sys.exit(1)

    if "google_calendar" not in cfg:
        cfg["google_calendar"] = {}
    gc = cfg["google_calendar"]
    gc["client_id"] = cid
    gc["client_secret"] = csec
    gc["email"] = email
    gc["refresh_token"] = refresh_token
    gc["enabled"] = True
    gc.setdefault("timezone", "Asia/Riyadh")

    _save_config(cfg)

    print("Google Calendar configured!")
    print(f"   Email:          {email}")
    print(f"   Access token:   {access_token[:20]}...")
    print("   Refresh token:  saved to config.json")
    print("\nTest it with:  python padel_booking.py google-test")


def run_google_test(cfg: dict) -> None:
    """Verify the Google Calendar connection by listing upcoming events."""
    gc = GoogleCalendar(cfg)
    if not gc.enabled:
        print("Google Calendar is not enabled.")
        print("Run 'python padel_booking.py google-auth' first.")
        sys.exit(1)

    try:
        token = gc._get_access_token()
        print(f"Access token obtained: {token[:20]}...")

        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(gc.timezone)
        now = datetime.now(tz)
        time_min = now.isoformat()
        time_max = (now + timedelta(days=7)).isoformat()

        resp = requests.get(
            f"{CALENDAR_API}/calendars/{gc.email}/events",
            params={
                "timeMin": time_min,
                "timeMax": time_max,
                "maxResults": 5,
                "singleEvents": "true",
            },
            headers=gc._headers(),
            impersonate="chrome",
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"Failed to list events: {resp.status_code} {resp.text}")
            sys.exit(1)

        events = resp.json().get("items", [])
        print(f"\nUpcoming events (next 7 days): {len(events)}")
        for ev in events:
            start = ev.get("start", {})
            when = start.get("dateTime") or start.get("date", "?")
            print(f"  {when}  {ev.get('summary', '(no title)')}")
        if not events:
            print("  (none)")

        print("\nGoogle Calendar connection is working!")
    except Exception as e:
        print(f"Google Calendar test failed: {e}")
        sys.exit(1)


def run_google_list_calendars(cfg: dict) -> None:
    """List all accessible Google calendars with their IDs."""
    gc = GoogleCalendar(cfg)
    if not gc.enabled:
        print("Google Calendar is not enabled.")
        print("Run 'python padel_booking.py google-auth' first.")
        sys.exit(1)

    try:
        calendars = gc.list_calendars()
        print(f"Available calendars ({len(calendars)}):")
        for cal in calendars:
            cal_id = cal.get("id", "?")
            summary = cal.get("summary", "(no name)")
            access = cal.get("accessRole", "")
            print(f"  {cal_id}  ({access})  {summary}")
        print("\nSet the desired ID in config.json:")
        print('  "google_calendar": { "calendar": "<calendar_id>" }')
    except Exception as e:
        print(f"Failed to list calendars: {e}")
        sys.exit(1)
