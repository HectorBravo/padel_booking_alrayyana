#!/usr/bin/env python3
"""Probe the new Aldar Live portal (the Asteco portal was migrated).

Purpose (T27): prove, end to end, that we can
  1. authenticate against ForgeRock AM (`frald.aldar.com/am`, realm `alpha`,
     journey "LIVE ALDAR WEB USER EMAIL LOGIN") with the email/password from
     config.json,
  2. capture the emailed 4-digit OTP (from no-reply@aldar.com) via Gmail,
  3. finish the callback chain and keep whatever session/token works,
  4. call the read-only communityservice booking APIs (categories -> classes
     -> schedules/packages -> available slots) and dump their shapes.

It NEVER calls `classes/bookings` (the create endpoint): this script is a
read-only probe.

Usage:
  python aldar_probe.py auth            # steps 1-3, saves aldar_session.json
  python aldar_probe.py api             # step 4 using the saved session
  python aldar_probe.py all             # both
"""

import json
import re
import sys
import time
from pathlib import Path

from curl_cffi import requests as creq

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.json"
PENDING_FILE = HERE / "aldar_pending.json"
SESSION_FILE = HERE / "aldar_session.json"

# --- ForgeRock AM (extracted from the live SPA bundle, verified 17-11-2026) --
FR_BASE = "https://frald.aldar.com/am"
AUTH_URL = (f"{FR_BASE}/json/realms/root/realms/alpha/authenticate"
            "?authIndexType=service"
            "&authIndexValue=LIVE%20ALDAR%20WEB%20USER%20EMAIL%20LOGIN"
            "&flow=LIVE%20ALDAR%20WEB%20USER%20EMAIL%20LOGIN")

# --- Community service (booking APIs; 403 = exists+auth, 404 = wrong verb) ---
API_BASE = "https://communityservice.live.aldar.com/api/v1/"

UA_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/131.0.0.0 Safari/537.36"}

CFG_EM = ""
CFG_PW = ""


def log(msg: str) -> None:
    print(msg, flush=True)


def load_cfg() -> dict:
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 1+3. ForgeRock callback chain
# --------------------------------------------------------------------------

def fr_headers() -> dict:
    h = dict(UA_HEADERS)
    h.update({
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://www.aldar.com",
        "Referer": "https://www.aldar.com/",
        "Accept-API-Version": "protocol=1.0,resource=2.0",
        "X-Requested-With": "XMLHttpRequest",
    })
    return h


def fr_post(s, payload: dict):
    r = s.post(AUTH_URL, headers=fr_headers(),
               data=json.dumps(payload), timeout=60)
    try:
        body = r.json()
    except ValueError:
        body = {"_raw": r.text[:400]}
    return r.status_code, body


def describe_callbacks(body: dict) -> str:
    out = []
    for cb in body.get("callbacks", []):
        ins = [i.get("name") for i in cb.get("input", [])]
        outs = [(o.get("name"), o.get("value")) for o in cb.get("output", [])]
        out.append(f"{cb.get('type')} in={ins} out={outs}")
    return " | ".join(out)


def answer_callbacks(body: dict, otp):
    """Fill outputs of the callbacks. Returns (callbacks, otp_was_needed)."""
    needs_otp = False
    for cb in body.get("callbacks", []):
        t = cb.get("type")
        if t in ("NameCallback", "TextInputCallback"):
            label = ""
            for o in cb.get("output", []):
                if o.get("name") == "prompt":
                    label = str(o.get("value", "")).lower()
            want_pw = "password" in label or any(
                i.get("name") == "IDToken2" for i in cb.get("input", []))
            value = CFG_PW if want_pw else CFG_EM
            for o in cb.get("output", []):
                if o.get("name") == "value":
                    o["value"] = value
        elif t in ("PasswordCallback", "PasswordConfirmCallback"):
            for o in cb.get("output", []):
                if o.get("name") == "value":
                    o["value"] = CFG_PW
        elif t in ("TextOutputCallback", "ConfirmationCallback"):
            log(f"  [chain] {t}: {json.dumps(cb.get('output'))[:300]}")
            if t == "ConfirmationCallback":
                # Answer "yes/ok" (optionIndex 0) so the chain proceeds.
                for o in cb.get("output", []):
                    if o.get("name") == "optionIndex":
                        o["value"] = 0
        elif t == "NumberCallback":
            needs_otp = True
            if otp is not None:
                for o in cb.get("output", []):
                    if o.get("name") == "value":
                        o["value"] = int(otp)
        else:
            # Unknown callback that takes a value -> assume it wants the OTP.
            log(f"  [chain] unknown callback {t}: {json.dumps(cb)[:300]}")
            needs_otp = True
            if otp is not None:
                for o in cb.get("output", []):
                    if o.get("name") == "value":
                        try:
                            o["value"] = int(otp)
                        except ValueError:
                            o["value"] = otp
    return body.get("callbacks", []), needs_otp


