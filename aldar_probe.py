#!/usr/bin/env python3
"""Probe the new Aldar Live portal (the Asteco portal was migrated).

Purpose (T27): prove, end to end, that we can
  1. authenticate against ForgeRock AM (`frald.aldar.com/am`, realm `alpha`,
     journey "LIVE ALDAR WEB USER EMAIL LOGIN") with the email/password from
     config.json,
  2. capture the emailed 4-digit OTP (from no-reply@aldar.com) via Gmail,
  3. finish the callback chain and keep whatever session/token works,
  4. call the read-only communityservice booking APIs (categories -> classes
     -> schedules/packages -> available slots) and dump their shapes.

It NEVER calls `classes/bookings` (the create endpoint): this script is a
read-only probe.

Usage:
  python aldar_probe.py auth            # steps 1-3, saves aldar_session.json
  python aldar_probe.py api             # step 4 using the saved session
  python aldar_probe.py all             # both
"""

import json
import re
import sys
import time
from pathlib import Path

from curl_cffi import requests as creq

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.json"
PENDING_FILE = HERE / "aldar_pending.json"
SESSION_FILE = HERE / "aldar_session.json"

# --- ForgeRock AM (extracted from the live SPA bundle, verified 17-11-2026) --
FR_BASE = "https://frald.aldar.com/am"
AUTH_URL = (f"{FR_BASE}/json/realms/root/realms/alpha/authenticate"
            "?authIndexType=service"
            "&authIndexValue=LIVE%20ALDAR%20WEB%20USER%20EMAIL%20LOGIN")

# --- Community service (booking APIs; 403 = exists+auth, 404 = wrong verb) ---
API_BASE = "https://communityservice.live.aldar.com/api/v1/"

UA_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/131.0.0.0 Safari/537.36"}

CFG_EM = ""
CFG_PW = ""


def log(msg: str) -> None:
    print(msg, flush=True)


def load_cfg() -> dict:
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 1+3. ForgeRock callback chain
# --------------------------------------------------------------------------

def fr_headers() -> dict:
    h = dict(UA_HEADERS)
    h.update({
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://www.aldar.com",
        "Referer": "https://www.aldar.com/",
        # Verified in chunk 1006 (FRAuth Next SDK, module 41006):
        #   headers: Accept, Accept-API-Version protocol=1.0,resource=2.1,
        #            Content-Type, X-Requested-With: forgerock-sdk
        #   credentials: "include"
        # The `flow=` query param is NOT sent by the SPA (only authIndexType /
        # authIndexValue), and resource=2.0 makes AM replay the tree from the
        # beginning — which is why every round came back as Name/Password.
        "Accept-API-Version": "protocol=1.0,resource=2.1",
        "X-Requested-With": "forgerock-sdk",
    })
    return h


def fr_post(s, payload: dict):
    r = s.post(AUTH_URL, headers=fr_headers(),
               data=json.dumps(payload), timeout=60)
    try:
        body = r.json()
    except ValueError:
        body = {"_raw": r.text[:400]}
    return r.status_code, body


def describe_callbacks(body: dict) -> str:
    out = []
    for cb in body.get("callbacks", []):
        ins = [i.get("name") for i in cb.get("input", [])]
        outs = [(o.get("name"), o.get("value")) for o in cb.get("output", [])]
        out.append(f"{cb.get('type')} in={ins} out={outs}")
    return " | ".join(out)


def merge_callbacks(base: list, latest: list) -> list:
    """Merge the latest callback shape onto the base payload.

    DEPRECATED — kept only for reference. The SPA does NOT merge: it keeps the
    response envelope from `start()` and re-submits *that exact* object on
    every `next()` (see otp.js: `let e=JSON.parse(E); e.payload.callbacks[0]
    .input[0].value=z; d.Z.next(e)`). Merging made step 2 send the already
    consumed NameCallback + PasswordCallback(password) again, which AM answers
    with 401 "Login failure". Use `update_in_place` instead.
    """
    merged = json.loads(json.dumps(base))
    for cb in latest:
        t = cb.get("type")
        if t == "TextOutputCallback":
            continue  # display-only; never answered back
        match = next((m for m in merged if m.get("type") == t), None)
        if match is not None:
            # Refresh only the input slot names/emptiness (e.g. IDToken1 ->
            # IDToken2 when the same PasswordCallback node re-asks for OTP).
            match["input"] = [
                {"name": i.get("name"), "value": ""}
                for i in cb.get("input", [])]
        else:
            merged.append({"type": t,
                           "output": json.loads(
                               json.dumps(cb.get("output", []))),
                           "input": [{"name": i.get("name"), "value": ""}
                                     for i in cb.get("input", [])]})
    return merged


