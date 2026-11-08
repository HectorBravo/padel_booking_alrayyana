# Padel Court Booking Automation

Automates booking the Al Rayyana community padel court on the Asteco portal
(`https://myportal.asteco.com`) without a browser.

## How it works
- **Login** is two-phase because the portal emails a one-time password (OTP):
  1. `login-start` submits your credentials (obfuscated exactly like the
     portal's JS) and triggers the OTP email.
  2. `login-finish <OTP>` verifies the OTP and stores the session.
- The authenticated **session cookie is persisted** to `session.json`, so you
  only enter an OTP once per session lifetime. Every later command reuses it.
- **Slots** are fetched from the portal's `ajaxctrl/getAmenityBookingSlot`
  endpoint (the same call the web UI makes when you pick a date).
- **Booking** posts the booking form (`#form_submit`) with your unit,
  attendees, date, chosen slot and the T&C acceptance. Uses
  `stream=True` + `allow_redirects=False` to discard the server's
  ~5s HTML response body — the portal processes the booking in <1s and
  immediately sends a 303 redirect, so the client captures the status
  code in ~0.6s and moves on.
- **My bookings / cancel** read the portal's `My Bookings` page
  (`/booking/myBooking`). Each row links to a booking-details id
  (`/booking/bookingDetails/<id>`); cancelling is a plain POST to
  `/booking/cancelAmenityBooking` with that id (the on-page confirm popup is
  client-side only). A booking can be cancelled while its start time is still
  in the future; the portal then marks it `Reject`. Cancel also uses the
  `stream=True` technique (~0.4s).
- **In-memory state** — after a one-time `refresh_state()` call (at startup
  and after each booking/cancel), the bot keeps the booking metadata and the
  user's bookings list in memory (`_STATE`). All subsequent checks (already
  booked?, booking limit?) run against this cache with zero extra network
  calls. The cache is refreshed from the portal after every successful
  booking or cancellation so `/mybookings` always shows current data.
- **Bookable window comes from the portal** — the booking page lists the
  bookable dates in its "Check in date" `<select>`, and only those dates are
  accepted (a date it does not offer is rejected when submitted, even though
  the slot endpoint still answers for it). The bot reads that list instead of
  assuming `today .. today+6`, so it never offers or attempts a date the
  portal has not opened. `python padel_booking.py window` and the bot's
  `/window` show the live window.
- **Booking limit** — the portal allows a maximum of **3 active (non-cancelled)
  bookings** within its bookable window. The bot enforces this locally: if you
  already hold 3 bookings in the window, further booking attempts are skipped
  without hitting the portal.

## Files
| File               | Purpose                                              | Protected |
|--------------------|------------------------------------------------------|-----------|
| `padel_booking.py` | The automation script                                | —         |
| `config.json`      | Credentials + booking defaults (email, password, …)  | `600`     |
| `session.json`     | Persisted login session (cookies)                    | `600`     |
| `pending_login.json`| Temporary state between login-start and login-finish | `600`     |
| `padel_telegram.py` | Telegram bot API client + interactive bot (login/OTP, book, autobooking) | —         |
| `TELEGRAM.md` | Step-by-step Telegram bot setup guide | —         |

`config.json`, `session.json` and `pending_login.json` are **never** meant to
be committed (see `.gitignore`) and are created with owner-only permissions
(`chmod 600` on Unix; on Windows the same call just clears the read-only bit,
which is harmless — the files are already private to your account).

## Platform

Works on both **Windows** and **Linux** (and macOS) — the same `padel_booking.py`
runs on both, with no unguarded platform-specific code:
- **Colors** are emitted only on a real TTY and auto-disabled when output is
  piped; on Windows the console is first upgraded to VT (ANSI) mode — a no-op
  on Unix.
- **File permissions** use `chmod 600`: a real restriction on Unix, a harmless
  no-op on Windows.
- **Paths** use `pathlib` and **text I/O** is explicit UTF-8, so there are no
  separator or encoding surprises between the two OSes.

The only OS differences are how you keep the bot running in the background
(see below) and the interpreter name: `python3` on Unix, usually `python` on
Windows.

