# Padel booking automation — Docker image
#
# Design note: the app resolves ALL data files (config.json, session.json,
# booked.json, ...) relative to the SCRIPT's directory:
#
#     HERE = Path(__file__).resolve().parent
#     CONFIG_FILE = HERE / "config.json"
#
# So the code and the data must live in the same directory. We keep the
# canonical code in /opt/padel (read-only image layer) and mount the host's
# ./data directory at /app; the entrypoint syncs the .py files into /app on
# every start, then runs from there. All JSON state persists in the volume.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PUID=1000 \
    PGID=1000

# curl_cffi needs a real OpenSSL/curl stack; the portal sits behind Akamai
# bot-detection that fingerprints TLS/HTTP2, so the browser-like fingerprint
# (impersonate="chrome") is what makes the OTP login work.
# setpriv (util-linux) lets the entrypoint drop from root to the PUID:PGID
# user after it has fixed ownership of the bind-mounted /app volume.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcurl4-openssl-dev \
    libssl3 \
    ca-certificates \
    util-linux \
    && rm -rf /var/lib/apt/lists/*

# The app runs as PUID:PGID (default 1000:1000 — set these to your host
# user's UID/GID via `id` so the data files are owned by you). The container
# starts as root only long enough for the entrypoint to chown the
# bind-mounted /app volume, then drops to PUID:PGID via setpriv. The app
# itself runs as that non-root user, not root.

WORKDIR /app

# Install Python dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ============================
# Production stage
# ============================
FROM base AS production

# Canonical code in /opt/padel; /app is the data directory (mounted volume)
COPY entrypoint.sh /opt/padel/entrypoint.sh
COPY padel_booking.py padel_telegram.py /opt/padel/
RUN chmod +x /opt/padel/entrypoint.sh \
    && mkdir -p /app \
    && chown -R 1000:1000 /app

# The app runs as the non-root PUID:PGID (default 1000:1000). The entrypoint
# starts as root only long enough to sync the code into /app (the volume) and
# chown it to PUID:PGID, then drops privileges via setpriv to run the CLI.
# Default: start the Telegram bot (autobooking included).
# Override with e.g.:  docker run hecbr/padel-booking-alrayyana slots --date 2026-10-01
ENTRYPOINT ["/opt/padel/entrypoint.sh"]
CMD ["telegram"]
