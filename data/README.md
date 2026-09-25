# Data directory (Docker volume)

This directory is the **persistent data volume** for the Docker container.
The app reads and writes all of its state here (the entrypoint syncs the
code into this directory on every start).

## Files

| File | Purpose | Created by |
|------|---------|------------|
| `config.json` | Credentials + booking preferences + Telegram keys | **You** (copy from `config.sample.json`) |
| `session.json` | Persisted login session (cookies) | The app |
| `booked.json` | Booking memory (prevents double-booking) | The app |
| `telegram_state.json` | Bot state (autobook status, …) | The app |
| `pending_login.json` | Temporary state between login-start / login-finish | The app |

## Setup

```bash
mkdir -p data
cp config.sample.json data/config.json
# edit data/config.json, then:
docker compose up -d
```

## Backup

```bash
tar -czf padel-backup.tar.gz data/
```