def answer_callbacks(body: dict, otp):
    """Fill `input[].value` of the callbacks AM is currently asking for.

    Works on the *live* callback list of the current response envelope and
    only touches input values — exactly like the SPA
    (`payload.callbacks[0].input[0].value = code`). The envelope itself is
    re-submitted unchanged, which is what AM expects; merging or rebuilding
    callbacks from an earlier round makes AM answer 401 "Login failure".

    Returns (needs_otp, filled) where `filled` lists the input slot names that
    received a value this round — used to prove we are not resubmitting stale
    answers.
    """
    needs_otp = False
    filled = []
    callbacks = body.get("callbacks", [])
    for cb in callbacks:
        t = cb.get("type")
        if t == "TextOutputCallback":
            log(f"  [chain] TextOutputCallback: "
                f"{json.dumps(cb.get('output'))[:300]}")
            continue
        if t in ("NameCallback", "TextInputCallback"):
            label = ""
            for o in cb.get("output", []):
                if o.get("name") == "prompt":
                    label = str(o.get("value", "")).lower()
            want_pw = "password" in label or any(
                i.get("name") == "IDToken2" for i in cb.get("input", []))
            value = CFG_PW if want_pw else CFG_EM
            # ForgeRock expects the answer in input[].value (NOT output).
            for i in cb.get("input", []):
                i["value"] = value
                filled.append(i.get("name"))
        elif t in ("PasswordCallback", "PasswordConfirmCallback"):
            label = ""
            for o in cb.get("output", []):
                if o.get("name") == "prompt":
                    label = str(o.get("value", "")).lower()
            if "otp" in label:
                # The OTP arrives as a PasswordCallback prompted "OTP".
                needs_otp = True
                if otp is not None:
                    for i in cb.get("input", []):
                        # AM compares the code as a STRING ("1691"), never an
                        # int — sending an int makes it fail with
                        # "OTP is invalid." even when the code is correct.
                        i["value"] = str(otp)
                        filled.append(i.get("name"))
            else:
                for i in cb.get("input", []):
                    i["value"] = CFG_PW
                    filled.append(i.get("name"))
        elif t == "ConfirmationCallback":
            # Reached when AM rejected the code ("OTP is invalid." /
            # "OTP Limit Exceeded") and asks Yes/No. The SPA re-runs `next()`
            # on that response, which sends optionIndex back untouched (its
            # defaultOption) — so mirror that instead of guessing.
            log(f"  [chain] ConfirmationCallback: "
                f"{json.dumps(cb.get('output'))[:300]}")
            for i in cb.get("input", []):
                if i.get("name") == "optionIndex":
                    filled.append(i.get("name"))
        elif t == "NumberCallback":
            needs_otp = True
            if otp is not None:
                for i in cb.get("input", []):
                    i["value"] = int(otp)
                    filled.append(i.get("name"))
        else:
            # Unknown callback that takes a value -> assume it wants the OTP.
            log(f"  [chain] unknown callback {t}: {json.dumps(cb)[:300]}")
            needs_otp = True
            if otp is not None:
                for i in cb.get("input", []):
                    i["value"] = str(otp)
                    filled.append(i.get("name"))
    return needs_otp, filled


