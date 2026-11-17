# AI Tasks Log

## Summary

| Created | Task | Status | Type | Subtasks | Time Spent | Blockers |
|---------|------|--------|------|----------|------------|----------|
| 18-11-2026 00:07:55 | [T27: Aldar Live migration — probe script for the new ForgeRock auth + booking APIs](#task-t27-aldar-live-migration--probe-script-for-the-new-forgerock-auth--booking-apis) | <span style="background-color:#0969da;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">in_progress</span> | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">feat</span> | 0/6 | 0m | none |
| 17-11-2026 10:18:27 | [T26: Autobook re-login never fires when the session dies inside the booking race](#task-t26-autobook-re-login-never-fires-when-the-session-dies-inside-the-booking-race) | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span> | <span style="background-color:#9e6a03;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">fix</span> | 5/5 | 11m | none |

---

## Task T27: Aldar Live migration — probe script for the new ForgeRock auth + booking APIs

- **Status**: <span style="background-color:#0969da;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">in_progress</span>
- **Type**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">feat</span>
- **Created**: 18-11-2026 00:07:55
- **Last Updated**: 18-11-2026 00:50:00
- **Time Spent**: 42m
- **Branch**: [`feat/ai-aldar-live-probe`](https://github.com/HectorBravo/padel_booking_alrayyana/tree/feat/ai-aldar-live-probe)
- **Commit(s)**: [4254370](https://github.com/HectorBravo/padel_booking_alrayyana/commit/4254370) (log), [16c5bbc](https://github.com/HectorBravo/padel_booking_alrayyana/commit/16c5bbc) (probe script)
- **Blockers**: none
- **Findings & Notes**:
  - The Asteco portal (`myportal.asteco.com`) is gone; the community moved to Aldar Live (`https://www.aldar.com/live`), a Next.js SPA whose whole API surface was extracted statically from its JS bundles (no iPhone traffic capture needed).
  - Auth = ForgeRock AM at `https://frald.aldar.com/am`, realm `alpha`, journey **`LIVE ALDAR WEB USER EMAIL LOGIN`**. Verified live: `POST /am/json/realms/root/realms/alpha/authenticate?authIndexType=service&authIndexValue=LIVE%20ALDAR%20WEB%20USER%20EMAIL%20LOGIN&flow=<same>` → HTTP 200 with `callbacks`: `NameCallback(IDToken1)` + `PasswordCallback(IDToken2)`.
  - OTP = **4 digits**, emailed by `no-reply@aldar.com` ("Use the following One-Time Password (OTP)… <code> … valid for the next 10 minutes"). The old Gmail capture in `google_otp.py` filters `from:asteco.com` + "is your ASTECO OTP" → must be re-pointed at aldar.com.
  - Booking APIs are on **`https://requestservice.live.aldar.com/api/v1/`** (NOT communityservice; verified live with a real browser session): `classes/categories`, `classes/packages`, `classes/schedules`, `classes/availableSlots`, `classes/getBookings` (history), `classes/bookings` (create). The `bookings/classes` family from the static endpoint map is dead (404).
  - **403 root cause was OAuth scope**: the SPA asks for `UAEPASS openid profile email`; our probe asked only `openid profile email` and got `Forbidden resource`. Fixed by requesting the UAEPASS scope.
  - **The Padel Court is an "Amenity" booking (`bookingTypeVal: "Amenity"`), not a class** — `classes/*` returns empty for all categories. The amenity booking endpoints are still to be discovered (next step, via browser capture).
  - Access token lives **180 days**; storage state saved at `ai_out/aldar_pw_state.json` (`FR-SDK-FRPROD.accessToken`) is enough to call the APIs without cookies.
  - Create-booking payload (decoded from the `booking/detail` chunk): `{classScheduleId, classId, packageId, packageType, numberOfParticipants, unitId, customerId, referenceId(PersonEmail), participants[{name,age,ParticipantId,bookingAmount,vatAmount}], totalBookingAmount, totalVatAmount}`.
  - User confirmation: web UI only shows booking *history* for them; the create flow exists in the SPA (detail page) but they couldn't reach it — hence the probe-first approach before rewriting the bot.

### User Confirmations

**Pending (awaiting user response):**

- None yet.

**Confirmed (user provided):**

- (18-11-2026 00:05) Capture route? → "First write a small probe script that logs in, lists categories/classes/slots so we confirm the flow before rewriting the bot"
- (18-11-2026 00:06) Credentials? → "I'll edit config.json myself with the new credentials; you just make the probe read from it"

### Subtasks / Plan

- [x] Subtask 1 — `aldar_probe.py`: ForgeRock login phase 1 (credentials → OTP callback), print callback shape
- [x] Subtask 2 — OTP capture from Gmail for aldar.com emails (probe-local regex first)
- [x] Subtask 3 — Phase 2: submit the OTP into the callback chain, obtain the session/token (**DONE — full OAuth access + refresh token acquired**)
- [x] Subtask 4 — Authenticated calls to the booking APIs (**DONE — `classes/getBookings` returns the real Padel Court booking; 403 root cause was the missing `UAEPASS` OAuth scope, see Findings**)
- [x] Subtask 5 — Dump each response (status + JSON head) so the request shapes are confirmed; save tokens for reuse (**DONE via playwright-cli network capture: `ai_out/cap_*.json|txt`, storage state `ai_out/aldar_pw_state.json`**)
- [ ] Subtask 6 — Decide how to reach the class/schedule list (the SPA shows "No classes available in this category" for Fitness/Wellness/Lifestyle, yet an Amenity booking exists → amenity bookings are not served by `classes/*`; needs its own endpoints)

- [ ] Subtask 7 — Apply the `UAEPASS` scope fix in `aldar_probe.py` and re-verify `classes/packages` returns 201 from our own auth (not just the browser token)
- [ ] Subtask 8 — Discover the amenity booking endpoints (browser capture on the facility tiles), then design the padel booking flow

**Progress (18-11-2026 03:40) — AMENITY BOOKING IS NOT IN THE WEB APP AT ALL**:
Dumped the SPA's complete endpoint map (`app.js` module 85767, `let i = {…}` → 273 entries, resolved to origins; full dump in `ai_out/epmap.txt`). Every booking-ish route is:

| group | endpoints | service |
|---|---|---|
| classes (the only "booking" flow in the web UI) | `classes/categories`, `classes/packages`, `classes/schedules`, `classes/availableSlots`, `classes/bookings` (create), `classes/getBookings`, `classes/initiate-payment` | requestservice / transactionservice |
| dead legacy (`bookings/classes`, `bookings/class/details`, `bookings/provider/classes`) | 404 "Cannot POST" | — |
| service-request appointments (villa maintenance/handover, NOT amenities) | `appointment/slots`, `appointment/schedule`, `appointment/candidates`, `appointment/create`, `appointment/reschedule`, `appointment/handover`, `appointments/dates|slots|create|cancel` | requestservice / identityservice |
| sales unit booking (buying property, not amenities) | `booking/unit/locking`, `booking/unit/details`, `booking/payment/plans` … | identityservice |

**There is no amenity/facility/court endpoint anywhere in the web bundle.** Combined with the booking record (`bookingTypeVal: "Amenity"`) and the UI ("No classes available in this category"), the conclusion is: **Aldar kept amenity booking (the Padel Court) in the mobile app only** — exactly what the user reports.

Targeted probes (3 requests total, no sweeps):
- `classes/schedules` with `{customerId,unitId,referenceId,communityId,buildingId,classId:"08pTq000006ihQnIAI"}` → **400 "packageId is required"** (validator contract learned).
- same + `packageId:"x"` → **500 "Failed to retrieve schedules"** — so the ids are genuinely looked up; a real class+package id would return data. The endpoint works; we simply have no amenity ids.
- `classes/availableSlots` and `classes/bookings` with `{}` → **403 "Forbidden resource"** (same guard as before: body must carry identity + valid ids).

**Decision point / options for the user**:
1. **Capture the mobile app's traffic** (the original question). Android emulator + proxy is the practical route: `app.apk` + mitmproxy/Burp, or a real Android with Wi-Fi proxy + user CA cert. iOS needs a jailbreak or an SSL-kill-switch device; a Mac + Proxyman works for non-pinned traffic. **Must first check whether the app pins certificates** — if it does, only Frida/objection on a rooted device will work.
**Progress (18-11-2026 04:20) — MOBILE APP CONFIRMED, TOOLING READY, APK SOURCE PENDING**:
Play Store (`google_play_scraper`): **Live Aldar**, `app.aldar.live`, **v7.8.8**, developer **Aldar Properties PJSC**, 10K+ installs, **updated 17-11-2026** (yesterday). The store description is now a lifestyle/rewards super-app (shop, dine, hotels, attractions, school fees, property mgmt) — **amenity booking is not mentioned at all**, consistent with it being buried/removed. No developer-provided APK link (only Website / +97180025327 / customercare@aldar.com).

Tooling installed and verified on this machine:
- `adb` + `aapt` — already present at `D:\android_tools\` (also `C:\RSL\platform-tools`, and a **Nox emulator** at `C:\Program Files\Nox\bin\`). No device/emulator currently attached (`adb devices` empty on both).
- `frida 17.27.0`, `frida-tools`, `objection 1.12.5` — installed (needed only if the app pins certificates).
- `google_play_scraper` (metadata), `playwright-cli` (web capture, already used successfully).

**How to get the APK (pick one, then I take over):**
1. **Real Android phone with USB debugging** → `adb pull $(adb shell pm path app.aldar.live | cut -d: -f2 | head -1) ai_out\aldar_live.apk` — official build, exact version you use. Best option.
2. **Start the Nox emulator**, install Live Aldar inside it, then the same `adb pull`. (Nox is x86 → needs Houdini/ARM translation; may fail on ARM-only native libs.)
3. Third-party APK mirror — possible but I'd rather not: unverifiable binary provenance.

**Progress (18-11-2026 04:50) — BUILDING OUR OWN EMULATOR (Nox unusable, no Hyper-V role)**:
The user's Nox emulator cannot start (needs the Hyper-V feature; user declined to install it). Instead of giving up, we use the **Google Android Emulator with WHPX**, which works because a hypervisor is already present (`Win32_ComputerSystem.HypervisorPresent = True`) — no Windows feature changes required. Host: i7-12700K, 128 GB RAM.

What was set up (all under `D:\android_sdk`, nothing system-wide):
- Android **command-line tools** (`commandlinetools-win-11076708`) → `D:\android_sdk\cmdline-tools\latest`.
- Reused the existing JDK: `D:\tools\jdk-17.0.2` (set as `JAVA_HOME` only inside our scripts — no global env changes).
- All SDK licenses accepted; installed **platform-tools**, **emulator**, **platforms;android-34**, and **system-images;android-34;google_apis_playstore;x86_64** (Google Play image → real Play Store inside the AVD, so Live Aldar installs legitimately *and* x86_64 runs natively on this CPU — no ARM translation problem).
- Helper scripts: `ai_out/yes.bat` (auto-accept prompts), `ai_out/sdk_install.bat` (re-runnable installer; PowerShell mangles the `;` in SDK package ids, so keep it in a .bat).

**Why the Google Play image matters**: with Play Store inside the AVD we can install the official app and sign in; and if the app pins certificates, we still have the option of an **x86_64 frida-server** (native, no ARM shims) + `objection sslpinning disable`.

Next: create the AVD (`avdmanager create avd -n aldar -k "system-images;android-34;google_apis_playstore;x86_64" -d pixel_6`), boot it headless with `-writable-system`, install Live Aldar from Play, then capture traffic (mitmproxy on the host; emulator reaches it via `10.0.2.2`) or dump the JS bundle/endpoints straight off the device.

**Progress (18-11-2026 02:55) — ENDPOINTS PROVEN WITH A REAL BROWSER SESSION (playwright-cli)**:
Installed the `microsoft/playwright-cli` skill (`npm i -g @playwright/cli`). Drove a headless Chromium through the real UI once: `/live/login` (email, password, **Remember Me checked**) → `/live/otp` (4-digit code captured from Gmail by `aldar_probe.capture_aldar_otp`) → logged in. Then saved storage state and recorded every XHR with `playwright-cli requests / request-body / response-body`.

1. **The 403 was a scope problem, not headers or body.** The browser's access token has `scope: ["UAEPASS","openid","profile","email"]`; the token our probe minted had only `["openid","profile","email"]`. With the browser token, `classes/packages` (which 403'd before) answers **201**. → Fix in `aldar_probe.py`: request scope `UAEPASS openid profile email` on `/authorize` and verify the claim in the returned JWT.
2. **Booking APIs live on `requestservice.live.aldar.com/api/v1/`** (not `communityservice`). Confirmed routes: `classes/categories`, `classes/packages`, `classes/schedules`, `classes/availableSlots`, `classes/getBookings`, `classes/bookings` (create — never called). `bookings/classes`, `bookings/class/details`, `bookings/provider/classes` are **dead** (404 "Cannot POST").
3. **Exact captured bodies** (`ai_out/cap_getbookings_body.txt`, `cap_packages_body.txt`):
   - `classes/getBookings`: `{"customerId","referenceId","selectedUnits":[{unitId,buildingId,projectId}],"page":1,"status":"All","pageSize":6}`
   - `classes/packages`: `{"customerId","unitId","referenceId","communityId","buildingId","categories":"Fitness"}` (note: `categories`, singular value; `communityId` = the *project* id)
4. **Only headers that matter**: `Authorization: Bearer …`, `Content-Type: application/json`, `Origin/Referer`. The datadog/`traceparent`/`x-request-id` headers are RUM telemetry — replay without them works (verified 201).
5. **The real booking record** (`ai_out/cap_booking_detail.json`) reveals the domain split: `"bookingTypeVal":"Amenity"`, `classType:null`, `packageType:null`, `ageGroup:null`, amounts `null`. So **the Padel Court is an AMENITY, not a "class"** — that's why `classes/packages` returns `[]` for every category and the UI says "No classes available in this category". The amenity booking flow has its own endpoints, still undiscovered.
6. **Session lifetime**: access token `exp - iat = 15552000 s` (**180 days**), and with Remember Me the persisted state (`ai_out/aldar_pw_state.json`: `FR-SDK-FRPROD` + `persist:root` in localStorage) is enough to call the APIs directly — no cookies needed. Re-login is only needed every ~6 months.
7. **Gotcha**: navigating straight to `/live/user/overview` after some flows lands on `/live/user/no-properties` (unit context lost). The unit ids must come from `config.json` / the captured constants, not from navigation state.

**Next step for subtask 6**: find the *amenity* endpoints. Best route is the same one that worked here — drive the browser to the amenity booking screen (the "Bookings" landing page shows facility tiles: Swimming / Tennis / Basketball / Spa & Wellness / yoga) and capture what it calls when a facility is opened; `classes/schedules` + `classes/availableSlots` bodies will come with it.

**Progress (18-11-2026 00:50) — AUTH FULLY WORKING**: `python aldar_probe.py auth` now completes end-to-end and writes `aldar_session.json` (`token` = OAuth access_token JWT, plus cookies; AM also returns a refresh_token). Root causes that were blocking the OTP step, all fixed in `aldar_probe.py`:
1. **Wrong request headers.** The SPA's FRAuth Next SDK (chunk `1006`, module `41006`) sends `Accept-API-Version: protocol=1.0,resource=2.1` and `X-Requested-With: forgerock-sdk`. With `resource=2.0` AM replayed the journey from the start every round (infinite Name/Password loop).
2. **Extra `flow=` query param** was wrong — the SPA sends only `authIndexType` + `authIndexValue`. Removed.
3. **Payload handling.** The SPA keeps the *response envelope* and re-submits it unchanged, filling only `input[].value`. Merging/rebuilding callbacks produced 401 "Login failure". Rewritten as an `envelope` loop; `merge_callbacks()` kept only as a deprecated reference.
4. **Stale OTP.** Aldar's OTP is single-use; the first captured email was often already consumed, so AM answered "OTP is invalid." → "OTP Limit Exceeded". Now every OTP round re-reads Gmail and skips codes already tried (`capture_aldar_otp(..., exclude=used_otp)`); query `subject:"One-Time Password" newer_than:20m`, filtered to subjects containing "Live Aldar" (Amazon delivery OTPs share that subject). Aldar's mail API omits `internalDate`, so time filtering is best-effort — ordering relies on Gmail's newest-first.
5. **Success detection.** ForgeRock finishes with `{"tokenId":…,"realm":"/alpha","successUrl":…}` and **no** `callbacks`; the loop used to treat that as failure. Now breaks on `tokenId`.
6. **OAuth leg.** After the tree succeeds, replaying `/authorize` (same session + PKCE verifier) returns `?code=…`, exchanged at `/oauth2/realms/root/realms/alpha/access_token` for access + refresh tokens. Scope `openid profile email UAEPASS` was tried and makes **no** difference to the API 403 below.

**Current blocker (subtask 4): every booking endpoint answers `403 {"Forbidden resource"}`** even with a valid bearer token (FR `/userinfo` returns 200 with the user's property/darna authorizations). Verified: authenticated `POST` → 403, unauthenticated `POST` → also 403, `GET` → 404. So the endpoint exists and the gate is not our token alone.

### Full Context Notes for AI Agents

> **Purpose**: self-contained knowledge base to resume this task without other context.

**Goal**: migrate `padel_booking.py` (Asteco) to Aldar Live. This task delivers ONLY a probe script (`aldar_probe.py`) that proves the new auth + read endpoints; the full bot rewrite is a follow-up task.

**Auth flow (ForgeRock AM, verified)**:
- Base: `https://frald.aldar.com/am`; realm path `realms/root/realms/alpha`.
- Start: `POST /json/realms/root/realms/alpha/authenticate?authIndexType=service&authIndexValue=LIVE%20ALDAR%20WEB%20USER%20EMAIL%20LOGIN&flow=LIVE%20ALDAR%20WEB%20USER%20EMAIL%20LOGIN` with `Content-Type: application/json`, body `{}` → 200 `{authId, callbacks:[{type:"NameCallback",input:[{name:"IDToken1"}]},{type:"PasswordCallback",input:[{name:"IDToken2"}]}], header, description}`.
- Continue: POST the same URL with `{"authId": <previous authId>, "callbacks":[...output filled...]}`. Name → `output: email`; Password → `output: password`. The chain then emits an OTP callback (type TBD live — likely `TextOutputCallback` + `NameCallback`, or a CM-OTP node); answer it with the 4-digit code. Final step returns `{"token": <JWT>, "success": true}` or a redirect URL. Whether the communityservice API wants the AM JWT (`sessionToken`/`id_token`) as `Authorization: Bearer` or an OAuth access token is **TBD live** — probe must try both.
- The web app posts with headers `Content-Type: application/json`; FR SDK adds `Accept-API-Version: protocol=1.0,resource=2.0` + `X-Requested-With` (values seen in login chunk; probe should send these too).

**OTP email**: sender `no-reply@aldar.com`; body contains "One-Time Password (OTP)" and a bare 4-digit line. Gmail capture code: reuse `google_otp.py` machinery (`_get_access_token`, `_list_otp_messages`, `_get_message`, `message_text`) but with query `from:aldar.com` and a regex tolerant of the new wording ("Use the following One-Time Password (OTP)… <4 digits> … valid for the next 10 minutes"). In the probe, implement locally (do not edit google_otp.py yet).

**Booking APIs (communityservice)** — all under `https://communityservice.live.aldar.com/api/v1/`:
- Verbs/shapes TBD live; known paths (from `_app` chunk endpoint map): `classes/categories`, `bookings/classes`, `bookings/class/details`, `bookings/provider/classes`, `classes/schedules`, `classes/packages`, `classes/availableSlots`, `classes/bookings` (CREATE — never call in probe), `classes/getBookings` (history), `bookings/faq`, `bookings/terms`, `classes/initiate-payment`.
- Probe must try GET first, then POST `{}`; record status + body head. 403 = auth-gated path exists; 404 = wrong verb/path or missing query params.
- Verified unauthenticated: `classes/availableSlots` → 403, `classes/bookings` → 403 (POST), `classes/getBookings` → 404 (GET), `bookings/classes` → 404 (GET). So verbs matter and are TBD.

**Config**: probe reads the existing `config.json` (`email`, `password`). User will update those to the new Aldar credentials themselves. Do not commit `config.json`.

**Local artifacts**: `ai_out/` holds downloaded SPA bundles (`app.js` = `_app-248c41826c60b326.js`, `detail.js`, `bm.js` = build manifest) + `find_vars.py` (deobfuscation helper). Useful for later endpoint-shape digging; keep untracked or gitignore.

**Build-manifest routes** (`bm.js`): booking pages are `/iceridge/user/user-requests/booking/{booking-class,bookingHistory,bookingStatus,detail,receipt}` — the web app CAN create bookings (detail page has Book Now + `booking_create_*` GA events); user just couldn't see the entry point.

**Current state**: nothing written yet for T27 except this log. Next: write `aldar_probe.py`, then run phase 1 live (safe, no side effects), then ask the user to confirm config.json credentials are updated before running the OTP step (it emails them).


| 15-11-2026 23:40:00 | [T22: Startup UX — rocket Bot-started message + console logs for session/google/slots](#task-t22-startup-ux--rocket-bot-started-message--console-logs-for-sessiongoogleslots) | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span> | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">feat</span> | 2/2 | 15m | none |
| 15-11-2026 23:05:00 | [T21: setupgoogle flow — email, then auth, then pick from real calendar list](#task-t21-setupgoogle-flow--email-then-auth-then-pick-from-real-calendar-list) | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span> | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">feat</span> | 4/4 | 25m | none |
| 15-11-2026 12:45:00 | [T20: Auto-login at program start (CLI + telegram) via Gmail OTP](#task-t20-auto-login-at-program-start-cli--telegram-via-gmail-otp) | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span> | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">feat</span> | 3/3 | 25m | none |
| 13-11-2026 19:52:03 | [T19: setupgoogle defaults, /status Google test, stop retrying booked days](#task-t19-setupgoogle-defaults-status-google-test-stop-retrying-booked-days) | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span> | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">feat</span> | 3/3 | 35m | none |
| 13-11-2026 19:21:45 | [T18: Make the Google OAuth callback endpoint work (fix the 502)](#task-t18-make-the-google-oauth-callback-endpoint-work-fix-the-502) | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span> | <span style="background-color:#9e6a03;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">fix</span> | 6/6 | 40m | none |
| 13-11-2026 13:34:10 | [T17: Auto-capture the OTP from Gmail instead of asking the user](#task-t17-auto-capture-the-otp-from-gmail-instead-of-asking-the-user) | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span> | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">feat</span> | 9/9 | 18m | none (live proof awaits Google re-auth) |
| 11-11-2026 04:23:36 | [T16: Diagnose the unreadable picker that makes waits pointless](#task-t16-diagnose-the-unreadable-picker-that-makes-waits-pointless) | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span> | <span style="background-color:#9e6a03;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">fix</span> | 5/5 | 12m | none |

## Task T26: Autobook re-login never fires when the session dies inside the booking race

- **Status**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span>
- **Type**: <span style="background-color:#9e6a03;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">fix</span>
- **Created**: 17-11-2026 10:18:27
- **Last Updated**: 17-11-2026 10:29:26
- **Time Spent**: 11m
- **Branch**: [`fix/ai-autobook-relogin-in-race`](https://github.com/HectorBravo/padel_booking_alrayyana/tree/fix/ai-autobook-relogin-in-race) (merged into `main`, branch deleted)
- **Commit(s)**: [0e2c4f7](https://github.com/HectorBravo/padel_booking_alrayyana/commit/0e2c4f7) (log), [edf4809](https://github.com/HectorBravo/padel_booking_alrayyana/commit/edf4809132bc215674993b88704f983d2c3f0ffd) (code); merged via [PR #15](https://github.com/HectorBravo/padel_booking_alrayyana/pull/15)
- **Blockers**: none
- **Findings & Notes**:
  - Symptom (user's Telegram): after the autobook retry limit, "Booking error: Session expired. Re-login before booking." — the automatic re-login never ran.
  - Root cause: `book_day()` validates the session once via `ensure_session()`, then calls `run_booking_race()`. Inside the race, every portal call (`get_booking_meta` prewarm, `portal_bookable_dates`, `get_available_slots`) can raise `RuntimeError("Session expired…")` (raised at `padel_booking.py:652`). That escaped `book_day`'s `try/except` as a bare "Booking error" and never reached `relogin()`.
  - Fix: added an optional `on_session_expired` callback to `run_booking_race`; every network step now runs through a `_step(fn, what)` wrapper that, on an expired-session `RuntimeError`, calls the recovery hook once and retries. `book_day` passes its own `ensure_session` (which does keepalive → relogin). Non-session `RuntimeError`s propagate unchanged.
  - Also: the long-wait refresh (`on_wait`) now routes an expired session through the same recovery path instead of only logging it.
  - Test-harness bug found while verifying: `ai/verify_t9_behaviors.py` scheduler check hung (12–41 MB of identical log lines). Its `race` wrapper tried `k.setdefault("max_attempts", 4)`, but `book_day` calls the race with `wait_for_open=False`, so the only real bound is the wall-clock deadline — which cannot fire once `time.sleep` is faked. Re-bounded via the race's own `stop` guard (checked every refresh) + a fake sleep counter, and opened the picker on the 2nd scheduler attempt to exercise trigger 3.
  - **Merged (17-11-2026 10:29)** via [PR #15](https://github.com/HectorBravo/padel_booking_alrayyana/pull/15) as a **fast-forward** (`origin/main` tip = `d327815`, the branch tip; no merge commit). Post-merge on `main`: compile OK, T26 exit 0, T9 15/15, T25/T24/T23/T17 all exit 0. Branch deleted locally and on the remote.
  - **Not yet deployed** — redeploy after the CI image rebuild (`docker compose pull && docker compose up -d`) and confirm that a mid-race session expiry now recovers instead of showing "Booking error: Session expired…".

### User Confirmations

**Pending (awaiting user response):**

- None yet.

**Confirmed (user provided):**

- (17-11-2026 10:18:27) Reported symptom → "if the automatic rebooking fails after it reaches the limit (I think it was 12 hours) the relogin does not work. The message I see on telegram is 'booking error: session expired. Re-login before booking'."

### Subtasks / Plan

- [x] Subtask 1 — Reproduce: `ai/verify_t26_autobook_relogin.py` (session dies mid-race → must recover, not escape).
- [x] Subtask 2 — Add `on_session_expired` + `_step` recovery wrapper in `run_booking_race`; wrap the three portal calls and route `on_wait` failures through it.
- [x] Subtask 3 — Wire `book_day` to pass `on_session_expired=ensure_session`.
- [x] Subtask 4 — Fix the `verify_t9_behaviors.py` scheduler hang (bound the inner race via its `stop` guard; open picker on 2nd attempt). Full suite green: T9 15/15, T26/T25/T24/T23/T17 exit 0.
- [x] Subtask 5 — Committed (`0e2c4f7` log, `edf4809` code), pushed with `-u`, merged via [PR #15](https://github.com/HectorBravo/padel_booking_alrayyana/pull/15) (fast-forward), branch deleted locally + remote. Final log commit follows on `main`.

### Full Context Notes for AI Agents

> **Purpose**: self-contained knowledge base to resume this task without other context.

**Exact code change (`padel_booking.py`)**:
- `run_booking_race` signature (:2283) gained `on_session_expired=None`.
- Three closures after `now = datetime.now()` (~:2360): `_expired(err)` (matches `"session expired"`/`"session missing"` lowercased — matches the production string at :652), `_recover()` (calls `on_session_expired()`, returns bool, swallows+logs its own exceptions), and `_step(fn, what)` (runs `fn()`; on a session-expired `RuntimeError` prints "session expired during {what}; attempting recovery…", calls `_recover()`, retries once, else re-raises; non-session errors propagate).
- Wrapped call sites: prewarm `s, meta = _step(lambda: get_booking_meta(cfg), "booking-meta prewarm")`; loop picker `offered = _step(lambda: portal_bookable_dates(cfg), "picker read")`; slots `slots = _step(lambda: get_available_slots(s, cfg, api_date, meta), "slot read")`.
- `on_wait` block (~:2513): on exception, `if _expired(e) and _recover(): print("session restored during wait.") else: <old non-fatal log>`.
- `book_day` race call (~:2771) now passes `on_session_expired=ensure_session`.

**Why this is the fix**: `book_day` already had a session-validity guard, but it runs *before* the race; the portal session can die during the (up to 12h) wait or between validation and the first network hit. The escaped error string was identical to the pre-guard message, so it looked like "re-login didn't work" when in fact `relogin()` was never invoked from inside the race.

**Test harness (`ai/verify_t9_behaviors.py`, scheduler check ~:468)**:
- Fakes `pb.time` (time/sleep/monotonic) with a virtual clock, so `datetime.now()` stays real while `time.sleep` is stubbed. Because `book_day` calls the race with `wait_for_open=False`, the ONLY in-loop bound that can fire under faked sleep is the `stop` guard at :2419 — hence the old `max_attempts=4` setdefault did nothing and the loop spun forever.
- New design: a global `sleeps["n"]` counter raises the shared `stop` Event at 40 total stubbed sleeps; `offered()` returns the picker WITHOUT the target until `races["n"] >= 2`, so pass-1 returns `not_yet_open`, the loop advances past `retry_wait_secs` (120s of virtual time via 0.5s ticks), trigger 3 (`due`) re-attempts, and pass-2 books on its first refresh. Result: terminates in <1s, ALL PASS (15/15).

**Verification commands** (all green):
- `python -m py_compile padel_booking.py padel_telegram.py google_calendar.py google_otp.py ai/verify_t9_behaviors.py ai/verify_t26_autobook_relogin.py`
- `python ai/verify_t9_behaviors.py` → ALL PASS, exit 0
- `python ai/verify_t26_autobook_relogin.py`, `_t25_login_google_status`, `_t24_otp_single_use`, `_t23_login_not_blocked`, `_t17_otp_capture` → all exit 0

**Current state**: DONE and on `main` (fast-forward merge of [PR #15](https://github.com/HectorBravo/padel_booking_alrayyana/pull/15); branch deleted locally + remote). All suites green post-merge — T26 exit 0, T9 15/15, T25/T24/T23/T17 exit 0; compile + import OK. Next step: none for the code; deploy proof pending after the CI image rebuild.

**Constraints**: git identity via `GIT_AUTHOR_NAME`/`GIT_COMMITTER_NAME=AI_bot` env vars + `--author='AI_bot <user_email>'`; never modify `git config`; `[ai]` tag after the colon. `config.json`/`session.json`/`pending_login.json` are gitignored. The harness swallows Python stdout — write findings to a file when debugging, and always bound any loop test with a watchdog or stop guard.

---

## Task T25: /login should also confirm the Google integration state

- **Status**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span>
- **Type**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">feat</span>
- **Created**: 16-11-2026 16:05:49
- **Last Updated**: 16-11-2026 16:22:57
- **Time Spent**: 17m
- **Branch**: [`feat/ai-login-reports-google-status`](https://github.com/HectorBravo/padel_booking_alrayyana/tree/feat/ai-login-reports-google-status) (merged into `main`, branch deleted)
- **Commit(s)**: [0443aa1](https://github.com/HectorBravo/padel_booking_alrayyana/commit/0443aa1b9f69062cc42c0a8c57406752ad70c64d) (code + tests), on top of [8114969](https://github.com/HectorBravo/padel_booking_alrayyana/commit/8114969) (task log) — merged via [PR #14](https://github.com/HectorBravo/padel_booking_alrayyana/pull/14) as a fast-forward
- **Blockers**: none
- **Findings & Notes**:
  - After [T24](#task-t24-otp-login-fails-with-a-correctly-captured-otp-finishable-pending-stub), `/login` prints only "✅ Login successful — session saved." The user could not tell whether Calendar sync + Gmail OTP reading are alive, unlike at boot where `_startup_status()` reports them.
  - Added `PadelBot._google_status_line()`: runs the same live `test_connection(cfg)` check as boot and returns one line for all three states (active / not configured / configured-but-dead). Appended to both login-success messages (`_on_login` fast path, `_otp_from_gmail` already-authenticated path).
  - Deliberately NOT added to `_ensure_login_startup`: `_startup_status()` already reports Google at boot, so it would duplicate.
  - **Merged (16-11-2026 16:22)** via [PR #14](https://github.com/HectorBravo/padel_booking_alrayyana/pull/14) as a **fast-forward** (`origin/main` = `0443aa1`, no merge commit). Post-merge on `main`: compile OK, T25 5/5, T24/T23/T17/T9 all exit 0. Branch deleted locally and on the remote.
  - **Not yet deployed** — redeploy after the CI image rebuild and confirm `/login` replies with both lines (session saved + Google Calendar active).

### User Confirmations

**Pending (awaiting user response):**

- None yet.

**Confirmed (user provided):**

- (16-11-2026 16:03:00) "What do you see / expect?" → "I see 'login successful - session saved'. But I'd expect to get also the confirmation that the google integration is also ok as when the program starts"

### Subtasks / Plan

- [x] Subtask 1 — `PadelBot._google_status_line()` mirroring `_startup_status` wording for all three Google states.
- [x] Subtask 2 — Append it to the `/login` success message and the `_otp_from_gmail` already-authenticated success message.
- [x] Subtask 3 — `ai/verify_t25_login_google_status.py` (5 checks): all three states, `/login` fast path, `_otp_from_gmail` path. Regressions: T24/T23/T17/T9 all exit 0.
- [x] Subtask 4 — Committed (`8114969` log, `0443aa1` code), pushed with `-u`, merged via [PR #14](https://github.com/HectorBravo/padel_booking_alrayyana/pull/14) (fast-forward), branch deleted locally + remote. Final log commit follows on `main`.

### Full Context Notes for AI Agents

> **Purpose**: self-contained knowledge base to resume this task without other context.

**Why T24 made this visible**: before T24, `/login` always went through `_otp_from_gmail`, which sends "📥 Google integration is on — reading the OTP from your Gmail…". Now `login_start()` returns True (already authenticated or auto-finished internally) and `/login` short-circuits to a bare success line, so the Google signal disappeared.

**Key code**:
- `padel_telegram.py:_google_status_line` (~line 615) — new helper; calls `test_connection(self.cfg)` → `(True|False|None, detail)`. `None` means not configured. Never raises (`test_connection` swallows everything).
- `padel_telegram.py:_startup_status` (:540) — the boot report whose wording is mirrored (kept as the source of the phrasing; not refactored to use the helper because it also `_notify`s with a keyboard on the dead-token case).
- Success messages updated: `_on_login` `finished_here` branch, `_otp_from_gmail` `is_logged_in` branch. Both do `"✅ Login successful — session saved." + self._google_status_line()`.
- `google_calendar.test_connection` (:692) — live check: fresh access token + one Calendar list call. Proves refresh-token AND calendar read; Gmail shares the same OAuth grant, so it is a fair proxy for OTP reading.

**Test**: `ai/verify_t25_login_google_status.py` — `BotStub(pt.PadelBot)` with `_send` captured (same pattern as `verify_t23_login_not_blocked.py`); patches `pt.test_connection`, `pt.login_start`, `pt.capture_otp`, `pt.is_logged_in`, `pt.get_session`. 5/5 pass.

**Current state**: DONE and on `main` (fast-forward merge of PR #14; `origin/main` = the code commit, plus this final log commit). All suites green — T25 5/5, T24/T23/T17/T9 exit 0; compile + import OK.

**Next step**: none for the code. Deploy proof pending — after the CI image rebuilds, `docker compose pull && docker compose up -d` on the production host and confirm `/login` answers with both lines:

```
✅ Login successful — session saved.
📅 Google Calendar active - sync and OTP reading are on.
```

**Constraints**: same git identity rules as T24 (`AI_bot` name via env vars + `--author`, never touch `git config`, `[ai]` tag). `config.json`/`session.json`/`pending_login.json` are gitignored.

---

## Task T24: OTP login fails with a correctly captured OTP (finishable pending stub)


- **Status**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span>
- **Type**: <span style="background-color:#9e6a03;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">fix</span>
- **Created**: 16-11-2026 15:18:34
- **Last Updated**: 16-11-2026 15:54:58
- **Time Spent**: 46m
- **Branch**: [`fix/ai-login-pending-stub-and-otp-reuse`](https://github.com/HectorBravo/padel_booking_alrayyana/tree/fix/ai-login-pending-stub-and-otp-reuse) (pushed to `main` directly by the user, branch deleted)
- **Commit(s)**: [46a513a](https://github.com/HectorBravo/padel_booking_alrayyana/commit/46a513a) (code + tests), on top of [735fea9](https://github.com/HectorBravo/padel_booking_alrayyana/commit/735fea9) (task log before push) — both now on `main`
- **Blockers**: none
- **Findings & Notes**:
  - The live `pending_login.json` was the smoking gun: `cookies: []` and **every** `otp_fields` value empty, with a fresh timestamp. That state can only be written by `login_start` (its one `PENDING_FILE.write_text`) — meaning it reached the "checkOtp" URL branch but parsed an OTP form that was not there.
  - A pending record like that can **never** be finished: `verifyOtp` answers `"3"` (invalid) for any code, so the user sees "it can't login with a correctly captured OTP".
  - Second defect: `login_start` already auto-finishes internally (`_auto_login`), yet all three Telegram callers capture + verify **again**. The portal's OTP is single-use, so the second attempt fails.
  - **Delivered (16-11-2026 15:54)**: the user pushed the branch's commits straight to `main` (fast-forward; `origin/main` = `af8a45a`), so there was no PR/merge commit. Post-push on `main`: compile OK, T24 4/4, T23 7/7, T17 18/18, T9 ALL PASS (all exit 0). Branch deleted locally and on the remote.

### User Confirmations

**Pending (awaiting user response):**

- None yet.

**Confirmed (user provided):**

- None yet.

### Subtasks / Plan

- [x] Subtask 1 — Reproduce with a faithful portal model (`ai/verify_t24_otp_single_use.py`): already-authenticated session whose redirect chain stays on `/login/checkOtp` writes the empty-fields stub → FAIL before fix.
- [x] Subtask 2 — `login_start`: validate the parsed OTP form (`id_user` present) instead of trusting the URL; treat an unparseable page as "no OTP step" (already-authenticated / clear error). No stub is written.
- [x] Subtask 3 — `login_finish`: a stale/empty-field pending record now fails with the real cause and clears itself, instead of blaming a correct code with "OTP invalid".
- [x] Subtask 4 — `login_start` returns True when the login is already complete; the three Telegram callers (`_on_login`, `_ensure_login_startup`, `_relogin`) stop re-capturing / re-verifying (single-use OTP).
- [x] Subtask 5 — Fix the unrealistic fixture in `ai/verify_t17_otp_capture.py` (empty OTP page) that my new validation correctly rejected; suite now exits 0.
- [x] Subtask 6 — Committed (`735fea9` log, `46a513a` code); the user pushed to `main` directly, branch deleted. Final log commit follows on `main`.

### Full Context Notes for AI Agents

> **Purpose**: self-contained knowledge base to resume this task without other context.

**Symptom**: OTP capture succeeds, but login then reports it can't log in with that (correct) OTP.

**Root cause 1 — finishable-looking pending stub (`padel_booking.py:login_start`)**
- `login_start` only tested `"checkOtp" not in r.url`. When the browser session is already authenticated, Akamai can keep the redirect chain on `/login/checkOtp` while serving the landing page. Parsing that yields every hidden field empty; the code still wrote `pending_login.json`.
- Only one writer exists (`PENDING_FILE.write_text`, verified by search), so an all-empty `otp_fields` file proves this path ran. Confirmed against the live file: `{"cookies": [], "otp_fields": {all ""}, "created_at": "2026-11-16T08:55:02"}` with `session.json` written 1s later (the "Already authenticated" branch).
- Fix: after parsing, `if not otp_fields["id_user"]:` → if `is_logged_in` save + `return True`, else raise a clear RuntimeError. No stub is ever written for a non-OTP page.

**Root cause 2 — misleading error (`padel_booking.py:login_finish`)**
- Loads the pending record and posts `verifyOtp`; empty `otp_fields` produced `"3"` → "OTP invalid", blaming a correct code.
- Fix: check `otp_fields.get("id_user")` first; unlink the stale record and raise "The pending login is stale or incomplete (no OTP was actually requested). Run 'login-start' again."

**Root cause 3 — double verification of a single-use OTP**
- `login_start` ends with `if _auto_login(cfg): return True`, so it already runs `capture_otp` + `login_finish`. Callers re-captured and re-finished:
  - `padel_telegram.py:_on_login` (~line 618)
  - `padel_telegram.py:_ensure_login_startup` (~line 509)
  - `padel_telegram.py:_relogin` (~line 1504)
- Fix: `login_start` now returns bool (True = complete). Each caller checks it and reports success without re-capturing/verifying.
- `padel_booking.ensure_login` (:528) was already safe — it tests `is_logged_in(get_session(), cfg)` before its defensive `_auto_login`, so no change needed.

**What was tried that did NOT work**
- A throwaway probe suggested the OTP must be verified on the *same* TLS/Akamai session (cookie replay looked insufficient). That was a **harness artifact**: the fake `new_session` handed out the same object twice and `_load_cookies` ran after `login_finish`'s own load. Modelled faithfully, cookie replay works — session affinity is NOT the defect; do not chase it.
- PowerShell swallows Python tracebacks (`2>&1 | Select-Object`, `>file 2>&1` both lose them). Write diagnostics to a file from inside Python instead.

**Verification suite**: `ai/verify_t24_otp_single_use.py` (4 checks; prints + exit code; no network — patches `new_session`, `_timed`, `PENDING_FILE`, `capture_otp`, `is_logged_in`, `save_session`, `install_session`, `_STATE`). Check 1a reproduces the production stub shape. Before fix: check1a FAIL (3/4). After: 4/4.

**Current state**: DONE and on `main`. All suites green — T24 4/4, T23 7/7, T17 18/18 (exit 0), T9 ALL PASS; `py_compile` + import OK for all modules. `origin/main` = `af8a45a`; the fix is `46a513a`.

**Next step**: none for the code. Deploy proof still pending — after the CI image rebuilds, redeploy on the production host (`docker compose pull && docker compose up -d`) and confirm `/login` with Google configured completes in one pass (no "OTP invalid"), and that `pending_login.json` never again appears with empty `otp_fields`.

**Constraints / gotchas**
- Git identity: `GIT_AUTHOR_NAME="AI_bot"` + `GIT_COMMITTER_NAME="AI_bot"` only; never set email env vars or touch `git config`. Log commits: `docs(ai): [ai] ...` pushed to the user's current branch (`main`). `[ai]` tag after the colon in every commit.
- `pending_login.json`, `config.json`, `session.json` are gitignored — never commit them.
- Deploy flow: PR → merge to `main` → `.github/workflows/docker-publish.yml` rebuilds `hecbr/padel-booking-alrayyana` → user runs `docker compose pull && docker compose up -d` on the production host.

---

## Task T23: /login stopped working — swallowed by the auto-started Google setup flow


- **Status**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span>
- **Type**: <span style="background-color:#9e6a03;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">fix</span>
- **Created**: 16-11-2026 07:51:02
- **Last Updated**: 16-11-2026 08:53:30
- **Time Spent**: 1h 2m
- **Branch**: [`fix/ai-login-blocked-by-stale-setup-flow`](https://github.com/HectorBravo/padel_booking_alrayyana/tree/fix/ai-login-blocked-by-stale-setup-flow) (merged into `main`, branch deleted)
- **Commit(s)**: [36b7383](https://github.com/HectorBravo/padel_booking_alrayyana/commit/36b7383fb2c0008f8e3bc0e808e7634eb01a21e2) (code), merged as [0115346](https://github.com/HectorBravo/padel_booking_alrayyana/commit/0115346ca2dfa9fcc035a701f596838351eb7cfb) — [PR #13](https://github.com/HectorBravo/padel_booking_alrayyana/pull/13)
- **Blockers**: none
- **Findings & Notes**:
  - **Root cause**: `_startup_status()` (added in `3e64f44`, made interactive by `3fca3b1`) called `self._on_setupgoogle(chat_ids[0])` at boot whenever Google was configured but its token was dead. That leaves `pending[chat] = {action: "gcal_email"}` waiting for an answer **forever**. `_on_message` routed *every* text through the pending-flow branch before the `/command` dispatch, so the user's next `/login` was fed to the email validator → "That doesn't look like an email." and `_on_login` never ran. /login looked broken; it was unreachable.
  - **Fix 1**: a slash command is never flow input — only plain text answers the Google setup flow. Any command also clears the stale `gcal_*` state so it cannot interfere afterwards.
  - **Fix 2**: startup no longer opens the interactive setup unasked; it reports the dead token and offers a one-tap 🔧 Fix Google setup button (`gc:start` → `_on_setupgoogle`). The user starts the flow deliberately.
  - **Fix 3** (second `/login` trap): when `capture_otp` finds nothing, `_otp_from_gmail` fell back to "reply with the code" even when `login_start` had already authenticated the session ("Already authenticated", no `pending_login.json`). It now checks `is_logged_in(get_session(), cfg)` first and reports success.
  - New suite `ai/verify_t23_login_not_blocked.py` (7 checks) — failed 3/5 before the fix, 7/7 after. Regressions: T17 18/18, T9 ALL PASS. All modules compile + import.
  - **Merged (16-11-2026 08:53)** via [PR #13](https://github.com/HectorBravo/padel_booking_alrayyana/pull/13) as merge commit `0115346`; the branch was rebuilt first (see context notes) so the PR carried **code + tests only** and merged with no conflicts. Post-merge on `main`: compile OK, T23 7/7, T17 18/18. Branch deleted locally and on the remote.
  - **Not yet deployed** — redeploy on ububu.lan after the CI image rebuild (~120s) and confirm the boot log says `offering re-setup` (not `starting re-setup`) and that `/login` reaches `_on_login`.

### User Confirmations

**Pending (awaiting user response):**

- None yet.

**Confirmed (user provided):**

- None yet.

### Subtasks / Plan

- [x] Slash commands bypass the pending Google-setup flow + clear stale state
- [x] Startup offers re-setup as a button instead of auto-opening the flow
- [x] `_otp_from_gmail` succeeds when the session is already authenticated
- [x] New T23 suite + T17/T9 regressions green (7/7, 18/18, ALL PASS)
- [x] Commit/push on the AI branch (`344686b`) + PR #13 opened against `main`

### Full Context Notes for AI Agents

> **Purpose**: self-contained recovery knowledge.
>
> - Symptom: `/login` produced no login; user saw the Google-setup email prompt. Reproduced with `ai/verify_t23_login_not_blocked.py` (3/5 FAIL before the fix).
> - Pending-flow router (`padel_telegram.py:_on_message`, ~line 438): previously `if gc_state.startswith("gcal_"): self._on_gcal_setup(...); return` — it ran **before** `if not text.startswith("/")` and the `cmd == "/login"` dispatch. Now: flow only consumes non-slash text; a slash command pops the `gcal_*` state, sends "Google setup cancelled…", and falls through to normal dispatch. The OTP branch (`action == "otp"`, ~line 410) is untouched — it legitimately consumes digits and already accepts `/login`.
> - `_startup_status()` (~line 521): the `else` (configured-but-dead token) used to call `_on_setupgoogle(self.chat_ids[0])`. Now sends a warning + `self._gcal_fix_keyboard()` (`{"inline_keyboard":[[{"text":"🔧 Fix Google setup","callback_data":"gc:start"}]]}`). `_notify` gained an optional `reply_markup` arg (~line 1467) so notifications can carry buttons.
> - `_on_gcal_callback` (~line 723): `act = data[3:]` moved to the top; `gc:start` runs `_on_setupgoogle(chat_id)` **before** the "no setup in progress" guard (that button is pressed with no flow open by design). Other `gc:*` actions unchanged.
> - `_otp_from_gmail` (~line 583): after `capture_otp` returns no OTP, `if is_logged_in(get_session(), self.cfg): send "✅ Login successful — session saved."; return True`. `is_logged_in` / `get_session` are already module-level imports from `padel_booking`, so both stay patchable.
> - Harness fix (not a product bug): `ai/verify_t17_otp_capture.py` `BotStub.__init__` needed `self.chat_ids = [1]` and its `tg_case` needed `patch.object(pt, "is_logged_in", lambda s, cfg: False)` — the stub bypasses `PadelBot.__init__`, so the new session probe hit AttributeError / a real network call.
> - There is **no** `/cancel` bot command (cancellation is inline-keyboard only) and the gcal flow never offered one — an early draft of this fix special-cased "/cancel" as flow input; that was wrong and was removed. Do not reintroduce it.
> - Verified: `python -m py_compile` on all 4 modules + both suites; `import padel_telegram` OK; T23 7/7, T17 18/18, T9 ALL PASS.
> - **Next**: commit/push on `fix/ai-login-blocked-by-stale-setup-flow`; after the user merges, write the final `done` log commit on `main`, then `git merge --ff-only origin/main` + `--force-with-lease` re-push of the AI branch. Deploy pattern: wait ~120s for the GitHub Actions image rebuild, then redeploy on ububu.lan and confirm boot log shows `offering re-setup` (not `starting re-setup`) and that `/login` reaches `_on_login`.
>
> **DONE (16-11-2026 08:53)** — merged as `0115346` (PR #13), branch deleted local + remote. One extra step was needed before merging: the PR reported `mergeable_state: dirty` because the two `docs(ai):` log commits (`42a2421`, `fade63d`) were pushed directly to `main` **and** also sat on the AI branch, so the merge conflicted with itself on `ai/AI_TASKLOG.md`. Fixed by `git reset --mixed 694b55d`, restoring `ai/AI_TASKLOG.md` from that commit (leaving only code + tests changed), re-committing as `36b7383`, and `git push --force-with-lease`. **Lesson for future tasks: never leave the task-log commits on the AI branch — put them on the user's branch only, so the PR carries just the code.**
> Remaining follow-up: redeploy on ububu.lan (CI rebuild ~120s) and confirm `/login` works live.

---

## Task T22: Startup UX — rocket Bot-started message + console logs for session/google/slots

- **Status**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span>
- **Type**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">feat</span>
- **Created**: 15-11-2026 23:40:00
- **Last Updated**: 15-11-2026 23:55:00
- **Time Spent**: 15m
- **Branch**: [`main`](https://github.com/HectorBravo/padel_booking_alrayyana)
- **Commit(s)**: [06f7049](https://github.com/HectorBravo/padel_booking_alrayyana/commit/06f7049), [3283b17](https://github.com/HectorBravo/padel_booking_alrayyana/commit/3283b17)
- **Blockers**: none
- **Findings & Notes**:
  - `run()` now sends `self._notify("🚀 Bot started.")` right after the command menu is set — the first chat message at boot (before login/status/banner).
  - `_startup_status()` now also writes console (`_log`) lines: portal session valid/invalid, and Google active / not-configured / configured-but-not-active. The run() banner block logs the desired booking timeslots (or "no preferred slots").
  - Deployed on ububu.lan after a 120s CI wait; live boot log shows: `startup: portal session is valid.` / `startup: Google Calendar active (sync + OTP reading on).` / `startup: desired booking timeslots -> sunday: 19:00, 20:00, 21:00; tuesday: 20:00, 19:00, 21:00`. Container Up/Healthy.
  - Follow-up (`fd9aa8e`, deployed): dropped the redundant "Padel bot started —" prefix from both autobooking banner messages (now start with "🤖 Autobooking is active.") since the 🚀 Bot started message already announces startup. Docstring cleanup `6868c04`.

### User Confirmations

**Confirmed (user provided):**

- (15-11-2026 23:40:00) "Indicate at startup with a Bot started message + rocket emoji; also log session/google/timeslots to the docker console" → implemented.

### Subtasks / Plan

- [x] Add 🚀 Bot started notify at start of run() (`06f7049`)
- [x] Add _log lines for session/google in `_startup_status` + timeslots in run() banner (`3283b17`)

### Full Context Notes for AI Agents

> Startup chat order in `run()` (padel_telegram.py ~line 296+): 🚀 Bot started → `_ensure_login_startup()` → `_startup_status()` (session + google, now also _log'd) → `_start_autobook()` → Padel-bot-started banner (+ timeslots _log). Console log prefix is `[telegram] startup: ...`. Verified live on ububu.lan.

---



- **Status**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span>
- **Type**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">feat</span>
- **Created**: 15-11-2026 23:05:00
- **Last Updated**: 15-11-2026 23:20:00
- **Time Spent**: 25m
- **Branch**: [`main`](https://github.com/HectorBravo/padel_booking_alrayyana)
- **Commit(s)**: [da26a13](https://github.com/HectorBravo/padel_booking_alrayyana/commit/da26a13)
- **Blockers**: none
- **Findings & Notes**:
  - New `/setupgoogle` flow: (1) ask Google email (keep-current / New buttons when already configured), then (2) OAuth authenticate, then (3) list the user's REAL calendars via `GoogleCalendar.list_calendars()` as tap buttons; the tapped calendar id is saved directly to `google_calendar.calendar`. No more typing a calendar name that may not exist.
  - `_on_setupgoogle` → email step. `_gcal_after_email` reuses the saved client pair (or asks for it) then `_gcal_start_auth`. `_gcal_finish_auth` now saves tokens WITHOUT choosing a calendar, then calls `_gcal_calendar_buttons`. New `_gcal_pick_calendar(idx)` saves the chosen id and runs `_gcal_test_and_report`.
  - Calendar buttons use `gc:cal:<index>` (short, safe) with the id list stored in `pending[chat]["_calendars"]`; primary calendar marked ⭐.
  - Removed old methods: `_gcal_calendar_keyboard`, `gc_cfg_calendar`, `_gcal_prefill_calendar`, and the `gcal_calendar` text step + `keep_cal`/`new_cal` callbacks.
  - Verified: all 4 modules compile; T9 ALL PASS; a flow static-check (7 assertions) ALL PASS. Deployed on ububu.lan after a 120s CI wait (`da26a13`): running container has `_gcal_calendar_buttons`=1, `_gcal_pick_calendar`=1, `_gcal_test_and_report`=1, `gc:cal:`=1, and `_gcal_prefill_calendar`=0 (old flow gone); clean boot + autobook active.

### User Confirmations

**Confirmed (user provided):**

- (15-11-2026 23:05:00) "Change setupgoogle to: ask email, then authenticate, then list the user's calendars as buttons to pick an existing one" → implemented.

### Subtasks / Plan

- [x] Rewrite `_on_setupgoogle` (email step only)
- [x] Add `_gcal_after_email`, `_gcal_calendar_buttons`, `_gcal_pick_calendar`, `_gcal_test_and_report`; split `_gcal_finish_auth`
- [x] Remove old calendar-typing methods/steps/callbacks
- [x] Compile + T9 + flow static-check; commit/push (`da26a13`) + redeploy on ububu.lan with proof

### Full Context Notes for AI Agents

> **Purpose**: self-contained recovery knowledge.
>
> Flow state machine (pending[chat]["action"]): `gcal_email` → (validate email) `_gcal_after_email` → if client pair saved: `_gcal_start_auth`; else `gcal_client_id` → `gcal_client_secret` → `_gcal_start_auth`. Auth completes via callback file poll or manual code → `_gcal_finish_auth` (saves tokens, keeps prior calendar id so list works) → `_gcal_calendar_buttons` sets action `gcal_pick` + `_calendars` id list. Button `gc:cal:<i>` → `_gcal_pick_calendar` saves `google_calendar.calendar = <id>`, then `_gcal_test_and_report`.
> - `list_calendars()` (google_calendar.py:161) hits `/users/me/calendarList`, returns `{id, summary, primary}` items using the refresh token.
> - Calendar ids stored raw; `_resolve_calendar_id` uses them directly when they contain `@` or `-`.
> - Editor tool repeatedly failed exact-match on multi-line blocks this session; used PowerShell base64 → file + Python splice scripts (all temp files cleaned up).

---



- **Status**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span>
- **Type**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">feat</span>
- **Created**: 15-11-2026 12:45:00
- **Last Updated**: 15-11-2026 20:55:00
- **Time Spent**: 1h 35m
- **Branch**: [`main`](https://github.com/HectorBravo/padel_booking_alrayyana)
- **Commit(s)**: [be1ab95](https://github.com/HectorBravo/padel_booking_alrayyana/commit/be1ab95), [025a028](https://github.com/HectorBravo/padel_booking_alrayyana/commit/025a028), [7d618e1](https://github.com/HectorBravo/padel_booking_alrayyana/commit/7d618e1), [d63005b](https://github.com/HectorBravo/padel_booking_alrayyana/commit/d63005b)
- **Blockers**: none
- **Findings & Notes**:
  - New `ensure_login(cfg)` in padel_booking.py: if `is_logged_in(get_session(), cfg)` → True (no-op); if Google off → False (leave manual path); else run full OTP flow (`login_start` → Gmail capture → `login_finish`). Reuses `_auto_login` defensively.
  - CLI `main()`: for portal commands (explore/window/slots/book/pick/mybookings/cancel/autobook/keepalive) call `ensure_login(cfg)` before dispatch. Skips login-*/google/telegram.
  - Telegram: new `_ensure_login_startup()` method (silent console log, no chat_id), called in `run()` right before `_start_autobook()`. No-op if already logged in or Google off.
  - Verified with a temp mock suite (`ai/_tmp_verify_startup.py`, since deleted): already-logged-in → True no login; google-off → False; google-on → login_finish called with captured OTP. 3/3 PASS. T17 (18/18) + T9 (ALL PASS) regressions green.
  - Deployed on ububu.lan after CI rebuild: running container has `def ensure_login` and the `_ensure_login_startup()` call before `_start_autobook()`.
  - **Deploy proof (15-11-2026 17:07)**: connected via PuTTY `plink.exe -batch hbravo@ububu.lan` (Pageant agent). `docker compose up --wait` → Pulled fresh image, Recreated + Started + Healthy. Running container `cf7d029a6017` (Up, 8010->8010): grep confirms `ensure_login`=2, `_ensure_login_startup`=2, `needs_session` gate present. Bot log shows clean boot → autobook booking active (valid session at startup).
  - **Forced-relogin-at-boot proof (15-11-2026 17:11)**: deleted `session.json` from volume `padel-booking_padel-data` (sudo), then `docker compose up --wait`. Boot log: `[telegram] no live session at startup — running an automatic login (OTP read from Gmail)...` → OTP sent → Gmail captured 859064 (expired, stale email) → defensive second `_auto_login` captured fresh 690900 → `Login successful. Session saved` → `✅ session established at startup from Gmail` → autobook started. `session.json` recreated, login-status valid=True. Proves the startup OTP-capture path fires when no session exists.
  - Emoji icons on startup-login messages (commits 025a028, 7d618e1, d63005b): the boot relogin now pushes the SAME user-facing chat messages as /login + _otp_from_gmail, with matching emoji: warning sign on the no-session line, email emoji on Gmail-mentioning lines, green tick on the success line. Verified live on ububu.lan (boot 17:38:20): fresh OTP captured first try, Login successful, session valid=True; running container line 490 shows the warning icon.
  - **Bug-hunt round (15-11-2026 20:55)**: audited telegram/gcal/otp/booking workflows and fixed + redeployed (each after a 120s CI wait) four bugs on `main`: (a) `setupgoogle` fresh-setup hint was passed as the reply_markup arg instead of text (`11255ad`); (b) re-setup New-email path dead-ended with no calendar prompt - added `_email_was_new` flag + `gc_cfg_calendar` helper to show the calendar buttons (`abe2fd0`); (c) `_gcal_finish_auth` could clobber a named calendar with an empty string - now only overwrites when non-empty (`405d5d3`); (d) earlier this session: `/status` tri-state mislabeled not-configured as broken, and `test_connection` used naive time + gc.email (the always-invalidated HTTP 400 root cause). AST scan confirmed no other `_send` reply_markup misuse. All modules compile; T9 ALL PASS, T17 18/18; live `/status` test_connection = (True, Google Calendar reachable); container Up/Healthy with clean boot log.

### User Confirmations

**Confirmed (user provided):**

- (15-11-2026 12:45:00) "Add auto-login-on-startup so the bot/CLI triggers the full OTP capture flow when no session exists" → implemented in both entry points.

### Subtasks / Plan

- [x] Add `ensure_login(cfg)` (padel_booking.py)
- [x] Wire into CLI `main()` for portal commands
- [x] Wire `_ensure_login_startup()` into Telegram `run()` before autobook
- [x] Compile + verify suites (T17/T9) + startup mock suite
- [x] Commit/push + redeploy on ububu.lan

### Full Context Notes for AI Agents

> **Purpose**: Self-contained recovery context.
>
> - `ensure_login(cfg)` lives at padel_booking.py ~line 493, right before `get_authenticated_session` (~528). It calls `login_start(cfg)` (which internally already tries `_auto_login` Gmail capture), then defensively re-checks `is_logged_in`, then `_auto_login(cfg)` again. Returns bool.
> - CLI dispatch: after `cfg = load_config(); cmd = sys.argv[1]; args = sys.argv[2:]`, a `needs_session` tuple gate calls `ensure_login(cfg)`. login-start/finish/status, google-*, and telegram are excluded (telegram self-heals via autobook re-login).
> - Telegram `_ensure_login_startup()` at padel_telegram.py ~line 470, called in `run()` (~299) immediately before `self._start_autobook()`. Uses module-level `is_logged_in`, `get_session`, `google_configured`, `login_start`, `capture_otp`, `login_finish` (all imported at top). Silent: logs via `_log`, no `_send`.
> - The autobook loop already had `ensure_session()`→`relogin` mid-run; this addition is the STARTUP proactive login so the first booking attempt doesn't wait for a keepalive failure to trigger re-login.
> - Deploy pattern (unchanged): on `ububu.lan`, after CI rebuild, `docker stop $(docker ps -q --filter "not like=.*")` loop + `docker start <id>`. Container id 94165b3a023f. SSH: `D:\repos\serverconnect\connect.ps1 -C "<remote command>"`.
> - No config keys added; no new imports.

---

## Task T19: setupgoogle defaults, /status Google test, stop retrying booked days


- **Status**: done
- **Type**: feat
- **Created**: 13-11-2026 19:52:03
- **Last Updated**: 13-11-2026 19:55:00
- **Time Spent**: 35m
- **Branch**: `main` (committed directly)
- **Commit(s)**: [6cd1402](https://github.com/HectorBravo/padel_booking_alrayyana/commit/6cd1402) (`feat(bot)` three UX/logic changes)
- **Blockers**: none
- **Findings & Notes**: Three user-reported issues fixed in one commit.

### Subtasks / Plan

- [x] Issue 1 — `/setupgoogle` offers existing email/calendar as defaults when Google already enabled; re-setup skips client_id/secret (already saved) and goes straight to authorize. Empty reply keeps current value, text overrides.
- [x] Issue 2 — `/status` also runs `google_calendar.test_connection(cfg)` (lightweight Calendar list + token exchange) when integration enabled; reports OK or "connection broken -> /setupgoogle". Added `test_connection -> (ok, detail)` (never raises/exits).
- [x] Issue 3 — autobook no longer floods "refreshing in 2 min" for already-booked/away-from-open days. Added `_near_window_open(cfg, date)` band gate + early returns in `run_booking_race`: open+already-booked -> outcome `already_booked` (return immediately); open+no-free-slot or not-opened but AWAY from booking_open_hour (and wait_for_open=True) -> return now; near-open keeps the bounded refresh to absorb server out-of-sync. Callers (`book_day`, `cmd_autobook`) handle new `already_booked` outcome.

### Full Context Notes for AI Agents

> Purpose: Recovery for the three UX/logic changes (commit 6cd1402).

- Issue 1 (padel_telegram.py): `_on_setupgoogle` checks `self.cfg["google_calendar"].enabled` + saved email/calendar; if present sends a 2-step re-setup ("already configured", reply empty=keep, text=override) and pre-fills calendar via `_gcal_prefill_calendar`. `_on_gcal_setup` reads `reuse = bool(data.get("_reuse"))`; on the `gcal_calendar` step, if reuse AND client_id/secret present -> jumps to `_gcal_start_auth` (skips re-pasting credentials). Fresh 4-step flow unchanged.
- Issue 2: new `google_calendar.test_connection(cfg) -> (ok, detail)`; disabled -> `(None,"not configured")`; else refresh token + GET calendar events (maxResults=1); HTTP!=200 -> `(False,...)`; success -> `(True,"Google Calendar reachable")`. Imported into padel_telegram. `_on_status` appends the Google line only when `gc.enabled`.
- Issue 3 (padel_booking.py): new module fn `_near_window_open(cfg, target_date)` — open moment = today's `booking_open_hour` (default 0), +1 day if target is a future date; near-open iff `0 <= elapsed_min <= booking_retry_band_minutes` (default 30). In `run_booking_race` "nothing bookable yet" branch: if `ever_opened` and (`is_day_booked(target_date)` or `already_booked_successfully`) -> return outcome `"already_booked"`; the band-tightening early-returns only apply when `wait_for_open=True` (standalone CLI), so scheduler/tests passing `wait_for_open=False` keep old bounded-wait. New outcomes handled in `book_day()` (~2646, returns True + notify "already booked") and `cmd_autobook()` (~2439, prints + return).
- Why band only when wait_for_open=True: T9's failing scenarios all pass `wait_for_open=False` (simulate server out-of-sync near open, expect waiting). The band is a production tightening for the standalone one-shot; keeping it off in scheduler mode preserves the "keeps trying an unopened day" contract.
- Verification: ai/verify_t9_behaviors.py = ALL PASS (15 checks), ai/verify_t17_otp_capture.py = 18/18, plus a temp check of `_near_window_open` True/False and `test_connection` disabled path (removed after).
- Current state: DONE, committed+pushed (6cd1402); CI rebuild queued. Tune `booking_retry_band_minutes` in config.json to widen/narrow the retry window around open.

## Task T18: Make the Google OAuth callback endpoint work (fix the 502)

- **Status**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span>
- **Type**: <span style="background-color:#9e6a03;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">fix</span>
- **Created**: 13-11-2026 19:21:45
- **Last Updated**: 13-11-2026 19:55:00
- **Time Spent**: 40m
- **Branch**: `main` (committed directly; [PR workflow not used for this fix])
- **Commit(s)**: [d6908b6](https://github.com/HectorBravo/padel_booking_alrayyana/commit/d6908b6aee20216794ecc1183dfff3f1f8bbea2e) (`fix(oauth)` persistent callback)
- **Blockers**: none
- **Findings & Notes**:
  - Symptom: `https://padel-booking.destr0.com/callback` returned **502 Bad Gateway** during Google consent, forcing manual code-paste.
  - Topology (confirmed): nginx on Windows host `huevo.lan` terminates TLS for `padel-booking.destr0.com` and `proxy_pass http://ububu.lan:8010`. The bot runs in docker compose on the **Linux** server `ububu.lan` (`~/repos/dockers/padel-booking/docker-compose.yml`, container `padel-booking-bot`).
  - **Root cause**: the deployed compose had **no `ports:` section at all** → port 8010 was never exposed, nothing listened there (curl `code=000`), so nginx returned 502. The repo's earlier `"443:8010/tcp"` line is invalid TCP mapping and the certbot/HTTPS-in-container path was obsolete (nginx already terminates TLS).
  - **Fix**: (1) compose now maps `ports: ["8010:8010"]`; (2) `google_calendar.serve_callback_forever()` keeps port 8010 answering `/callback` for the whole `telegram`/autobooking process; (3) `start_callback_server` simplified to plain HTTP (dropped HTTPS/cert_dir); (4) `run_google_auth` reuses the persistent listener by polling `oauth_callback.json`, else one-shot; (5) removed obsolete certbot install from Dockerfile + entrypoint.sh block; (6) gitignored `oauth_callback.json`.
  - **Live proof**: pushed → GitHub Action rebuilt image (`hectorbravo/padel_booking_alrayyana` run 43451544076 success, 51s) → redeployed on ububu.lan via `docker compose up` (pull_policy always) → port 8010 LISTEN confirmed; container `/callback?code=PING` = 200; **public** `https://padel-booking.destr0.com/callback?code=PROBE_FINAL` = **200 "Authorized!"** page. Root `/` correctly 404.

### Subtasks / Plan

- [x] Diagnose: read nginx conf (`sites-available/padel-booking.destr0.com.conf`) + connect to ububu.lan (paramiko) — found no `ports:` in deployed compose, nothing on 8010.
- [x] Add persistent callback server (`serve_callback_forever`) + port-in-use detection in google_calendar.py.
- [x] Rewrite `run_google_auth` to poll the handoff file when a listener is already up; one-shot otherwise.
- [x] Wire `cmd_telegram` (the container's long-running entry) to start the callback server.
- [x] Fix compose ports + remove obsolete certbot path (Dockerfile, entrypoint.sh); gitignore `oauth_callback.json`.
- [x] Deploy: commit/push → CI rebuilds image → `docker compose up` on ububu.lan → verify public `/callback` = 200.

### Full Context Notes for AI Agents

> **Purpose**: Self-contained recovery for the OAuth-callback fix.

- **Topology**: Windows host `huevo.lan` runs nginx (`D:\Repos\configs\nginx`, real running conf likely under its own root; user-facing files in `sites-available/` + `sites-enabled-huevo/`). nginx block: `location / { proxy_pass http://ububu.lan:8010; ... X-Forwarded-Proto https }`. TLS certs `certs/fullchain.destr0.com.pem` exist and domain resolves.
- **Linux server** `ububu.lan`: user `hbravo`, in docker group, Ubuntu 24.04 (kernel 6.8). Compose at `/home/hbravo/repos/dockers/padel-booking/docker-compose.yml`. Container `padel-booking-bot`, image `hecbr/padel-booking-alrayyana:latest`, `pull_policy: always`, runs `telegram` (CMD) via entrypoint.sh.
- **Access from Windows**: no sshpass; used Python **paramiko** (`set_missing_host_key_policy(AutoAddPolicy())`, `connect(host, username, password, look_for_keys=False, allow_agent=False)`). Password auth only (no key). This is the reliable non-interactive path here.
- **Docker on ububu.lan is OLD**: `docker ps --format '{{.Name}}'` fails ("can't evaluate field Name"); use plain `docker ps -a | grep`. `ss -ltnp` works; container has NO curl (use python urllib inside).
- **Why 502 not connection-refused-to-browser**: nginx upstream unreachable → nginx renders its own 502 page.
- **Key functions** (google_calendar.py): `start_callback_server(port)` plain HTTP one-shot; `serve_callback_forever(port=8010)` daemon-thread loop writing code to `OAUTH_CALLBACK_FILE` (= HERE/`oauth_callback.json`), guarded by module flag `CALLBACK_SERVER_RUNNING`; `_callback_port_in_use(port)` socket-probe. `run_google_auth`: clears stale file, picks persistent (poll `_wait_for_file`) vs one-shot.
- **padel_booking.py**: `cmd_telegram` calls `from google_calendar import serve_callback_forever; serve_callback_forever()` before `PadelBot(cfg).run()`. Container CMD is `telegram`, so that's the only resident process in production (autobook runs as its thread).
- **CI**: `.github/workflows/docker-publish.yml` builds+pushes on push to main. Watch via `gh run list --limit 1`. Takes ~50s.
- **What did NOT work / was wrong**: certbot-inside-container + `443:8010/tcp` (invalid TCP mapping) + HTTPS-in-container (`HTTPSServer` not in 3.13). All removed. nginx terminates TLS so the bot only needs plain HTTP on 8010.
- **Current state**: DONE & live-proven public 200. If it regresses, first check `docker inspect padel-booking-bot | grep 8010` (port mapping) and `ss -ltnp|grep :8010`, then that the running image has `serve_callback_forever` (`docker exec ... grep -c`).

- **Status**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span>
- **Type**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">feat</span>
- **Created**: 13-11-2026 13:34:10
- **Last Updated**: 13-11-2026 18:45:10
- **Time Spent**: 25m
- **Branch**: [`feat/ai-auto-capture-otp-from-gmail`](https://github.com/HectorBravo/padel_booking_alrayyana/tree/feat/ai-auto-capture-otp-from-gmail) (merged into `main` via [PR #12](https://github.com/HectorBravo/padel_booking_alrayyana/pull/12))
- **Commit(s)**: [f9eb1d1](https://github.com/HectorBravo/padel_booking_alrayyana/commit/f9eb1d1c6b7c4dd462e24475b324e108f8091616) (`docs(ai)` log before push), [45138de](https://github.com/HectorBravo/padel_booking_alrayyana/commit/45138dec9f652079a00b0763b80061fe24e1432d) (feat — Gmail OTP capture), [ab69b07](https://github.com/HectorBravo/padel_booking_alrayyana/commit/ab69b07cf071841e9977c889cc1e83a96a0ebf9a) (`docs(ai)` log for push), merge [eb60198](https://github.com/HectorBravo/padel_booking_alrayyana/commit/eb60198334da47c19f4bb7c869a65f3e14b694e3), [41841b8](https://github.com/HectorBravo/padel_booking_alrayyana/commit/41841b82d9d37175cef4c7211907bdb65a51180b) (helper for re-auth), [6b89614](https://github.com/HectorBravo/padel_booking_alrayyana/commit/6b89614523fd5485918846663c508b053c89d92d) (fix — real email markup + live proof)
- **Blockers**: none (one user action left: re-run `google-auth`, see User Confirmations)
- **Findings & Notes**:
  - New module **`google_otp.py`** (~10.5 KB): `capture_otp(cfg, log, skip)` → `(otp, error)`. Polls Gmail `from:asteco.com` (`maxResults=10`, newest first), requires the sender domain **and** the body phrase `<digits> is your ASTECO OTP` (case-insensitive, `\s+` tolerant), **5 s between checks, 12 attempts max** (~1 minute). Never raises — every caller can degrade to the manual flow.
  - Wired into all four entry points: CLI `login-start` (auto-completes), CLI `login-finish` with no argument (`_auto_login`), Telegram `/login` (new `_otp_from_gmail`) and autobooking `_relogin`. The manual "reply with the code" path stays as fallback everywhere.
  - Stale-code guard: the last code handed out is stored in `last_otp.txt` (gitignored/dockerignored, chmod 600) and skipped on later logins, so an old inbox OTP is never re-submitted; that state keeps polling instead of wasting the fresh login.
  - Body decoding handles base64url (unpadded), nested `multipart/alternative`, and both text/html + text/plain.
  - One consent now covers both integrations: `AUTH_SCOPE = calendar + gmail.readonly` used by `generate_auth_url` **and** `run_google_auth`; `google_otp` refreshes with that scope explicitly so a re-authorised token is upgraded. Verified `generate_auth_url` emits both scopes (`both scopes in url: True`).
  - Tests: new `ai/verify_t17_otp_capture.py` → **17/17 checks**; existing `ai/verify_t9_behaviors.py` → **ALL PASS**; `py_compile` clean for all modules.
  - **Pre-existing issue found (not caused by this task)**: the refresh token in `config.json` is rejected by Google — `POST oauth2.googleapis.com/token` → **400 `invalid_grant`** both with no scope and with `gmail.readonly`. This also breaks the existing Calendar sync (`google-test`). Until re-auth, the new code fails fast with `Re-run 'google-auth' (or /setupgoogle).` and falls back to the manual OTP path — confirmed live. **RESOLVED 13-11-2026 18:41**: user re-authorised (pasted the OAuth code into the exchange helper); granted scopes confirmed `auth/calendar` + `auth/gmail.readonly`, and live capture returned the real OTP.
  - **Live-proof fix**: the real OTP email wraps the code in markup — `<b>315813</b> is your ASTECO OTP` (sender `Asteco <donotreply@asteco.com>`). The original regex required whitespace between digits and "is", so extraction failed (`no_code`) even though the message was found. Fix: bounded gap `(?:\s|</?[a-zA-Z][^>]*>|&\w+;|"|'){0,30}` — matches tags/entities/quotes but not letters, so it cannot skip over the "is" of another sentence. Suite extended with the exact live markup → 18/18.

### User Confirmations

**Pending (awaiting user response):**

- None yet.

**Confirmed (user provided):**

- (13-11-2026 13:44:00) "Merge vs live proof first?" → "Merge PR #12 now, then help me run the google-auth re-authorisation"
- (13-11-2026 18:41:00) Authorization code pasted for the OAuth exchange → tokens exchanged successfully; granted scopes confirmed as `auth/calendar` + `auth/gmail.readonly`; live capture then returned OTP `315813` from the real inbox.

### Full Context Notes for AI Agents

- **Repo**: `D:\Repos\padel_booking_alrayyana`, remote `git@github.com:HectorBravo/padel_booking_alrayyana.git`, base `main` HEAD `b015bac`. Windows/PowerShell, Python 3.13.11. User email `hbravo@cuic.net`.
- **Requirement (user)**: when the Google integration is configured, never ask the user for the OTP — read it from Gmail right after `login_start`: poll every **5 s**, max **12 retries** (≈60 s), messages **from a sender in the `asteco.com` domain**, body containing `<digits> is your ASTECO OTP`. Fall back to asking the user only when Google is not configured or capture fails.
- **Live config state**: `config.json` → `google_calendar = {enabled: true, email: hectorbravogarcia@gmail.com, calendar: Social, client_id/secret/refresh_token set, timezone: Asia/Dubai}`. Portal login email is also `hectorbravogarcia@gmail.com`.
- **BLOCKER FOUND (pre-existing, not caused by this task)**: the stored refresh token is rejected by Google — `POST https://oauth2.googleapis.com/token` returns **400 `invalid_grant` / "Bad Request"** both with no scope and with `gmail.readonly`. This means `google-test` / calendar sync are also broken right now. Re-auth is required: `python padel_booking.py google-auth` (or `/setupgoogle`), which must now request the Gmail scope too so one consent grants Calendar + Gmail read.
- **OTP flow today**: `login_start(cfg)` (`padel_booking.py:371`) posts credentials, parses hidden OTP fields, stashes `pending_login.json`, prints "OTP sent to your email". `login_finish(cfg, otp)` (`:415`) verifies. CLI `cmd_login_finish` (`:478`) requires the OTP as argv. Telegram `_on_login` (`padel_telegram.py:468`) sets `pending[chat]={"action":"otp"}` and waits for a text message; `_relogin` (`:1136`) does the same for background re-login with a 900 s wait.
- **Design**: new module `google_otp.py` (Gmail read via the existing OAuth client + `curl_cffi`, no new deps). `capture_otp(cfg)` → 5 s × 12 poll loop, newest-first Gmail search (`from:asteco.com`), extracts the code with a case-insensitive regex on `<digits> is your ASTECO OTP`; ignores codes already tried (dedupe via `last_otp.txt`). Returns `(otp, error)`.
- **OAuth**: add `GMAIL_SCOPE = https://www.googleapis.com/auth/gmail.readonly` and request `SCOPE + " " + GMAIL_SCOPE` in the auth URL and in the refresh call, so a single consent covers Calendar + Gmail. Refresh must keep sending the scope explicitly (Google returns only the granted subset).
- **CLI**: `login-finish` with no argument → auto-capture when Google is enabled; `login-start` also auto-completes when Google is configured (so `login-start && slots` works unattended), and falls back to printing the manual instructions.
- **Telegram**: `_on_login` / `_relogin` try Gmail capture first (notify "reading your inbox"), keep the manual OTP path as fallback.
- **Test harness**: `ai/verify_t17_otp_capture.py` — patches the token/Gmail HTTP calls with synthetic OTP messages and asserts parse/filter/retry/fallback behaviour; plus `ai/verify_t9_behaviors.py` regression run.
- **NEXT (user action, to prove it live)**: ~~re-authorise Google~~ **DONE 13-11-2026 18:41** — user pasted the OAuth code; `ai/google_auth_reauth.py` exchanged it and config.json now holds a fresh refresh token with Calendar + Gmail read scopes. Live proof succeeded (OTP `315813`). If Google ever revokes again (`invalid_grant`), re-run `google-auth` / `/setupgoogle`; revoke at https://myaccount.google.com/permissions if consent is cached without a refresh_token.

### Subtasks / Plan

- [x] Create `google_otp.py`: token refresh with Calendar+Gmail scopes, Gmail search for `from:asteco.com`, OTP regex `<digits> is your ASTECO OTP`, 5 s × 12 retry loop, dedupe of already-used codes
- [x] Add `GMAIL_SCOPE` to `google_calendar.py` auth URL + token refresh so one consent covers Calendar + Gmail read (`AUTH_SCOPE`)
- [x] Wire CLI: `login-start` auto-completes when Google is enabled; `login-finish` without an argument auto-captures (`_auto_login`)
- [x] Wire Telegram `_on_login` (new `_otp_from_gmail`) and `_relogin` to capture from Gmail first, manual OTP reply as fallback
- [x] Update docs + packaging: README, TELEGRAM.md, `.gitignore`/`.dockerignore` (`last_otp.txt`), `Dockerfile` + `entrypoint.sh` (ship `google_otp.py`)
- [x] Write `ai/verify_t17_otp_capture.py` (synthetic Gmail responses) and make it pass → **17/17 checks**
- [x] Run existing `ai/verify_t9_behaviors.py` regression → **ALL PASS**
- [x] Live proof against the real mailbox → **OTP `315813` captured** (the 3-hours-ago email); required one fix: the portal wraps the code in HTML (`<b>315813</b> is your ASTECO OTP`), which the plain `\s+` gap regex missed → gap now tolerates tags/entities/quotes (commit [6b89614](https://github.com/HectorBravo/padel_booking_alrayyana/commit/6b89614523fd5485918846663c508b053c89d92d)); suite now **18/18**
- [x] Commit + push branch, open PR → [`feat/ai-auto-capture-otp-from-gmail`](https://github.com/HectorBravo/padel_booking_alrayyana/tree/feat/ai-auto-capture-otp-from-gmail), [PR #12](https://github.com/HectorBravo/padel_booking_alrayyana/pull/12)

---

| 11-11-2026 02:50:02 | [T15: Merge every AI branch into main and delete them](#task-t15-merge-every-ai-branch-into-main-and-delete-them) | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span> | <span style="background-color:#57606a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">chore</span> | 5/5 | 4m | none |

| 11-11-2026 01:14:00 | [T14: Stop POSTing unopened dates (fail-closed) and keep retrying them](#task-t14-stop-posting-unopened-dates-fail-closed-and-keep-retrying-them) | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span> | <span style="background-color:#da3633;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">hotfix</span> | 6/6 | 11m | none |
| 11-11-2026 01:04:22 | [T13: Treat the picker as the real window limit (7-day assumption is wrong)](#task-t13-treat-the-picker-as-the-real-window-limit-7-day-assumption-is-wrong) | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span> | <span style="background-color:#da3633;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">hotfix</span> | 5/6 | 14m | none |
| 11-11-2026 00:50:01 | [T12: Telegram notice on first retry while a day is unavailable](#task-t12-telegram-notice-on-first-retry-while-a-day-is-unavailable) | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span> | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">feat</span> | 4/5 | 8m | none |
| 11-11-2026 00:52:00 | [T11: Default booking_max_wait_minutes to 12h](#task-t11-default-booking_max_wait_minutes-to-12h) | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span> | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">feat</span> | 4/5 | 7m | none |
| 11-11-2026 00:28:43 | [T10: Deploy the T9 fix — it was never merged](#task-t10-deploy-the-t9-fix--it-was-never-merged) | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span> | <span style="background-color:#da3633;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">hotfix</span> | 5/6 | 5m | none |
| 09-11-2026 00:41:06 | [T9: Derive bookable days from the portal](#task-t9-derive-bookable-days-from-the-portal) | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span> | <span style="background-color:#9e6a03;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">fix</span> | 20/20 | 25m | none |

> ✅ **19 completed task(s)** — [View completed tasks](#completed-tasks)

---

## Task T16: Diagnose the unreadable picker that makes waits pointless

- **Status**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span>
- **Type**: <span style="background-color:#9e6a03;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">fix</span>
- **Created**: 11-11-2026 04:23:36
- **Last Updated**: 11-11-2026 04:28:30
- **Time Spent**: 12m
- **Branch**: [`fix/ai-log-unreadable-picker-dump`](https://github.com/HectorBravo/padel_booking_alrayyana/tree/fix/ai-log-unreadable-picker-dump) (merged into `main`, branch deleted)
- **Commit(s)**: [90131f6](https://github.com/HectorBravo/padel_booking_alrayyana/commit/90131f6) (`docs(ai)` log before push), [bcb3967](https://github.com/HectorBravo/padel_booking_alrayyana/commit/bcb3967) (diag dump + warn), [8cf58a5](https://github.com/HectorBravo/padel_booking_alrayyana/commit/8cf58a5) (`docs(ai)` log T16 done), [725e46f](https://github.com/HectorBravo/padel_booking_alrayyana/commit/725e46f) (datepicker startDate/endDate range fix — now `main` tip)
- **Blockers**: none
- **Findings & Notes**:
  - User's log line `Tue 17 Nov is inside the portal's window (offered so far up to Tue 17 Nov, picker unreadable) ... refreshing in 2 min` while the browser shows 17 Nov bookable.
  - The `, picker unreadable` clause at `padel_booking.py:2207` prints **only when `offered == []`**, so `portal_bookable_dates()` returned empty → `parse_booking_dates()` found no parsable date `<select>`.
  - With an empty picker, `run_booking_race` step (2) (`opened = bool(offered) and target_date in offered`, `:2179`) can never be True → slots are not even queried → the race waits forever. Fail-closed is correct for *unknown*, but here it is a **false negative**: the day IS offered, parsing just fails.
  - This is the residual risk already noted at T14's log (`ai/AI_TASKLOG.md:170`). Nothing in the log says *why* parsing failed → undiagnosable from the host.
  - **Fix (diagnostic first)**: `_dump_unreadable_picker(html)` writes `diag_booking_page.html` + a WARNING line on every empty picker read, so the live markup can be inspected and the parser corrected. Dump failure is non-fatal (`except Exception`, not `OSError` — cp1252 consoles raise UnicodeEncodeError on the emoji).
  - **RESOLVED root cause (live proof, OTP 817471)**: the booking page has **NO date `<select>`**. The picker is a jQuery UI datepicker bound to `<input type="text" id="check_in_date" name="check_in_date">` (`diag_booking_page.html:925-927`), configured at `:1173-1179` with `startDate:new Date($('#startDate').val())`, `endDate:new Date($('#endDate').val())`. The bounds sit in hidden inputs at `:1027-1030`: `startDate=Nov 11, 2026`, `endDate=Nov 17, 2026`, `interval=1`, `recurrence=W`. So 17 Nov IS selectable today — exactly what the browser showed.
  - **Fix (parser)**: added `_datepicker_range(soup, today)` that reads those hidden `startDate`/`endDate` inputs and returns the inclusive enumerated range; `parse_booking_dates` now prefers it over `<select>` parsing. Live `window` now prints "Opened by 'Check in date': Wed 11 Nov .. Tue 17 Nov 2026 (7 of 7 days)"; `autobook 2026-11-17 --dry-run` selects 19:00 immediately (no wait, no "picker unreadable").

### User Confirmations

**Pending (awaiting user response):**

- None yet.

**Confirmed (user provided):**

- (11-11-2026 04:23:36) "Why does the bot keep waiting when the day is already bookable?" → Because the picker read returned empty; root cause = `parse_booking_dates` not matching the live markup. Confirmed by code path, pending live proof.
- (11-11-2026 04:26:43) "OTP 794343" → expired at login-finish ("OTP expired"); re-ran login-start.
- (11-11-2026 04:27:08) "OTP 817471" → login succeeded, session saved; enabled the live page dump.

### Subtasks / Plan

- [x] Trace the log line to its branch and prove `offered == []` — done (`:2207` clause + `:2179` opened check)
- [x] Add `_dump_unreadable_picker(html)` in `portal_bookable_dates` (empty-read path) + WARNING log line (cp1252-safe)
- [x] Add `diag_booking_page.html` to `.gitignore` (Debug artifacts block)
- [x] Obtain a live booking page (OTP 817471) and confirm why parsing fails — the picker is a jQuery UI datepicker on `<input id="check_in_date">`, NOT a `<select>`; bounds in hidden `startDate`/`endDate` inputs (`diag_booking_page.html:925-927,1027-1030,1173-1179`)
- [x] Correct `parse_booking_dates` for the real markup (added `_datepicker_range()` enumerating startDate..endDate; prefers it over `<select>`), then re-run `ai/verify_t9_behaviors.py` → ALL PASS (14 checks incl. new `datepicker_range_check`)

### Full Context Notes for AI Agents

> **Purpose**: Self-contained knowledge base to resume without other context.

**Symptom (user's log)**: `[11/11/2026 04:18:30] [bot] Tue 17 Nov is inside the portal's window (offered so far up to Tue 17 Nov, picker unreadable) but the portal has not opened this date yet; refreshing in 2 min...` — meanwhile the browser shows 17 Nov selectable.

**Code path that produces it** (`padel_booking.py`, current line numbers after T16 edits):
- `run_booking_race:2065` loop → `offered = portal_bookable_dates(cfg)` at `:2167`.
- Hard ceiling check `if offered and target_date > max(offered): return not_yet_open` at `:2170` — skipped when `offered == []` (unreadable must NOT refuse, comment at `:2162-2165`).
- `opened = bool(offered) and target_date in offered` at `:2179` → **False** for empty picker. The slot query (`get_available_slots`, `:2184`) is inside `if opened:` so it never runs; hence the log says "not opened this date yet" even though the day is open.
- Wait branch prints at `:2205-2208`; `{'' if offered else ', picker unreadable'}` at `:2207` is the ONLY place that clause appears → proves `offered == []`.

**Why empty**: `parse_booking_dates(html, today)` (was `:731`, now ~`:755` after this insertion; body unchanged) returns `[]` when: no `<select>` tags at all (`if not selects: return []`), or the named date select (`_BOOKABLE_DATE_SELECT_NAMES`) yields no parsable option, or unnamed selects' options don't match `_option_date` formats in `_BOOKABLE_DATE_FORMATS` / `_BOOKABLE_DAYMONTH_FORMATS`.
- `portal_bookable_dates(cfg, force)` (was `:798`, now ~`:822`) → now calls `_dump_unreadable_picker(html)` when `dates == []`; `_dump_unreadable_picker` inserted just before it (writes `HERE / "diag_booking_page.html"`, chmod 600, prints WARNING with `ts_prefix()`).
- Cache: `_BOOKABLE_DATES_TTL` (60 s) via `_bookable_dates_cached/_store`.

**T13/T14 context (do not regress)**: picker's last date is the real ceiling; POST requires positive picker membership (`submit_booking_fast` only when `target_date in portal_bookable_dates(cfg)` with a NON-EMPTY list). Empty picker = "unknown" → arithmetic fallback + never POST. Do NOT weaken that to "assume bookable".

**Environment facts**:
- Repo: `D:\Repos\padel_booking_alrayyana`, remote `git@github.com:HectorBravo/padel_booking_alrayyana.git`, base branch `main` tip `11c6ff1`. User email `hbravo@cuic.net`. Windows/PowerShell; console cp1252 → emoji prints raise UnicodeEncodeError unless the stream is reconfigured (`padel_telegram.py:64` does it for the bot, CLI does not).
- Live config `config.json` (gitignored): `asset_booking_id="370"`, no `booking_window_days` → 7. Preferred slots: sunday/tuesday/saturday.
- Portal: `BASE_URL=https://myportal.asteco.com`; booking page `GET /asset/assetbooking/370` (`fetch_booking_page`); slot API does NOT validate dates (why the 303 bug happened).

**Live-proof status**: `session.json` dated 27/09/2026 → `login-status` prints `Session valid: False`. `login-start` was run at 11-11-2026 04:21:39 and stashed `pending_login.json`; **`login_finish` needs the OTP number from the user's email** — cannot be obtained non-interactively. Once provided:
```
python padel_booking.py login-finish <OTP>
python padel_booking.py window        # force=True read → dumps diag_booking_page.html if picker empty
```
Then inspect `diag_booking_page.html` for `<select>` markup and fix `_option_date`/`parse_booking_dates`.

**Verification**: `python -m py_compile padel_booking.py` (passes); `python ai/verify_t9_behaviors.py` must still end in `ALL PASS` (13 checks; patches `portal_bookable_dates`, so the new dump hook is not exercised by it — safe).

**Deploy path**: branch → PR → merge to `main` → `.github/workflows/docker-publish.yml` rebuilds `hecbr/padel-booking-alrayyana`; host runs `docker compose pull && docker compose up -d` (entrypoint re-copies `.py` from `/opt/padel`). Docker is NOT installed on this dev box.

**Next step on resume**: if the OTP answer arrived, run login-finish + window, read `diag_booking_page.html`, and correct the parser; else finish log/commit/push for the diagnostic part only.

---

## Task T15: Merge every AI branch into main and delete them

- **Status**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span>
- **Type**: <span style="background-color:#57606a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">chore</span>
- **Created**: 11-11-2026 02:50:02
- **Last Updated**: 11-11-2026 02:54:00
- **Time Spent**: 4m
- **Branch**: `main` (user's current branch — log only; the merges landed on `main`)
- **Commit(s)**: [3baf991](https://github.com/HectorBravo/padel_booking_alrayyana/commit/3baf991) (`docs/ai-explain-taken-slot-retry`), [d994ce0](https://github.com/HectorBravo/padel_booking_alrayyana/commit/d994ce0) (`test/ai-config-override-check`)
- **Blockers**: none
- **Findings & Notes**:
  - Seven local AI branches existed. Five were already fully contained in `main` (`ahead=0 behind=0`) — merged via earlier PRs, so nothing to merge: `feat/ai-booking-max-wait-12h`, `feat/ai-retry-notice-once-per-day`, `fix/ai-deploy-portal-bookable-dates`, `fix/ai-fail-closed-unreadable-picker`, `fix/ai-never-book-unoffered-date`.
  - Only two carried commits: `docs/ai-explain-taken-slot-retry` (`f81c922`, `taken_forever_check`) and `test/ai-config-override-check` (`e7e15ed`, `config_override_check`). Both added a test to `ai/verify_t9_behaviors.py` at the same insertion point (end of file, before the final summary print), which is why both merges conflicted.
  - Resolution: keep **both** checks in main's ordering. After `3baf991` the file contains all 13 checks; `d994ce0` was merged with `-X ours` because its content (`config_override_check`) was already present from `3baf991` — the merge only records the branch history.
  - Verified after each merge: `python ai/verify_t9_behaviors.py` → **ALL PASS** (13 checks, no FAIL).
  - All seven branches (local + remote) deleted after the merges; `origin/fix/ai-portal-bookable-dates` (T9's branch, merged via PR #7) was pruned too.

### User Confirmations

**Pending (awaiting user response):**

- None yet.

**Confirmed (user provided):**

- (11-11-2026 02:50:02) "merge all the ai branches into main, update tasklist and delete them after" → merged on `main` directly, then deleted local + remote branches.

### Subtasks / Plan

- [x] Subtask 1 — Inventory AI branches; determine which are ahead of `main`
- [x] Subtask 2 — Merge `docs/ai-explain-taken-slot-retry` (conflict in `ai/verify_t9_behaviors.py`, resolved keeping both checks)
- [x] Subtask 3 — Merge `test/ai-config-override-check` (`-X ours`; content already integrated by subtask 2)
- [x] Subtask 4 — Re-run `ai/verify_t9_behaviors.py` after each merge → ALL PASS (13 checks)
- [x] Subtask 5 — Delete all seven AI branches locally and on `origin`

### Full Context Notes for AI Agents

> **Purpose**: self-contained recovery notes. Read only if resuming or repeating this chore.

**Repo / remote**: `git@github.com:HectorBravo/padel_booking_alrayyana.git` → https://github.com/HectorBravo/padel_booking_alrayyana (strip `.git`). User's branch: `main`. Working tree clean before starting.

**Branch inventory at start** (`git rev-list --count main..<branch>` / `<branch>..main`):

| Branch | ahead | behind | Action taken |
|--------|-------|--------|--------------|
| `docs/ai-explain-taken-slot-retry` | 1 (`f81c922`) | 1 | merged → `3baf991` |
| `test/ai-config-override-check` | 1 (`e7e15ed`) | 1 | merged → `d994ce0` |
| `feat/ai-booking-max-wait-12h` | 0 | 0 | already in main; deleted |
| `feat/ai-retry-notice-once-per-day` | 0 | 0 | already in main; deleted |
| `fix/ai-deploy-portal-bookable-dates` | 0 | 0 | already in main; deleted |
| `fix/ai-fail-closed-unreadable-picker` | 0 | 0 | already in main (PR #11); deleted |
| `fix/ai-never-book-unoffered-date` | 0 | 0 | already in main; deleted |

**The conflict and its exact shape**: both branches appended a check function to the *end* of `ai/verify_t9_behaviors.py`, immediately before:

```python
print("\n" + ("ALL PASS" if all(results) else "FAILURES PRESENT"))
sys.exit(0 if all(results) else 1)
```

`ort` therefore produced a single conflict hunk whose HEAD side held everything main already had (`default_wait_check`, `config_override_check`, `retry_notice_check`, …, `scheduler_retries_until_open_check`) and whose branch side held only the new check. Correct resolution = **keep HEAD's body + insert the branch's check before the final print**, i.e. both checks coexist; there is no semantic choice to make.

**Gotcha hit**: `git checkout --ours <file>` during a conflicted merge restores the *merged/conflicted* blob (with markers), not a clean file — it left `<<<<<<< HEAD` in place and broke parsing. The reliable recovery was `git merge --abort` followed by an explicit strategy (`-X ours`). Do not use `checkout --ours` to "clean" a conflicted file.

**Second gotcha**: `.git/index.lock` appeared transiently mid-sequence (IDE auto-checkpoint commits — see the `cline checkpoint …` refs in `git log --all`). No stale lock needed removing; it cleared by itself, and no git process was running (`Get-Process git*` empty). Retry rather than deleting locks blindly.

**Post-merge state to expect**: `main` tip = the T15 task-log commit on top of `d994ce0`; `ai/verify_t9_behaviors.py` has 13 checks and prints `ALL PASS`; only `main` remains as a local branch, and remote branches are `main` + any non-AI ones.

**Verification command**: `python ai/verify_t9_behaviors.py` (must end in `ALL PASS`; it imports `padel_booking`/`padel_telegram`, so run from the repo root).

---

## Task T14: Stop POSTing unopened dates (fail-closed) and keep retrying them

- **Status**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span>
- **Type**: <span style="background-color:#da3633;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">hotfix</span>
- **Created**: 11-11-2026 01:14:00
- **Last Updated**: 11-11-2026 01:26:00
- **Time Spent**: 11m
- **Branch**: [`fix/ai-fail-closed-unreadable-picker`](https://github.com/HectorBravo/padel_booking_alrayyana/tree/fix/ai-fail-closed-unreadable-picker)
- **Commit(s)**: [9ad4b0c](https://github.com/HectorBravo/padel_booking_alrayyana/commit/9ad4b0c1474830c813e6f7b9c81898201cc87dbf) (merge [bf1baa7](https://github.com/HectorBravo/padel_booking_alrayyana/commit/bf1baa7656f024da16d9832aff06d59a0f368f56), PR #11)
- **Blockers**: none
- **Findings & Notes**:
  - User's 4th report of the same symptom: `*** FAILED to book 19:00-20:00 ... booking not confirmed (slots still available, HTTP 303)` for Tue 17 Nov while the "Check in date" dropdown did not offer it.
  - **T13's diagnosis was incomplete.** T13 made the picker's last option the ceiling but kept a fallback: an *empty/unreadable* picker fell back to the arithmetic bound, and `opened = not offered or target in offered` treated "unreadable" as bookable. That path still POSTs.
  - Proof by differential test (temp script, since removed): with the picker read returning `[]`, BOTH the previously deployed code (9231ec6) and T13's main produced `posts=1 outcome=slot_found` → the doomed POST. With the picker returning 10..16 Nov, both produced `posts=0`. So the empty-picker path is the only one that reproduces the user's log line.
  - **Root cause**: the slot endpoint does not validate dates — it answers with free slots for days the form has no `<option>` for. Any "assume bookable" branch therefore produces a POST the portal rejects with HTTP 303.
  - **Fix = fail closed.** A POST now requires *positive* proof: `opened = bool(offered) and target_date in offered`. Unknown (empty picker) keeps the race waiting and never submits.
  - Kept the user's core requirement intact: an unopened future date is now `not_yet_open` (retry later), **not** `outside_window` (give up). Removed `effective_bookable_last()` so no API implies the arithmetic bound authorises a POST.

### User Confirmations

**Pending (awaiting user response):**

- None yet.

**Confirmed (user provided):**

- (11-11-2026 01:14:00) "The window for the 17th is not yet open but I want it to try to book as it's in the next 7 days as agreed... keep trying for up to 12 hours" → implemented: never POST an unlisted date, but wait and re-attempt until the portal opens it.

### Subtasks / Plan

- [x] Reproduce which code path POSTs an unopened date (differential test across versions)
- [x] Make booking fail-closed: require positive picker confirmation before any POST
- [x] Keep waiting behaviour for unopened future dates (`not_yet_open`, not `outside_window`)
- [x] Fix scheduler trigger (3) so unlisted desired days are re-attempted on a cadence
- [x] Add regression tests (empty picker never posts but keeps waiting; opens later → books; scheduler retries → books)
- [x] Record the real commit hash after push

### Full Context Notes for AI Agents

> **Purpose**: self-contained recovery knowledge.

**The invariant that must never be broken again**: `submit_booking_fast` may
only be called when `target_date in portal_bookable_dates(cfg)` is True with a
NON-EMPTY list. The slot endpoint (`get_available_slots`) returns plausible free
slots for dates the picker has no option for, so trusting it yields HTTP 303
"booking not confirmed (slots still available)". Every previous regression
(T9, T13) came from an "assume bookable when unknown" branch.

**Key locations in `padel_booking.py`**:
- `run_booking_race` steps (1)/(2)/(3) (~line 2178+): the fail-closed gate —
  `opened = bool(offered) and target_date in offered`;
  `if offered and target_date > max(offered): return "not_yet_open"`.
- `window_limit()` (~860): arithmetic bounds only; docstring now states it is
  never permission to POST. `effective_bookable_last` was DELETED (dead + misleading).
- `book_day` outcome handling (~2468): `outside_window`/`not_yet_open` share one
  branch; past-date vs not-yet-open distinguished via
  `target_day = getattr(target, "date", lambda: target)()` (book_day receives a
  `date`, so `.date()` raised AttributeError — caught by the scheduler test).
- `run_autobook_loop` (~2580+): new `retry_wait_secs = max(60, slot_retry_minutes*60)`
  and `last_pending_try`; trigger (3) is now `elif fresh or due:` with
  `pending_now = [d for d in candidates if d not in window_dates]` and
  `todo = sorted(set(fresh) | (set(pending_now) if due else set()))`.

**Second real bug found while verifying**: trigger (3) only re-attempted dates
that appeared in the picker (`d not in last_window`). A date the portal never
lists is never "fresh", so it was never retried — which would have silently
broken "keep trying until it opens" even after the race fix. The pending-day
cadence fixes it without hammering the portal every 0.5s.

**Verification**: `python ai/verify_t9_behaviors.py` → ALL PASS (14 checks),
exit 0. New checks: unreadable picker never posts but waits 3x120s; portal opens
the day mid-wait → books once; scheduler retries an unopened day then books it
(posts=1). `py_compile` clean for padel_booking.py, padel_telegram.py, harness.

**Deployment**: merge to main → GH Actions rebuilds
`hecbr/padel-booking-alrayyana:latest` → user must run
`docker compose pull && docker compose up -d` on the production host (docker is
NOT installed on this dev box).

**Known residual risk**: if the portal's picker becomes permanently unreadable,
the bot waits and never books (fail-closed by design). `/window` prints the
parsed picker so an operator can spot a parsing break immediately.

---

## Task T9: Derive bookable days from the portal

- **Status**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span>
- **Type**: <span style="background-color:#9e6a03;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">fix</span>
- **Created**: 09-11-2026 00:41:06
- **Last Updated**: 09-11-2026 01:14:00
- **Time Spent**: 25m
- **Branch**: [`fix/ai-portal-bookable-dates`](https://github.com/HectorBravo/padel_booking_alrayyana/tree/fix/ai-portal-bookable-dates)
- **Commit(s)**: code [fefb096](https://github.com/HectorBravo/padel_booking_alrayyana/commit/fefb0964aef88a0168f7f44bd9a82eb5ea0bee63) (round 1 — portal-derived dates, 2-min refresh) and [9d0d2c4](https://github.com/HectorBravo/padel_booking_alrayyana/commit/9d0d2c48c033b8a0d54689ef27f90297d1a2c2fc) (round 2 — 7-day hard limit, wait-until-open), both on the AI branch · log [535741a](https://github.com/HectorBravo/padel_booking_alrayyana/commit/535741a03f5f682ba486fde7efbd3f9801c93c79), [db36422](https://github.com/HectorBravo/padel_booking_alrayyana/commit/db364228038f815f886c53fb8ba7267950bcaa2e), [1b7311b](https://github.com/HectorBravo/padel_booking_alrayyana/commit/1b7311b), [5c53bae](https://github.com/HectorBravo/padel_booking_alrayyana/commit/5c53bae) on `main` and [bf6d822](https://github.com/HectorBravo/padel_booking_alrayyana/commit/bf6d822) on the AI branch — **merged to `main`** on 11-11-2026 via [PR #7](https://github.com/HectorBravo/padel_booking_alrayyana/pull/7) (merge `33d261b`), which rebuilt the production image; see [T10](#task-t10-deploy-the-t9-fix--it-was-never-merged). Until then the code existed only on the AI branch, so production never changed.
- **Blockers**: none
- **Findings & Notes**:
  - **Symptom (user report, 09-11-2026)**: the portal's "Check in date" dropdown at `/asset/assetbooking/370` only offers dates up to **14 Nov 2026**, but the Telegram bot offers **15 Nov** and the booking then fails.
  - **Root cause**: the bookable window is never read from the portal — `today .. today+6` is *assumed* in four places: `padel_telegram.py:_show_date_keyboard:686-694` (`for i in range(7)`), `padel_booking.py:cmd_pick:1519-1528`, `padel_booking.py:bookable_targets:1649-1692` (`open_dt = (candidate-6d) @ booking_open_hour`) and `run_autobook_loop:2152` (`new_day = now.date() + timedelta(days=6)`). Today's window is only 6 days wide (ends today+5), so the +6 assumption is simply wrong.
  - **Why the failure is nearly silent**: the *slot* endpoint `ajaxctrl/getAmenityBookingSlot` does not validate the date, so slots are shown for 15 Nov and the date is offered; the *booking* POST `/asset/assetbooking/{id}` with `check_in_date="Nov 15, 2026"` is rejected because that value is not one of the form's options. `_do_book:920-974` verifies with `ok = len(verify_slots) == 0` → False → "❌ Booking FAILED".
  - **Fix direction (user instruction)**: the authoritative bookable dates are the `<option>`s of the "Check in date" `<select>` rendered by the booking page (already fetched for `parse_booking_meta`); drive every decision from them instead of arithmetic.
  - **Retry strategy (user instruction)**: refresh every **2 minutes** while the target day IS offered but its slots are not open/available yet; stop as soon as a preferred slot is bookable (book + report); stop **immediately** when the portal does not offer the day at all.
  - **Refined by the user (09-11-2026 01:07)**: the "not offered → stop now" rule was too strong. The **7-day rolling window is the hard limit**; a target day inside it that the dropdown has not opened yet means "not open yet" and must keep the same 2-minute refresh (including at program start). Only a day **beyond** the 7-day window stops.
  - **New outcomes in `run_booking_race`**: `slot_found` / `not_yet_open` (in window, picker never opened it within the bound — nothing attempted) / `no_slot` (offered, never free) / `outside_window` (beyond the window — stop as of now) / `stopped` (`/stopautobook`).
  - **`bookable_targets` no longer filters unoffered days out**: candidacy = each target weekday's occurrence inside the 7-day window; the picker only orders the attempts (opened dates freshest-first, then unopened dates nearest-first), so a start-up with a closed target day still attempts it through the wait loop.
  - **Session stays alive during long waits**: `run_booking_race` gained an `on_wait` callback that the autobooking loop passes as `keepalive(cfg)`, run before every refresh — an hours-long wait for a date to open can no longer end on an expired session.
  - **Manual paths refuse but explain the difference**: `cmd_slots`/`cmd_book`/`cmd_pick` and the bot's `_show_slots`/`_show_date_keyboard`/`_on_autobook` distinguish "outside the 7-day window (can never be booked now)" from "inside the window, the portal has not opened it yet (autobooking will wait for it)". A manual submit of an unopened date would still be rejected by the portal, so it is refused before wasting a request.
  - **Both views are now visible**: `python3 padel_booking.py window` and the bot's `/window` print the rolling window (today .. today+6) and mark each date `bookable` or `NOT OPEN YET`.
  - **Degraded mode kept on purpose**: a page with no parsable date `<select>` yields `[]` and callers fall back to the old arithmetic with a logged warning — a changed portal markup must never block booking.
  - **Not verified live**: `config.json` is gitignored/absent here, so no portal request is possible from this machine; verification = `py_compile` + synthetic-HTML harness. Real `<select>` markup still needs confirming (see pending item).

### User Confirmations

**Pending (awaiting user response):**

- [ ] (09-11-2026 00:55:00) Exact markup of the portal's date picker — is "Check in date" a `<select name="check_in_date">` with `<option value="Nov 15, 2026">`? A saved `booking_page.html` or the DevTools `outerHTML` pins the parser; it is currently structure-agnostic and format-tolerant.
- [ ] (09-11-2026 00:55:00) Retry defaults `slot_retry_minutes = 2` (per instruction) and `booking_max_wait_minutes = 360` cap — confirm the 6h bound is acceptable (it only prevents an endless loop).
- [ ] (09-11-2026 01:14:00) Merge `fix/ai-portal-bookable-dates` (tip `9d0d2c4`) into `main` — until it is merged the code is only on the AI branch, so T9 stays `in_progress` and its `tree/fix/ai-portal-bookable-dates` badge 404s once the branch is deleted.

**Confirmed (user provided):**

- (09-11-2026 00:41:06) "bookable days should be obtained from the portal itself … the dates selectable from the 'Check in date' at `/asset/assetbooking/370`" → bookability comes from the portal page, not `booking_open_hour`/+6 arithmetic.
- (09-11-2026 00:41:06) "whenever the booking window is not open, refresh every 2 minutes until success … if the day is bookable and the slot is available, stop and indicate it; if the day is not bookable, stop as of now" → tri-state: BOOK (stop) / WAIT 2 min (offered, no slot yet) / NOT BOOKABLE (stop now).
- (09-11-2026 01:07:00) "If I start the program and the booking window is not yet open for the target days, it should also go into the same process … the booking window is 7 days, but the portal might not report all days bookable from the dropdown. In those cases it should keep trying" → **refines** the stop rule: the 7-day rolling window is the hard limit; in-window-but-unlisted means "not open yet" → keep the 2-min refresh (startup included); only out-of-window stops as of now.

### Follow-up plan (clarification of 09-11-2026 01:07 — done on the same branch)

- [x] `booking_window_days` config (default 7) + `rolling_window_dates()` / `window_limit()` / `in_booking_window()` — the hard "can ever be booked now" bounds
- [x] `bookable_targets` redefined: candidacy = each target weekday's occurrence inside the 7-day window; the picker only orders attempts (opened freshest-first, then unopened nearest-first)
- [x] `run_booking_race` redefined: hard window check first (`outside_window` → stop now), then picker (`not_yet_open` → keep refreshing), then slots (`no_slot`); `slot_found` books and stops; `on_wait` keeps the session alive
- [x] Startup trigger (1) attempts unopened in-window days and logs "portal has not opened … yet — waiting"
- [x] Honest outcome messages in `book_day` (`outside_window` / `not_yet_open`) and in `cmd_autobook` (exit 2 = outside, exit 3 = not open yet, exit 1 = no slot)
- [x] Manual paths split the same way: `cmd_slots`, `cmd_book`, `cmd_pick`, `_show_slots`, `_on_autobook`, `_do_book` failure note
- [x] `cmd_window` + `/window` print BOTH views (rolling window and opened dates, `NOT OPEN YET` markers)
- [x] Docs: `booking_window_days` in README/TELEGRAM.md/config.sample.json, module docstrings, caveats reworded
- [x] Harness: 27 checks `/tmp/t9_test.py` + 10 loop-wiring checks `/tmp/t9_loop_test.py` — all green

### Subtasks / Plan

- [x] Parse the "Check in date" `<select>` options into dates — `parse_booking_dates(html)` (structure-agnostic, multi-format)
- [x] `portal_bookable_dates(cfg)` — fetch/cached (TTL 60 s) via the existing booking-page GET, returns sorted `date` list
- [x] `bookable_window_note(...)` + `cmd_window` CLI command (`python3 padel_booking.py window`) to show the live window
- [x] `cmd_slots` — warn when a requested date is outside the portal window (still list what the endpoint says)
- [x] `cmd_book` — refuse a non-bookable date, message carries the portal's window
- [x] `cmd_pick` — iterate the portal's dates instead of `today .. today+6`
- [x] `bookable_targets` — choose target weekdays from the portal's dates (arithmetic only as fallback)
- [x] `run_booking_race` — 2-minute refresh loop + tri-state result (`slot_found` / `no_slot` / `not_bookable`)
- [x] `book_day` — pre-check bookability + tri-state reporting; stop now when the day is not bookable
- [x] `run_autobook_loop` — trigger 3 becomes "a new date appeared in the portal's window" (no `today+6`, no `booking_open_hour`)
- [x] `padel_telegram.py` — `_show_date_keyboard` builds buttons from portal dates; `_show_slots` refuses non-bookable dates; `_do_book` says "portal window only goes to X" on failure; new `/window` command
- [x] Docs: README.md, TELEGRAM.md, config.sample.json, module docstrings (new `slot_retry_minutes`, `booking_max_wait_minutes`)

### Full Context Notes for AI Agents

> **Purpose**: Self-contained knowledge base to resume without other context.

- **Repo**: `/home/hbravo/repos/padel_booking_alrayyana`, remote `git@github.com:HectorBravo/padel_booking_alrayyana.git`, base branch `main` HEAD `a87ee07`. Deps present: `curl_cffi` (browser TLS impersonation, required for Akamai), `bs4` (`beautifulsoup4`), Python 3.12. **`config.json` is gitignored and absent** → no live portal request is possible from this machine (login needs an OTP email); verification = `py_compile` + synthetic HTML test in `/tmp` (repo has NO test framework, `requirements.txt` has no pytest).
- **Portal endpoints** (`padel_booking.py`, `BASE_URL = https://myportal.asteco.com`): booking page `GET /asset/assetbooking/{asset_booking_id}` (id `370` in `config.sample.json`, CodeIgniter + jQuery, server-rendered); slot list `POST /ajaxctrl/getAmenityBookingSlot` with `serach_val[date]=d-m-yyyy`, `serach_val[id_asset]`, `serach_val[is_paid]=N`, `user`, `id_community`, `attendees`, `id_unit`, `clsId` (`_slots_endpoint:688-732` → `(slots, alert_danger_text)`); booking submit `POST /asset/assetbooking/{id}` with `check_in_date = to_form_date(...)` = `"%b %d, %Y"` (`to_form_date:515`) + `check_in_time = slot["value"]` (`submit_booking:906-951`, `submit_booking_fast:954-983` uses `stream=True` + `r.close()` so the alert text is NOT readable there); cancel `POST /booking/cancelAmenityBooking`; my-bookings `POST /booking/myBooking` (slow ~4.5 s).
- **Why the POST is rejected**: the page's form field `check_in_date` is a `<select>`; jQuery submits the selected option's *value* and the server validates it against the option list it generated for the current window. Dates outside the window have no option → rejected (Bootstrap `alert-danger`, surfaced by `_booking_error:1002-1015` / `_alert_text:1290-1296`). The slot endpoint does NOT validate the date → that is exactly why 15 Nov looked bookable.
- **Cache pattern to mirror**: `_META_CACHE = (session, monotonic_ts, meta)`, `_META_TTL = 120`, `_meta_cached(s)`/`_meta_store(s, meta)` at `padel_booking.py:286-300`; `get_booking_meta(cfg):626-645` caches `_STATE["meta"]`; `_STATE = {"meta": None, "bookings": []}` at :578; `_SESSION_LOCK = threading.Lock()` at :204.
- **Call sites that must change (verified)**: `cmd_slots:811-840`, `cmd_book:843-904`, `cmd_pick:1507-1597` (`for i in range(7)`), `bookable_targets:1649-1692` (call sites `run_autobook_loop:2127` and `:2142`), `run_booking_race:1727-1779` (call sites `cmd_autobook:1803`, `book_day:1959`), `run_autobook_loop:1853-2198` (trigger 3 at :2147-2196 uses `new_day = now.date() + timedelta(days=6)` + 360×10 s sync wait), `book_day` nested at :1908-2052. Telegram: `_show_date_keyboard:686-694` (call sites `_on_slots:684`, `_show_slots:708`, `_on_book:719`), `_show_slots:696-716`, `_do_book:920-974`, dispatch `_handle:436-462`, imports `padel_telegram.py:26-38`.
- **Decision — single source of truth**: `portal_bookable_dates(cfg, refresh=True) -> list[date]` (sorted, deduped, `[]` == unknown). Every decision point consumes it; `today .. today+6` arithmetic survives ONLY as the fallback when it returns `[]` (log a warning). `booking_open_hour` stays in config so nothing breaks; it is then used only by that fallback.
- **Decision — tri-state return, not bool**: `run_booking_race` returns `(slot, slots, status, outcome)` with `outcome ∈ {"slot_found","no_slot","not_bookable"}`. `book_day` and `cmd_autobook` MUST unpack 4 values — a stale 3-tuple unpack is the main regression risk; grep `run_booking_race(` after editing.
  - **Amended after the 01:07 clarification**: `outcome ∈ {"slot_found","no_slot","not_yet_open","outside_window","stopped"}`; `book_day`, `cmd_autobook` and the loop all unpack 4 values (verified: `grep -c 'not_bookable'` = 0, and the harness runs both call sites).
- **Decision — window width vs. opened dates (final semantics)**: `BOOKING_WINDOW_DAYS = 7` / config `booking_window_days` drives `rolling_window_dates(now, days)`, `window_limit(cfg, now)` and `in_booking_window(cfg, day, now)`; the picker (`portal_bookable_dates`) only says which of those days are OPEN. `bookable_targets(targets, now, dates, window_days)` returns in-window occurrences whether or not they are offered (order: opened freshest first, then unopened nearest first). Never confuse the two again: a closed-but-in-window day must be WAITED for, an out-of-window day must stop.
- **Decision — the 2-minute refresh lives inside the race**, not in the scheduler: `slot_retry_minutes` (default 2) replaces the hardcoded `time.sleep(30)`; `booking_max_wait_minutes` (default 360) bounds ONE attempt. The scheduler re-attempts as the window slides, so a day inside the window is never permanently given up. `on_wait=lambda: keepalive(cfg)` is invoked before every refresh from `book_day`, so hours-long waits keep the session alive.
- **`booking_open_hour` is now only a micro-optimisation**: inside `run_booking_race` it sleeps until that hour ONLY when the target is the last day of the rolling window and `open_hour` is non-zero. It is no longer used for candidacy (the old `open_dt = (candidate-6d) @ booking_open_hour` rule was deleted).
- **Trigger 3 redesign**: replace "new calendar day ⇒ book today+6" with "diff the portal's bookable-date set against the last seen set; for each newly offered target weekday, run the race". Kills the `booking_open_hour` dependency and stays correct when the window is 6 days instead of 7.
- **Parser tolerance requirement (no live sample)**: prefer `select` whose `name`/`id` contains `check_in_date`; else scan every `<select>` and keep the one with the most date-like options. Accept option `value` AND option text in `%b %d, %Y`, `%d %b %Y`, `%d %B %Y`, `%d/%m/%Y`, `%d-%m-%Y`, `%Y-%m-%d`, plus day/month-only with year inference. Ignore non-date options (weekday headers, "Today", …).
- **Current state (09-11-2026 01:14)**: BOTH rounds implemented on `fix/ai-portal-bookable-dates` — round 1 (portal-derived dates, 2-min refresh) commit `fefb096`, round 2 (7-day hard limit, `not_yet_open`/`outside_window`, `on_wait` keepalive, both-views `/window`, `booking_window_days` config, docs) still to be committed. `py_compile` clean for both modules, `import padel_telegram` OK, `config.sample.json` valid JSON, `grep not_bookable` = 0, `grep _arithmetic_window` = 0. Harnesses green: `/tmp/t9_test.py` (27 checks) and `/tmp/t9_loop_test.py` (10 loop-wiring checks: startup waits for the unopened Sunday, 15 Nov IS attempted, no submit for a closed date, `not_yet_open`/`outside_window` messages + notifications). **Next**: commit round 2 on the AI branch, push, then wait for the user to merge → final `done` log commit on `main` → `git merge --ff-only origin/main` + `--force-with-lease` re-push.
- **Git flow (rules)**: log-first `docs(ai):` on `main` (done), AI branch `fix/ai-portal-bookable-dates` pushed with `-u`, code commit `fix(booking): [ai] …`, never push code to `main` (only `ai/AI_TASKLOG.md` is allowed); final `done` log commit on `main` AFTER the user merges, then `git merge --ff-only origin/main` + `--force-with-lease` re-push of the AI branch.
- **Environment gotcha**: `run_commands` runs the commands of one call **concurrently**, so never read git/file state in the same call that mutates it (it already bit the previous task).

## Task T8: Merge and delete three AI branches

- **Status**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span>
- **Type**: <span style="background-color:#57606a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">chore</span>
- **Created**: 09-11-2026 00:26:36
- **Last Updated**: 09-11-2026 00:31:00
- **Time Spent**: 5m
- **Branch**: n/a — ref-only cleanup. The deliverable is a *branch deletion*, which no branch or commit can carry, so no `*/ai-*` branch was created (the rules' "never push to `main`" guard applies to code changes; the only file touched here is this log, which the rules allow on the user's branch).
- **Commit(s)**: [383ca82](https://github.com/HectorBravo/padel_booking_alrayyana/commit/383ca82) (`feat/ai-google-calendar-integration` tip) · [c579ded](https://github.com/HectorBravo/padel_booking_alrayyana/commit/c579ded) (`feat/ai-install-nano` tip) · [34410be](https://github.com/HectorBravo/padel_booking_alrayyana/commit/34410be) (`fix/ai-reconcile-booked-json` tip) — all three remain reachable from `main`; branch deletion itself creates no commit, and this entry ships in its own `docs(ai):` commit on `main`
- **Blockers**: none
- **Findings & Notes**:
  - **Nothing to merge**: `git merge` on each branch printed `Already up to date.`; `git branch --merged main` listed all three; `git log main..<branch>` empty for all three; `git merge-base main <branch>` == branch tip. Their work was already integrated by earlier merges — `a7fbf78` (PR #3, google-calendar), `41ff437` (Merge branch `feat/ai-install-nano`), `4874386` (PR #2, reconcile-booked-json, absorbed transitively via the google-calendar branch chain).
  - **Already merged on GitHub too**: `git ls-remote --heads origin` listed **only** `refs/heads/main` — GitHub auto-deleted the three head refs when the PRs were merged. The local clone still had stale `origin/...` tracking refs, which is why `git branch -a` showed them.
  - **Actions taken**: `git branch -d` (safe form — refuses unmerged branches, so it doubles as proof of merge status) for the three local branches; `git fetch origin --prune` removed the stale tracking refs; `git push --dry-run origin --delete ...` proved the remote refs no longer exist (`remote ref does not exist`).
  - **Post-state verified**: `git branch -a` → only `main`, `origin/HEAD`, `origin/main`; `git branch -vv` → `main f86ec6a [origin/main]`; remote heads → only `main`. Working tree clean, `main` == `origin/main`.
  - **Side effect on this log**: the `tree/<branch>` links for T1/T2/T3 (and the deleted T4–T7 branches) now 404 on GitHub; the `commit/...` links still resolve because those commits stay reachable from `main`.

### User Confirmations

None yet.

### Subtasks / Plan

- [x] Enumerate the AI branches (`git branch -a`) and their ancestry vs `main` (`merge-base`, `main..<branch>`, `--merged`)
- [x] Confirm remote state with `git ls-remote --heads origin`
- [x] Merge all three into `main` (git reported `Already up to date.` — no-op, `main` still `f86ec6a`)
- [x] Delete the three local branches with `git branch -d`
- [x] Remove the stale remote-tracking refs (`git fetch origin --prune` / `git remote prune origin`)
- [x] Verify the remote side: `git push --dry-run --delete` → "remote ref does not exist" (already gone)
- [x] Record T8 in this log + commit/push the log-only `docs(ai):` commit on `main`

### Full Context Notes for AI Agents

> **Purpose**: Self-contained knowledge base to resume without other context.

- **Repo**: `/home/hbravo/repos/padel_booking_alrayyana` (Linux), remote `git@github.com:HectorBravo/padel_booking_alrayyana.git`, current branch `main`, HEAD `f86ec6a` == `origin/main`.
- **Task**: purely a ref-management chore — merge + delete `feat/ai-google-calendar-integration`, `feat/ai-install-nano`, `fix/ai-reconcile-booked-json`. **No source file was touched**, so `padel_booking.py` / `google_calendar.py` / `Dockerfile` are unchanged.
- **Merge status (verified 3 independent ways)**: all three tips are ancestors of `main`: `383ca82` (google-calendar), `c579ded` (install-nano), `34410be` (reconcile-booked-json). Chain: `34410be` → merged into google-calendar branch at `4874386`; `c579ded` → merged into `main` at `41ff437`; `383ca82` → `main` at `a7fbf78` (PR #3). Deleting them therefore loses nothing, and `git branch -d` (not `-D`) accepted them, which re-proves it.
- **Commands used and their real output**: `git merge <b>` → `Already up to date.`; `git branch -d b1 b2 b3` → "Deleted branch ... (was <tip>)" x3; `git push --dry-run origin --delete b1 --delete b2 --delete b3` → exit 1 with "unable to delete '<b>': remote ref does not exist" for each (expected: GitHub auto-deletes PR head refs on merge); `git fetch origin --prune` → "[deleted] (none) -> origin/<b>" x3.
- **Gotcha for future sessions**: `run_commands` executes the commands of one call concurrently, so a `git branch -a` issued in the same call as the deletions printed the **pre-delete** listing. Re-checking in a fresh call showed the true post-state (only `main`). Do not trust same-call listings for state-changing git operations.
- **Current state = done**: no `*/ai-*` branch exists locally or remotely; only `main` / `origin/main` remain. Nothing pending.
- **Not applicable to this task**: the `*/ai-*` branch + PR workflow — there is no code diff to carry on a branch. Later *code* tasks must still follow the normal flow (AI branch → push -u → PR/merge → final log commit on `main` → `git merge --ff-only origin/main` + `--force-with-lease` re-push).
- **Rule 5 (core.filemode)**: untouched — this repo was not cloned in this session. It is a Windows/Linux shared repo (`ai/AI_TASKLOG.md` references `D:\Repos\padel_booking_alrayyana`), so if it is ever re-cloned on Windows, unset the local `core.filemode` after cloning and keep the global `false` (Windows) / `true` (Linux).


## Task T7: Diagnose OTP login hang; session check via /dashboard
- **Status**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span>
- **Type**: <span style="background-color:#9e6a03;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">fix</span>
- **Created**: 11-10-2026 15:50:19
- **Last Updated**: 11-10-2026 16:07:06
- **Time Spent**: 17m
- **Branch**: [`fix/ai-login-diag-dashboard-check`](https://github.com/HectorBravo/padel_booking_alrayyana/tree/fix/ai-login-diag-dashboard-check)
- **Commit(s)**: [14eb5e2](https://github.com/HectorBravo/padel_booking_alrayyana/commit/14eb5e2ede7fe503c27edf90ed2140d1c5fbebaf) (code) · [2434815](https://github.com/HectorBravo/padel_booking_alrayyana/commit/2434815) (PR #6 merge)
- **Blockers**: none
- **Findings & Notes**:
  - **Symptom (user report)**: a manual browser login takes 3-4 s total (`/login/checkLogin` → `/login/checkOtp` → `verifyOtp` → `/dashboard/index`), yet the bot keeps failing with `Login failed: Failed to perform, curl: (28) Operation timed out after 90xxx ms **with 0 bytes received**` (message comes from `padel_telegram.py:414`, the OTP-reply handler wrapping `login_finish`).
  - **Key insight**: `0 bytes received` = the portal **never answered** that request at all — it is a silent drop (WAF/Akamai blackhole), not slowness. That is why T6's 90 s default only delays the same failure.
  - **Why the browser works and the bot doesn't**: the bot's `login_finish` (`padel_booking.py:393-429`) builds a **fresh** session (`new_session(restore=False)` at :401 — new TLS/HTTP2 fingerprint vs the `login_start` connection) and POSTs `/login/verifyOtp` (:406) with **no `Referer` header**, then GETs the redirect target (:416). The browser does the same steps over the *same already-validated connection* with `Referer: .../login/checkOtp`. The OTP email arriving proves `checkLogin`+`checkOtp` succeed from the bot; the suspect is the `verifyOtp` POST (or the redirect GET) on the new connection.
  - **Changes in this task**: (1) `is_logged_in()` now checks `GET /dashboard/index` directly (same criterion: `200` and final URL without `/login`) instead of `GET /asset/assetbooking/{id}` — per user instruction, mirrors the browser flow, faster; (2) every request of `login_start` / `login_finish` / `is_logged_in` is wrapped in the **existing but previously uncalled** `_timed()` (`padel_booking.py:264`) so the production log records each step (`[http] POST /login/verifyOtp -> 200 in 1.3s` or `FAILED after 90.02s: ...`); (3) `set_http_log` is wired in `main()` and the startup line prints the active `PORTAL_TIMEOUT` (confirms the deployed image value).
  - **Verified live** (dev machine, `python padel_booking.py login-status`): startup line `[start] portal=https://myportal.asteco.com session timeout=90s`; per-request line `[http] GET https://myportal.asteco.com/dashboard/index -> 200 in 7.41s`; `Session valid: False` = correct (the saved dev session was expired → portal redirected to `/login`, exactly what the new check detects). `py_compile` clean on both files; `new_session(restore=False).timeout == 90`.
  - **Next if the hang persists after deploy**: replicate the browser's `verifyOtp` request headers (Referer/Origin/Accept/X-Requested-With) from the user's DevTools capture (planned T8). The new `[http]` lines will show which request returns `0 bytes`.

### User Confirmations

**Pending (awaiting user response):**

- [ ] (11-10-2026 15:50:19) DevTools request headers of the browser's `POST https://myportal.asteco.com/login/verifyOtp` (sent from the `/login/checkOtp` page) — needed for the follow-up fix only if the instrumented log still shows a silent drop.

**Confirmed (user provided):**

- (11-10-2026 ~15:35) "¿Qué endpoint consultas para validar la cookie de la sesión?" → "puedes ir directamente a https://myportal.asteco.com/dashboard/index".
- (11-10-2026 ~15:45) "¿Cómo procedo con el diagnóstico?" → "Sí, haz 1+2: instrumentar el login (log por petición) + cambiar is_logged_in() a /dashboard/index, y te pido los headers de verifyOtp de DevTools para el paso 3."

### Subtasks / Plan

- [x] Record T7 in `ai/AI_TASKLOG.md` before touching code (dedicated `docs(ai):` commit on `main`)
- [x] `is_logged_in()` → `GET /dashboard/index` (keep `(s, cfg)` signature; same `/login`-redirect criterion)
- [x] Instrument `login_start` (GET /, POST /login/checkLogin), `login_finish` (POST /login/verifyOtp, GET redirect target) and `is_logged_in` via `_timed()`
- [x] Wire `set_http_log` in `main()` (all commands incl. `telegram`) + print active `PORTAL_TIMEOUT` at startup
- [x] Verify: `py_compile` both files, `new_session().timeout == 90`, one live `login-status` (single read-only GET to the portal with saved cookies)
- [x] Code commit on `fix/ai-login-diag-dashboard-check` + push to origin (`14eb5e2`)
- [x] Open PR #6, merge to `main` (`2434815`), confirm the `Build and Push Docker Image` workflow run succeeds (run `38137811315`)
- [x] Final log commit on `main` (status `done`, real hashes) + fast-forward the AI branch + re-push
- [ ] (user) Production host: `docker compose pull && docker compose up -d`; check the `[http]` lines of the next login
- [ ] (user, if login still fails) Provide DevTools `verifyOtp` headers → follow-up fix (T8)

### Full Context Notes for AI Agents

> **Purpose**: Self-contained knowledge base to resume without other context.

- **Repo**: `D:\Repos\padel_booking_alrayyana`, remote `git@github.com:HectorBravo/padel_booking_alrayyana.git`, base `main` HEAD `600a92f` (T6 final log). Dev machine is Windows; **no docker** (production pull must run on the host); `gh` CLI available (v2.102.0) for PR create/merge.
- **Git identity (per project rules)**: env vars `GIT_AUTHOR_NAME="AI_bot"` + `GIT_COMMITTER_NAME="AI_bot"` only; never set author/committer email (user's `git config user.email` stays untouched). Every AI commit message contains `[ai]` after the colon. Task log commits: `docs(ai): [ai] ...` pushed to `main` (user's current branch). Code goes on `fix/ai-login-diag-dashboard-check`.
- **Symptom chain**: `padel_telegram.py:399-424` — when the user replies with the OTP, `_on_message` calls `login_finish(self.cfg, digits)` (:407); any exception becomes `❌ Login failed: {e}` (:414). `e` is the `curl: (28) ... 0 bytes received` from `curl_cffi`. Same failure reachable from CLI `login-finish` (`padel_booking.py:455-459`) and autobook re-login (`padel_telegram.py:988-1024`).
- **`login_finish` exact steps** (`padel_booking.py:393-429`): (1) `s = new_session(restore=False)` (:401, fresh session; pending cookies loaded via `_load_cookies` :402 from `pending_login.json`); (2) `r2 = s.post(BASE_URL + "/login/verifyOtp", data={"user_otp": otp, **otp_fields})` (:406-407) — AJAX endpoint returning a redirect URL, or literal `"2"` (expired) / `"3"` (invalid) checked at :408-412; (3) `target = resp if resp.startswith("http") else BASE_URL + resp` (:415), then `s.get(target, allow_redirects=True)` (:416); (4) `if not is_logged_in(s, cfg): raise RuntimeError("OTP accepted but session still not authenticated.")` (:418-419); (5) `save_session(s)`, `install_session(s)`, clear `_STATE`, `PENDING_FILE.unlink()` (:421-428).
- **`login_start` exact steps** (`padel_booking.py:352-390`): `s = new_session(restore=False)` (:356); `s.get(BASE_URL + "/")` (:359); `r = s.post(BASE_URL + "/login/checkLogin", data={"username": encode(cfg["email"]), "password": encode(cfg["password"])})` (:360-363); if `r.url` lacks `checkOtp` → `is_logged_in` check (:365) or RuntimeError (:369); parse hidden fields from the OTP page (:373-378); stash `pending_login.json` with cookies + `otp_fields` (:381-387).
- **`is_logged_in` current code** (`padel_booking.py:317-321`): `r = s.get(f"{BASE_URL}/asset/assetbooking/{cfg['asset_booking_id']}", allow_redirects=True)`; `return r.status_code == 200 and "/login" not in r.url.lower()`. **New**: `r = _timed("GET", f"{BASE_URL}/dashboard/index", lambda: s.get(f"{BASE_URL}/dashboard/index", allow_redirects=True))`; same return expression. Call sites (all keep the `(s, cfg)` signature): `login_start:365`, `login_finish:418`, `get_authenticated_session:437`, `cmd_login_status:465`, `padel_telegram.py:652` (`/status`).
- **`_timed` / `set_http_log`** (`padel_booking.py:249-278`): `_http_log` is a module global (default `None`); `set_http_log(fn)` assigns it; `_timed(method, url, call)` times zero-arg `call()`; on success logs `[http] {method} {url} -> {status} in {dt:.2f}s`, on exception logs `[http] {method} {url} FAILED after {dt:.2f}s: {e}` and re-raises. **Currently has zero call sites and `set_http_log` is never called** (verified by search) — this task activates them. `ts_prefix()` exists in `padel_booking.py` (imported by `padel_telegram.py:36`). Wire in `main()` (`padel_booking.py:2209-2251`) right after `cfg = load_config()` (:2218): `set_http_log(lambda m: print(f"{ts_prefix()} {m}", flush=True))` plus `print(f"[start] portal={BASE_URL} session timeout={PORTAL_TIMEOUT}s", flush=True)`.
- **Wrapping pattern** (lambda over the exact existing call, keep all args identical — do not add/alter headers or cookies in this task): `r2 = _timed("POST", BASE_URL + "/login/verifyOtp", lambda: s.post(BASE_URL + "/login/verifyOtp", data={"user_otp": otp, **otp_fields}))`; `r = _timed("POST", BASE_URL + "/login/checkLogin", lambda: s.post(BASE_URL + "/login/checkLogin", data={"username": encode(cfg["email"]), "password": encode(cfg["password"])}))`; `s.get(target)` becomes `_timed("GET", target, lambda: s.get(target, allow_redirects=True))`; `s.get(BASE_URL + "/")` → `_timed("GET", BASE_URL + "/", lambda: s.get(BASE_URL + "/"))`.
- **Constants**: `PORTAL_TIMEOUT = 90` at `padel_booking.py:83` (T6); `new_session(restore)` at :155-167 → `requests.Session(impersonate="chrome", timeout=PORTAL_TIMEOUT)`; `MYBOOKINGS_TIMEOUT = PORTAL_TIMEOUT` at :1076. `BASE_URL = "https://myportal.asteco.com"`.
- **What NOT to do in this task**: no header changes on `verifyOtp`, no cookie/session handling changes, no Google Calendar/Telegram/autobook timeout changes. Header replication is follow-up T8, conditional on the instrumented log + the user's DevTools headers.
- **Verification**: `python -m py_compile padel_booking.py padel_telegram.py`; `python -c "from padel_booking import new_session; print(new_session(restore=False).timeout)"` → `90`; live `python padel_booking.py login-status` (one read-only GET `/dashboard/index` with the saved session; expect a `[http] GET .../dashboard/index -> 200 in X.XXs` line and `Session valid: True`). `config.json`/`session.json` exist in the repo dir (chmod 600) so the live check can run locally.
- **Deploy**: PR from `fix/ai-login-diag-dashboard-check` → merge to `main` → `.github/workflows/docker-publish.yml` rebuilds `hecbr/padel-booking-alrayyana` (latest + SHA) → user runs `docker compose pull && docker compose up -d` on the production host (entrypoint re-syncs `.py` on start) → next `/login` produces the `[http]` lines that identify the silently-dropped request.

---

## Task T6: Raise portal-wide session timeout to 90s

- **Status**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span>
- **Type**: <span style="background-color:#9e6a03;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">fix</span>
- **Created**: 08-10-2026 01:47:09
- **Last Updated**: 08-10-2026 01:57:09
- **Time Spent**: 10m
- **Branch**: [`fix/ai-portal-session-timeout-90s`](https://github.com/HectorBravo/padel_booking_alrayyana/tree/fix/ai-portal-session-timeout-90s)
- **Commit(s)**: [062d106](https://github.com/HectorBravo/padel_booking_alrayyana/commit/062d106515f2fa150ce002d77a17f0af6e2a39da) (code) · [2d342cb](https://github.com/HectorBravo/padel_booking_alrayyana/commit/2d342cb92ef66b0b1449e6e8c80795c997c82877) (PR #5 merge)
- **Blockers**: none
- **Findings & Notes**:
  - **Symptom**: at program start the bot fails with `Login failed: Failed to perform, curl: (28) Operation timed out after 30002 milliseconds with 0 bytes received` → the **login** phase (`login_start`: `GET /`, `POST /login/checkLogin`) timed out at exactly 30 s.
  - **Root cause**: T5 only raised the timeout on the 3 *mybookings* call sites. Every *other* portal request (login, `is_logged_in`, slots, book, cancel) is made **without** an explicit `timeout`, so it inherits the curl_cffi `Session` default of **30 s**. `new_session()` created `requests.Session(impersonate="chrome")` with no timeout → default 30 (confirmed live: `.timeout == 30`). When the portal (already the slow one) takes >30 s on any phase of the OTP login, curl aborts with `curl: (28)` after 30000 ms.
  - **Fix**: set the **session-wide** default timeout to 90 s in one place — `new_session()` → `requests.Session(impersonate="chrome", timeout=PORTAL_TIMEOUT)` — and introduce `PORTAL_TIMEOUT = 90` next to `BASE_URL`. `MYBOOKINGS_TIMEOUT` is now `PORTAL_TIMEOUT` (same value, single source of truth) so the existing mybookings `timeout=` call sites keep working. Any request that doesn't pass an explicit timeout now gets 90 s.
  - **Verified live**: after the change `new_session(restore=False).timeout == 90` (was 30); `py_compile` passes. Per-call `timeout=` overrides (Google Calendar `timeout=30`, Telegram `get_updates timeout=50`/`timeout=timeout`, webhook `timeout=15`, autobook race `timeout=18000`, `event.wait(timeout=900)`) are untouched — different services, keep their own values.
  - **Deploy (user intent "llevalo a produccion")**: same production flow as T5 — PR → merge to `main` → GitHub Actions rebuilds `hecbr/padel-booking-alrayyana`.
### User Confirmations

**Pending (awaiting user response):**

- (08-10-2026 ~01:45) Reported the 30 s `curl: (28)` login timeout at program start and asked why it still happens → AI confirmed the login (and all non-mybookings) requests still used the 30 s `Session` default; the user's standing intent is "llevalo a produccion".

**Confirmed (user provided):**

None yet.

### Subtasks / Plan

- [x] Introduce `PORTAL_TIMEOUT = 90` constant next to `BASE_URL`
- [x] Apply `timeout=PORTAL_TIMEOUT` in `new_session()` so every portal request inherits 90 s
- [x] Rebase `MYBOOKINGS_TIMEOUT` onto `PORTAL_TIMEOUT` (single source of truth)
- [x] Verify with `py_compile` + live check that `new_session().timeout == 90`
- [x] Deploy to production: PR → merge to `main` → confirm Docker image rebuild (PR #5 merged, commit 2d342cb)
### Full Context Notes for AI Agents

> **Purpose**: Self-contained knowledge base to resume without other context.

- **Why T5 was not enough**: T5 added `timeout=MYBOOKINGS_TIMEOUT` only to the 3 `/booking/myBooking` calls. The **login flow** (`login_start`/`login_finish` in `padel_booking.py`, plus `is_logged_in`, `get_authenticated_session`, slots/book) never passes an explicit `timeout`, so those calls fall back to the `curl_cffi` `Session.timeout` default, which is **30** (a bare `Session(impersonate="chrome")` → `.timeout == 30`, confirmed live). That is exactly the `30002 ms` / `curl: (28)` the user saw at startup (login is the first portal call the bot makes).
- **Files/lines** (content anchors; line numbers shift slightly after edit):
  - `padel_booking.py` `BASE_URL = "https://myportal.asteco.com"` — added `PORTAL_TIMEOUT = 90` immediately after `BASE_URL` (comment explains the 30→90 rationale and the `curl: (28)` symptom).
  - `new_session(restore)` — was `s = requests.Session(impersonate="chrome")`; now `s = requests.Session(impersonate="chrome", timeout=PORTAL_TIMEOUT)`. This single change makes **every** bot session (shared `_SESSION`, login sessions, `get_authenticated_session`) inherit 90 s. `curl_cffi.requests.Session` accepts `timeout=` in the constructor (verified: `Session(impersonate="chrome", timeout=90).timeout == 90`).
  - `MYBOOKINGS_TIMEOUT` block (near `MYBOOKINGS_URL`/`CANCEL_URL`) — value changed from literal `90` to `PORTAL_TIMEOUT` (same value, now single-source). The 3 mybookings call sites still pass `timeout=MYBOOKINGS_TIMEOUT` (explicit) → behavior unchanged.
- **Intentionally NOT changed** (different services, keep their own values): `google_calendar.py` all `timeout=30` (Google API); `padel_telegram.py` `get_updates(..., timeout=50)` (long-poll), the `requests.post(url, json=params, timeout=timeout)` Telegram call, the webhook `timeout=15`, autobook `run_booking_race(..., timeout=18000)` and `event.wait(timeout=900)`. Unrelated to the Asteco portal and were correct before.
- **Verification command**: `python -c "from padel_booking import new_session; print(new_session(restore=False).timeout)"` → expected `90`. Also `python -m py_compile padel_booking.py`.
- **Base**: `d5e90ab` (current `main` HEAD). **Branch**: create `fix/ai-portal-session-timeout-90s` from `main`, push, open PR, merge to `main`, confirm the `Build and Push Docker Image` workflow run succeeds.
- **Production**: `docker` is NOT on this dev machine. After the merge rebuilds `hecbr/padel-booking-alrayyana` on Docker Hub, the production host must run `docker compose pull && docker compose up -d` to activate the new image (entrypoint re-syncs `.py` on start).
- **Git identity**: override only `GIT_AUTHOR_NAME="AI_bot"` and `GIT_COMMITTER_NAME="AI_bot"`; do NOT set author/committer email (use the user's `git config user.email`). Commit tag format `fix(portal): [ai] ...` / `docs(ai): ...`. Push the task log to the user's current branch (`main`).

---

## Task T13: Treat the picker as the real window limit (7-day assumption is wrong)

- **Status**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span>
- **Type**: <span style="background-color:#da3633;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">hotfix</span>
- **Created**: 11-11-2026 01:04:22
- **Last Updated**: 11-11-2026 01:11:30
- **Time Spent**: 14m
- **Branch**: [`fix/ai-never-book-unoffered-date`](https://github.com/HectorBravo/padel_booking_alrayyana/tree/fix/ai-never-book-unoffered-date)
- **Commit(s)**: [42cf189](https://github.com/HectorBravo/padel_booking_alrayyana/commit/42cf189), merged to `main` via [PR #10](https://github.com/HectorBravo/padel_booking_alrayyana/pull/10) (merge `677f3f9`)
- **Blockers**: none
- **Findings & Notes**:
  - **User evidence (production, 11-11-2026 00:56:46)**: `*** Booking Tue 17 Nov 2026 ... *** FAILED to book 19:00-20:00 [...]: booking not confirmed (slots still available, HTTP 303) [race: 0.40s]`.
  - **User then supplied the missing fact**: on `/asset/assetbooking/370` the "Check in date" picker currently offers **10 Nov .. 16 Nov** — i.e. 17 Nov is *not selectable*, so its window is not open yet.
  - **The new code IS deployed** (proved): startup printed `(portal window Wed 11 Nov .. Tue 17 Nov 2026)`; pre-T9 logged the same line with no window at all (`git show 2434815:padel_booking.py`). T9–T12 are live.
  - **Corrected root cause — T9's central assumption is false.** `BOOKING_WINDOW_DAYS = 7` (and `rolling_window_dates()` → `today .. today+6`) is used as the **hard bookable limit**, and `run_booking_race` only returns `outside_window` when the target falls outside it. But the picker proves the real limit is **not** `today+6`: today is 11 Nov, yet the picker stops at **16 Nov (today+5)**. 17 Nov is therefore *inside* the assumed window → the race never says "outside", treats the date as merely "not open yet" — and because the slot endpoint answers for closed dates, it submitted the booking instead of waiting. The 0.40s race time and the absence of any "refreshing in 2 min" line match: one refresh, slot looked free, POST sent, portal rejected it (HTTP 303).
  - **In short**: `today .. today+6` is a *superset* of what is actually bookable. Anything beyond the picker's last date can never be booked now and must be reported as such, not waited for.
  - **Secondary defect found while probing** (synthetic markup): `<option ... disabled>` is counted as bookable by `parse_booking_dates.collect()` — a greyed-out date would still look open even once the picker is read correctly.
  - **Fix direction**: make the **picker's own last date** the effective hard limit when it is readable (fall back to `today + booking_window_days - 1` only when it is not), so an unopened far date yields `outside_window` → honest "not bookable yet" instead of a doomed POST; and ignore `disabled` options.

### User Confirmations

**Pending (awaiting user response):**

None.

**Confirmed (user provided):**

- (11-11-2026 ~00:59) Production still attempted 17 Nov; log quoted; asked to reevaluate why and fix it.
- (11-11-2026 ~01:04) "the selected values for the 'Check in date' are only from the 10th to the 16th right now. So this indicates that the window for the 17th is not yet open" → the picker's last offered date, not `today+6`, is the real limit.

### Subtasks / Plan

- [x] `effective_bookable_last(cfg, now, offered=None)` (`padel_booking.py:869`) — picker's last option is the ceiling; arithmetic bound only when the picker is unreadable
- [x] `run_booking_race` hard-limit check now uses that ceiling (`:2184-2188`) → an unopened far date returns `outside_window` and is **never POSTed**; `offered` is read once per refresh (no extra request)
- [x] Ignore `<option disabled>` in `parse_booking_dates.collect` (`:750-756`)
- [x] Wait-until-open preserved for dates the picker's ceiling already reaches; `book_day` now reports "not offered by the picker yet … will be re-tried" instead of a false "can never be booked" (`:2484-2503`)
- [x] Regression tests: production reproduction (picker ..today+5, target today+6 → 0 POSTs) + disabled-option exclusion
- [ ] (user) On the production host: `docker compose pull && docker compose up -d`

### Verification results

```
python ai/verify_t9_behaviors.py -> ALL PASS (exit 0, 11 checks)
  [PASS] date beyond the picker's last option is never POSTed (the production 303 bug)
          target=today+6, picker ends today+5 -> posts=0 (outcome=outside_window);
          target=today+5 (offered) -> posts=1 (outcome=slot_found)
  [PASS] <option disabled> is not treated as bookable
  ... plus the 9 pre-existing checks (wait-until-open, retry notice, 12h default, …)
```

**Tests corrected, not just added**: three existing scenarios encoded the bug itself — they waited for `today+6` while the picker stopped at `today+4`, which under correct semantics is "beyond the ceiling", not "not open yet". They now target a date inside the picker's reach (`NEAR = today+3`) and assert the right outcomes.

Docker image rebuilt on the merge: run `42902188276` ✓ success.

**Trigger (3) still guarantees the retry**: `last_window` records the dates seen (10–16 Nov); when 17 Nov appears in the picker it is absent from that set, so the loop books it then — verified by reading `fresh = [d for d in candidates if d not in last_window]`.

### Full Context Notes for AI Agents

> **Purpose**: Self-contained knowledge base to resume T13 without other context.

- **Base**: `main` at `9231ec6` (T9–T12 merged; PRs #7–#9).
- **Live config** (`config.json`, gitignored): `asset_booking_id=370`; `booking_window_days` ABSENT → default 7; `preferred_slots` keys `sunday, tuesday, saturday`. On 11 Nov (Wed) the in-window occurrences are Sun 15 and Tue 17 — matching the log order (17 attempted first as "freshest", 15 skipped as already booked).
- **Key anchors** (`padel_booking.py`): `BOOKING_WINDOW_DAYS:823`; `rolling_window_dates:~838`; `window_limit:~848`; `in_booking_window`; `booking_window_days(cfg):~829`; `_option_date:695`; `parse_booking_dates:731` (`collect()` reads `opt.get("value")` then `opt.get_text()`, ignores `disabled`); `run_booking_race` hard-limit check at the top of its loop (returns `outside_window`) and the `opened = not offered or target_date in offered` line; `bookable_targets`; startup notice `if not window_dates: window_dates = rolling_window_dates(...)`.
- **Do not regress**: when the picker is unreadable (`[]`), the bot must keep working via arithmetic (never "nothing is bookable"); `/window`, `_show_date_keyboard`, `_show_slots` and the autobook triggers all consume these helpers. The wait-until-open behavior for *in-range* dates must stay (T9/T10 checks).
- **Semantics to implement**: effective_last = max(picker dates) when picker non-empty else `today + booking_window_days - 1`. A target after effective_last → `outside_window` (log "not open yet / cannot be booked now", no POST). A target within → existing wait/retry logic.
- **Harness**: `ai/verify_t9_behaviors.py` (9 checks green before this task). Add the reproduction: picker `[10..16 Nov]`, target 17 Nov, `submit_booking_fast` must not be called; plus a disabled-option parse case.
- **Deploy**: PR → merge `main` → watch `Build and Push Docker Image` → user runs `docker compose pull && docker compose up -d`.
- **Git identity**: only `GIT_AUTHOR_NAME`/`GIT_COMMITTER_NAME` = `AI_bot`; never set email; `[ai]` tag after the colon; task log committed to the user's current branch (`main`).

---

## Task T12: Telegram notice on first retry while a day is unavailable

- **Status**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span>
- **Type**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">feat</span>
- **Created**: 11-11-2026 00:50:01
- **Last Updated**: 11-11-2026 00:54:30
- **Time Spent**: 8m
- **Branch**: [`feat/ai-retry-notice-once-per-day`](https://github.com/HectorBravo/padel_booking_alrayyana/tree/feat/ai-retry-notice-once-per-day)
- **Commit(s)**: [6bd3b40](https://github.com/HectorBravo/padel_booking_alrayyana/commit/6bd3b40) (cherry-picked config-override check) + [0780f09](https://github.com/HectorBravo/padel_booking_alrayyana/commit/0780f09) (the change), merged to `main` via [PR #9](https://github.com/HectorBravo/padel_booking_alrayyana/pull/9) (merge `cd1a98a`)
- **Blockers**: none
- **Findings & Notes**:
  - User asked whether the log shows an attempt while a desired day is NOT yet available. It already does, per refresh, at `padel_booking.py:2170` (`...but the portal has not opened this date yet; refreshing in 2 min...` / `...but no preferred slot is free...`). Verified by running the race with output captured, not just by reading code.
  - The gap is **visibility in Telegram**, not in the log: that line is stdout only, so during a multi-hour wait the chat stays silent (only the startup notice and the final outcome arrive). `_log()`/`print()` also write nothing under `pythonw` (`padel_telegram.py:69`).
  - **User's chosen behavior**: send to Telegram **once per day on the first retry**, then only the final result. Chosen over "every retry" (would be up to ~360 messages across a 12h wait) and over "no change".
  - Design: `run_booking_race` gains an optional `on_retry` callback invoked **only when `attempt == 1`** — the race itself has no notion of de-duplication, so the loop keeps it simple and the caller decides the policy. `book_day()` passes a closure that calls `notify_()` exactly once per call; because each trigger (`start` / `prefs change` / `new date offered`) calls `book_day()` afresh, one notice fires per day per trigger, which matches "once per day".
  - **Backward compatible**: `on_retry=None` default → `cmd_autobook` (CLI) and every existing caller are unchanged; only the scheduler wires up Telegram notifications.

### User Confirmations

**Pending (awaiting user response):**

None.

**Confirmed (user provided):**

- (11-11-2026 ~00:50) "do you indicate on the log when the new day is not available but you are trying to book?" → yes, already logged every refresh; the missing part was Telegram visibility.
- (11-11-2026 ~00:50) Chose: "Also send to Telegram once per day (first retry), then a final result."

### Subtasks / Plan

- [x] Add `on_retry` parameter to `run_booking_race` (`padel_booking.py:2057`) and fire it only on the first refresh (`:2178`, `attempt == 1`), wrapped in try/except so a failing callback can never abort the wait
- [x] Wire `book_day()` to notify via `notify_()` exactly once per attempt (`:2424-2427`, passed at `:2435`)
- [x] Extend `ai/verify_t9_behaviors.py`: one notice while waiting, none on an immediate booking, none for outside-window days
- [x] Verify: `py_compile` clean; harness **ALL PASS** (9 checks); merged via PR #9 → image rebuilt (run `42899683557` ✓ success)
- [ ] (user) On the production host: `docker compose pull && docker compose up -d`

### Verification results

```
python ai/verify_t9_behaviors.py -> ALL PASS (exit 0)
  ...
  [PASS] exactly ONE retry notice per attempt, none when it books or refuses at once
          waited 6 refreshes -> notices=1 (outcome=not_yet_open); immediate book
          -> notices=0 (outcome=slot_found); outside window -> notices=0
          (outcome=outside_window)
```

Message the user now receives once per day per attempt:

`⏳ Trying to book Tue 17 Nov: the portal has not opened this date yet. Refreshing every 2 min — I will report the result here.`

> **Note**: `6bd3b40` is the cherry-pick of the config-override check from `test/ai-config-override-check`, which was still unmerged when this branch was cut; PR #9 therefore landed that test too.

### Full Context Notes for AI Agents

> **Purpose**: Self-contained knowledge base to resume T12 without other context.

- **Base**: `main` at `bda1b65` (T9/T10/T11 all merged; PRs #7 and #8).
- **Anchors** (`padel_booking.py`):
  - `run_booking_race:2053-2057` — signature to extend with `on_retry=None`.
  - `run_booking_race:2162-2181` — the wait branch; `attempt` counter incremented at the top of the loop (`attempt += 1`), so "first retry" is `attempt == 1`. The existing stdout line is at `:2170`; fire `on_retry(reason)` right after it, before `on_wait()`/`sleep`.
  - `run_autobook_loop.book_day:2409-2412` — the call site to pass `on_retry=` through; `notify_` is defined at `:2323` in the enclosing scope.
  - Final-result notices already exist and must NOT be duplicated: `not_yet_open` `:2425-2433`, `outside_window` `:2418-2424`, no-slot `:2511-2517`, booked `:2451-2460`.
  - CLI `cmd_autobook:2223` calls the race without `on_retry` — leave as is.
- **Callback contract**: called with one arg, the human-readable reason string already computed for the log (`"the portal has not opened this date yet"` / `"no preferred slot is free"`). Must never break the booking loop: wrap the call in try/except like `on_wait` does.
- **Why not de-dupe inside the race**: the race is a bounded single attempt; de-dup state across attempts belongs to the caller. A closure counter inside `book_day` is per-attempt and resets naturally on the next trigger, which is the requested "once per day" semantics.
- **Harness**: `ai/verify_t9_behaviors.py` patches `portal_bookable_dates`, `get_available_slots`, `submit_booking_fast`, `time.sleep`; add checks that count `on_retry` invocations. Must stay green for all pre-existing checks.
- **Deploy**: PR → merge to `main` → watch `Build and Push Docker Image` → user runs `docker compose pull && docker compose up -d` (no `docker` on this dev machine).
- **Git identity**: only `GIT_AUTHOR_NAME`/`GIT_COMMITTER_NAME` = `AI_bot`; never set email; `[ai]` tag after the colon; task log committed to the user's current branch (`main`).

---

## Task T11: Default booking_max_wait_minutes to 12h

- **Status**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span>
- **Type**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">feat</span>
- **Created**: 11-11-2026 00:52:00
- **Last Updated**: 11-11-2026 00:47:00
- **Time Spent**: 7m
- **Branch**: [`feat/ai-booking-max-wait-12h`](https://github.com/HectorBravo/padel_booking_alrayyana/tree/feat/ai-booking-max-wait-12h)
- **Commit(s)**: [ed25f08](https://github.com/HectorBravo/padel_booking_alrayyana/commit/ed25f08) (cherry-picked `no_slot` check) + [0ce49ee](https://github.com/HectorBravo/padel_booking_alrayyana/commit/0ce49ee) (the change), merged to `main` via [PR #8](https://github.com/HectorBravo/padel_booking_alrayyana/pull/8) (merge `fbf72a4`)
- **Blockers**: none
- **Findings & Notes**:
  - User asked for the default `booking_max_wait_minutes` to become **720 (12h)** instead of 360 (6h).
  - The value `360` was hardcoded as a fallback in **four** places (`padel_booking.py:2095, 2215, 2422, 2621`) — three are user-facing log/notification strings, one is the real bound. Changing only one would silently desync the messages from actual behavior, so all four must move together.
  - Fix introduces a single module constant `BOOKING_MAX_WAIT_MINUTES = 720` next to `BOOKING_WINDOW_DAYS` (`:823`) and makes every call site reference it — same pattern already used for `PORTAL_TIMEOUT` / `MYBOOKINGS_TIMEOUT`.
  - Docs that quote "default 360 = 6h" are updated too: `README.md:114,131`, `TELEGRAM.md:178`, `config.sample.json:17`, and the `run_booking_race` docstring at `padel_booking.py:2080`.
  - **No host config change needed**: the user's `config.json` does not set this key, so it inherits the new default automatically. Anyone who *did* set 360 explicitly keeps 360 (explicit config always wins).

### User Confirmations

**Pending (awaiting user response):**

None.

**Confirmed (user provided):**

- (11-11-2026 ~00:52) "make the default for booking_max_wait_minutes equal to 12 hours" → implemented as a constant, default 720.

### Subtasks / Plan

- [x] Add `BOOKING_MAX_WAIT_MINUTES = 720` constant (`padel_booking.py:830`, next to `BOOKING_WINDOW_DAYS`)
- [x] Replace all four hardcoded `360` fallbacks with the constant (`:2103` functional bound, `:2222` `cmd_autobook`, `:2429` `book_day` log, `:2628` startup log) — no literal `360` remains
- [x] Update `config.sample.json`, `README.md` (×2), `TELEGRAM.md` and the `run_booking_race` docstring
- [x] Verify: `py_compile` clean; `BOOKING_MAX_WAIT_MINUTES == 720`; `ai/verify_t9_behaviors.py` → **ALL PASS** (7 checks, incl. the new 12h default check)
- [ ] (user) On the production host: `docker compose pull && docker compose up -d` to activate the new image

### Verification results

```
python -c "import padel_booking as p; print(p.BOOKING_MAX_WAIT_MINUTES)"  -> 720
python ai/verify_t9_behaviors.py                                          -> ALL PASS (exit 0)
  [PASS] in-window but NOT OPEN YET -> keeps waiting, never books
  [PASS] day OPENS after 2 refreshes -> books on the 3rd
  [PASS] day open, slot taken then free -> books when free
  [PASS] OUTSIDE the 7-day window -> stops at once, never waits
  [PASS] bot keyboard offers ONLY portal-opened days
  [PASS] day open but slots NEVER free -> gives up (outcome=no_slot), books nothing
  [PASS] default booking_max_wait_minutes is 12h   (timeout=43200s = 12h)
```

Docker image rebuilt on the merge: run `42898155471` ✓ all steps green.

> **Note**: this branch also carries `ed25f08`, the cherry-pick of the `no_slot` check from `docs/ai-explain-taken-slot-retry` (that branch was still unmerged when this one was cut, so its harness addition had to be brought along). Merging PR #8 therefore closed out that work too.

### Full Context Notes for AI Agents

> **Purpose**: Self-contained knowledge base to resume T11 without other context.

- **Base**: `main` (T9/T10 already merged, PR #7 / merge `33d261b`). Branch created from it: `feat/ai-booking-max-wait-12h`.
- **Exact call sites of the old literal 360** (content anchors; verify before editing):
  - `padel_booking.py` `run_booking_race` — `timeout = max(retry_secs, int(cfg.get("booking_max_wait_minutes", 360)) * 60)` — the ONLY functional use.
  - `cmd_autobook` — `maxw = int(cfg.get("booking_max_wait_minutes", 360))` (printed in messages).
  - `book_day` inside `run_autobook_loop` — `not_yet_open` log line.
  - `run_autobook_loop` startup log — "up to N min each".
- **Semantics reminder**: this bounds ONE `run_booking_race` attempt, not the whole program. The scheduler (`run_autobook_loop`) re-attempts on its three triggers (start / prefs change / new date offered), so a taken day still stops after one bounded attempt — see T10 notes and the 6th check in `ai/verify_t9_behaviors.py`.
- **Verification command**: `python -c "import padel_booking as p; print(p.BOOKING_MAX_WAIT_MINUTES)"` → 720. Then re-run `python ai/verify_t9_behaviors.py` → ALL PASS (the harness passes an explicit CFG, so it must stay green unchanged).
- **Deploy**: same flow as T10 — PR to `main`, watch the `Build and Push Docker Image` run to success, then the user runs `docker compose pull && docker compose up -d` on the host (`docker` absent on this dev machine).
- **Git identity**: only `GIT_AUTHOR_NAME`/`GIT_COMMITTER_NAME` = `AI_bot`; never set email; `[ai]` tag after the colon; task log committed to the user's current branch (`main`).

---

## Task T10: Deploy the T9 fix — it was never merged

- **Status**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span>
- **Type**: <span style="background-color:#da3633;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">hotfix</span>
- **Created**: 11-11-2026 00:28:43
- **Last Updated**: 11-11-2026 00:33:27
- **Time Spent**: 5m
- **Branch**: [`fix/ai-deploy-portal-bookable-dates`](https://github.com/HectorBravo/padel_booking_alrayyana/tree/fix/ai-deploy-portal-bookable-dates)
- **Commit(s)**: [9716769](https://github.com/HectorBravo/padel_booking_alrayyana/commit/9716769) (verification harness, on the AI branch) · merge [33d261b](https://github.com/HectorBravo/padel_booking_alrayyana/commit/33d261b) (PR #7 → `main`) · T9 code now on `main` via [695baee](https://github.com/HectorBravo/padel_booking_alrayyana/commit/695baee)
- **Blockers**: none
- **Findings & Notes**:
  - **The user's report is correct, and the cause is not a bad fix — it is an unmerged one.** [T9](#task-t9-derive-bookable-days-from-the-portal) implemented everything requested, but its code commits (`fefb096`, `9d0d2c4`) live **only** on `fix/ai-portal-bookable-dates`. `git merge-base --is-ancestor origin/fix/ai-portal-bookable-dates origin/main` → exit 1 (**not merged**). Only the *task-log* commits from T9 landed on `main`, which is why the log looked finished while production never changed.
  - **Proof production runs the old code**: searching `main`'s two `.py` files for `parse_bookable|bookable_dates|/window|cmd_window` → **0 matches**. The whole portal-derived-window feature is absent from `main`, so the deployed image still offers `today..today+6` in the bot and never waits for a date to open.
  - **Why a restart cannot help**: `entrypoint.sh` re-copies the `.py` files from `/opt/padel` (the image, built off `main`) into `/app` on every start. Only a merge + image rebuild activates T9.
  - **Both behaviors the user asked to verify ARE implemented on the branch** (read at `af33dcc`; both files pass `py_compile`):
    - *Bot offers only portal-opened days*: `padel_telegram.py:_show_date_keyboard:731-758` builds the keyboard from `portal_bookable_dates(self.cfg)` instead of `for i in range(7)`; `_show_slots:760-786` refuses an unopened date before offering slots; `/window` (`:683-718`) marks each day `bookable` / `NOT OPEN YET`.
    - *Autobooking waits until the day opens*: `padel_booking.py:run_booking_race:2122-2174` refreshes every `slot_retry_minutes` (default 2) with session keepalive and books as soon as a preferred slot is free; it hard-stops only for dates outside the rolling window.
  - **Config note**: `slot_retry_minutes` / `booking_max_wait_minutes` are new on the branch (`config.sample.json:16-17`) and absent from the host's `config.json`; both have safe in-code defaults (2 min / 360 min), so no host config edit is needed.

### User Confirmations

**Pending (awaiting user response):**

None.

**Confirmed (user provided):**

- (11-11-2026 ~00:28) "you didn't fix my request with the last commits … still no option to book for the 17th on the web but the bot offers it; verify it will autobook until the day opens" → root cause is the unmerged T9 branch; deploying it is the fix.

### Subtasks / Plan

- [x] Confirm `origin/fix/ai-portal-bookable-dates` is not an ancestor of `main` (exit 1 before the merge → confirmed unmerged)
- [x] Merge the T9 code into `main` via PR so the Docker image rebuilds from it — [PR #7](https://github.com/HectorBravo/padel_booking_alrayyana/pull/7), merged as `33d261b`
- [x] Confirm the `Build and Push Docker Image` workflow run succeeds on the merge — run `42896328459` ✓ (all steps green, image republished)
- [x] Re-verify post-merge that `main` contains `portal_bookable_dates`, the portal-driven date keyboard, and the wait-until-open loop — `padel_booking.py:792`, `padel_telegram.py:731`, retry log at `padel_booking.py:2165`; `py_compile` clean
- [x] Record verification of both behaviors with executable evidence — `ai/verify_t9_behaviors.py`, **ALL PASS** (5 checks, see Full Context Notes)
- [ ] (user) On the production host: `docker compose pull && docker compose up -d` to activate the new image

### Verification results (`python ai/verify_t9_behaviors.py`)

Network-free harness: every portal entry point is patched, so it runs anywhere.

| Check | Result | Evidence |
|-------|--------|----------|
| In-window day NOT OPEN YET → keeps waiting, never books | PASS | `outcome=not_yet_open`, 0 booking attempts, slept 840s over 7 refreshes (2 min each), keepalive ran 7× |
| Portal opens the day on the 3rd refresh → books then | PASS | `outcome=slot_found`, 1 booking attempt after 2 waits |
| Day open but slot taken, then free → books when free | PASS | `outcome=slot_found`, retried twice before booking |
| Date beyond the 7-day window → stops at once, no waiting | PASS | `outcome=outside_window`, 0 attempts, 0s slept |
| Bot date keyboard offers ONLY portal-opened days | PASS | 4 of 7 window days opened → exactly those 4 buttons |

### Full Context Notes for AI Agents

- **State at start (11-11-2026 00:28)**: `main` = `358a739`; `origin/fix/ai-portal-bookable-dates` = `af33dcc` (docs only) over `9d0d2c4` (code round 2) over `fefb096` (code round 1). Working tree clean; the T9 branch exists only under `remotes/origin/`.
- **Why a PR is required**: previous fixes reached production via PRs #4/#5/#6 → merge to `main` → `.github/workflows/docker-publish.yml` republishes `hecbr/padel-booking-alrayyana:latest`. Reproduce that exactly; do not re-implement T9.
- **Inspecting the branch safely**: `git worktree add C:\temp\t9wt origin/fix/ai-portal-bookable-dates --detach` (created; clean up with `git worktree remove C:\temp\t9wt`). Avoid `git show … > file` in PowerShell — it emits UTF-16 and `py_compile` then fails with "source code string cannot contain null bytes".
- **Verification done**: `python -m py_compile padel_booking.py padel_telegram.py` on the branch files → both exit 0.
- **Key anchors at `af33dcc`**:
  - `padel_booking.py`: `portal_bookable_dates:792`, `_bookable_dates_cached/_store:778-789` (TTL 60s), option parsing `:736-776`, `window_limit:846`, `bookable_window_note:868`, `cmd_window:1049`, `bookable_targets` (candidacy = rolling window; picker only orders), `run_booking_race:2049-2174`, autobook loop `:2290+` (`outside_window` → stop, `not_yet_open` → retry later).
  - `padel_telegram.py`: imports `portal_bookable_dates, window_label, bookable_window_note` at `:38`; `/window` `:683-718`; `_show_date_keyboard:731-758`; `_show_slots:760-786`; `_on_autobook:849+`.
- **Post-merge check (must pass)**: `git merge-base --is-ancestor origin/fix/ai-portal-bookable-dates origin/main` → exit 0, and `portal_bookable_dates` present in `main:padel_booking.py`.
- **Production step is user-side**: `docker` is not installed here. After the rebuild the host runs `docker compose pull && docker compose up -d`; `entrypoint.sh` re-syncs the `.py` files into `/app`, activating the new behavior at that restart.
- **Git identity**: override only `GIT_AUTHOR_NAME="AI_bot"` / `GIT_COMMITTER_NAME="AI_bot"`; never set author/committer email (user's is `hbravo@cuic.net`). Commit tag `[ai]` after the colon. Task log goes to the user's current branch (`main`).

---


- **Status**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span>
- **Type**: <span style="background-color:#9e6a03;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">fix</span>
- **Created**: 08-10-2026 00:27:49
- **Last Updated**: 08-10-2026 01:04:46
- **Time Spent**: 6m
- **Branch**: [`fix/ai-mybookings-timeout-90s`](https://github.com/HectorBravo/padel_booking_alrayyana/tree/fix/ai-mybookings-timeout-90s)
- **Commit(s)**: [13be7da](https://github.com/HectorBravo/padel_booking_alrayyana/commit/13be7da) (code on AI branch), [526321f](https://github.com/HectorBravo/padel_booking_alrayyana/commit/526321f) (merge to `main`)
- **Blockers**: none
- **Findings & Notes**:
  - The `/booking/myBooking` calls in `padel_booking.py` pass **no** explicit timeout, so they inherit curl_cffi `Session`'s default of **30s** (verified live: `requests.Session(impersonate="chrome").timeout == 30`).
  - This makes the slow mybookings endpoint (~4.5s normally) vulnerable to timeouts when the portal is sluggish. Bumping to 90s gives ~3× headroom without affecting other endpoints.
  - Fix is surgical: a single `MYBOOKINGS_TIMEOUT = 90` constant applied to the 3 mybookings call sites only.
  - **Deployed to production** on user request ("llevalo a produccion"): opened [PR #4](https://github.com/HectorBravo/padel_booking_alrayyana/pull/4) and merged it to `main` (merge commit `526321f`). The `Build and Push Docker Image` workflow (`.github/workflows/docker-publish.yml`) ran on the merge (run `37686567087`, ✓ success in 36s) and republished `hecbr/padel-booking-alrayyana` (tags `latest` + SHA) to Docker Hub **with the 90s timeout**. The change is therefore live for any new container pull.
  - **Remaining host step (user)**: on the production host, `docker compose pull && docker compose up -d` to pull the new image and restart the running container (the entrypoint re-syncs the `.py` files on start). `docker` is not installed on this dev machine, so the pull/restart must run on the host.

### User Confirmations

**Pending (awaiting user response):**

None.

**Confirmed (user provided):**

- (08-10-2026 ~01:00) "¿Llevarlo a producción?" → "llevalo a produccion" (user approved merging to `main` / rebuilding the production image).

### Subtasks / Plan

- [x] Add `MYBOOKINGS_TIMEOUT = 90` constant in `padel_booking.py` (next to `MYBOOKINGS_URL` / `CANCEL_URL`)
- [x] Apply `timeout=MYBOOKINGS_TIMEOUT` to `_fetch_my_bookings_fast` (POST), `_fetch_mybookings_page` (POST), and `fetch_my_bookings` (GET)
- [x] Verify with `py_compile` and confirm no other call sites changed
- [x] Deploy to production: open PR #4, merge to `main`, confirm Docker image rebuilt
- [ ] (user) On the production host: `docker compose pull && docker compose up -d` to activate the new image

### Full Context Notes for AI Agents

> **Purpose**: Self-contained knowledge base to resume without other context.

- **File**: `padel_booking.py`
- **Library**: `padel_booking.py` imports `from curl_cffi import requests` (line 71), NOT stdlib `requests`. `curl_cffi.requests.Session` uses `**kwargs`/`BaseSessionParams` and stores the timeout as `self.timeout`; when a request method is called without an explicit `timeout`, it falls back to `self.timeout`, whose default is **30** (confirmed by instantiating a session: `self.timeout = 30`).
- **Shared session**: `_SESSION` (line 194) is created by `new_session()` (line 148) → `requests.Session(impersonate="chrome")`. No timeout is set there, so the 30s library default applies to every request that doesn't pass one.
- **The 3 call sites that hit `/booking/myBooking`** (all currently timeout-less):
  1. `_fetch_my_bookings_fast` (line ~571): `r = s.post(f"{BASE_URL}/booking/myBooking", data=data, allow_redirects=True)` → add `timeout=MYBOOKINGS_TIMEOUT`.
  2. `_fetch_mybookings_page` (line ~1175): `r = s.post(f"{BASE_URL}/{post_url}", data=data, allow_redirects=True)` → add `timeout=MYBOOKINGS_TIMEOUT`.
  3. `fetch_my_bookings` (line ~1191): `r = s.get(MYBOOKINGS_URL, allow_redirects=True)` → add `timeout=MYBOOKINGS_TIMEOUT`.
- **Where the constant goes**: `padel_booking.py` lines 1062-1063 already define `MYBOOKINGS_URL = f"{BASE_URL}/booking/myBooking"` and `CANCEL_URL = f"{BASE_URL}/booking/cancelAmenityBooking"`. Add `MYBOOKINGS_TIMEOUT = 90` immediately after (line 1064 area). Note `_fetch_my_bookings_fast` (line 559) is defined *before* this constant block but Python resolves the global at call time, so this is safe.
- **Do NOT touch** the many other timeout values in `google_calendar.py` (all `timeout=30` — that's a different service) or the Telegram `get_updates` / autobook-race `timeout=18000` / `event.wait(timeout=900)` values.
- **Base**: `41ff437` (current `main` HEAD). **Branch**: `fix/ai-mybookings-timeout-90s` (create from `main`, push to origin). **User's current branch**: `main` (the task log is committed/pushed to `main`).

---

## Task T4: Make Google Calendar sync non-fatal

- **Status**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span>
- **Type**: <span style="background-color:#9e6a03;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">fix</span>
- **Created**: 05-10-2026 22:49:53
- **Last Updated**: 05-10-2026 22:55:00
- **Time Spent**: 5m
- **Branch**: [`fix/ai-nonfatal-gcal-sync`](https://github.com/HectorBravo/padel_booking_alrayyana/tree/fix/ai-nonfatal-gcal-sync)
- **Commit(s)**: [d38bcc2](https://github.com/HectorBravo/padel_booking_alrayyana/commit/d38bcc2)
- **Blockers**: none
- **Findings & Notes**:
  - `sync_booking` calls in `cmd_book`, `cmd_autobook`, and `run_autobook_loop` were not wrapped in try/except
  - A Google Calendar API failure would crash the booking flow
  - Added non-fatal error handling with user-friendly messages in all 3 call sites

### User Confirmations

None yet.

### Subtasks / Plan

- [x] Wrap `sync_booking` in try/except in `cmd_book`
- [x] Wrap `sync_booking` in try/except in `cmd_autobook`
- [x] Wrap `sync_booking` in try/except in `run_autobook_loop`

### Full Context Notes for AI Agents

> **Purpose**: Self-contained knowledge base to resume without other context.

- **File**: `padel_booking.py`
- **3 call sites** for `sync_booking`:
  1. `cmd_book` (~line 860): After booking confirmed, calls `sync_booking(args[0], target["label"], cfg, description)`
  2. `cmd_autobook` (~line 1791): After booking confirmed, calls `sync_booking(target_str, best["label"], cfg, cfg.get("description", "Padel booking"))`
  3. `run_autobook_loop` (~line 1957): In the autobook loop, calls `sync_booking(target_str, best["label"], cfg, cfg.get("description", "Padel booking"))`
- **Change**: Each call site now wraps `sync_booking` in try/except, prints/logs a non-fatal warning on failure, and notifies the user
- **Base**: `a7fbf78` (current main HEAD)
- **Branch**: `fix/ai-nonfatal-gcal-sync`

---

## Task T1: Reconcile booked.json against portal

- **Status**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span>
- **Type**: <span style="background-color:#9e6a03;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">fix</span>
- **Created**: 10-04-2026 14:47:00
- **Last Updated**: 10-04-2026 15:20:00
- **Time Spent**: 15m
- **Branch**: [`fix/ai-reconcile-booked-json`](https://github.com/HectorBravo/padel_booking_alrayyana/tree/fix/ai-reconcile-booked-json)
- **Commit(s)**: [34410be](https://github.com/HectorBravo/padel_booking_alrayyana/commit/34410be)
- **Blockers**: none
- **Findings & Notes**:
  - `booked.json` accumulates stale entries when portal-side booking expires
  - Added `remove_booking_record(date_str)` and `_reconcile_booked(cfg)` to `padel_booking.py`
  - Reconciliation runs on startup and every keepalive cycle
  - Telegram cancellation handler calls `remove_booking_record` so autobook can retry

### User Confirmations

None yet.

### Subtasks / Plan

- [x] Add `remove_booking_record()` function to `padel_booking.py`
- [x] Add `_reconcile_booked()` reconciliation function
- [x] Call reconciliation in `refresh_state()` and keepalive loop
- [x] Call `remove_booking_record` in Telegram cancellation handler

### Full Context Notes for AI Agents

> **Purpose**: Self-contained knowledge base to resume without other context.

- **Files**: `padel_booking.py`, `padel_telegram.py`
- **`remove_booking_record(date_str)`**: Removes entry matching `YYYY-MM-DD` from `booked.json`
- **`_reconcile_booked(cfg)`**: Calls `get_mybookings(cfg)`, prunes `booked.json` entries not in portal response
- **Startup**: `refresh_state()` calls `_reconcile_booked(cfg)`
- **Keepalive**: Loop calls `_reconcile_booked(cfg)` each cycle
- **Telegram cancel**: Parses formatted date, calls `remove_booking_record()`
- **Base**: `31a3ee6` | **Branch**: `fix/ai-reconcile-booked-json` | **Commit**: `34410be`

---

## Task T2: Google Calendar sync integration

- **Status**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span>
- **Type**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">feat</span>
- **Created**: 10-04-2026 14:47:00
- **Last Updated**: 10-04-2026 15:25:00
- **Time Spent**: 20m
- **Branch**: [`feat/ai-google-calendar-integration`](https://github.com/HectorBravo/padel_booking_alrayyana/tree/feat/ai-google-calendar-integration)
- **Commit(s)**: [b03f5d0](https://github.com/HectorBravo/padel_booking_alrayyana/commit/b03f5d0)
- **Blockers**: none
- **Findings & Notes**:
  - New `google_calendar.py` — OAuth + Calendar API client
  - `/setupgoogle` interactive Telegram flow for OAuth
  - `sync_booking` / `sync_cancellation` are non-fatal (log + notify, never break booking)
  - Docker: port 8010 for OAuth redirect, `google_calendar.py` in COPY/entrypoint

### User Confirmations

None yet.

### Subtasks / Plan

- [x] Create `google_calendar.py` with OAuth + Calendar API client
- [x] Add `/setupgoogle` interactive Telegram flow
- [x] Hook `sync_booking` into booking confirmation
- [x] Hook `sync_cancellation` into cancellation handler
- [x] Update Docker files (COPY, entrypoint, port 8010)
- [x] Update `config.sample.json` with `google_calendar` section

### Full Context Notes for AI Agents

> **Purpose**: Self-contained knowledge base to resume without other context.

- **`google_calendar.py`**: `sync_booking(date,time,cfg)`, `sync_cancellation(date,time,cfg)`, `start_auth_flow(cfg)`, `complete_auth_flow(code,cfg)`. Local HTTP server on port 8010 for OAuth redirect.
- **`padel_booking.py`**: Imports `sync_booking`, calls after successful booking (try/except)
- **`padel_telegram.py`**: Imports GC functions. `/setupgoogle` command handles OAuth flow. Cancel handler calls `sync_cancellation`. Booking confirmation calls `sync_booking`.
- **Docker**: `Dockerfile` COPY + `entrypoint.sh` cp include `google_calendar.py`. `docker-compose.yml` exposes port 8010.
- **Config**: `google_calendar` section: enabled, email, calendar, client_id, client_secret, refresh_token, timezone
- **Base**: `31a3ee6` | **Branch**: `feat/ai-google-calendar-integration` | **Commit**: `b03f5d0`

---

## Task T3: Install nano in Docker image

- **Status**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">done</span>
- **Type**: <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">feat</span>
- **Created**: 10-04-2026 15:10:00
- **Last Updated**: 10-04-2026 15:30:00
- **Time Spent**: 5m
- **Branch**: [`feat/ai-install-nano`](https://github.com/HectorBravo/padel_booking_alrayyana/tree/feat/ai-install-nano)
- **Commit(s)**: [c579ded](https://github.com/HectorBravo/padel_booking_alrayyana/commit/c579ded)
- **Blockers**: none
- **Findings & Notes**:
  - Added `nano` to the existing `apt-get install` line in `Dockerfile`
  - Useful for debugging: `docker exec -it padel-booking-bot nano /app/config.json`

### User Confirmations

None yet.

### Subtasks / Plan

- [x] Add `nano` to `apt-get install` in Dockerfile
- [x] Commit and push to `feat/ai-install-nano`

### Full Context Notes for AI Agents

> **Purpose**: Self-contained knowledge base to resume without other context.

- **File**: `Dockerfile` only
- **Change**: Added `nano \` to the `apt-get install -y --no-install-recommends` list (after `util-linux`)
- **Base**: `31a3ee6` | **Branch**: `feat/ai-install-nano` | **Commit**: `c579ded`

---

## Completed Tasks

| Created | Task | Type | Subtasks | Time Spent |
|---------|------|------|----------|------------|
| 16-11-2026 16:05:49 | [T25: /login should also confirm the Google integration state](#task-t25-login-should-also-confirm-the-google-integration-state) | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">feat</span> | 4/4 | 17m |
| 16-11-2026 15:18:34 | [T24: OTP login fails with a correctly captured OTP (finishable pending stub)](#task-t24-otp-login-fails-with-a-correctly-captured-otp-finishable-pending-stub) | <span style="background-color:#9e6a03;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">fix</span> | 6/6 | 46m |
| 16-11-2026 07:51:02 | [T23: /login stopped working — swallowed by the auto-started Google setup flow](#task-t23-login-stopped-working--swallowed-by-the-auto-started-google-setup-flow) | <span style="background-color:#9e6a03;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">fix</span> | 5/5 | 1h 2m |
| 13-11-2026 13:34:10 | [T17: Auto-capture the OTP from Gmail instead of asking the user](#task-t17-auto-capture-the-otp-from-gmail-instead-of-asking-the-user) | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">feat</span> | 9/9 | 18m |
| 11-11-2026 02:50:02 | [T15: Merge every AI branch into main and delete them](#task-t15-merge-every-ai-branch-into-main-and-delete-them) | <span style="background-color:#57606a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">chore</span> | 5/5 | 4m |
| 11-11-2026 01:04:22 | [T13: Treat the picker as the real window limit (7-day assumption is wrong)](#task-t13-treat-the-picker-as-the-real-window-limit-7-day-assumption-is-wrong) | <span style="background-color:#da3633;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">hotfix</span> | 5/6 | 14m |
| 11-11-2026 00:50:01 | [T12: Telegram notice on first retry while a day is unavailable](#task-t12-telegram-notice-on-first-retry-while-a-day-is-unavailable) | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">feat</span> | 4/5 | 8m |
| 11-11-2026 00:52:00 | [T11: Default booking_max_wait_minutes to 12h](#task-t11-default-booking_max_wait_minutes-to-12h) | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">feat</span> | 4/5 | 7m |
| 09-11-2026 00:41:06 | [T9: Derive bookable days from the portal](#task-t9-derive-bookable-days-from-the-portal) | <span style="background-color:#9e6a03;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">fix</span> | 20/20 | 25m |
| 11-11-2026 00:28:43 | [T10: Deploy the T9 fix — it was never merged](#task-t10-deploy-the-t9-fix--it-was-never-merged) | <span style="background-color:#da3633;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">hotfix</span> | 5/6 | 5m |
| 09-11-2026 00:26:36 | [T8: Merge and delete three AI branches](#task-t8-merge-and-delete-three-ai-branches) | <span style="background-color:#57606a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">chore</span> | 7/7 | 5m |
| 11-10-2026 15:50:19 | [T7: Diagnose OTP login hang; session check via /dashboard](#task-t7-diagnose-otp-login-hang-session-check-via-dashboard) | <span style="background-color:#9e6a03;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">fix</span> | 8/10 | 17m |
| 08-10-2026 01:47:09 | [T6: Raise portal-wide session timeout to 90s](#task-t6-raise-portal-wide-session-timeout-to-90s) | <span style="background-color:#9e6a03;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">fix</span> | 5/5 | 10m |
| 08-10-2026 00:27:49 | [T5: Increase mybookings timeout to 90s](#task-t5-increase-mybookings-timeout-to-90s) | <span style="background-color:#9e6a03;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">fix</span> | 4/5 | 6m |
| 05-10-2026 22:49:53 | [T4: Make Google Calendar sync non-fatal](#task-t4-make-google-calendar-sync-non-fatal) | <span style="background-color:#9e6a03;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">fix</span> | 3/3 | 5m |
| 10-04-2026 14:47:00 | [T1: Reconcile booked.json against portal](#task-t1-reconcile-bookedjson-against-portal) | <span style="background-color:#9e6a03;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">fix</span> | 4/4 | 15m |
| 10-04-2026 14:47:00 | [T2: Google Calendar sync integration](#task-t2-google-calendar-sync-integration) | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">feat</span> | 6/6 | 20m |
| 10-04-2026 15:10:00 | [T3: Install nano in Docker image](#task-t3-install-nano-in-docker-image) | <span style="background-color:#22863a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">feat</span> | 2/2 | 5m |  - **Messages mirrored to /login (15-11-2026 17:24, commit 025a028)**: rewrote `_ensure_login_startup` to push the SAME user-facing messages as the /login + _otp_from_gmail path via `self._notify`: "No live session at startup - logging in now", "Google integration is on - reading the OTP from Gmail...", "Login successful - session saved (OTP {otp} read from Gmail)", plus failure notifies. So a boot login is visible to the user, not just console. Redeploy proof: deleted session.json + compose up; startup login fired, autobook relogin captured fresh 259906 -> Login successful; login-status valid=True.
