# Telegram bot setup (step by step)

This guide walks you through setting up a **private Telegram bot** for the padel
booking automation. Once it's done, you get:

- **Chat with your booking bot from Telegram** — check slots, book, list and
  cancel bookings with simple commands and tap-able buttons, no terminal needed.
- **OTP login from your phone** — the bot triggers the portal's OTP email and
  asks you for the 6-digit code right in the chat.
- **Background autobooking** — while the bot runs it books your preferred
  slot automatically when the window opens, and messages you with the result
  (✅ BOOKED / ❌ FAILED / no slot). Toggle it with /startautobook and
  /stopautobook.
- **Automatic re-login** — if the keep-alive detects the session has
  expired, the bot triggers a new OTP email itself, asks you for the code in
  Telegram, completes the login, verifies the session, and confirms
  "✅ Session restored".

No extra Python packages are needed — the bot reuses the `curl_cffi` dependency
the project already has.

## 1. Create your bot with BotFather

1. Open Telegram, search for **@BotFather** (the verified one with a blue tick),
   tap **Start**.
2. Send `/newbot`.
3. BotFather asks for a **display name** — anything you like, e.g. `Padel Booker`.
4. Then a **username** — it **must** end in `bot` (e.g.
   `padel_booker_alrayyana_bot`). If it's already taken, try another.
5. BotFather replies with a **token** that looks like `123456789:AAH...` —
   copy it.

> ⚠️ **The token is your bot's secret.** Anyone with it can control the bot.
> Never commit it, never share it, never paste it anywhere public.

- *(Optional, recommended)* send `/setprivacy` → pick your bot → **Disable** —
  so the bot can see all your messages in a group later. Not required for
  1-on-1 chat.

## 2. Get your Telegram chat ID

The bot only talks to chat IDs you explicitly allow, so you need your own
numeric chat ID:

- **Easiest:** message **@userinfobot** on Telegram — it replies with your
  numeric `id` (e.g. `123456789`). Copy it.
- **Alternative:** send any message to *your* new bot, then open in a browser:
  `https://api.telegram.org/bot<TOKEN>/getUpdates`
  (replace `<TOKEN>` with your BotFather token) and find
  `"chat":{"id":123456789,...}` in the JSON.

## 3. Configure the bot

Edit `config.json` in the project folder and add the two Telegram keys:

```json
{
  "telegram_bot_token": "PASTE_YOUR_TOKEN_HERE",
  "telegram_chat_ids": [YOUR_CHAT_ID]
}
```

- `telegram_bot_token` — the token from @BotFather (step 1).
- `telegram_chat_ids` — an array of numeric chat IDs allowed to use the bot
  (step 2). Add more IDs to the array to allow more people.

`config.json` is **gitignored** — your token never reaches the repo.
(`config.sample.json` already contains placeholder values for both keys.)

## 4. Run the bot

First make sure you can log in once — the bot needs a valid portal session for
the booking commands:

```bash
python padel_booking.py login-start          # sends the OTP to your email
python padel_booking.py login-finish 123456  # enter the code you received
```

…or just use `/login` inside the bot (step 5) — the bot does exactly this and
asks you for the code in the chat.

Now start the bot:

```bash
python padel_booking.py telegram
```

You should see `[telegram] bot @your_bot ready; allowed chats: [...]` in the
terminal. Open your bot in Telegram and send `/start`.

> Note: only the chat ID(s) in `telegram_chat_ids` are allowed; everyone else
> gets "⛔ This bot is private."

## 5. Command reference

| Command | What it does |
|---|---|
| `/start` | Show the command list (same as `/help`) |
| `/help` | Show the command list |
| `/login` | Start a fresh OTP login — the bot emails the OTP, you reply with the code in chat |
| `/status` | Check whether the saved portal session is still valid |
| `/slots [date]` | List free slots for a date (or tap a date button) |
| `/book` | Pick a date, then a slot, to book (with confirm button) |
| `/mybookings` | List your portal bookings, with cancel buttons |
| `/autobook [date]` | Book your best preferred slot for a date (default: today + 6) |
| `/startautobook` | Start the background autobooking |
| `/stopautobook` | Stop the background autobooking |
| `/prefs` | Set which days to book and each day's preferred slots (interactive) |

Dates accept `YYYY-MM-DD`, `DD/MM/YYYY` or `DD-MM-YYYY`.

**The `/book` flow, step by step:**

1. Send `/book` — the bot replies with one button per day (today through
   today + 6). **Tap a date.**
2. The bot lists the free slots for that day as buttons. **Tap a slot.**
3. The bot asks "Book 20:00-21:00 on Fri 26 Sep?" — tap **✅ Yes, book**
   (or **❌ No** to abort).
4. The bot submits, verifies the booking against "My Bookings", and replies
   **✅ BOOKED …** or **❌ Booking FAILED …**.

**The `/login` OTP flow, step by step:**

