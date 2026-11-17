"""T26 check: an expired session during autobooking must trigger re-login.

Reported symptom: after the automatic retry limit (booking_max_wait_minutes,
default 12h) the bot says "Booking error: Session expired. Re-login before
booking." instead of re-logging in.

Why: `book_day()` validates the session once via `ensure_session()` ->
`keepalive()`, and on failure it calls the injected `relogin` callback. But
`run_booking_race()` then calls `get_booking_meta(cfg)` (padel_booking.py:2352),
which hits the booking page again. If the session died in between, that raises
RuntimeError("Session expired..."), which escapes book_day's try/except as
"Booking error: ..." and never reaches relogin().

Run with:  python ai/verify_t26_autobook_relogin.py
"""
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, ".")
import padel_booking as pb  # noqa: E402


CFG = {"email": "u@example.com", "password": "pw", "asset_booking_id": "1",
       "slot_retry_minutes": 1, "booking_max_wait_minutes": 720,
       "booking_open_hour": 0, "booking_retry_band_minutes": 30}


class Expired(RuntimeError):
    pass


def _race_setup(log):
    """Fakes so run_booking_race reaches the meta fetch with a session that is
    expired exactly once: the first booking-meta call fails, and after the
    re-login callback runs, the retry succeeds. That is what a real recovery
    looks like (login_finish installs a fresh shared session)."""
    calls = {"relogin": 0, "keepalive": 0, "meta": 0}

    def keepalive(cfg):
        calls["keepalive"] += 1
        raise RuntimeError("Session missing/expired. Run 'login-start' ...")

    def relogin():
        calls["relogin"] += 1
        return True

    def meta_expired_once(cfg):
        calls["meta"] += 1
        if calls["meta"] == 1:
            raise RuntimeError("Session expired. Re-login before booking.")
        return (object(), {"unit": "1"})

    return calls, keepalive, relogin, meta_expired_once


def check_race_relogins_on_expiry(log):
    """run_booking_race must recover from an expired session instead of letting
    RuntimeError escape, when the caller supplies a recovery callback."""
    calls, keepalive, relogin, meta_expired = _race_setup(log)

    target = datetime.now() + timedelta(days=1)
    with patch.object(pb, "get_booking_meta", meta_expired), \
         patch.object(pb, "keepalive", keepalive), \
         patch.object(pb, "portal_bookable_dates", lambda cfg: []):
        try:
            pb.run_booking_race(
                CFG, target, ["18:00 - 19:00"], wait_for_open=False,
                max_attempts=1, dry_run=True, on_wait=lambda: keepalive(CFG),
                on_session_expired=relogin)
            escaped = False
        except RuntimeError as e:
            escaped = "Session expired" in str(e)

    recovered = calls["relogin"] >= 1
    ok = (not escaped) and recovered
    log(f"[{'PASS' if ok else 'FAIL'}] run_booking_race recovers via the "
        f"re-login callback (escaped={escaped}, relogin_calls={calls['relogin']})")
    return [ok]


def check_race_without_callback_still_raises(log):
    """No recovery callback -> the error must still surface (CLI autobook and
    every direct call keep their old behaviour)."""
    _calls, keepalive, _relogin, _meta_once = _race_setup(log)

    def meta_always_expired(cfg):
        raise RuntimeError("Session expired. Re-login before booking.")

    target = datetime.now() + timedelta(days=1)
    with patch.object(pb, "get_booking_meta", meta_always_expired), \
         patch.object(pb, "keepalive", keepalive), \
         patch.object(pb, "portal_bookable_dates", lambda cfg: []):
        try:
            pb.run_booking_race(
                CFG, target, ["18:00 - 19:00"], wait_for_open=False,
                max_attempts=1, dry_run=True)
            escaped = False
        except RuntimeError as e:
            escaped = "Session expired" in str(e)

    ok = escaped
    log(f"[{'PASS' if ok else 'FAIL'}] without a callback the error still "
        f"propagates (escaped={escaped})")
    return [ok]


def main():
    def log(msg):
        print(msg)

    results = []
    results += check_race_relogins_on_expiry(log)
    results += check_race_without_callback_still_raises(log)

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
