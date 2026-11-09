# Automated test plan: fleet features

Covers the work on branches `feat/fleet-agent` (this repo, the plugin) and
`feat/fleet-monitoring` (the portal, `D:\Code\BLD Portal`). Nothing is committed yet.

## 1. Run everything (about 1 minute)

Plugin (this repo):

```bash
cd "D:/Code/Forest FPP"
python -m pytest -q          # expect: 247 passed
python -m pyflakes app tests # expect: only the known "app.models imported but unused" line
```

Portal:

```bash
cd "D:/Code/BLD Portal"
.venv/Scripts/python -m pytest -q   # expect: 232 passed (about 45 s)
```

Plugin plus the unmerged "editable saved colors" work that is also on the Pi (this is exactly
what is deployed to the .89 Pi): the scratch copy used for deployment passed **258**
(247 plus the 11 saved-color tests). To repeat it, check out `feat/editable-saved-colors`
changes on top of this branch, or just run the colors branch tests on the Pi's tree.

If anything fails, the two likely causes are a stale `.venv` (run
`.venv/Scripts/pip install -r requirements-dev.txt`, the portal now needs `cryptography`) or a
local `.env` leaking into the plugin tests (the suite pins its own environment, so it should not).

## 2. What each new test file protects

### Portal: `tests/test_fleet.py` (39 tests) plus the updated `tests/test_heartbeat.py`

| Area | Tests check |
|---|---|
| Heartbeat | new report fields kept, unknown ones dropped; one history sample per 5 minutes; a heartbeat closes an open outage; events de-duplicated when resent; queued commands handed out once and results recorded; another site's command id cannot be answered; stale commands never delivered; body cap |
| Uptime | an outage opens when quiet and is not duplicated; uptime maths; unknown before any history |
| Health alerts | sustained under-voltage mails once, reminds after a day, mails when cleared; one blip does not alert; disk and temperature thresholds; player-down needs to persist; controller events mailed once; assigned users alerted only when switched on |
| Remote actions | admin only (customer gets 403); duplicate refused; reboot/update/restore need the site address typed; unknown action rejected; restore accepts only that site's backup; revoking cancels waiting actions; waiting or answered-late actions fail on timeout |
| Backups | stored encrypted (plaintext not on disk); admin-only download; controller can fetch only its own backup; non-zip refused; size cap; newest N kept; unknown peer refused; deleting a site removes its files |
| Pages | fleet and site pages render with data; filters; new site options saved; customer dashboard shows next show and uptime but no fleet link and no temperature |
| Reports and housekeeping | month rollover; report goes to the customer list plus an admin digest, once per month; skipped when off or no history; plugin-revision lookup cached and failure-safe; "behind" logic; certificate warning mailed once a day; a full monitor pass runs clean |
| Tunnel listener | only `/api/heartbeat` and `/api/backup` answer; everything else is 404 |

### Plugin: `tests/test_fleet_agent.py` (52 tests) plus the updated `tests/test_heartbeat_sender.py`

| Area | Tests check |
|---|---|
| Activity log | changes logged without bodies or PINs; plain operation (play, read) not logged; failed changes not logged; good and bad logins logged with the role; trimmed to 2000 rows; served newest first |
| Staff PIN | must differ from the admin PIN; staff login lands on Controls; admin PIN still wins; **every route is walked as staff** (anything off the allow-list must be refused before its view runs, everything on it must pass); nav hides admin links; admin sessions unaffected |
| Pause and run | pausing disables every entry and resuming restores them exactly; pausing twice keeps the originals; schedule edits refused while paused (409); a failed save leaves nothing paused; length validation; the timer resumes; "skip tonight" end time (evening show, after-midnight show, currently playing, unknown); timed run starts, stops at the deadline, does not stop something else that took over; bad input refused |
| Show log | runs tracked and counted; unreachable player ignored; old runs not counted |
| Agent | report extras; events resent until accepted; allow-listed actions only; owner can switch maintenance off; results reported before a restart; results survive a restart; at most three commands at a time; commands in the heartbeat reply are run; update reports up to date / updated / failed |
| Backups | upload and remember; oversize refused; failure reported not raised; nightly only when opted in, once per day, in the 02:00-04:59 window; restore fetches, applies and reports; download failure and step errors reported |
| Options and show check | options default (maintenance on, backup off), saved and logged; show check for a dead player, nothing scheduled, paused schedule, missing files, low power |
| Monitor hook | show log updates even with email alerts off; a missed show is queued for the portal |

The Terms-scope test (`test_report_matches_what_the_terms_describe`) was updated on purpose:
the heartbeat now also carries `maintenance`, `backup_opt_in`, `shows_30d`, and (when known)
`next_show`, `last_show`, `paused_until`, `backup_at`. See the status report: the Terms
should be reviewed before this goes to customers.

## 3. Live end-to-end check against the test portal and the Pi (about 1 minute)

`deploy/e2e_check.py` (portal repo) renders the pages as an admin and as a customer, confirms
the Pi is reporting with the new fields, queues a settings backup, waits for the Pi to do it
and checks the result. It was run last night and passed (14 checks).

```bash
ssh -i ~/.ssh/id_ed25519_relay root@45.76.238.14
cd /opt/bld-portal-test && set -a && . ./.env && set +a
PYTHONPATH=. COOKIE_SECURE=0 .venv/bin/python deploy/e2e_check.py bldtest
```

Expect fourteen `PASS` lines and `All checks passed`.

## 4. What automation does not cover (see the manual plan)

* Email delivery and wording (tests capture the messages; they are not sent).
* How the pages look, especially on a phone (only that they render and their scripts parse).
* Real hardware behaviour: reboot, restart of the show player, update from git, restore of FPP's
  own configuration, schedule pause against the real scheduler, under-voltage and temperature.
* The monthly report on a real month boundary, and the nightly backup window.
* Deploying to the live portal and a live Pi (not done; see the status report).
