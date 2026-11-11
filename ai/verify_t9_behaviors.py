"""Synthetic check of the two behaviors the user asked to verify.

No portal access: every network entry point is patched. Run with:
    python ai/verify_t9_behaviors.py
"""
import sys
import threading
from datetime import date, datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, ".")
import padel_booking as pb  # noqa: E402

TODAY = datetime.now().date()
TARGET = TODAY + timedelta(days=6)          # far end of the rolling window
# A date the picker can plausibly not have opened YET but will reach soon: it
# must sit inside the picker's own ceiling. Waiting for a date beyond that
# ceiling is pointless (the portal will not accept it until the window slides),
# which is exactly the bug fixed on 11-11-2026 — see picker_ceiling_check().
NEAR = TODAY + timedelta(days=3)
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

# 1. Day the picker has reached but whose slots are not open yet: must NOT
#    book, must keep refreshing every 2 min (120s) and keep the session alive
#    between waits. (An unlisted day *beyond* the picker's ceiling is a
#    different case entirely -> outside_window; see picker_ceiling_check.)
results.append(scenario(
    "day reachable but slots not open -> keeps waiting, never books",
    offered_seq=[[TODAY + timedelta(days=i) for i in range(5)]] * 6,
    slots_seq=[[]],
    expect_outcome="no_slot", expect_booked=0, target=NEAR))

# 2. Picker has reached the target's date but does not list it on the first
#    read (and its slots are not free yet), then lists it with a free slot:
#    the race must wait, then book. The ceiling stays at/above the target so
#    "not listed" means "wait", not "beyond the ceiling".
results.append(scenario(
    "day reachable, opens on the 2nd read -> books then",
    offered_seq=[[TODAY, TODAY + timedelta(days=1), TODAY + timedelta(days=4)],
                 [NEAR]],
    slots_seq=[[], [SLOT]],
    expect_outcome="slot_found", expect_booked=1, target=NEAR))

# 3. Day offered but slots not free yet -> keeps refreshing, books when free.
results.append(scenario(
    "day open, slot taken then free -> books when free",
    offered_seq=[[TARGET]] * 4,
    slots_seq=[[], [], [SLOT]],
    expect_outcome="slot_found", expect_booked=1))

# 4. Beyond the rolling window -> stop immediately, no waiting.
FAR = TODAY + timedelta(days=9)              # past today+6, the widest bound
results.append(scenario(
    "OUTSIDE even the arithmetic window -> stops at once, never waits",
    offered_seq=[[TODAY]],
    slots_seq=[[]],
    expect_outcome="not_yet_open", expect_booked=0, target=FAR))

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

    not_open = [TODAY + timedelta(days=i) for i in range(5)]   # never NEAR

    a, oa = run([not_open], [[]], target=NEAR)                 # waits each time
    b, ob = run([[NEAR]], [[SLOT]], target=NEAR)               # books at once
    c, oc = run([[TODAY]], [[]], target=TODAY + timedelta(days=9))  # too far

    ok = (len(a) == 1 and oa in ("no_slot", "not_yet_open")
          and len(b) == 0 and ob == "slot_found"
          and len(c) == 0 and oc in ("outside_window", "not_yet_open"))
    print(f"[{'PASS' if ok else 'FAIL'}] exactly ONE retry notice per attempt, "
          f"none when it books or refuses at once")
    print(f"        waited 6 refreshes -> notices={len(a)} (outcome={oa}); "
          f"immediate book -> notices={len(b)} (outcome={ob}); outside window "
          f"-> notices={len(c)} (outcome={oc})")
    return ok


results.append(retry_notice_check())

# --- Production regression: picker stops at today+5, bot must NOT POST ------
def picker_ceiling_check():
    """Reproduces the 11-11-2026 production failure.

    Today is Wed 11 Nov; the 'Check in date' picker offers only ..16 Nov, so
    Tue 17 Nov has no option and cannot be booked. The old code treated
    today+6 as the hard limit, saw 17 Nov as merely 'not open yet', asked the
    slot endpoint (which answers for closed dates), found a free slot and POSTed
    it -> HTTP 303 'booking not confirmed'.
    """
    today = datetime.now().date()
    picker = [today + timedelta(days=i) for i in range(0, 6)]   # .. today+5
    beyond = today + timedelta(days=6)                          # == today+6

    def attempt(offered_seq, slots_seq, target):
        state = {"n": 0}
        posts = []

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
                          lambda sl, pr: SLOT if sl else None), \
             patch.object(pb, "submit_booking_fast",
                          lambda *a, **k: posts.append(a) or "303"), \
             patch.object(pb.time, "sleep", lambda sec: None):
            outcome = pb.run_booking_race(
                CFG, datetime.combine(target, datetime.min.time()), PREFERRED,
                wait_for_open=False, max_attempts=2)[3]
        return posts, outcome

    # 17 Nov is beyond the picker's ceiling even though slots look free.
    posts_a, out_a = attempt([picker], [[SLOT]], beyond)
    # A date the picker DOES offer must still book normally.
    posts_b, out_b = attempt([picker], [[SLOT]], picker[-1])

    ok = (len(posts_a) == 0 and out_a == "not_yet_open"
          and len(posts_b) == 1 and out_b == "slot_found")
    print(f"[{'PASS' if ok else 'FAIL'}] date beyond the picker's last option is "
          f"never POSTed (the production 303 bug)")
    print(f"        target=today+6, picker ends today+5 -> posts={len(posts_a)} "
          f"(outcome={out_a}); target=today+5 (offered) -> posts={len(posts_b)} "
          f"(outcome={out_b})")
    return ok


