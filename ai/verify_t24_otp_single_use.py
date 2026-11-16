"""T24 checks: a correctly captured OTP must not be consumed twice, and a login
that never reached a real OTP form must not leave an finishable-looking stub.

Run with:  python ai/verify_t24_otp_single_use.py

Root cause this pins down: `login_start()` only tested for "checkOtp" in the
final URL. When the browser session is already authenticated, Akamai can keep
the redirect chain on /login/checkOtp while serving the landing page, so parsing
yields every hidden field EMPTY — and that stub gets written to
pending_login.json (exactly what was found in production: cookies [] plus all
otp_fields ""). Any later `login_finish` then answers "OTP invalid" for whatever
code is supplied, which reads to the user as "it can't login with a correctly
captured OTP".
"""
import sys
from unittest.mock import patch

sys.path.insert(0, ".")
import padel_booking as pb  # noqa: E402


class Cookie:
    def __init__(self, name, value, domain, path):
        self.name, self.value, self.domain, self.path = name, value, domain, path


class FakeCookies:
    def __init__(self):
        self.jar = []

    def set(self, name, value, domain=None, path=None):
        self.jar.append(Cookie(name, value, domain or "", path or ""))


class _Resp:
    status_code = 200
    text = "<html></html>"


class Portal:
    """Models the server: one pending login per browser session, single-use OTP."""

    def __init__(self):
        self.pending_sessions = set()   # labels waiting for an OTP
        self.used_codes = set()         # codes already redeemed
        self.verify_calls = []          # (label, otp) audit trail
        self.authenticated = set()      # labels the portal treats as logged in
        # Per-session URL left after checkLogin; lets a test keep the browser on
        # /login/checkOtp even though no OTP form was served.
        self.checklogin_url = {}


class FakeSession:
    def __init__(self, portal, label):
        self.portal = portal
        self.label = label
        self.cookies = FakeCookies()

    def get(self, url, **kw):
        r = _Resp()
        r.url = url
        return r

    def post(self, url, data=None, **kw):
        r = _Resp()
        if url.endswith("/login/checkLogin"):
            if self.label in self.portal.authenticated:
                # Already-authenticated session: no OTP form in the body.
                r.url = self.portal.checklogin_url.get(
                    self.label, pb.BASE_URL + "/dashboard/index")
                r.text = "<html><body>dashboard</body></html>"
                return r
            self.portal.pending_sessions.add(self.label)
            self.cookies.set("ai", "AKAI-" + self.label, ".asteco.com", "/")
            r.url = pb.BASE_URL + "/login/checkOtp"
            r.text = ('<input name="id_user" value="7">'
                      '<input name="email" value="u@example.com">')
            return r
        if url.endswith("/login/verifyOtp"):
            otp = (data or {}).get("user_otp")
            self.portal.verify_calls.append((self.label, otp))
            if self.label not in self.portal.pending_sessions:
                r.text = "3"                       # no pending login here
            elif otp in self.portal.used_codes:
                r.text = "3"                       # single-use: already redeemed
            else:
                self.portal.used_codes.add(otp)
                self.portal.pending_sessions.discard(self.label)
                r.text = "/dashboard/index"
            return r
        r.url = url
        return r


CFG = {"email": "u@example.com", "password": "pw",
       # Google configured, as in production: this is what makes login_start's
       # internal _auto_login actually capture and finish.
       "google_calendar": {"enabled": True, "refresh_token": "rt",
                           "client_id": "cid", "client_secret": "cs"}}


class PendingFile:
    """In-memory stand-in for pending_login.json."""

    def __init__(self):
        self.data = None

    def exists(self):
        return self.data is not None

    def read_text(self, encoding=None):
        import json as _j
        return _j.dumps(self.data)

    def write_text(self, text, encoding=None):
        import json as _j
        self.data = _j.loads(text)

    def unlink(self, missing_ok=False):
        self.data = None

    def chmod(self, *a, **k):
        return None


