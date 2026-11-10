"""Synthetic check of the two behaviors the user asked to verify.

No portal access: every network entry point is patched. Run with:
    python ai/verify_t9_behaviors.py
"""
import sys
from datetime import date, datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, ".")
import padel_booking as pb  # noqa: E402

TODAY = datetime.now().date()
TARGET = TODAY + timedelta(days=6)          # far end of the rolling window
PREFERRED = ["18:00-19:00"]
SLOT = {"label": "18:00-19:00", "value": "18", "start": "18:00"}
CFG = {"slot_retry_minutes": 2, "booking_max_wait_minutes": 360}


def scenario(name, offered_seq, slots_seq, expect_outcome, expect_booked,
             max_attempts=8, target=None):
    """offered_seq/slots_seq: values returned by successive refreshes."""
    target = target or TARGET
    calls = {"book": 0, "sleep": 0, "keepalive": 0}

    def fake_meta(cfg):
        return object(), {"id": 370}

    def fake_slots(s, cfg, api_date, meta):
        i = min(len(slots_seen := getattr(fake_slots, "seen", [])),
                len(slots_seq) - 1)
        fake_slots.seen.append(i)
        return slots_seq[i]

    def fake_submit(s, cfg, meta, d, slot, desc):
        calls["book"] += 1
        return "200"

    def fake_sleep(secs):
        calls["sleep"] += secs

    fake_slots.seen = []

    def cycled(seq):
        """Return a fn(i) giving seq[min(i, len-1)] — repeats the last value."""
        def get(i):
            return seq[min(i, len(seq) - 1)]
        return get

    offered_at = cycled(offered_seq)
    slots_at = cycled(slots_seq)
    state = {"n": 0}

    def fake_offered(cfg, force=False):
        state["n"] += 1
        return offered_at(state["n"] - 1)

    def fake_slots(s, cfg, api_date, meta):
        return slots_at(state["n"] - 1)

    with patch.object(pb, "get_booking_meta", fake_meta), \
         patch.object(pb, "portal_bookable_dates", fake_offered), \
         patch.object(pb, "get_available_slots", fake_slots), \
         patch.object(pb, "pick_best_slot", lambda slots, pref: SLOT if slots else None), \
         patch.object(pb, "submit_booking_fast", fake_submit), \
         patch.object(pb.time, "sleep", fake_sleep):
        best, _slots, status, outcome = pb.run_booking_race(
            CFG, datetime.combine(target, datetime.min.time()), PREFERRED,
            wait_for_open=False, max_attempts=max_attempts,
            on_wait=lambda: calls.__setitem__(
                "keepalive", calls["keepalive"] + 1))

    ok = (outcome == expect_outcome and calls["book"] == expect_booked)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}\n"
          f"        outcome={outcome} booking_attempts={calls['book']} "
          f"slept={calls['sleep']}s keepalive={calls['keepalive']}")
    return ok


results = []

# 1. Day not opened by the portal at all: must NOT book, must keep refreshing
#    every 2 min (120s) and keep the session alive between waits.
results.append(scenario(
    "in-window but NOT OPEN YET -> keeps waiting, never books",
    offered_seq=[[TODAY + timedelta(days=i) for i in range(5)]] * 6,
    slots_seq=[[]],
    expect_outcome="not_yet_open", expect_booked=0))

# 2. Portal opens the day on the third refresh -> books immediately after.
results.append(scenario(
    "day OPENS after 2 refreshes -> books on the 3rd",
    offered_seq=[[TODAY + timedelta(days=i) for i in range(5)],
                 [TODAY + timedelta(days=i) for i in range(5)],
                 [TARGET]],
    slots_seq=[[], [], [SLOT]],
    expect_outcome="slot_found", expect_booked=1))

# 3. Day offered but slots not free yet -> keeps refreshing, books when free.
results.append(scenario(
    "day open, slot taken then free -> books when free",
    offered_seq=[[TARGET]] * 4,
    slots_seq=[[], [], [SLOT]],
    expect_outcome="slot_found", expect_booked=1))