1. Send `/login` — the bot submits your credentials to the portal and replies
   "📧 OTP sent to your email."
2. Check your email (and the spam folder), copy the 6-digit code.
3. **Reply with the code in the chat** — that's it. The bot verifies it, saves
   the session, and replies "✅ Login successful — session saved."

**The `/prefs` flow, step by step (set days & preferred slots):**

1. Send `/prefs` — the bot lists every weekday with its current preferred
   slots (top = tried first) and shows a button for each day, plus
   **💾 Save** and **❌ Cancel**.
2. **Tap a day** to open its editor. There you can:
   - tap a slot to **remove** it,
   - tap **➕ Add slot** and type a start time (`HH:MM`, e.g. `20:00`) to add one,
   - tap **🔄 Disable/Enable** to turn the day off/on (enabling starts it at `20:00`).
3. Tap **⬅ Back to days** to keep editing other days, or **💾 Save** to write
   the result to `config.json` (as the per-day `preferred_slots` map) and apply
   it to the running autobooking immediately — no restart needed. **❌ Cancel**
   discards everything.

> The days you enable are exactly the days the background autobooking will try
> to book, and the per-day list is the priority order it attempts.

## 6. Background autobooking + automatic re-login

The bot **autostarts** the background autobooking when you run it, so just:

```bash
python padel_booking.py telegram
```

While it runs, the bot:

- **Books your preferred slot automatically** — every day at midnight it checks
  whether the newly opened date (`today + 6`) is one of the days in your `preferred_slots` map,
  and if so books that day's first free `preferred_slots` entry (one request
  every 30s, up to 5h).
- **Alerts you when the session expires** (keep-alive failure) — so you know
  before a booking attempt fails.
- **Re-logs in automatically**: it requests a new OTP email itself, asks you
  for the 6-digit code in Telegram, completes the login, verifies the session,
  and confirms **"✅ Session restored"** — no terminal needed, you can be on
  your phone.
- **Notifies you of every booking result** — ✅ BOOKED, or ❌ FAILED with the
  portal's error.

Pause the autobooking with **`/stopautobook`** and resume it with
**`/startautobook`** (e.g. while you're re-booking manually).

> **IMPORTANT: only ONE process may poll updates per bot token.** Run a single
> `python padel_booking.py telegram` instance. A second one exits with a
> **409 Conflict** error ("another instance of this bot is already polling").

## 7. Run it in the background

**Windows:**

- Quick: `pythonw padel_booking.py telegram` — runs with no console window.
- Better: a **Task Scheduler** task that starts at logon:
  - Action: `pythonw` (full path, e.g. `C:\Python312\pythonw.exe`)
  - Argument: `padel_booking.py telegram`
  - **Start in:** the project folder (e.g. `D:\Repos\padel_booking_alrayyana`)
    — keep this, otherwise the script can't find `config.json`.

**Linux / macOS:**

```bash
nohup python padel_booking.py telegram >> bot.log 2>&1 &
tail -f bot.log
```

…or a **systemd user service** (survives logout, auto-restarts). Create
`~/.config/systemd/user/padel-bot.service`:

```ini
[Unit]
Description=Padel booking bot
After=network-online.target

[Service]
WorkingDirectory=/path/to/padel_booking_alrayyana
ExecStart=/path/to/venv/bin/python padel_booking.py telegram
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now padel-bot
journalctl --user -u padel-bot -f     # follow the log
```

## 8. Security notes

- **The bot token is a secret** — treat it like a password. Anyone with it can
  read and send messages as your bot.
- **The chat-ID whitelist is the authorization**: only the IDs listed in
  `telegram_chat_ids` can use the bot; everyone else gets
  "⛔ This bot is private."
- **Secrets stay out of git**: `config.json`, `session.json` and
  `telegram_state.json` are all gitignored — verify with `git status` that
  none of them show up.
- **To revoke access**: talk to **@BotFather** → `/revoke` → pick your bot →
  copy the new token into `config.json`. The old token stops working
  immediately.

## 9. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `Telegram error 401: Unauthorized` | Wrong bot token — copy it again from @BotFather and update `config.json`. |
| `403` / "chat not found" when the bot sends a message | Wrong chat ID in `telegram_chat_ids`, or you haven't pressed **Start** on the bot yet — open the bot, tap Start, and re-check the ID with @userinfobot. |
| `409 Conflict` | Two processes are polling the same token (two bot instances). Stop the other one — only one `telegram` instance may run at a time. |
| Bot doesn't answer at all | Is it running? Check the terminal output for errors; make sure your chat ID is in `telegram_chat_ids`; make sure you actually sent a command (e.g. `/start`). |
| OTP not arriving | Check your spam folder; codes expire quickly — send `/login` again and use the newest code. |
| "⛔ This bot is private." | Your chat ID isn't in the whitelist — add it to `telegram_chat_ids` and restart the bot. |