def _patch_common(pf, sess, capture=None):
    """Patch the portal + persistence surface login_start/login_finish use."""
    return [
        patch.object(pb, "new_session", lambda restore=True: sess),
        patch.object(pb, "_timed", lambda m, u, fn: fn()),
        patch.object(pb, "PENDING_FILE", pf),
        patch.object(pb, "is_logged_in", lambda s, cfg: True),
        patch.object(pb, "save_session", lambda s: None),
        patch.object(pb, "install_session", lambda s: None),
        patch.object(pb, "_STATE", {"meta": None, "bookings": []}),
    ] + ([patch.object(pb, "capture_otp", capture)] if capture else [])


def _enter(patches):
    """Enter a list of patches and return their unwrapped targets."""
    from contextlib import ExitStack
    stack = ExitStack()
    for p in patches:
        stack.enter_context(p)
    return stack


def check_no_stub_when_already_authed(log, keep_checkotp):
    """An already-authenticated login_start must not leave a pending stub, and
    must report the login as complete."""
    tag = "1a" if keep_checkotp else "1b"
    portal, pf = Portal(), PendingFile()
    portal.authenticated.add("AUTHED")
    if keep_checkotp:
        # Akamai keeps the browser on /login/checkOtp although the body is the
        # landing page — the exact production shape.
        portal.checklogin_url["AUTHED"] = pb.BASE_URL + "/login/checkOtp"
    sess = FakeSession(portal, "AUTHED")

    stack = _enter(_patch_common(pf, sess))
    try:
        completed = pb.login_start(CFG)
    finally:
        stack.close()

    stub_left = pf.data is not None
    if stub_left:
        empty = all(v == "" for v in pf.data.get("otp_fields", {}).values())
        log(f"  [{tag}] stub left: cookies={pf.data.get('cookies')} "
            f"all_otp_fields_empty={empty}")
    ok = (not stub_left) and completed is True
    log(f"[{'PASS' if ok else 'FAIL'}] check{tag}: already-authenticated login "
        f"start -> no pending stub, returns True (stub_left={stub_left}, "
        f"returned={completed!r})")
    return ok


def check_single_use_otp(log):
    """login_start's internal Gmail capture finishes the login; a second
    verification of the same code must be refused and must not be attempted."""
    portal, pf = Portal(), PendingFile()
    sess = FakeSession(portal, "START")

    stack = _enter(_patch_common(pf, sess, capture=lambda cfg, log=None, since=None: ("315813", "")))
    try:
        completed = pb.login_start(CFG)
        # A caller that ignores the return value and tries to finish again:
        second_err = None
        try:
            pb.login_finish(CFG, "315813")
        except Exception as e:  # noqa: BLE001
            second_err = str(e)
    finally:
        stack.close()

    calls = portal.verify_calls
    ok = completed is True and len(calls) == 1 and second_err is not None
    log(f"[{'PASS' if ok else 'FAIL'}] check2: OTP verified exactly once; "
        f"re-verify refused (calls={calls}, returned={completed!r}, "
        f"second_error={second_err!r})")
    return ok


def check_stale_stub_message(log):
    """A pending stub with empty otp_fields must fail with a clear cause, not
    the misleading 'OTP invalid'."""
    portal, pf = Portal(), PendingFile()
    pf.data = {"cookies": [], "otp_fields": {k: "" for k in (
        "id_user", "phone", "email", "first_name", "last_name", "password",
        "username", "name_usr")}, "created_at": "2026-11-16T08:55:02"}
    sess = FakeSession(portal, "S1")

    stack = _enter(_patch_common(pf, sess))
    try:
        err = None
        try:
            pb.login_finish(CFG, "315813")
        except Exception as e:  # noqa: BLE001
            err = str(e)
    finally:
        stack.close()

    ok = err is not None and "invalid" not in err.lower()
    log(f"[{'PASS' if ok else 'FAIL'}] check3: stale stub gives a clear cause, "
        f"not 'OTP invalid' (error={err!r})")
    return ok


def main():
    def log(msg):
        print(msg)

    results = [
        check_no_stub_when_already_authed(log, keep_checkotp=True),
        check_no_stub_when_already_authed(log, keep_checkotp=False),
        check_single_use_otp(log),
        check_stale_stub_message(log),
    ]
    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
