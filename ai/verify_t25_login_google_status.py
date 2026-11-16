"""T25 checks: /login must confirm the Google integration state too, like boot does.

The user saw only "Login successful — session saved" and had no way to tell that
Calendar sync + Gmail OTP reading are alive. `_startup_status()` reports that at
boot; login success paths now reuse the same live check via
`PadelBot._google_status_line()`.

Run with:  python ai/verify_t25_login_google_status.py
"""
import sys
from unittest.mock import patch

sys.path.insert(0, ".")
import padel_telegram as pt  # noqa: E402


def _cfg():
    return {"email": "user@example.com", "password": "pw",
            "telegram_bot_token": "t", "telegram_chat_ids": [1],
            "google_calendar": {"enabled": True, "email": "u@gmail.com",
                                "refresh_token": "rt", "client_id": "cid",
                                "client_secret": "cs"}}


class BotStub(pt.PadelBot):
    """PadelBot without the Telegram/network setup."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.chat_ids = [1]
        self.pending = {}
        self.sent = []

    def _send(self, chat_id, text, reply_markup=None, parse_mode=None):
        self.sent.append(text)


def check_status_line_variants(log):
    """The helper mirrors _startup_status wording for all three states."""
    results = []
    cases = [
        ((True, "Google Calendar reachable"), "Google Calendar active"),
        ((None, "not configured"), "not configured"),
        ((False, "Calendar access failed (HTTP 401)"), "NOT active"),
    ]
    for (gok, detail), needle in cases:
        bot = BotStub(_cfg())
        with patch.object(pt, "test_connection", lambda cfg, _g=gok, _d=detail: (_g, _d)):
            line = bot._google_status_line()
        ok = needle.lower() in line.lower() and line.startswith("\n")
        log(f"[{'PASS' if ok else 'FAIL'}] state {gok!r} -> {line.strip()!r}")
        results.append(ok)
    return results


def check_login_reports_google(log):
    """When login_start says the login is already complete, /login must send the
    session line AND the Google line."""
    bot = BotStub(_cfg())
    with patch.object(pt, "login_start", lambda cfg: True), \
         patch.object(pt, "test_connection",
                      lambda cfg: (True, "Google Calendar reachable")):
        bot._on_login(1)

    ok = (len(bot.sent) == 1
          and "Login successful" in bot.sent[0]
          and "Google Calendar active" in bot.sent[0])
    log(f"[{'PASS' if ok else 'FAIL'}] /login (already complete) reports both:\n"
        f"        {bot.sent}")
    return [ok]


def check_gmail_path_reports_google(log):
    """The _otp_from_gmail fallback that finds the session already authenticated
    must report Google state too."""
    bot = BotStub(_cfg())
    with patch.object(pt, "google_configured", lambda cfg: True), \
         patch.object(pt, "capture_otp", lambda cfg, log=None, since=None: ("", "no mail")), \
         patch.object(pt, "is_logged_in", lambda s, cfg: True), \
         patch.object(pt, "get_session", lambda: None), \
         patch.object(pt, "test_connection",
                      lambda cfg: (True, "Google Calendar reachable")):
        finished = bot._otp_from_gmail(1)

    ok = (finished is True and len(bot.sent) == 2
          and "Google Calendar active" in bot.sent[1])
    log(f"[{'PASS' if ok else 'FAIL'}] _otp_from_gmail already-authed path "
        f"reports Google:\n        {bot.sent}")
    return [ok]


def main():
    def log(msg):
        print(msg)

    results = []
    results += check_status_line_variants(log)
    results += check_login_reports_google(log)
    results += check_gmail_path_reports_google(log)

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
