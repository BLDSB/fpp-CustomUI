# Hardening Notes — Field Deployment Review

Review pass for unattended Raspberry Pi deployments (remote sites, Dataplicity
access, unreliable power/network). Architecture note: this "plugin" is a
standalone Flask service (`fpp-ui.service`) running beside fppd and talking to
it over the REST API — it is not loaded into FPP's process, so "plugin
lifecycle" concerns map to systemd lifecycle + FPP API availability.

## 1. Error Handling & Failure Modes

**Found:** Several places where malformed data or a failed FPP call produced an
unhandled exception (HTTP 500) or a silently wrong result.

**Changed:**
- `app/routes/__init__.py` (`set_brightness`): if reading FPP's output
  processors failed, the code wrote back an **empty list plus brightness**,
  silently deleting every other output processor. Now aborts with 502 instead.
- `app/alert_monitor.py`: SMTP port / alert delay / schedule day-mask parsed
  with a safe `_to_int()` (garbage → default) so one bad settings row can't
  break alerting every poll cycle; `_parse_hms` now range-checks H/M/S so a
  malformed schedule entry can't blow up `datetime()`. Alert emails retry
  3× with 10 s spacing — a one-shot "show is dark" alert shouldn't be lost to
  a network blip.
- `app/routes/scenes.py` / `effects.py`: playlist-registration POSTs now call
  `raise_for_status()` so an FPP error response is logged instead of silently
  treated as success. Corrupt stored hex colors skip the zone with an ERROR
  log instead of crashing scene apply with a 500.
- `app/models.py`: `EffectPreset.to_dict()` tolerates corrupted JSON columns
  (returns `[]`) instead of raising on every preset listing.