def auth_start(cfg: dict, manual: bool) -> bool:
    """Steps 1-3. Returns True when the chain completed."""
    global CFG_EM, CFG_PW
    CFG_EM, CFG_PW = cfg["email"], cfg["password"]
    s = creq.Session(impersonate="chrome")

    # OAuth leg first (exactly what the SPA does): /authorize with PKCE lands
    # on the AM login tree; completing that tree makes /authorize answer with
    # ?code=..., which we exchange for the access token the APIs want.
    verifier = _b64url(__import__("secrets").token_bytes(32))
    challenge = _b64url(_sha256(verifier))
    # Scope MUST include UAEPASS: the booking APIs answer 403 "Forbidden
    # resource" to a token that only has openid/profile/email (proved by
    # replaying classes/packages with both tokens — see ai/AI_TASKLOG.md T27).
    authz_url = (f"{FR_BASE}/oauth2/realms/root/realms/alpha/authorize"
                 "?response_type=code&client_id=FRPROD"
                 "&scope=UAEPASS%20openid%20profile%20email"
                 "&redirect_uri=https%3A%2F%2Fwww.aldar.com%2Flive%2Fapi"
                 "%2Fauth%2Fcallback"
                 f"&code_challenge={challenge}&code_challenge_method=S256")
    r = s.get(authz_url, headers=UA_HEADERS, allow_redirects=True,
              timeout=60)
    log(f"[oauth] GET /authorize -> {r.status_code} ({r.url[:120]})")

    # Journey WITHOUT the flow= parameter (the UI-Login-embedded tree; with
    # flow= it is the SPA's direct API and rejects a plain browser session).
    tree_url = (f"{FR_BASE}/json/realms/root/realms/alpha/authenticate"
                "?authIndexType=service"
                "&authIndexValue=LIVE%20ALDAR%20WEB%20USER%20EMAIL%20LOGIN")

    def fr_post_tree(payload: dict):
        rr = s.post(tree_url, headers=fr_headers(),
                    data=json.dumps(payload), timeout=60)
        try:
            return rr.status_code, rr.json()
        except ValueError:
            return rr.status_code, {"_raw": rr.text[:400]}

    log(f"[auth] POST {tree_url}")
    status, body = fr_post_tree({})
    log(f"  -> {status}: "
        f"{describe_callbacks(body) or json.dumps(body)[:200]}")
    auth_id = body.get("authId")
    if not auth_id:
        log("[auth] no authId — cannot continue")
        return False

    # The SPA keeps the response envelope it got from `start()` and re-submits
    # *that same object* on every `next()`, only refreshing input values
    # (otp.js: `let e=JSON.parse(E); e.payload.callbacks[0].input[0].value=z;
    #  d.Z.next(e)`). Do exactly that — no merging, no rebuilding.
    envelope = {"authId": auth_id,
                "callbacks": json.loads(json.dumps(body.get("callbacks", [])))}
    otp = None
    used_otp = set()
    for step in range(1, 9):
        wants_otp_now, _ = answer_callbacks(envelope, None)
        if wants_otp_now:
            # The code is single-use: never resend one AM already rejected.
            fresh, err = capture_aldar_otp(cfg, exclude=used_otp)
            if not fresh:
                log(f"[auth] automatic OTP capture failed: {err}")
                return False
            if fresh != otp:
                log(f"[auth] captured OTP {fresh} (was {otp})")
            otp = fresh

        _, filled = answer_callbacks(envelope, otp)
        if wants_otp_now and otp:
            used_otp.add(str(otp))
        log(f"[auth] step {step}: submitting slots={filled} "
            f"{'(OTP ' + str(otp) + ')' if (wants_otp_now and otp) else ''}")
        status, body = fr_post_tree(envelope)
        # ForgeRock success has NO callbacks: {"tokenId": ..., "realm": ...}
        if body.get("tokenId") or body.get("success") is True:
            log(f"  -> {status}: AUTHENTICATED (tokenId acquired)")
            break
        if not body.get("callbacks"):
            log(f"  -> {status}: {json.dumps(body)[:400]}")
            log("[auth] chain ended without success — see the summary above")
            return False
        # Keep the whole envelope from AM (authId + new callbacks), then fill.
        envelope = {"authId": body.get("authId", envelope["authId"]),
                    "callbacks": body["callbacks"]}
        log(f"  -> {status}: {describe_callbacks(body)}")

    # Tree done in this browser session -> replay /authorize to get the code.
    tok_hdr = body.get("tokenId") or ""
    if tok_hdr:
        # ForgeRock: hand the AMSession token back so the OAuth authorize
        # endpoint recognises this session (the SPA does it with cookies,
        # which AM also sets on the authenticate response).
        s.cookies.set("amlbcookie", "01", domain=".aldar.com")
        authz_with_tok = authz_url + f"&IDToken={tok_hdr}"
        r2 = s.get(authz_with_tok, headers=UA_HEADERS,
                   allow_redirects=False, timeout=60)
    else:
        r2 = s.get(authz_url, headers=UA_HEADERS, allow_redirects=False,
                   timeout=60)
    loc = r2.headers.get("location") or ""
    log(f"[oauth] replay /authorize -> {r2.status_code} -> {loc[:160]}")
    code = None
    if "code=" in loc:
        code = re.search(r"[?&]code=([^&]+)", loc).group(1)
    if not code:
        log("[oauth] no authorization code — cannot get an access token")
        save_session(s, body)  # still keep whatever AM gave us
        return False

    tok = s.post(
        f"{FR_BASE}/oauth2/realms/root/realms/alpha/access_token",
        headers={**UA_HEADERS,
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "authorization_code", "client_id": "FRPROD",
              "redirect_uri": "https://www.aldar.com/live/api/auth/callback",
              "code": code, "code_verifier": verifier},
        timeout=60)
    log(f"[oauth] POST /access_token -> {tok.status_code}")
    try:
        tj = tok.json()
    except ValueError:
        tj = {}
    if not tj.get("access_token"):
        log("[oauth] no access_token: " + json.dumps(tj)[:300])
        save_session(s, body)
        return False
    log(f"[oauth] access_token acquired ({str(tj['access_token'])[:24]}...), "
        f"refresh_token: {'yes' if tj.get('refresh_token') else 'no'}")
    save_session(s, {"token": tj["access_token"], "oauth": tj})
    return True


