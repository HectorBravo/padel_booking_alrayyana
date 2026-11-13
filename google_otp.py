#!/usr/bin/env python3
"""Read the Asteco portal OTP straight out of Gmail.

When the Google integration is configured (``config.json`` → ``google_calendar``),
the OTP that the portal emails lands in the same Gmail account we already have
OAuth access to.  Instead of asking a human to read their inbox and type the
code, this module polls Gmail for it:

  * search ``from:asteco.com`` (the OTP sender domain), newest first;
  * require the body to contain ``<digits> is your ASTECO OTP``;
  * retry every ``OTP_POLL_INTERVAL`` seconds, at most ``OTP_MAX_RETRIES``
    times — if it has not arrived within ~a minute the portal did not send it.

Only read-only Gmail access is used (``gmail.readonly``), and the OAuth token
is refreshed with the Calendar scope too so that one consent covers both
integrations.  Uses ``curl_cffi`` like the rest of the project — no new
dependencies.
"""

import base64
import json
import re
import time
from pathlib import Path

from curl_cffi import requests

HERE = Path(__file__).resolve().parent
# Remembers the last code we already handed out, so a repeated login does not
# re-use the previous (already consumed / expired) OTP from the inbox.
LAST_OTP_FILE = HERE / "last_otp.txt"

TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"

# Read-only Gmail access, added to the Calendar scope in google_calendar.py so
# a single `google-auth` consent grants both.
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

# Portal OTP sender domain + body pattern ("315813 is your ASTECO OTP").
OTP_SENDER_DOMAIN = "asteco.com"
OTP_QUERY = f"from:{OTP_SENDER_DOMAIN}"
# The real email wraps the code in markup (`<b>315813</b> is your ASTECO OTP`),
# so tags/entities/quotes between the digits and the phrase are tolerated.
OTP_BODY_RE = re.compile(
    r"(\d{4,8})"             # the code itself
    r"(?:\s|</?[a-zA-Z][^>]*>|&\w+;|\"|'){0,30}"  # markup/whitespace gap
    r"is\s+your\s+ASTECO\s+OTP",
    re.IGNORECASE)

# Polling policy: 12 attempts x 5 s ≈ one minute (the portal either emails the
# OTP within seconds or not at all).
OTP_POLL_INTERVAL = 5
OTP_MAX_RETRIES = 12


class GoogleOtpError(Exception):
    """A Gmail API / OAuth failure while looking for the OTP."""


def google_configured(cfg: dict) -> bool:
    """True when the Google integration is enabled and has an OAuth token.

    When this returns False the caller must fall back to asking a human for
    the OTP — nothing here is attempted.
    """
    gc = cfg.get("google_calendar", {})
    return bool(gc.get("enabled") and gc.get("refresh_token")
                and gc.get("client_id") and gc.get("client_secret"))


def _get_access_token(cfg: dict) -> str:
    """Refresh an access token covering Calendar + Gmail read."""
    from google_calendar import SCOPE  # single source for the calendar scope

    gc = cfg["google_calendar"]
    resp = requests.post(
        TOKEN_URL,
        data={
            "refresh_token": gc["refresh_token"],
            "client_id": gc["client_id"],
            "client_secret": gc["client_secret"],
            "grant_type": "refresh_token",
            # Ask for both explicitly: Google returns only the granted subset,
            # so a token issued before the Gmail scope existed is upgraded as
            # soon as the user re-authorises.
            "scope": f"{SCOPE} {GMAIL_SCOPE}",
        },
        impersonate="chrome",
        timeout=30,
    )
    if resp.status_code != 200:
        raise GoogleOtpError(
            f"Google token refresh failed ({resp.status_code}): "
            f"{resp.text.strip()[:200]}. Re-run 'google-auth' (or /setupgoogle).")
    token = resp.json().get("access_token")
    if not token:
        raise GoogleOtpError("Google token response had no access_token.")
    return token


def _gmail_get(url: str, token: str, params: dict) -> dict:
    """GET a Gmail API endpoint and return the parsed JSON body."""
    resp = requests.get(
        url,
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        impersonate="chrome",
        timeout=30,
    )
    if resp.status_code != 200:
        raise GoogleOtpError(
            f"Gmail API error {resp.status_code} for {url}: "
            f"{resp.text.strip()[:200]}")
    try:
        return resp.json()
    except ValueError as e:
        raise GoogleOtpError(f"Gmail API returned non-JSON: {e}")


def _list_otp_messages(token: str) -> list:
    """Return the ids of recent OTP emails, newest first."""
    data = _gmail_get(
        f"{GMAIL_API}/messages", token,
        {"q": OTP_QUERY, "maxResults": 10, "fields": "messages(id)"})
    return [m["id"] for m in data.get("messages", []) if m.get("id")]


def _get_message(token: str, msg_id: str) -> dict:
    """Fetch one message with its payload (headers + body)."""
    return _gmail_get(f"{GMAIL_API}/messages/{msg_id}", token,
                      {"format": "full"})


def _walk_parts(payload: dict) -> list:
    """Yield every MIME part of a message payload (handles multipart nesting)."""
    parts = []
    stack = [payload]
    while stack:
        part = stack.pop()
        if not isinstance(part, dict):
            continue
        parts.append(part)
        for child in part.get("parts") or []:
            stack.append(child)
    return parts


