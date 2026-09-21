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
  attendees, date, chosen slot and the T&C acceptance.
- **My bookings / cancel** read the portal's `My Bookings` page
  (`/booking/myBooking`). Each row links to a booking-details id
  (`/booking/bookingDetails/<id>`); cancelling is a plain POST to
  `/booking/cancelAmenityBooking` with that id (the on-page confirm popup is
  client-side only). A booking can be cancelled while its start time is still
  in the future; the portal then marks it `Reject`.

## Files
| File               | Purpose                                              | Protected |
|--------------------|------------------------------------------------------|-----------|
| `padel_booking.py` | The automation script                                | —         |
| `config.json`      | Credentials + booking defaults (email, password, …)  | `600`     |
| `session.json`     | Persisted login session (cookies)                    | `600`     |
| `pending_login.json`| Temporary state between login-start and login-finish | `600`     |
| `padel_telegram.py` | Telegram bot API client + interactive bot (login/OTP, book, cancel) + daemon notifications | —         |
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

The only OS differences are how you keep the `daemon` running in the background
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
     "target_weekdays": ["Sunday", "Tuesday"],
     "preferred_slots": ["20:00", "21:00", "19:00"],
     "min_start_hour": "18:00",
     "description": "Padel booking",
     "keepalive_minutes": 20,
     "booking_open_hour": 0
   }
   ```
   `asset_booking_id` is the number in the booking URL
   (`/asset/assetbooking/370`).

   Bot settings (used by `autobook` / `daemon`):
   - `target_weekdays` – which days of week to auto-book (e.g. `Sunday`, `Tuesday`).
   - `preferred_slots` – slot **start times** in priority order (`HH:MM`, 24h);
     the bot books the first one that is free. `20:00` = 8–9pm, `19:00` = 7–8pm.
   - `min_start_hour` – time cutoff (`HH:MM`, 24h, default `18:00` = 6pm):
     `slots` lists only slots from that time on, while `pick` shows all slots
     but highlights the matching ones in green. Override with `--from HH:MM`.
   - `booking_open_hour` – the hour (24h) the portal opens a new day (0 = midnight).
   - `keepalive_minutes` – how often the daemon refreshes the session.
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

# 4b) OR browse interactively: lists every available slot from today through
#     the last bookable day (today + 6), then you pick by number. You can
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

## Auto-booking bot

The portal opens each day's bookings at **midnight (00:00) for the date 6 days
ahead** (e.g. Sun 27 Sep opens Mon 21 Sep at midnight). Concurrency is low, so
the bot simply polls one request every 30s from the moment the window opens
until it books your slot (or 5h pass).

- `daemon` – resident process. Every day at midnight it checks whether the newly
  opened date (`today + 6 days`) is one of your `target_weekdays`. If so, it
  polls the slot endpoint (one request every 30s) and books the first free
  `preferred_slots` entry, retrying until it succeeds or 5h pass (one slot per
  day). It only fires on the day the target slot's window opens. It also
  refreshes the session every `keepalive_minutes` to keep you logged in.
- `autobook [date]` – one-shot: book a preferred slot for a date right now
  (useful for catch-up or manual runs).
- `keepalive` – manually refresh the persisted session.

Run the daemon in the background and keep its log:

**Unix (Linux / macOS):**
```bash
nohup python3 padel_booking.py daemon >> daemon.log 2>&1 &
tail -f daemon.log
```

**Windows (PowerShell)** — run it in a dedicated terminal window:
```powershell
python padel_booking.py daemon *>> daemon.log
```
For a hands-off bot, schedule `pythonw padel_booking.py daemon` at logon via
Task Scheduler (no console window).

Test the whole flow **without** submitting any real booking:
```bash
python3 padel_booking.py autobook 2026-09-27 --dry-run
python3 padel_booking.py daemon --dry-run
```

### Important caveats
- **The bot cannot re-login by itself.** Login needs a human-supplied email OTP.
  The keep-alive is meant to prevent the session from expiring, but if you see a
  `session expired` warning in the log, run `login-start` + `login-finish`
  before the next midnight.
- **It is a race.** Concurrency is low, so the bot polls one request every 30s
  from the moment the window opens and keeps trying until it books (or 5h
  pass). If your top preference is taken, it falls back to the next one.
- **Slot list = only what's free.** The portal's slot endpoint returns only
  *available* slots. The court's bookable window is 9am–10pm in 1-hour steps,
  so any of your preferred slots (7–8, 8–9, 9–10pm) may or may not appear
  depending on what's already been booked.
- The daemon books **at/after** midnight (within the 5h race window), so if it
  was briefly down it will still catch up the same night (while the slot is
  still free).

## Telegram bot

Control everything from Telegram: log in with the emailed OTP (the bot asks
you for the code in chat), check availability, book, list and cancel bookings —
plus daemon alerts and **automatic re-login** when the session expires.

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
