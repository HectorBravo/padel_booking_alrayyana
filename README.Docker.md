# Docker Setup for Padel Booking Bot

Run the Padel Booking Telegram bot (with autobooking) as a Docker container.

## How it works

- The **image** (`hecbr/padel-booking-alrayyana`) is built and published to
  **Docker Hub** automatically by the GitHub Actions workflow
  (`.github/workflows/docker-publish.yml`) on every push to `main`.
- The app resolves **all data files** (`config.json`, `session.json`,
  `booked.json`, `telegram_state.json`, `pending_login.json`) relative to the
  script's own directory. The image therefore keeps the code in read-only
  `/opt/padel/` and mounts your host's `./data` directory at `/app`; the
  entrypoint syncs the `.py` files into `/app` on every start, so **all state
  persists in `./data`** and code updates come from the image.

## Quick start

### 1. Prepare your config

```bash
mkdir -p data
cp config.sample.json data/config.json
# edit data/config.json: email, password, telegram_bot_token,
# telegram_chat_ids, preferred_slots, ...
```

### 2. Run the bot

```bash
docker compose up -d          # pulls hecbr/padel-booking-alrayyana:latest
docker compose logs -f        # follow the bot logs
docker compose down           # stop
```

### 3. One-off commands (no bot)

```bash
# Check slot availability
docker run --rm -v "$PWD/data:/app" hecbr/padel-booking-alrayyana slots --date 2026-10-01

# Test a booking without submitting
docker run --rm -v "$PWD/data:/app" hecbr/padel-booking-alrayyana autobook 2026-10-01 --dry-run

# List current bookings
docker run --rm -v "$PWD/data:/app" hecbr/padel-booking-alrayyana mybookings

# Manual login (OTP flow)
docker run --rm -v "$PWD/data:/app" hecbr/padel-booking-alrayyana login-start
docker run --rm -v "$PWD/data:/app" hecbr/padel-booking-alrayyana login-finish <OTP>
```

## Data directory

```
./data/
├── config.json          # your credentials + preferences (you create this)
├── session.json         # persisted login session (auto-created)
├── booked.json          # booking memory (auto-created)
├── telegram_state.json  # bot state (auto-created)
└── pending_login.json   # OTP login state (auto-created, temporary)
```

Back it up regularly: `tar -czf padel-backup.tar.gz data/`

## Publishing the image (automatic)

The workflow builds and pushes `hecbr/padel-booking-alrayyana` (tags:
`latest` + git SHA) on every push to `main`.

One-time setup:

1. Create the Docker Hub repository `padel-booking-alrayyana` under the
   `hecbr` account (https://hub.docker.com/ → Create Repository).
2. Generate an access token: https://hub.docker.com/settings/security →
   New Access Token.
3. Add the secret to the GitHub repo:
   **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `DOCKERHUB_TOKEN`
   - Value: the access token

Then just push — or trigger manually from the **Actions** tab
(*Build and Push Docker Image* → *Run workflow*).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ERROR: config file not found at /app/config.json` | Create `./data/config.json` (step 1 above) |
| Bot asks for OTP in Telegram | Session expired — send the emailed code in chat |
| `409 Conflict` | Two bot instances polling the same token — stop the other one |
| Stale code after image update | `docker compose pull && docker compose up -d` (entrypoint re-syncs code on start) |

## Security notes

- Container runs as non-root user `appuser`
- Secrets (`config.json`, `session.json`, …) are **never** baked into the
  image (see `.dockerignore`) and never committed (see `.gitignore`)
- The Docker Hub token lives only in GitHub Actions secrets