## Setup
1. Install the dependencies (a virtual environment is recommended):
   ```bash
   python -m venv .venv
   # activate it first:  Windows: .venv\Scripts\activate   |   Unix: source .venv/bin/activate
   pip install -r requirements.txt
   ```
   The requirements are just `curl_cffi` and `beautifulsoup4`:
   - `curl_cffi` is required (not plain `requests`): the portal sits behind
     Akamai bot-detection that fingerprints TLS/HTTP2. A browser-like
     fingerprint (`impersonate="chrome"`) is what lets the OTP login actually
     establish a real session — a plain `requests` session is silently
     rejected after the OTP is accepted.
2. Create your private config from the sample (it is gitignored and never
   committed), then edit it if your details differ:
   ```bash
   cp config.sample.json config.json
   ```
   (Windows: `Copy-Item config.sample.json config.json`)
   ```json
   {
     "email": "you@example.com",
     "password": "your-password",
     "asset_booking_id": "370",
     "unit": "09 01",
     "attendees": 4,
     "preferred_slots": {
       "Sunday": ["20:00", "19:00", "21:00"],
       "Tuesday": ["20:00", "21:00"]
     },
     "min_start_hour": "18:00",
     "description": "Padel booking",
     "keepalive_minutes": 20,
     "booking_open_hour": 0,
     "slot_retry_minutes": 2,
     "booking_max_wait_minutes": 360
   }
   ```
   `asset_booking_id` is the number in the booking URL
   (`/asset/assetbooking/370`).

   Bot settings (used by `autobook` / the Telegram bot):
   - `preferred_slots` — **per-day** slot **start times** in priority order (`HH:MM`, 24h);
     the bot books the first one that is free. **The keys are the days it auto-books**,
     so no separate `target_weekdays` is needed. A plain list is also accepted and
     applies to every day (then the days come from `target_weekdays`). `20:00` = 8–9pm, `19:00` = 7–8pm.
     You can also edit this at runtime with the bot's **/prefs** command.
   - `min_start_hour` – time cutoff (`HH:MM`, 24h, default `18:00` = 6pm):
     `slots` lists only slots from that time on, while `pick` shows all slots
     but highlights the matching ones in green. Override with `--from HH:MM`.
   - `slot_retry_minutes` – how often the bot re-reads the portal while a bookable day's slots are not open/free yet (default 2).
   - `booking_max_wait_minutes` – safety cap on that wait (default 360 = 6h). A day the portal does not offer at all is never waited for.
   - `booking_open_hour` – only used as a fallback when the booking page gives no parsable "Check in date" list (the old `today .. today+6` rule).
   - `keepalive_minutes` – how often the bot refreshes the session.
   - `description` – text stored with the booking.

## Usage
```bash
# 1) Authenticate (only needed once per session)
python3 padel_booking.py login-start          # sends the OTP to your email
python3 padel_booking.py login-finish 123456  # enter the OTP you received

# 2) Check the session is still valid
python3 padel_booking.py login-status

# 3) List available time slots for a date
python3 padel_booking.py slots 2026-09-26
#    ...or only from a given time on (e.g. evening slots from 6pm):
python3 padel_booking.py slots 2026-09-26 --from 18:00

# 4) Book a specific slot (asks for confirmation first)
python3 padel_booking.py book 2026-09-26 "18:00-19:00" "Padel with friends"

# 4b) OR browse interactively: lists every available slot across the portal's
#     bookable window (whatever its "Check in date" picker offers — usually
#     today .. today+5/6), then you pick by number. You can
#     choose several (one per day). It confirms before booking.
python3 padel_booking.py pick
#     ...or highlight the evening slots (green) from a given time on:
python3 padel_booking.py pick --from 18:00

# 5) List your existing bookings (from the portal's 'My Bookings' page),
#    each flagged as cancellable / past / already cancelled:
python3 padel_booking.py mybookings
#     ...or just the bookings for one date:
python3 padel_booking.py mybookings 2026-09-26

# 6) Cancel a booking. With no argument it lists your cancellable (future)
#    bookings and you pick one by number; it confirms before cancelling.
python3 padel_booking.py cancel
#     ...or target one directly by booking-id, details-id, or date:
python3 padel_booking.py cancel 7937374
python3 padel_booking.py cancel 2026-09-27

# 7) Interactive Telegram bot (login, book, cancel from your phone)
python3 padel_booking.py telegram               # interactive Telegram bot (login, book, cancel)
```

