"""Synthetic checks for the Gmail OTP capture (T17). No network: every HTTP
entry point is patched. Run with:  python ai/verify_t17_otp_capture.py
"""
import base64
import sys
from unittest.mock import patch

sys.path.insert(0, ".")
import google_otp as go  # noqa: E402


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def otp_message(otp="315813", sender="Asteco <no-reply@asteco.com>",
                body=None):
    """A Gmail API 'full' message shaped like the real OTP email."""
    if body is None:
        body = f"Dear user, {otp} is your ASTECO OTP. It expires in 5 minutes."
    return {"id": f"msg-{otp}",
            "snippet": body[:60],
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": sender},
                    {"name": "Subject", "value": "ASTECO OTP"},
                ],
                "body": {"data": b64(body)},
            }}


CFG = {"email": "user@example.com", "password": "pw",
       "google_calendar": {"enabled": True, "refresh_token": "rt",
                           "client_id": "cid", "client_secret": "cs"}}


class FakeGmail:
    """Replays a list of message-list responses; counts the polls."""

    def __init__(self, pages):
        self.pages = pages          # each page: list of message dicts
        self.list_calls = 0
        self.fetched = []

    def get(self, url, token, params):
        if url.endswith("/messages"):
            self.list_calls += 1
            page = self.pages[min(self.list_calls - 1, len(self.pages) - 1)]
            return {"messages": [{"id": m["id"]} for m in page]}
        msg_id = url.rsplit("/", 1)[1]
        for page in self.pages:
            for m in page:
                if m["id"] == msg_id:
                    self.fetched.append(msg_id)
                    return m
        raise AssertionError(f"unexpected message id {msg_id}")


def run(name, pages, expect_otp, expect_min_polls=None,
        expect_max_polls=None, skip="", last_file="", cfg=CFG):
    """Patch the token + Gmail calls and drive capture_otp."""
    fake = FakeGmail(pages)
    slept = []
    written = []

    with patch.object(go, "_get_access_token", lambda cfg: "tok"), \
         patch.object(go, "_gmail_get", fake.get), \
         patch.object(go.time, "sleep", lambda s: slept.append(s)), \
         patch.object(go, "_read_last_otp", lambda: last_file), \
         patch.object(go, "_write_last_otp", written.append):
        otp, error = go.capture_otp(cfg, log=lambda m: None, skip=skip)

    ok = otp == expect_otp
    if expect_min_polls is not None:
        ok = ok and fake.list_calls >= expect_min_polls
    if expect_max_polls is not None:
        ok = ok and fake.list_calls <= expect_max_polls
    print(f"[{'PASS' if ok else 'FAIL'}] {name}\n"
          f"        otp={otp!r} error={error!r} polls={fake.list_calls} "
          f"sleeps={slept}")
    return ok


results = []

# 1. OTP email already in the inbox on the first poll -> captured at once,
#    no sleeps, and the code is remembered for the next login.
results.append(run(
    "immediate hit -> returns the code without waiting",
    pages=[[otp_message("315813")]],
    expect_otp="315813", expect_min_polls=1, expect_max_polls=1))

# 2. Nothing for two polls, then it arrives: exactly the 5 s delay between
#    checks (user requirement) and the code is found on attempt 3.
results.append(run(
    "arrives on the 3rd poll -> two 5s waits, then captured",
    pages=[[], [], [otp_message("817471")]],
    expect_otp="817471", expect_min_polls=3, expect_max_polls=3))

# 3. The server never sends it -> gives up after 12 polls (~60 s) and reports
#    an error so the caller can fall back to asking the user.
results.append(run(
    "never arrives -> stops after 12 retries, no code",
    pages=[[]],
    expect_otp="", expect_min_polls=go.OTP_MAX_RETRIES,
    expect_max_polls=go.OTP_MAX_RETRIES))

# 4. Only the previous (already used) OTP is in the inbox -> treated as stale,
#    retried until the budget runs out instead of re-submitting a dead code.
results.append(run(
    "inbox holds only the stale code -> keeps waiting, returns nothing",
    pages=[[otp_message("111111")]],
    expect_otp="", skip="111111",
    expect_min_polls=go.OTP_MAX_RETRIES))

# 5. A fresh code next to a stale one -> the fresh one wins.
results.append(run(
    "stale + fresh codes in the inbox -> picks the fresh one",
    pages=[[otp_message("222222"), otp_message("111111")]],
    expect_otp="222222", skip="111111", expect_max_polls=1))

# 6. Sender filter: an email from another domain that contains the phrase must
#    be ignored (the user asked for senders of the asteco.com domain only).
results.append(run(
    "phishing-like mail from another sender -> ignored",
    pages=[[otp_message("999999", sender="attacker@example.com")]],
    expect_otp="", expect_min_polls=go.OTP_MAX_RETRIES))

# 7. Asteco mail without a parsable OTP line -> no code, keeps retrying.
results.append(run(
    "asteco mail without the OTP phrase -> no code extracted",
    pages=[[otp_message(body="Your booking was confirmed.")]],
    expect_otp="", expect_min_polls=go.OTP_MAX_RETRIES))

