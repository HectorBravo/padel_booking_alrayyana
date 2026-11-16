"""T23 checks: a stale interactive flow must never swallow /login (or any
other slash command).

The bug: `_on_message` routed *every* text through the pending-flow branch
before the `/command` dispatch. When Google was configured but its token was
dead, `_startup_status()` auto-started the `/setupgoogle` flow at boot and left
`pending[chat] = {action: "gcal_email"}` behind — so the user's next `/login`
was fed to the email validator ("That doesn't look like an email") and never
reached `_on_login`.

Run with:  python ai/verify_t23_login_not_blocked.py
"""
import sys
from unittest.mock import patch

sys.path.insert(0, ".")
import padel_telegram as pt  # noqa: E402


CFG = {"email": "user@example.com", "password": "pw",
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
        self.logins = 0
        self.gcal_inputs = []

    def _send(self, chat_id, text, reply_markup=None, parse_mode=None):
        self.sent.append(text)

    def _on_login(self, chat_id):
        self.logins += 1

    def _on_gcal_setup(self, chat_id, step, text):
        self.gcal_inputs.append((step, text))


def case(name, pending, text, expect_login, expect_gcal):
    bot = BotStub(CFG)
    if pending:
        bot.pending[1] = dict(pending)
    bot._on_message({"chat": {"id": 1}, "text": text})
    ok = bot.logins == (1 if expect_login else 0)
    ok = ok and len(bot.gcal_inputs) == (1 if expect_gcal else 0)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}\n"
          f"        /login ran={bot.logins} gcal_steps={bot.gcal_inputs} "
          f"pending_left={bot.pending.get(1)}")
    return ok


results = []

# 1. The regression itself: boot auto-started the Google setup, so a stale
#    `gcal_email` is pending. `/login` must still run the login.
results.append(case(
    "stale gcal_email does NOT swallow /login",
    {"action": "gcal_email", "data": {"_reuse": True, "email": "u@gmail.com"}},
    "/login", expect_login=True, expect_gcal=False))

# 2. Same for a mid-auth stale state (the OAuth code step).
results.append(case(
    "stale gcal_code does NOT swallow /login",
    {"action": "gcal_code", "data": {}, "_result": {}},
    "/login", expect_login=True, expect_gcal=False))

# 3. Any other command is still a command, never flow input.
results.append(case(
    "stale gcal_email does NOT swallow /status",
    {"action": "gcal_email", "data": {}},
    "/status", expect_login=False, expect_gcal=False))

# 4. The flow itself must keep working: plain text is still its input.
results.append(case(
    "plain text still feeds the pending Google flow",
    {"action": "gcal_email", "data": {}},
    "new@gmail.com", expect_login=False, expect_gcal=True))

# 5. The flow must stay reachable: plain text is still its input (covered
#    above), and a command now clears the stale state so it cannot interfere
#    with anything afterwards.
def case_stale_cleared(name, pending, text):
    bot = BotStub(CFG)
    bot.pending[1] = dict(pending)
    bot._on_message({"chat": {"id": 1}, "text": text})
    ok = 1 not in bot.pending and not bot.gcal_inputs
    print(f"[{'PASS' if ok else 'FAIL'}] {name}\n"
          f"        pending_left={bot.pending.get(1)} gcal_steps={bot.gcal_inputs}")
    return ok


results.append(case_stale_cleared(
    "a command clears the stale Google-setup state",
    {"action": "gcal_code", "data": {}, "_result": {}}, "/help"))


# ---- _otp_from_gmail: no OTP must not dead-end an already-good session ----- #
def gmail_case(name, otp, error, logged_in, expect_done):
    """When capture_otp finds nothing, the portal may already have completed the
    login inside login_start; in that case /login must report success instead of
    waiting for a code that can never validate."""
    bot = BotStub(CFG)
    with patch.object(pt, "google_configured", lambda cfg: True), \
         patch.object(pt, "capture_otp",
                      lambda cfg, log=None, since=None: (otp, error)), \
         patch.object(pt, "is_logged_in", lambda s, cfg: logged_in), \
         patch.object(pt, "get_session", lambda: None):
        done = bot._otp_from_gmail(1)
    ok = done == expect_done
    print(f"[{'PASS' if ok else 'FAIL'}] {name}\n"
          f"        finished={done} sent={bot.sent}")
    return ok


results.append(gmail_case(
    "no OTP + session already valid -> /login succeeds",
    "", "no fresh OTP email arrived", True, expect_done=True))
results.append(gmail_case(
    "no OTP + session still dead -> falls back to a typed code",
    "", "no fresh OTP email arrived", False, expect_done=False))

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
