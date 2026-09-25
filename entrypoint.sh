#!/bin/sh
# Entrypoint for the padel-booking-alrayyana image.
#
# The app resolves all data files (config.json, session.json, booked.json,
# telegram_state.json, pending_login.json) relative to the script's own
# directory (Path(__file__).parent). The canonical code ships in /opt/padel
# (read-only image layer) while the persistent data lives in /app (the
# mounted volume). We therefore sync the code into /app on every start and
# run from there, so all state ends up in the volume.
#
# /app is a bind-mounted host directory, so its ownership comes from the host,
# not the image. We therefore start as root, fix ownership of the volume to
# PUID:PGID, then drop to that (non-root) user via setpriv to run the app.
# Set PUID/PGID to your host user's UID/GID (see `id`) so the data files are
# owned by you and you can access them directly. The app runs as PUID:PGID,
# not root.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

cp -f /opt/padel/padel_booking.py /opt/padel/padel_telegram.py /app/
chown -R "$PUID:$PGID" /app

cd /app
exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups python padel_booking.py "$@"
