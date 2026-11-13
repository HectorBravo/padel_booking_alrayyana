"""Exchange the authorization code from the consent flow for fresh tokens.

Replicates `google_calendar.run_google_auth`'s exchange step (same client, same
redirect URI) but without the browser part, so a code pasted back into the
terminal can finish the flow. Saves config.json exactly like google-auth does.
"""
import json
import sys

from google_calendar import exchange_code_for_tokens, _save_config  # noqa

CODE = "<PASTE_CODE_HERE>"
REDIRECT_URI = "https://padel-booking.destr0.com/callback"

if __name__ == "__main__":
    cfg = json.load(open("config.json", encoding="utf-8-sig"))
    gc = cfg["google_calendar"]
    tokens = exchange_code_for_tokens(
        CODE, gc["client_id"], gc["client_secret"], REDIRECT_URI)
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        print("No refresh_token in the response — consent may have been "
              "cached. Revoke at https://myaccount.google.com/permissions "
              "and authorise again.")
        sys.exit(1)
    gc["refresh_token"] = refresh_token
    gc["enabled"] = True
    _save_config(cfg)
    scope = tokens.get("scope", "") or ""
    print("Tokens saved. Granted scopes:")
    for s in scope.split():
        print(" ", s)