def _b64url(raw: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _sha256(text: str) -> bytes:
    import hashlib
    return hashlib.sha256(text.encode("ascii")).digest()


def save_session(s, body: dict) -> None:
    data = {
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "token": body.get("token") or "",
        "cookies": [{"name": c.name, "value": c.value, "domain": c.domain,
                     "path": c.path} for c in s.cookies.jar],
    }
    SESSION_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        SESSION_FILE.chmod(0o600)
    except OSError:
        pass
    log(f"[auth] SUCCESS — session saved to {SESSION_FILE.name} "
        f"(token: {'yes' if data['token'] else 'no'}, "
        f"{len(data['cookies'])} cookies)")


# --------------------------------------------------------------------------
# 2. OTP capture (Aldar wording, via the existing Gmail plumbing)
# --------------------------------------------------------------------------

OTP_WORD_RE = re.compile(r"one[- ]time password|\bOTP\b", re.IGNORECASE)
CODE_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")


ALDAR_OTP_QUERY = "from:no-reply@aldar.com has:1691 OR newer_than:20m"


def dump_recent_aldar_mail(cfg: dict, query: str = "from:aldar.com newer_than:2h",
                           max_results: int = 5) -> None:
    """Debug helper: print recent aldar.com emails + extracted candidates."""
    import google_otp as go

    token = go._get_access_token(cfg)  # noqa: SLF001 (probe)
    data = go._gmail_get(  # noqa: SLF001 (probe)
        f"{go.GMAIL_API}/messages", token,
        {"q": query, "maxResults": max_results,
         "fields": "messages(id,internalDate)"})
    for m in data.get("messages", []):
        msg = go._get_message(token, m["id"])  # noqa: SLF001 (probe)
        text = go.message_text(msg)
        print("=" * 60)
        print("subject:", go.message_header(msg, "subject"))
        print("date ms:", m.get("internalDate"))
        flat = re.sub(r"<[^>]+>", "\n", text)
        flat = re.sub(r"\n{2,}", "\n", flat).strip()
        print(flat[:600])


def capture_aldar_otp(cfg: dict, since: float | None = None, exclude=None):
    """Poll Gmail for the newest Aldar OTP email. Returns (otp, error).

    `exclude` is a set of codes already tried — an Aldar OTP is single-use, so
    re-sending one AM already rejected just burns attempts until
    "OTP Limit Exceeded".
    """
    import google_otp as go

    if not go.google_configured(cfg):
        return "", "Google integration is not configured."
    try:
        token = go._get_access_token(cfg)  # noqa: SLF001 (probe)
    except go.GoogleOtpError as e:
        return "", str(e)

    tried = {str(c) for c in (exclude or set())}
    # Aldar's mail API omits internalDate, so the time filter is best effort;
    # Gmail returns newest-first, which is what actually orders the search.
    since_ms = int((since - 90) * 1000) if since is not None else 0
    for attempt in range(1, 13):
        try:
            data = go._gmail_get(  # noqa: SLF001 (probe)
                f"{go.GMAIL_API}/messages", token,
                {"q": 'subject:"One-Time Password" newer_than:20m',
                 "maxResults": 15, "fields": "messages(id,internalDate)"})
        except go.GoogleOtpError as e:
            return "", str(e)
        for m in data.get("messages", []):
            when = int(m.get("internalDate") or 0)
            if since_ms and when and when < since_ms:
                continue
            msg = go._get_message(token, m["id"])  # noqa: SLF001 (probe)
            subject = go.message_header(msg, "subject")
            if "live aldar" not in subject.lower():
                continue  # Amazon delivery OTPs share this subject line
            text = go.message_text(msg)
            if not OTP_WORD_RE.search(text):
                continue
            otp = pick_otp_code(subject, text)
            if otp and otp not in tried:
                return otp, ""
        log(f"  -> Gmail: no fresh Aldar OTP yet, attempt {attempt}/12")
        time.sleep(5)
    return "", "no fresh OTP email from aldar.com arrived"


def pick_otp_code(subject: str, text: str) -> str:
    """Extract the 4-digit code. Prefer an OTP-looking subject line."""
    m = re.search(r"(?<!\d)(\d{4})(?!\d)", subject or "")
    if m and re.search(r"otp|one[- ]time|verif|code|password",
                       subject, re.IGNORECASE):
        return m.group(1)
    # Body: strip HTML tags/attributes so tag numbers can never leak in,
    # then take the first standalone 4-digit token.
    plain = re.sub(r"<[^>]*>", "\n", text or "")
    plain = re.sub(r"&[a-zA-Z#0-9]+;", " ", plain)
    codes = re.findall(r"(?<![\d.,:])(\d{4})(?![\d.,:])", plain)
    codes = [c for c in codes if c != "2026"]
    return codes[0] if codes else ""


# --------------------------------------------------------------------------
# 4. requestservice API probes (read-only!)
#
# PROVEN 18-11-2026 by a real DevTools capture (user's Chrome session):
#   POST https://requestservice.live.aldar.com/api/v1/classes/getBookings
#   with Bearer <our OAuth access token> + the SPA header set returns 201 and
#   the booking list. The gate is NOT the headers — it is the *body*: the
#   endpoint answers `403 Forbidden resource` for an empty/identity-less body,
#   and needs at least {customerId, referenceId, selectedUnits[...]} (the SPA
#   derives these from the account state: customerId = JWT claim PropertiesId,
#   referenceId = JWT claim email).
# --------------------------------------------------------------------------

API_BASE = "https://requestservice.live.aldar.com/api/v1/"
COMMUNITY_API_BASE = "https://communityservice.live.aldar.com/api/v1/"


# Unit/building/project ids for the configured property. These come from the
# user's browser capture (the SPA resolves them from the profile/unit picker);
# config.json["selected_units"] overrides this fallback.
DEFAULT_UNITS = [{
    "unitId": "a2DJ7000000HAERMA4",
    "buildingId": "a1ZJ70000006pZnMAI",
    "projectId": "a22J7000000PpDWIA0",
}]


def api_session():
    s = creq.Session(impersonate="chrome")
    data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    for c in data.get("cookies") or []:
        s.cookies.set(c["name"], c["value"], domain=c.get("domain"),
                      path=c.get("path", "/"))
    return s, data


def jwt_claims(token: str) -> dict:
    import base64
    try:
        body = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception:  # noqa: BLE001
        return {}


def spa_headers(token: str) -> dict:
    """The SPA's axios header builder (bclass.js module 83833, function N()).

    x-request-id is base36(ms-now) + '.' + base36(random) — e.g. 'mwl734x1'.
    The datadog/traceparent headers the browser adds are RUM telemetry only;
    endpoints do not check them (verified: 201 without them).
    """
    def b36(n):
        out = ""
        while n:
            n, r = divmod(n, 36)
            out += "0123456789abcdefghijklmnopqrstuvwxyz"[r]
        return out or "0"

    rid = b36(int(time.time() * 1000)) + "." + b36(__import__("random").getrandbits(48))
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, GET, OPTIONS, DELETE, PATCH",
        "x-request-id": rid,
        "Origin": "https://www.aldar.com",
        "Referer": "https://www.aldar.com/",
        "Authorization": f"Bearer {token}",
    }