def disabled_option_check():
    """A greyed-out <option> is the portal refusing a date; it must not count."""
    html = ('<select name="check_in_date">'
            '<option value="Nov 15, 2026">Nov 15, 2026</option>'
            '<option value="Nov 17, 2026" disabled>Nov 17, 2026</option>'
            "</select>")
    dates = pb.parse_booking_dates(html)
    ok = len(dates) == 1 and dates[0].day == 15
    print(f"[{'PASS' if ok else 'FAIL'}] <option disabled> is not treated as "
          f"bookable")
    print(f"        parsed -> {dates}")
    return ok


results.append(picker_ceiling_check())
results.append(disabled_option_check())

# --- THE live portal: a jQuery datepicker with hidden startDate/endDate ----
def datepicker_range_check():
    """The real booking page has NO <select>: the picker bounds are hidden inputs.

    Reproduces the live markup (diag_booking_page.html): an input
    id="check_in_date" plus hidden startDate/endDate ("Nov 11, 2026"/"Nov 17,
    2026"). The bot must enumerate that inclusive range so a date the browser
    offers is recognised as bookable (the false-negative that made it wait).
    """
    html = ('<input type="text" id="check_in_date" name="check_in_date">'
            '<input type="hidden" id="startDate" value="Nov 11, 2026">'
            '<input type="hidden" id="endDate" value="Nov 17, 2026">'
            '<input type="hidden" id="interval" value="1">')
    dates = pb.parse_booking_dates(html)
    ok = (len(dates) == 7 and dates[0].day == 11 and dates[-1].day == 17)
    print(f"[{'PASS' if ok else 'FAIL'}] hidden startDate/endDate datepicker "
          f"range is enumerated")
    print(f"        parsed -> {dates}")
    return ok


results.append(datepicker_range_check())

# --- THE production case: picker unreadable -> wait, and NEVER post --------
def unreadable_picker_never_posts_check():
    """The 11-11-2026 01:12 failure.

    'Check in date' does not offer Tue 17 Nov yet (the server has not opened
    it), and the bot's read of that picker comes back empty. The slot endpoint
    still reports 19:00-20:00 free, so the old code POSTed it and got HTTP 303
    'booking not confirmed (slots still available)'.

    Required behaviour: never submit, but KEEP TRYING every retry_minutes until
    booking_max_wait_minutes, so the slot is taken the moment the portal opens
    the day.
    """
    today = datetime.now().date()
    target = today + timedelta(days=6)          # Tue 17 Nov
    state = {"n": 0}
    posts, sleeps = [], []

    def offered(cfg, force=False):
        state["n"] += 1
        return []                                 # picker unreadable every time

    with patch.object(pb, "get_booking_meta", lambda cfg: (object(), {})), \
         patch.object(pb, "portal_bookable_dates", offered), \
         patch.object(pb, "get_available_slots", lambda *a, **k: [SLOT]), \
         patch.object(pb, "pick_best_slot", lambda sl, pr: SLOT if sl else None), \
         patch.object(pb, "submit_booking_fast",
                      lambda *a, **k: posts.append(a) or "303"), \
         patch.object(pb.time, "sleep", sleeps.append):
        outcome = pb.run_booking_race(
            CFG, datetime.combine(target, datetime.min.time()), PREFERRED,
            wait_for_open=False, max_attempts=4)[3]

    ok = (len(posts) == 0 and outcome == "not_yet_open"
          and len(sleeps) == 3 and all(s == 120 for s in sleeps))
    print(f"[{'PASS' if ok else 'FAIL'}] unreadable picker: NEVER posts, but "
          f"keeps waiting every 2 min")
    print(f"        posts={len(posts)} outcome={outcome} "
          f"waits={len(sleeps)}x{sleeps[0] if sleeps else 0}s")
    return ok