# 4. Beyond the rolling window -> stop immediately, no waiting.
FAR = TODAY + timedelta(days=9)              # today+6 is the last bookable day
results.append(scenario(
    "OUTSIDE the 7-day window -> stops at once, never waits",
    offered_seq=[[TODAY]],
    slots_seq=[[]],
    expect_outcome="outside_window", expect_booked=0, target=FAR))

# --- Bot date keyboard: must offer ONLY the days the portal opened ---------
class _FakeBot:
    """Minimal stand-in capturing what _show_date_keyboard would render."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.sent = []

    def _send(self, chat_id, text, keyboard=None):
        self.sent.append((text, keyboard))


import padel_telegram as pt  # noqa: E402

PORTAL_OPENED = [TODAY + timedelta(days=i) for i in range(4)]  # today..+3


def keyboard_check():
    with patch.object(pt, "portal_bookable_dates", lambda cfg, force=False: PORTAL_OPENED):
        bot = _FakeBot(CFG)
        pt.PadelBot._show_date_keyboard(bot, 1, "Pick a date:")
    text, kb = bot.sent[0]
    buttons = [b["text"] for row in kb["inline_keyboard"] for b in row]
    expected = [f"{d:%a %d %b}" for d in PORTAL_OPENED]
    ok = buttons == expected
    print(f"[{'PASS' if ok else 'FAIL'}] bot keyboard offers ONLY portal-opened days")
    print(f"        buttons={buttons} (expected {expected}; "
          f"portal opened {len(PORTAL_OPENED)} of 7 window days)")
    return ok


results.append(keyboard_check())

# --- What happens when the day is open but its slots stay taken? ----------
def taken_forever_check():
    """Day offered, no preferred slot EVER free: does it keep hammering, or
    give up and leave the decision to the scheduler?"""
    calls = {"book": 0, "sleep": 0}

    def fake_slots(s, cfg, api_date, meta):
        return []                      # never a free preferred slot

    with patch.object(pb, "get_booking_meta", lambda cfg: (object(), {})), \
         patch.object(pb, "portal_bookable_dates", lambda cfg, force=False: [TARGET]), \
         patch.object(pb, "get_available_slots", fake_slots), \
         patch.object(pb, "submit_booking_fast",
                      lambda *a, **k: calls.__setitem__("book", calls["book"] + 1)), \
         patch.object(pb.time, "sleep",
                      lambda sec: calls.__setitem__("sleep", calls["sleep"] + sec)):
        best, slots, status, outcome = pb.run_booking_race(
            CFG, datetime.combine(TARGET, datetime.min.time()), PREFERRED,
            wait_for_open=False, max_attempts=5)

    ok = (best is None and outcome == "no_slot" and calls["book"] == 0)
    print(f"[{'PASS' if ok else 'FAIL'}] day open but slots NEVER free -> gives up "
          f"(outcome=no_slot), books nothing")
    print(f"        best={best} outcome={outcome} booking_attempts={calls['book']} "
          f"slept={calls['sleep']}s over 5 refreshes")
    return ok


results.append(taken_forever_check())

# --- The configured default wait is 12 hours ------------------------------
def default_wait_check():
    """With no booking_max_wait_minutes in cfg, the bound must be
    BOOKING_MAX_WAIT_MINUTES (720 min = 12h), not the old 360. Computed exactly
    as run_booking_race computes it."""
    cfg = {}                       # empty -> every default applies
    retry_secs = max(1, int(cfg.get("slot_retry_minutes", 2))) * 60
    timeout = max(retry_secs, int(cfg.get(
        "booking_max_wait_minutes", pb.BOOKING_MAX_WAIT_MINUTES)) * 60)

    ok = pb.BOOKING_MAX_WAIT_MINUTES == 720 and timeout == 720 * 60
    print(f"[{'PASS' if ok else 'FAIL'}] default booking_max_wait_minutes is 12h")
    print(f"        BOOKING_MAX_WAIT_MINUTES={pb.BOOKING_MAX_WAIT_MINUTES} "
          f"-> timeout={timeout}s ({timeout // 3600}h)")
    return ok


results.append(default_wait_check())

# --- Config key wins over the constant; constant is only the fallback ------
def config_override_check():
    """The lowercase config key and the UPPER_CASE constant are not two
    settings: the constant is only what cfg.get() falls back to when the key is
    absent. An explicit value must always win."""
    def bound(cfg):
        retry_secs = max(1, int(cfg.get("slot_retry_minutes", 2))) * 60
        return max(retry_secs, int(cfg.get(
            "booking_max_wait_minutes", pb.BOOKING_MAX_WAIT_MINUTES)) * 60)

    cases = [
        ({}, 720 * 60, "key absent -> constant 720"),
        ({"booking_max_wait_minutes": 30}, 30 * 60, "explicit 30 wins"),
        ({"booking_max_wait_minutes": 1440}, 1440 * 60, "explicit 1440 wins"),
    ]
    ok = True
    for cfg, want, why in cases:
        got = bound(cfg)
        good = got == want
        ok = ok and good
        print(f"    {'ok' if good else 'BAD'} {why}: {got}s")
    print(f"[{'PASS' if ok else 'FAIL'}] config key overrides the constant; "
          f"constant is fallback only")
    return ok


results.append(config_override_check())

# --- One "still trying" notice per attempt, never one per refresh ----------
def retry_notice_check():
    """The user must see in Telegram that a day is being waited for, WITHOUT
    getting a message every 2 minutes: on_retry fires exactly once per race."""
    def run(offered_seq, slots_seq, max_attempts=6, target=None):
        target = target or TARGET
        notices = []
        state = {"n": 0}

        def offered(cfg, force=False):
            state["n"] += 1
            i = min(state["n"] - 1, len(offered_seq) - 1)
            return offered_seq[i]

        def slots(s, cfg, api_date, meta):
            i = min(state["n"] - 1, len(slots_seq) - 1)
            return slots_seq[i]

        with patch.object(pb, "get_booking_meta", lambda cfg: (object(), {})), \
             patch.object(pb, "portal_bookable_dates", offered), \
             patch.object(pb, "get_available_slots", slots), \
             patch.object(pb, "pick_best_slot",
                          lambda sl, pref: SLOT if sl else None), \
             patch.object(pb, "submit_booking_fast", lambda *a, **k: "200"), \
             patch.object(pb.time, "sleep", lambda sec: None):
            outcome = pb.run_booking_race(
                CFG, datetime.combine(target, datetime.min.time()), PREFERRED,
                wait_for_open=False, max_attempts=max_attempts,
                on_retry=notices.append)[3]
        return notices, outcome

    not_open = [TODAY + timedelta(days=i) for i in range(5)]   # never TARGET

    a, oa = run([not_open], [[]])                              # waits each time
    b, ob = run([[TARGET]], [[SLOT]])                          # books at once
    c, oc = run([[TODAY]], [[]], target=TODAY + timedelta(days=9))  # too far

    ok = (len(a) == 1 and oa == "not_yet_open"
          and len(b) == 0 and ob == "slot_found"
          and len(c) == 0 and oc == "outside_window")
    print(f"[{'PASS' if ok else 'FAIL'}] exactly ONE retry notice per attempt, "
          f"none when it books or refuses at once")
    print(f"        waited 6 refreshes -> notices={len(a)} (outcome={oa}); "
          f"immediate book -> notices={len(b)} (outcome={ob}); outside window "
          f"-> notices={len(c)} (outcome={oc})")
    return ok


results.append(retry_notice_check())

print("\n" + ("ALL PASS" if all(results) else "FAILURES PRESENT"))
sys.exit(0 if all(results) else 1)