Dates accept `YYYY-MM-DD`, `DD/MM/YYYY` or `DD-MM-YYYY`.

## Auto-booking

Which days can be booked at all comes from the portal: the booking page's
**"Check in date" picker lists them** (the window can stop at today+5 rather
than today+6), and a date it does not list is rejected on submit even though
the slot endpoint still answers for it. The bot reads that list, so it never
offers or attempts a date the portal has not opened. While a bookable day's
slots are not open/free yet it **refreshes the portal every
`slot_retry_minutes` (default 2 min)** and stops as soon as a preferred slot
is free; a day the portal does not offer is **never waited for**.

The autobooking runs **inside the Telegram bot** — start it with
`python padel_booking.py telegram` (it autostarts). Whenever the portal starts
offering a new date (its window slid forward), it checks whether that date is
one of the days in your `preferred_slots` map; if so it polls the portal
(refreshing every `slot_retry_minutes`) and books the first free slot from
that day's `preferred_slots` list, retrying until it succeeds or
`booking_max_wait_minutes` passes (one slot per day). It only fires on days the
portal actually offers, refreshes the session every `keepalive_minutes`, and
messages you the result (✅ BOOKED / ❌ FAILED / no slot / ℹ️ not bookable
yet). Pause it with `/stopautobook`, resume with `/startautobook`.

You can change **which days** and **which slots** it books without editing
`config.json` by hand: send **`/prefs`** in Telegram and tap through the
days/slots, then **Save** — it writes the per-day `preferred_slots` map back
to `config.json` and applies it to the running bot immediately.

- `autobook [date]` – one-shot: book a preferred slot for a date right now
  (useful for catch-up or manual runs).
- `keepalive` – manually refresh the persisted session.

Run the bot in the background and keep its log:

**Unix (Linux / macOS):**
```bash
nohup python3 padel_booking.py telegram >> bot.log 2>&1 &
tail -f bot.log
```

**Windows (PowerShell)** — run it in a dedicated terminal window:
```powershell
python padel_booking.py telegram *>> bot.log
```
For a hands-off bot, schedule `pythonw padel_booking.py telegram` at logon via
Task Scheduler (no console window).

Test a one-shot booking **without** submitting a real booking:
```bash
python3 padel_booking.py autobook 2026-09-27 --dry-run
```

### Important caveats
- **The bot re-logs in through Telegram.** Login needs a human-supplied email
  OTP; when the session expires the bot asks you for the code in chat and
  completes the login automatically.
- **It is a race.** Concurrency is low, so the bot polls one request every 30s
  from the moment the window opens and keeps trying until it books (or 5h
  pass). If your top preference is taken, it falls back to the next one.
- **Slot list = only what's free.** The portal's slot endpoint returns only
  *available* slots. The court's bookable window is 9am–10pm in 1-hour steps,
  so any of your preferred slots (7–8, 8–9, 9–10pm) may or may not appear
  depending on what's already been booked.
- The bot books **at/after** the portal opened the date (within
  `booking_max_wait_minutes`), so if it was briefly down it will still catch
  up while the slot is still free.
- **Booking limit.** The portal caps you at **3 active bookings** in its
  bookable window. The bot checks this locally before attempting a booking and
  skips if the limit is reached. Cancelling a booking immediately frees a
  slot in the window (the cache is refreshed right after).

## Telegram bot

Control everything from Telegram: log in with the emailed OTP (the bot asks
you for the code in chat), check availability, book, list and cancel bookings,
and manage the background autobooking — plus **automatic re-login** when the
session expires.

- Setup: create a bot with **@BotFather**, get your chat id, add
  `telegram_bot_token` + `telegram_chat_ids` to `config.json`, then run
  `python padel_booking.py telegram`.
- Full step-by-step guide: see **[TELEGRAM.md](TELEGRAM.md)**.

## Notes
- If the session expires, re-run `login-start` + `login-finish`.
- The `book` command always asks for a `y/N` confirmation before submitting.
- The `cancel` command also asks for a `y/N` confirmation before cancelling;
  it only lists bookings whose start time is still in the future (and not
  already `Reject`) as cancellable.
- Slot labels/values shown by `slots` can be used directly with `book`.