- `app/__init__.py`: app-wide 500 handler rolls back the SQLAlchemy session
  (so one failed request can't poison the next) and returns JSON on
  `/api/`+`/internal/` paths.
- Brightness reads (`controls`, `get_brightness`) tolerate a corrupt stored
  value and a broken DB (default 100) instead of 500-ing the Controls page.

## 2. Input Validation & Sanitization

**Found:** Some API boundaries accepted unvalidated types/strings.

**Changed:**
- `effects.py`: `GET /api/effects/args/<path:name>` forwarded the raw name
  (including slashes/`..`) into the FPP URL — now rejected and URL-quoted.
  `models`/`args`/`systems` payload fields are type-checked as lists on
  run/stop/save-preset. Non-list `/overlays/effects` payloads handled.
- `settings.py` (`save_settings`): `alert_smtp_port` (1–65535),
  `alert_delay_minutes` (1–1440), `genius_pro_count` (0–8) validated as ints;
  `genius_pro_url_*` must be http(s). `genius_reboot` re-validates the stored
  URL scheme before calling out.
- `settings.py` (`restore_backup`): scene zones restricted to known
  `OVERLAY_MODELS` + valid hex (these values are replayed against FPP and
  parsed as hex later); saved colors validated; duplicate scene names skipped
  (Scene.name is UNIQUE — one dupe used to abort the entire restore);
  non-dict/non-list shapes tolerated; commit failure rolls back with a 500
  instead of a half-restore.
- `scenes.py` (`create_scene`): a scene where every zone was invalid used to
  be created empty — now rejected with 400.
- `playlists.py`: playlist/sequence listings filter non-string entries
  (mixed types used to crash `sorted()`).
- `app/config.py`: `FPP_BASE_URL` normalized (trailing slash stripped, non-http
  garbage falls back to `http://localhost/api` with a warning).

## 3. Resource Management

**Found:** The append-only `fpp-ui.log` had no rotation — guaranteed SD-card
fill on a long-running unit. Flask dev server spawns an unbounded thread per
request.

**Changed:**
- Added `deploy/fpp-ui.logrotate` (weekly/5 MB, 4 rotations, `copytruncate`
  because systemd holds the fd), installed by `fpp_install.sh`, removed by
  `fpp_uninstall.sh`.
- `run.py` now serves via **waitress** (fixed 8-thread pool, production WSGI
  server) with a logged fallback to the dev server if the dependency is
  missing. Added `waitress==3.0.2` to requirements.
- Login-throttle table is pruned when it exceeds 100 entries.
- Temp files from atomic writes are cleaned up on their error paths.

## 4. Concurrency & Thread Safety

**Found:** Two races and a lock-starvation risk around SQLite.

**Changed:**
- `models.get_all_zones()`: two request threads racing the first-call seeding
  hit a primary-key collision and 500 — now rolls back, logs, and serves
  queryable + transient defaults.
- `auth.py`: `.env` writes serialized behind a module lock (two concurrent PIN
  changes could interleave file rewrites).
- `app/config.py`: SQLite `timeout=15` busy-wait, since the alert-monitor
  thread and request threads share the DB file ("database is locked" was
  possible under write overlap).
- The alert monitor's shared `_pending` dict was already correctly
  lock-protected — no change.

## 5. Lifecycle (systemd, standing in for the FPP plugin contract)

**Found:** `Restart=on-failure` leaves the UI down after a clean-exit bug, and
systemd's default start-limit can give up entirely; a missing `.env` prevented
startup outright (`EnvironmentFile=` without `-`).

**Changed (`deploy/fpp-ui.service`):** `Restart=always`,
`StartLimitIntervalSec=0`, `EnvironmentFile=-…`. FPP show start/stop and
playlist transitions hold no state in this app (overlays are FPP-side and
cleared via playlist leadOut), and SIGTERM/SIGKILL leave nothing to clean up —
the startup sync (see §9) reconverges state after any restart.

## 6. Configuration & Defaults

**Found:** Non-atomic writes to three config files; world-readable secrets;
weak default secret key.

**Changed:**
- `auth.py` `_persist_pin`: `.env` updated via copy → `set_key` on the copy →
  `chmod 600` → `os.replace`. A crash or full disk mid-write can no longer
  truncate `.env` (which would wipe every secret on the box).
- `app/__init__.py`: `commandPresets.json` written atomically
  (temp + fsync + rename); if the existing file is corrupt it is left alone
  rather than clobbered. Same atomic pattern for `model-overlays.json` in
  `settings.py`.
- `app/config.py`: missing/placeholder `SECRET_KEY` now generates an ephemeral
  random key (sessions stay unforgeable; they just reset on restart) with a
  logged warning, instead of the guessable `"change-me-in-production"`.
- Installer/reset/set-path scripts `chmod 600 .env` (it was 644 —
  world-readable session secret, internal token, PIN hashes).

## 7. Logging & Diagnostics

**Found:** No `logging.basicConfig` — the root logger defaulted to WARNING with
no timestamps, so the alert monitor's INFO lines never reached `fpp-ui.log` and
nothing could be correlated with FPP's logs.

**Changed:**
- `run.py`: timestamped INFO-level logging configured before app import.
- `app/__init__.py`: startup banner logs resolved config (FPP URL, UI path, DB,
  whether the admin PIN / master PIN / internal token are set — values never
  logged) so a remote operator can diagnose a misconfigured unit from the log
  alone. The deferred FPP sync logs each retry and its outcome.
- Failed logins and throttle events log the client IP. Error paths throughout
  now log what was attempted and what the fallback is. No DEBUG logging in
  hot paths (the 60 s monitor loop logs at DEBUG only when FPP is unreachable).

## 8. Security & Permissions

**Found:** Unthrottled PIN login; world-readable `.env`; path traversal shape
in the effect-args proxy.

**Changed:**
- `auth.py`: per-IP login throttling — 5 free attempts, then an exponential
  lockout (30 s → 15 min). A 4-digit PIN space (10 000 codes) was otherwise
  brute-forceable from the LAN in minutes. `bcrypt` checks also no longer 500
  on a corrupt stored hash (treated as non-match with an ERROR log pointing at
  `fpp_reset_pin.sh`).
- `.env` permissions tightened to 600 everywhere it is created or rewritten.
- Effect-name traversal rejection (§2). Root use was already tightly scoped
  (sudoers limited to the root-owned `fpp-ui-set-path` helper, which
  re-validates its input) — no change needed.

## 9. Startup Robustness

**Found:** The biggest field bug in the review. At boot, `create_app()`
regenerated scene/effect playlists **synchronously at import time** — if fppd
wasn't up yet (boot order on a Pi is not guaranteed), the sync failed once,
logged a warning, and never retried, leaving scheduled scenes broken until the
next manual restart. `db.create_all()` failure also crash-looped the service.

**Changed (`app/__init__.py`):**
- All FPP-dependent startup work moved to a daemon thread that polls
  `/fppd/status` with exponential backoff (5 s → 60 s, up to ~10 min) and only
  then regenerates playlists. Failure after the deadline is logged at ERROR.
- `db.create_all()` failure now logs a precise diagnosis and lets the service
  stay up (readable over Dataplicity) instead of flapping under systemd.
- Install script reordered: service restart moved to the very end (after
  Apache config and `chown` back to fpp) and no longer aborts the install on
  failure; `hostname -I` can no longer kill a boot-time install under
  `set -e -o pipefail`.

## 10. Edge Cases & Stress

- **No show active / show already running:** all play actions stop current
  playback first; stop/clear calls treat 404 as "already off". No dangling
  state — verified by reading every FPP interaction path.
- **Disk full:** image uploads, `.env` writes, and both FPP config writes now
  fail with a logged, user-visible error instead of a stack trace, and atomic
  writes mean no file is left truncated.
- **Template render with broken DB:** the `site_settings` context processor
  (runs on *every* page, login included) now degrades to defaults instead of
  500-ing the whole UI.
- **Malformed playlist/schedule data from FPP:** non-list payloads, non-string
  entries, out-of-range times, and garbage day masks are all tolerated (§1/§2).
- Also fixed: `scripts/clear-overlay.sh` lacked the executable bit in git — FPP
  silently skips non-executable "Run Script" items (staged via
  `git update-index --chmod=+x`).

## Known Remaining Risks / Assumptions

- **LAN trust model:** anyone on the local network can reach the login page and
  (on an unprovisioned unit) claim it via first-run setup. Throttling slows
  PIN guessing but a 4-digit PIN is inherently weak; the master PIN and
  physical/network isolation are the real controls. Port 5000 is also directly
  reachable (not just via Apache) by design — Flask enforces its own login.
- **`/internal/` endpoints** rely on `INTERNAL_TOKEN` in playlist URLs, which
  is visible in FPP's playlist JSON to anyone with FPP web access. FPP's own
  UI is unauthenticated by default, so this is no weaker than FPP itself.
- **SMTP credentials** are stored in plaintext in the SQLite DB (as before) —
  file perms and LAN trust are the mitigation; use an app-specific password.
- **Ephemeral SECRET_KEY fallback** logs everyone out on restart when `.env` is
  missing — intentional trade-off vs. a forgeable static key.
- **FPP sync deadline:** if fppd takes longer than ~10 minutes to come up, the
  playlist regeneration is skipped until the next fpp-ui restart (logged at
  ERROR). Extending the wait indefinitely was judged worse than a loud log.
- Port 5000 is bound on all interfaces (not just via Apache) by design, so a
  device on the LAN can reach Flask directly; the login and the checks below
  still apply there.

## 11. Security Audit Pass (OWASP / auth / resilience)

Reviewed every route for authorization, injection, XSS, CSRF, upload handling
and error disclosure. SQL is all ORM or fixed-constant DDL (no injection
found); every route except login/setup/logout and the token-checked `/internal/`
pair requires a session (asserted for the whole URL map in `tests/test_security.py`).

| Finding | Risk | Fix |
|---|---|---|
| No CSRF defence beyond browser defaults | A page on another site could fire state-changing requests (reboot, restore, stop) at a logged-in admin's browser | `SESSION_COOKIE_SAMESITE=Lax` + HttpOnly; `app/security.py` rejects unsafe-method requests whose Origin/Referer host differs from the request host or Apache's `X-Forwarded-Host` (stock Apache does not forward the browser's Host, so in that case only a LAN-style origin — private IP, `.local`, single-label name — is accepted and public hostnames and `null` are refused; `TRUSTED_ORIGINS` in `.env` covers unusual proxies) |
| No security headers | Clickjacking and MIME sniffing | `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`; `Cache-Control: no-store` on `/api/` |
| Scripted SVG / disguised files accepted as branding images | Stored XSS on the UI's origin (the vhost CSP allows inline script) | `app/uploads.py` checks magic bytes and blocks scripts/handlers/`foreignObject` in SVGs, on upload and on restore; 8 MB cap |
| `hmac.compare_digest` on non-ASCII token | A junk `?token=` caused a 500 instead of 403 | One shared `internal_token_error()` compares bytes |
| Non-object / wrong-typed JSON bodies | `[1]` or `"name": 123` raised AttributeError (500) | `app/validation.py` (`json_object`, `str_field`) used by every route |
| Playlist names pasted into FPP URLs unencoded | `?`, `#`, `%` in a name rewrote the request path | `playlist_url()` percent-encodes |
| Restore chunk upload unbounded | An authenticated client could fill the SD card | Chunks may not exceed the declared total; 16 GB absolute cap |
| `change-pin` unthrottled | A stolen session could brute-force the current/master PIN | Shares the login lockout |
| No rate limiting beyond login | Reboot/restore/backup/upload could be hammered | `rate_limited()` on reboot, restore apply, backup, image upload, overlay rebuild |
| Raw exception text returned to the browser | Leaked the controller's address and connection internals | `fpp_error_text()` returns a plain sentence; detail goes to the log |
| API errors (404/405/413...) returned as HTML | The front end could not read them | JSON error bodies for `/api/` and `/internal/` |
| `err.message` placed in `innerHTML` | Server-supplied text rendered as markup | Wrapped in `escHtml()` |

## 12. FPP silently ignores overlay switch-on (field finding, 2026-10-09)

**Symptom:** scenes, colors and effects "do nothing" with no error anywhere; the
lights stay dark for minutes, then work again, often right after an fppd restart.

**Cause (reproduced on a Pi 3B+ running FPP 9.5.3):** after the "Stop Effects"
command the app sends before every scene or effect, FPP answers `200 OK` to
`PUT /overlays/model/<name>/state {"State": 1}` but leaves the model off
(`isActive: 0`). The fill still lands in the model's memory, so a check of the
data (`lit=100`) looks healthy while nothing is sent to the lights. A pause of
0.5 to 5 seconds before the switch-on does not help; sending the switch-on a
second time always does. Without the Stop Effects command it never happens.

**Fix:** `ensure_models_active()` in `app/fpp_api.py`. After switching models on,
read `/overlays/models`, and re-send the activation for any model still off, up to
twice. If one still will not switch on the request fails with a message naming it
instead of reporting success. If FPP cannot be asked, the original requests are
trusted. Used by scenes (including FPP's own `/internal/` callbacks), the color
picker and effects. Cost: about 0.25 s per apply, more when retries are needed
(a 15-zone scene took ~3 s while the quirk was firing every time).

**Related hardware finding:** the same Pi reported `Raspberry Pi Voltage Too Low`
(`vcgencmd get_throttled` = `0xd0005`, "Undervoltage detected" in `dmesg` every
~15 s). It may be what makes the quirk frequent. Check `vcgencmd get_throttled`
first when lights misbehave.

**Do not measure success by `lit` alone** when debugging: check `isActive`, and
watch UDP port 4048 to the output controller (`tcpdump -i eth0 host <ip>`).

## Not Changed (deliberately)

- Fire-and-forget `requests` calls in stop/clear paths (`_stop_current`,
  overlay deactivation loops): intentional best-effort semantics — a stop
  should proceed even if one model errors. These already had per-call
  exception handling.
- No new features, endpoints, or refactors beyond the issues above.