def api_call(s, token: str, path: str, body=None, base: str | None = None):
    r = s.post((base or API_BASE) + path, headers=spa_headers(token),
               data=json.dumps(body if body is not None else {}),
               timeout=45)
    return r.status_code, r.text[:1200]


def probe_api() -> None:
    s, data = api_session()
    token = data.get("token") or ""
    cl = jwt_claims(token)
    identity = {"customerId": cl.get("PropertiesId"),
                "referenceId": cl.get("email")}
    log(f"[api] identity from JWT: customerId={identity['customerId']} "
        f"referenceId={identity['referenceId']}")

    # 1) booking history — the exact shape captured from DevTools. The unit ids
    #    come from config.json (copied once from the user's browser capture).
    cfg = load_cfg()
    units = cfg.get("selected_units") or DEFAULT_UNITS
    body = {**identity,
            "selectedUnits": units,
            "page": 1, "status": "All", "pageSize": 6}
    status, text = api_call(s, token, "classes/getBookings", body)
    log(f"[api] POST classes/getBookings -> {status}")
    log("       " + re.sub(r"\s+", " ", text)[:500])

    # 2) categories — needs customerId + referenceId + unitId (verified 201).
    cat_body = {**identity, "unitId": units[0]["unitId"]} if units else {}
    status, text = api_call(s, token, "classes/categories", cat_body)
    log(f"[api] POST classes/categories -> {status}")
    log("       " + re.sub(r"\s+", " ", text)[:600])
    cats = []
    try:
        cats = json.loads(text).get("data") or []
    except ValueError:
        pass

    # 3) discover the real endpoints: probe both services for each candidate
    #    path. A 404 ("Cannot POST ...") means the route does not exist there;
    #    anything else (200/201/400/403) means the route exists.
    candidates = [
        "bookings/classes", "booking/classes", "classes/list",
        "bookings/class/details", "classes/details",
        "bookings/provider/classes", "providers/classes",
        "classes/schedules", "schedules",
        "classes/packages", "packages",
        "classes/availableSlots", "availableSlots",
    ]
    hits = {}
    for base_name, base in [("request", API_BASE), ("community", COMMUNITY_API_BASE)]:
        for path in candidates:
            status, text = api_call(s, token, path, {**identity,
                                                     "unitId": units[0]["unitId"]} if units else {})
            exists = not (status == 404 or "Cannot POST" in text)
            log(f"[disc] {base_name:>9} {path:28} -> {status}"
                f"{'  [route exists]' if exists else ''}")
            if exists:
                hits.setdefault(path, (base_name, status, re.sub(r"\s+", " ", text)[:200]))
    log(f"[disc] routes that exist: {json.dumps(list(hits), ensure_ascii=False)}")

    # 4) classes list — exact payload the SPA builds (bclass.js module):
    #    {customerId, unitId, referenceId, communityId, buildingId, categories}
    u0 = units[0]
    cls_body = {**identity, "communityId": u0["projectId"],
                "buildingId": u0["buildingId"], "categories": cat_id}
    classes = []
    for base_name, base in [("request", API_BASE), ("community", COMMUNITY_API_BASE)]:
        status, text = api_call(s, token, "bookings/classes", cls_body, base=base)
        log(f"[api] POST {base_name} bookings/classes [full SPA body] -> {status}")
        log("       " + re.sub(r"\s+", " ", text)[:600])
        try:
            j = json.loads(text)
            d = (j.get("data") or {})
            items = d if isinstance(d, list) else (d.get("data") or [])
            if items:
                classes = items
                log(f"[api] {len(items)} class(es) found on {base_name}")
                break
        except ValueError:
            pass