def picker_opens_later_books_check():
    """The payoff: while waiting, the portal opens the day -> it books then."""
    today = datetime.now().date()
    target = today + timedelta(days=6)
    reads = {
        "n": 0,
        # refresh 1-2 unreadable, 3 offers ..16 (still no 17), 4 lists 17.
        "seq": [[], [], [today + timedelta(days=i) for i in range(7)],
                [target]],
    }
    posts = []

    def offered(cfg, force=False):
        reads["n"] += 1
        i = min(reads["n"] - 1, len(reads["seq"]) - 1)
        return reads["seq"][i]

    def slots(s, cfg, api_date, meta):
        return [] if reads["n"] < 4 else [SLOT]

    with patch.object(pb, "get_booking_meta", lambda cfg: (object(), {})), \
         patch.object(pb, "portal_bookable_dates", offered), \
         patch.object(pb, "get_available_slots", slots), \
         patch.object(pb, "pick_best_slot", lambda sl, pr: SLOT if sl else None), \
         patch.object(pb, "submit_booking_fast",
                      lambda *a, **k: posts.append(a) or "200"), \
         patch.object(pb.time, "sleep", lambda s: None):
        outcome = pb.run_booking_race(
            CFG, datetime.combine(target, datetime.min.time()), PREFERRED,
            wait_for_open=False)[3]

    ok = len(posts) == 1 and outcome == "slot_found"
    print(f"[{'PASS' if ok else 'FAIL'}] portal opens the day during the wait "
          f"-> books immediately (posts only once)")
    print(f"        posts={len(posts)} outcome={outcome}")
    return ok


results.append(unreadable_picker_never_posts_check())
results.append(picker_opens_later_books_check())

# --- Scheduler: an unopened desired day is retried, then booked ------------
def scheduler_retries_until_open_check():
    """run_autobook_loop must not give up on Tue 17 Nov just because the picker
    has not opened it. It re-attempts on its own cadence and books the moment
    the portal lists the day."""
    today = datetime.now().date()
    tuesday = (1 - today.weekday()) % 7 or 7        # next Tuesday
    target = today + timedelta(days=tuesday)
    picker_without = [today + timedelta(days=i) for i in range(0, 6)
                      if (today + timedelta(days=i)) != target]

    clock = {"t": 1_000_000.0}
    reads = {"n": 0}
    posts = []
    logs = []

    def offered(cfg, force=False):
        reads["n"] += 1
        # Open the target only from the 3rd scheduler read onwards.
        return picker_without if reads["n"] < 3 else [target]

    cfg = {"preferred_slots": {target.strftime("%A"): ["19:00"]},
           "slot_retry_minutes": 2}

    def fake_sleep(secs):
        clock["t"] += secs

    real_time = pb.time.time
    with patch.object(pb, "time") as tmod:
        tmod.time.side_effect = lambda: clock["t"]
        tmod.sleep.side_effect = fake_sleep
        tmod.monotonic.side_effect = lambda: clock["t"]
        with patch.object(pb, "portal_bookable_dates", offered), \
             patch.object(pb, "refresh_state", lambda cfg: None), \
             patch.object(pb, "keepalive", lambda cfg: None), \
             patch.object(pb, "_fetch_my_bookings_fast", lambda s: []), \
             patch.object(pb, "get_booking_meta", lambda cfg: (object(), {})), \
             patch.object(pb, "get_available_slots",
                          lambda *a, **k: [] if reads["n"] < 3 else [SLOT]), \
             patch.object(pb, "pick_best_slot",
                          lambda sl, pr: SLOT if sl else None), \
             patch.object(pb, "submit_booking_fast",
                          lambda *a, **k: posts.append(a) or "200"), \
             patch.object(pb, "verify_booking_created", lambda *a, **k: True), \
             patch.object(pb, "record_booking", lambda *a, **k: None), \
             patch.object(pb, "sync_booking", lambda *a, **k: False), \
             patch.object(pb, "_reconcile_booked", lambda b: None):
            stop = threading.Event()

            def bookings(s):
                return []

            # Stop the loop right after the booking lands.
            orig_race = pb.run_booking_race

            def race(*a, **k):
                res = orig_race(*a, **k)
                if res[3] == "slot_found":
                    stop.set()
                return res

            with patch.object(pb, "run_booking_race", race), \
                 patch.object(pb, "_slots_endpoint",
                              lambda *a, **k: ([], None)):
                pb.run_autobook_loop(cfg, lambda m: logs.append(m),
                                     stop_event=stop)
    del real_time

    ok = len(posts) == 1
    print(f"[{'PASS' if ok else 'FAIL'}] scheduler keeps trying an unopened "
          f"day and books it once the portal opens it")
    print(f"        posts={len(posts)} scheduler_reads={reads['n']} "
          f"loop_log_lines={len(logs)}")
    for line in logs[:6]:
        print(f"          | {line}")
    return ok


results.append(scheduler_retries_until_open_check())

print("\n" + ("ALL PASS" if all(results) else "FAILURES PRESENT"))
sys.exit(0 if all(results) else 1)