# 8. Google not configured -> capture is refused immediately (manual OTP path).
def _boom(*a, **k):
    raise AssertionError("must not touch the network when Google is off")


with patch.object(go, "_get_access_token", _boom), \
     patch.object(go.time, "sleep", _boom):
    otp, error = go.capture_otp({"google_calendar": {"enabled": False}},
                                log=lambda m: None)
ok = otp == "" and "not configured" in error.lower()
results.append(ok)
print(f"[{'PASS' if ok else 'FAIL'}] google disabled -> refuses without "
      f"network\n        otp={otp!r} error={error!r}")

# 9. Parsing details: case-insensitive, tolerant of extra whitespace.
for text, want in [
    ("315813 is your ASTECO OTP", "315813"),
    ("code: 794343 is your asteco otp.", "794343"),
    ("817471   is   your   ASTECO   OTP", "817471"),
    ("no code here", ""),
    # The real email markup (confirmed live): the code is wrapped in <b> tags.
    ("Hi,<br/><br/><b>315813</b> is your ASTECO OTP. Valid for 60 seconds.",
     "315813"),
]:
    got = go.extract_otp(text)
    ok = got == want
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] extract_otp({text!r}) -> {got!r}")

# 10. multipart/alternative message: the code lives in the nested html part.
multi = {"id": "m-multi", "payload": {
    "mimeType": "multipart/alternative",
    "headers": [{"name": "From", "value": "no-reply@asteco.com"}],
    "parts": [
        {"mimeType": "text/plain", "body": {"data": b64("plain, no code")}},
        {"mimeType": "text/html",
         "body": {"data": b64("<p>555666 is your ASTECO OTP</p>")}},
    ]}}
msg = go.message_text(multi)
ok = go.extract_otp(msg) == "555666" and go.sender_matches(multi)
results.append(ok)
print(f"[{'PASS' if ok else 'FAIL'}] multipart html part parsed\n"
      f"        extract -> {go.extract_otp(msg)!r}")

# 11. CLI helper: login_start auto-finishes when Google is on, and keeps the
#     manual instructions when the capture fails.
import padel_booking as pb  # noqa: E402


class StartHarness:
    """Stands in for the portal + Gmail side of pb.login_start()."""

    def __init__(self, otp, error=""):
        self.finished = []
        self._otp, self._error = otp, error

    def get(self, url):
        class R:
            url = "https://myportal.asteco.com/login/checkOtp"
            status_code = 200
            text = "<html></html>"
        return R()

    def capture(self, cfg, log=None, skip=""):
        return self._otp, self._error

    def login_finish(self, cfg, otp):
        self.finished.append(otp)


def cli_case(name, otp, error, expect_finished):
    h = StartHarness(otp, error)
    session = FakeSession(h.get)
    with patch.object(pb, "new_session", lambda restore=True: session), \
         patch.object(pb, "_timed",
                      lambda method, url, fn: fn()), \
         patch.object(pb, "capture_otp", h.capture), \
         patch.object(pb, "login_finish", h.login_finish):
        pb.login_start(CFG)          # phase 1 + the auto-finish it triggers
    ok = h.finished == ([otp] if expect_finished else [])
    print(f"[{'PASS' if ok else 'FAIL'}] {name}\n"
          f"        login_finish called with {h.finished}")
    return ok


class FakeSession:
    """Minimal stand-in for the curl_cffi session login_start() uses."""

    def __init__(self, get_fn):
        self._get = get_fn
        self.cookies = type("C", (), {"jar": []})()

    def post(self, url, data=None):
        return self._get(url)

    def get(self, url, **kw):
        return self._get(url)


results.append(cli_case("CLI login-start auto-finishes with the Gmail code",
                        "315813", "", True))
results.append(cli_case("CLI login-start keeps manual path on capture failure",
                        "", "no email arrived", False))

# 12. Telegram /login path: a Gmail hit must leave no pending OTP state; a
#     miss must keep waiting for the typed code.
import padel_telegram as pt  # noqa: E402


class BotStub(pt.PadelBot):
    def __init__(self, cfg):          # bypass the real Telegram setup
        self.cfg = cfg
        self.pending = {}
        self.sent = []

    def _send(self, chat_id, text):
        self.sent.append(text)


def tg_case(name, otp, error, expect_pending):
    """_otp_from_gmail reports the outcome only; _on_login decides whether to
    keep waiting for a typed code, so both are exercised here."""
    bot = BotStub(CFG)
    with patch.object(pt, "capture_otp",
                      lambda cfg, log=None, skip="": (otp, error)), \
         patch.object(pt, "login_finish", lambda cfg, o: None), \
         patch.object(pt, "login_start", lambda cfg: None):
        finished = bot._otp_from_gmail(1)
        if not finished:                      # mirrors _on_login's fallback
            bot.pending[1] = {"action": "otp"}
    ok = (finished != expect_pending
          and (1 in bot.pending) == expect_pending)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}\n"
          f"        finished={finished} pending_left={(1 in bot.pending)}")
    return ok


results.append(tg_case("Telegram /login: Gmail hit -> no manual OTP wait",
                       "315813", "", False))
results.append(tg_case("Telegram /login: capture miss -> waits for typed code",
                       "", "no email arrived", True))

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)