# --------------------------------------------------------------------------

def _fr_probe_once():
    """One-shot experiment: run the chain and dump EVERYTHING about the OTP.

    Prints, in one go: the exact JSON body sent at each step, the raw response
    headers/body, the Gmail message metadata (Date header + internalDate) and
    the full plain-text OTP email with every 4-digit candidate it contains.
    This is what tells us whether the code we send is simply not the one AM
    issued for this session (e.g. AM emails via a different journey than the
    tree we drive, or the SPA's OAuth PKCE state matters).

    Run:  python aldar_probe.py probe
    """
    cfg = load_cfg()
    global CFG_EM, CFG_PW
    CFG_EM, CFG_PW = cfg["email"], cfg["password"]
    s = creq.Session(impersonate="chrome")

    import google_otp as go
    token = go._get_access_token(cfg)  # noqa: SLF001

    tree_url = (f"{FR_BASE}/json/realms/root/realms/alpha/authenticate"
                "?authIndexType=service&authIndexValue=LIVE%20ALDAR%20WEB%20USER%20EMAIL%20LOGIN")

    def post(payload):
        r = s.post(tree_url, headers=fr_headers(),
                   data=json.dumps(payload), timeout=60)
        try:
            return r.status_code, r.json(), r.text[:300]
        except ValueError:
            return r.status_code, {"_raw": r.text[:300]}, r.text[:300]

    def dump_mail():
        data = go._gmail_get(  # noqa: SLF001
            f"{go.GMAIL_API}/messages", token,
            {"q": 'subject:"One-Time Password" newer_than:5m',
             "maxResults": 4, "fields": "messages(id)"})
        msgs = data.get("messages", [])
        print(f"[mail] {len(msgs)} OTP email(s) in the last 5 min")
        for i, m in enumerate(msgs):
            msg = go._get_message(token, m["id"])  # noqa: SLF001
            hdrs = {h["name"].lower(): h["value"]
                    for h in msg.get("headers", [])}
            text = go.message_text(msg)
            plain = re.sub(r"<[^>]*>", "\n", text)
            plain = re.sub(r"&[a-zA-Z#0-9]+;", " ", plain)
            plain = re.sub(r"\s+", " ", plain).strip()
            cands = re.findall(r"(?<![\d.,:])(\d{4})(?![\d.,:])", plain)
            print(f"  #{i} id={m['id']}")
            print(f"      from={hdrs.get('from')}")
            print(f"      date={hdrs.get('date')}  subject={hdrs.get('subject')}")
            print(f"      message internalDate={msg.get('internalDate')} "
                  f"receivedDate={msg.get('receivedDate')}")
            print(f"      candidates={cands}")
            print(f"      text={plain[:300]}")

    status, body, _ = post({})
    print(f"[start] {status}: {describe_callbacks(body)}")
    env = {"authId": body.get("authId"),
           "callbacks": json.loads(json.dumps(body.get("callbacks", [])))}
    _, filled = answer_callbacks(env, None)
    status, body, raw = post(env)
    print(f"[creds] sent slots={filled} -> {status}: {describe_callbacks(body)}")
    print(f"[creds] raw={raw[:200]}")
    if not body.get("authId"):
        return False
    env = {"authId": body["authId"], "callbacks": body["callbacks"]}

    # Give the mail a moment, then look at what actually arrived.
    time.sleep(8)
    dump_mail()

    otp, err = capture_aldar_otp(cfg)
    print(f"[mail] picked OTP={otp!r} err={err!r}")
    _, filled = answer_callbacks(env, otp)
    sent = json.dumps(env)
    print(f"[otp] body sent = {sent[:400]}")
    status, body, raw = post(env)
    print(f"[otp] -> {status}: {describe_callbacks(body)}")
    print(f"[otp] raw={raw[:200]}")
    return bool(body.get("authId"))


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    cfg = load_cfg()
    if cmd == "auth":
        return 0 if auth_start(cfg, manual=True) else 1
    if cmd == "api":
        probe_api()
        return 0
    if cmd == "probe":
        return 0 if _fr_probe_once() else 1
    if cmd == "all":
        ok = auth_start(cfg, manual=True)
        if ok:
            probe_api()
        return 0 if ok else 1
    log(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