def _decode_part_body(part: dict) -> str:
    """Return the decoded text of one MIME part ('' when it has no body)."""
    data = (part.get("body") or {}).get("data")
    if not data:
        return ""
    try:
        # Gmail encodes bodies base64url with -/_ and usually no padding.
        pad = "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(data + pad).decode("utf-8", "replace")
    except Exception:  # pylint: disable=broad-exception-caught
        return ""


def message_text(msg: dict) -> str:
    """Flatten a Gmail message into the text we search for the OTP code."""
    payload = msg.get("payload") or {}
    chunks = [_decode_part_body(payload)]
    for part in _walk_parts(payload):
        if part is payload:
            continue
        mime = (part.get("mimeType") or "").lower()
        if mime.startswith("text/") or not mime:
            chunks.append(_decode_part_body(part))
    return "\n".join(c for c in chunks if c)


def message_header(msg: dict, name: str) -> str:
    """Return a message header (e.g. 'From'), case-insensitively."""
    want = name.lower()
    for h in (msg.get("payload") or {}).get("headers") or []:
        if (h.get("name") or "").lower() == want:
            return h.get("value") or ""
    return ""


def sender_matches(msg: dict) -> bool:
    """True when the From header is a address of the portal's domain."""
    from_ = message_header(msg, "from").lower()
    # Accept "Asteco <no-reply@asteco.com>", bare addresses and display names.
    return f"@{OTP_SENDER_DOMAIN}" in from_ or f"{OTP_SENDER_DOMAIN}:" in from_


def extract_otp(text: str) -> str:
    """Return the OTP from '<digits> is your ASTECO OTP', or '' if absent."""
    m = OTP_BODY_RE.search(text or "")
    return m.group(1) if m else ""


def _read_last_otp() -> str:
    try:
        return LAST_OTP_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_last_otp(otp: str) -> None:
    try:
        LAST_OTP_FILE.write_text(otp, encoding="utf-8")
        LAST_OTP_FILE.chmod(0o600)
    except OSError:
        pass


def find_otp_in_inbox(token: str, skip: str = "") -> tuple:
    """Scan recent OTP emails once.

    Returns (otp, state) where otp is '' when nothing usable was found and
    state explains what was seen ('' / 'no_message' / 'no_code' / 'stale').
    """
    ids = _list_otp_messages(token)
    if not ids:
        return "", "no_message"
    seen_code, seen_sender = "", ""
    for msg_id in ids:
        msg = _get_message(token, msg_id)
        if not sender_matches(msg):
            continue
        otp = extract_otp(message_text(msg))
        if not otp:
            continue
        seen_code = otp
        if skip and otp == skip:
            continue          # the previous login's code — not ours
        return otp, ""
    if seen_code:
        return "", "stale"     # only the already-used code is in the inbox
    return "", "no_code"


def capture_otp(cfg: dict, log=None, skip: str = "") -> tuple:
    """Poll Gmail for the portal OTP.  Returns (otp, error).

    *otp* is the 6-digit code ('' on failure); *error* is a short human
    message explaining why nothing was captured.  Never raises: callers can
    fall back to asking the user.

    `skip` lets a caller ignore a code it already knows is dead (e.g. the one
    that just expired), which otherwise would be picked up again immediately.
    """
    if log is None:
        log = lambda m: print(m, flush=True)  # noqa: E731

    if not google_configured(cfg):
        return "", "Google integration is not configured."

    try:
        token = _get_access_token(cfg)
    except GoogleOtpError as e:
        log(f"  -> Gmail: {e}")
        return "", str(e)

    stale = skip or _read_last_otp()
    last_state = ""
    for attempt in range(1, OTP_MAX_RETRIES + 1):
        try:
            otp, state = find_otp_in_inbox(token, skip=stale)
        except GoogleOtpError as e:
            log(f"  -> Gmail check {attempt}/{OTP_MAX_RETRIES} failed: {e}")
            return "", str(e)
        last_state = state
        if otp:
            _write_last_otp(otp)
            log(f"  -> Gmail: OTP {otp} captured "
                f"(attempt {attempt}/{OTP_MAX_RETRIES})")
            return otp, ""
        log(f"  -> Gmail: no OTP yet "
            f"({state or 'pending'}), attempt {attempt}/{OTP_MAX_RETRIES}")
        if attempt < OTP_MAX_RETRIES:
            time.sleep(OTP_POLL_INTERVAL)

    reason = {"no_message": f"no email from {OTP_SENDER_DOMAIN} arrived",
              "no_code": "the Asteco email did not contain an OTP code",
              "stale": "only a previous (already used) OTP was found"}.get(
                  last_state, "no OTP email arrived")
    msg = (f"No OTP from {OTP_SENDER_DOMAIN} after "
           f"{OTP_MAX_RETRIES} tries in ~"
           f"{OTP_MAX_RETRIES * OTP_POLL_INTERVAL}s ({reason}).")
    log(f"  -> Gmail: {msg}")
    return "", msg

    """Fetch one message with its payload (headers + body)."""
    return _gmail_get(f"{GMAIL_API}/messages/{msg_id}", token,
                      {"format": "full"})
