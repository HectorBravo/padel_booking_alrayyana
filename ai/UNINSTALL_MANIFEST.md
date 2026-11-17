# Uninstall Manifest — Aldar Live reverse-engineering session (T27)

Everything this task installed, with exact removal commands. Nothing here touched
system-wide Windows config: no Windows features (Hyper-V NOT installed), no global
env vars (`JAVA_HOME`/`ANDROID_HOME` are set only inside our `.bat` files), and the
existing JDK (`D:\tools\jdk-17.0.2`) and `adb` were already on the machine — **not**
installed by us.

Date of inventory: 18-11-2026. Sizes measured at that moment.

## 1. Android SDK — `D:\android_sdk` (~4.5 GB)  ← biggest item

Installed with Google's command-line tools into a **new** folder we created
(`D:\android_sdk`). Contains:

| Path | What it is | Size (MB) |
|---|---|---|
| `cmdline\clt.zip` | downloaded commandlinetools-win-11076708 archive | 146 |
| `cmdline-tools\latest\` | sdkmanager / avdmanager / apkanalyzer | 146 |
| `emulator\` | Android Emulator binaries | 1070 |
| `system-images\android-34\google_apis_playstore\x86_64\` | Android 14 Google Play x86_64 image | 3107 |
| `platforms\android-34\` | platforms;android-34 | 0 |
| `platform-tools\` | adb / fastjson **newly downloaded** (v37.0.1) — we did NOT reuse the pre-existing copies for the SDK | ~5 |
| `licenses\`, `.temp\`, `.knownPackages` | sdkmanager bookkeeping | 103 |

Verified installed (`sdkmanager --list_installed`): `emulator 37.2.13`,
`platform-tools 37.0.1`, `platforms;android-34`,
`system-images;android-34;google_apis_playstore;x86_64` (rev 14).

Note: separate **pre-existing** adb copies remain untouched at `D:\android_tools\`
and `C:\RSL\platform-tools\` — do not delete those.

**Remove:**
```powershell
Remove-Item -LiteralPath 'D:\android_sdk' -Recurse -Force
# AVDs / emulator runtime data (created only once we boot one; delete if present):
Remove-Item -LiteralPath "$env:USERPROFILE\.android" -Recurse -Force
Remove-Item -LiteralPath "$env:USERPROFILE\.emulator_console_auth_token" -Force -ErrorAction SilentlyContinue
```

## 2. Python packages (pip, into `D:\tools\python`)

Installed by us: `frida` 17.27.0, `frida-tools` 14.13.2, `objection` 1.12.5,
`google-play-scraper` 1.2.7 (pre-existing), `google-play-api-unofficial` 0.2.0.

Pulled in as dependencies by us: `cli-helpers`, `configobj` 5.0.9, `delegator-py`
0.1.1, `litecli` 1.17.1, `pexpect` 4.9.0, `ptyprocess` 0.7.0, `sqlparse` 0.6.1,
`tabulate` 0.10.0, `websockets` 13.1.

⚠️ Before removing, confirm none of these were already present (several are common
transitive deps). Check with: `python -m pip show <name>`.

**Remove:**
```powershell
python -m pip uninstall -y frida frida-tools objection google-play-api-unofficial
# only if they were not pre-existing:
python -m pip uninstall -y configobj delegator-py litecli pexpect ptyprocess sqlparse tabulate websockets
```

## 3. npm global — `@playwright/cli` 0.1.24

Installed globally for the web capture (used to log in and record network traffic).

**Remove:**
```powershell
npm uninstall -g @playwright/cli
```
No browser binaries were downloaded into `%LOCALAPPDATA%\ms-playwright` (only empty
`b\` / `daemon\` folders, 0 MB) — it used the system Chrome channel.

## 4. Skills (agent-level, not on this machine's runtime)

Installed via the skills CLI: `microsoft/playwright-cli` → skill `playwright-cli`.

**Remove:** delete its entry from `%USERPROFILE%\.agents\skills\` and
`%USERPROFILE%\.agents\.skill-lock.json` (or `npx skills remove playwright-cli`).

## 5. Repo-local artifacts (safe to delete; already gitignored)

| Path | Contents |
|---|---|
| `ai_out\` | downloaded SPA bundles (`app.js`, `detail.js`, …), captured requests (`cap_*`), endpoint map `epmap.txt`, helper scripts (`find_vars.py`, `replay_pkg.py`, `yes.bat`, `sdk_install.bat`), browser storage state `aldar_pw_state.json` (**contains a live 180-day token — delete when done**) |
| `.playwright-cli\` | playwright-cli working dir (page snapshots, console logs) |
| `aldar_session.json` | our probe's saved session/token (gitignored) |

**Remove:**
```powershell
Remove-Item -LiteralPath 'ai_out' -Recurse -Force
Remove-Item -LiteralPath '.playwright-cli' -Recurse -Force
Remove-Item -LiteralPath 'aldar_session.json' -Force
```

## 6. NOT installed by us (do not remove)

- `D:\android_tools\` (`adb.exe`, `aapt.exe`) — pre-existing, we only used it.
- `C:\RSL\platform-tools\`, `C:\Program Files\Nox\` — pre-existing.
- `D:\tools\jdk-17.0.2` — pre-existing JDK we reused.
- No Windows features enabled; no services added; no registry changes; no global
  environment variables modified.