def auth_start(cfg: dict, manual: bool) -> bool:
    """Steps 1-3. Returns True when the chain completed."""
    global CFG_EM, CFG_PW
    CFG_EM, CFG_PW = cfg["email"], cfg["password"]
    s = creq.Session(impersonate="chrome")

    log(f"[auth] POST {AUTH_URL}")
    status, body = fr_post(s, {})
    log(f"  -> {status}: "
        f"{describe_callbacks(body) or json.dumps(body)[:200]}")
    auth_id = body.get("authId")
    if not auth_id:
        log("[auth] no authId — cannot continue")
        return False

    otp = None
    for step in range(1, 9):
        callbacks, _ = answer_callbacks(body, otp)
        payload = {"authId": auth_id}
        if callbacks:
            payload["callbacks"] = callbacks
        log(f"[auth] step {step}: submitting "
            f"({'OTP filled' if otp else 'creds/answer'})")
        status, body = fr_post(s, payload)
        auth_id = body.get("authId", auth_id)
        summary = json.dumps({k: v for k, v in body.items()
                              if k != "callbacks"})[:300]
        log(f"  -> {status}: "
            f"{describe_callbacks(body) or summary}")

        # Success shapes seen with ForgeRock: {"success": true, "token": ...}
        if body.get("success") is True or body.get("token"):
            save_session(s, body)
            return True
        if not body.get("callbacks"):
            log("[auth] chain ended without success — see the summary above")
            return False

        # If the NEXT step needs an OTP we don't have yet, capture it.
        _, will_need = answer_callbacks(
            json.loads(json.dumps(body)), None)
        if will_need and otp is None:
            since = time.time()
            if manual:
                otp = input("OTP from no-reply@aldar.com: ").strip()
            else:
                otp, err = capture_aldar_otp(cfg, since=since - 2)
                if not otp:
                    log(f"[auth] automatic OTP capture failed: {err}")
                    return False
                log(f"[auth] captured OTP {otp}")

    return False


def save_session(s, body: dict) -> None:
    data = {
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "token": body.get("token") or "",
        "cookies": [{"name": c.name, "value": c.value, "domain": c.domain,
                     "path": c.path} for c in s.cookies.jar],
    }
    SESSION_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        SESSION_FILE.chmod(0o600)
    except OSError:
        pass
    log(f"[auth] SUCCESS — session saved to {SESSION_FILE.name} "
        f"(token: {'yes' if data['token'] else 'no'}, "
        f"{len(data['cookies'])} cookies)")


# --------------------------------------------------------------------------
# 2. OTP capture (Aldar wording, via the existing Gmail plumbing)
# --------------------------------------------------------------------------

OTP_WORD_RE = re.compile(r"one[- ]time password|\bOTP\b", re.IGNORECASE)
CODE_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")


def capture_aldar_otp(cfg: dict, since: float | None = None):
    """Poll Gmail for the newest aldar.com OTP email. Returns (otp, error)."""
    import google_otp as go

    if not go.google_configured(cfg):
        return "", "Google integration is not configured."
    try:
        token = go._get_access_token(cfg)  # noqa: SLF001 (probe)
    except go.GoogleOtpError as e:
        return "", str(e)

    base = since if since is not None else time.time()
    since_ms = int((base - 90) * 1000)
    for attempt in range(1, 13):
        try:
            data = go._gmail_get(  # noqa: SLF001 (probe)
                f"{go.GMAIL_API}/messages", token,
                {"q": "from:aldar.com newer_than:20m", "maxResults": 10,
                 "fields": "messages(id,internalDate)"})
        except go.GoogleOtpError as e:
            return "", str(e)
        for m in data.get("messages", []):
            when = int(m.get("internalDate") or 0)
            if since_ms and when and when < since_ms:
                continue
            msg = go._get_message(token, m["id"])  # noqa: SLF001 (probe)
            text = go.message_text(msg)
            if not OTP_WORD_RE.search(text):
                continue
            codes = CODE_RE.findall(text)
            # Drop the "next 10 minutes"/year boilerplate numbers.
            codes = [c for c in codes if c not in ("2026",)]
            if codes:
                return codes[0], ""
        log(f"  -> Gmail: no Aldar OTP yet, attempt {attempt}/12")
        time.sleep(5)
    return "", "no fresh OTP email from aldar.com arrived"


# --------------------------------------------------------------------------
# 4. communityservice API probes (read-only!)
# --------------------------------------------------------------------------

def api_session():
    s = creq.Session(impersonate="chrome")
    data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    for c in data.get("cookies") or []:
        s.cookies.set(c["name"], c["value"], domain=c.get("domain"),
                      path=c.get("path", "/"))
    return s, data


def api_call(s, token: str, method: str, path: str, body=None):
    url = API_BASE + path
    headers = dict(UA_HEADERS)
    headers.update({"Accept": "application/json",
                    "Origin": "https://www.aldar.com",
                    "Referer": "https://www.aldar.com/"})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = s.request(method, url, headers=headers,
                      data=json.dumps(body) if body is not None else None,
                      timeout=45)
        return r.status_code, r.text[:800]
    except Exception as e:  # noqa: BLE001 — the probe reports everything
        return -1, str(e)[:300]


READ_PROBES = [
    ("GET", "classes/categories", None),
    ("POST", "classes/categories", {}),
    ("GET", "bookings/classes", None),
    ("POST", "bookings/classes", {}),
    ("GET", "classes/getBookings", None),
    ("POST", "classes/getBookings", {}),
    ("GET", "classes/availableSlots", None),
    ("POST", "classes/availableSlots", {}),
    ("GET", "bookings/faq", None),
]


def probe_api() -> None:
    s, data = api_session()
    token = data.get("token") or ""
    for method, path, body in READ_PROBES:
        status, text = api_call(s, token, method, path, body)
        log(f"[api] {method:4} {path:28} -> {status}")
        head = re.sub(r"\s+", " ", text)[:300]
        log(f"       {head}")


# --------------------------------------------------------------------------

def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    cfg = load_cfg()
    if cmd == "auth":
        return 0 if auth_start(cfg, manual=True) else 1
    if cmd == "api":
        probe_api()
        return 0
    if cmd == "all":
        ok = auth_start(cfg, manual=True)
        if ok:
            probe_api()
        return 0 if ok else 1
    log(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
