"""T17 helper: re-authorise Google so the token gains Calendar + Gmail read.

Run in your OWN terminal (it opens a browser and needs to be interactive):

    python ai/google_auth_reauth.py

What it does: starts `python padel_booking.py google-auth` with the credentials
already in config.json, prints the consent URL, and waits for you to approve.
The local callback listener runs on port 8010; because
https://padel-booking.destr0.com/callback currently answers 502, the browser
page will show an error — that is expected. Copy the `code=` value from the
browser's address bar and paste it into this terminal if it is not picked up
automatically.

After it succeeds, verify:
    python padel_booking.py google-test      # Calendar works again
    python padel_booking.py login-start      # logs in with no OTP typed
"""
import subprocess
import sys

if __name__ == "__main__":
    print("Starting the Google authorisation flow (Calendar + Gmail read)...\n")
    sys.exit(subprocess.call([sys.executable, "padel_booking.py", "google-auth"]))
