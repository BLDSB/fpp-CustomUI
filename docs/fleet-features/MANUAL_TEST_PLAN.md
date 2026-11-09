# Manual test plan: fleet features

Allow about 90 minutes, plus waiting time for the offline and outage tests (start those first
and let them run while you do the others). Tick each box; note anything odd.

## Setup you need

| Thing | Where |
|---|---|
| Test portal | https://testportal.bldlightingcontrol.com (red TEST banner). Sign in as `selden@blacklightdesigns.com`; the email code arrives in your own inbox. Every portal email goes to you, subject prefixed `[TEST for <recipient>]`. |
| Test controller | The Pi at 10.27.200.89, UI at http://10.27.200.89/ui (or remotely through the portal's **Open** button). Site `bldtest`. |
| Pi shell | `ssh -i ~/.ssh/id_ed25519_fpp fpp@10.27.200.89` |
| Relay shell | `ssh -i ~/.ssh/id_ed25519_relay root@45.76.238.14` (test portal in `/opt/bld-portal-test`) |
| Phone | For the staff PIN and the layout checks. |

**Read this first.** The Pi at .89 was enrolled on the **live** portal as `bldtest` ("BLD Test
Production"). Last night it was moved to the **test** portal so these features could be tried
end to end. The live `bldtest` site therefore shows Offline and you will have had an "Offline"
email from the live portal (and get a daily reminder) until you put it back; see section 15.

Shortcut for the relay: a helper to run Python inside the test portal's app (used below).

```bash
cd /opt/bld-portal-test && set -a && . ./.env && set +a
alias tp='PYTHONPATH=. .venv/bin/python'
```

## 0. Automated checks first

- [ ] Run the plans in `AUTOMATED_TEST_PLAN.md` (sections 1 and 3). All green.

## 1. Fleet page (portal)

- [ ] Sign in to the test portal. The nav shows **Fleet** before Users.
- [ ] **Fleet** lists `BLD Test` with: Online, what is playing, `Next:` show, temperature and disk, plugin revision, a yellow **behind** badge (the Pi runs an older revision than GitHub's `main`; this is expected), `100.00%` uptime, "not tracked" monitoring.
- [ ] Filters: **Needs attention** includes it (because of "behind"); **Offline** and **Not enrolled** say "No sites match".
- [ ] The Relay card shows disk %, certificate days left (about 75), the newest plugin revision (a 7-character hash) and a recent self-check time.
- [ ] Open it on a phone: the table scrolls sideways, nothing is cut off.

## 2. Site page (portal)

- [ ] Click the site. The summary now includes Next show, Last show, versions with the **behind** badge.
- [ ] **History** card: 30-day uptime, a temperature graph once about a day of samples exist (today it may be empty: that is fine), and one past outage (the time between the Pi being on the live portal and today). The outage list is real; the uptime ignores time before the first heartbeat.
- [ ] **Settings** card has three new options: alert everyone assigned to the site, monthly report, monitoring-paid-until date. Set the date to 10 days from now, Save: the summary shows a yellow "Monitoring ends in 10 d" and the fleet page matches. Set it 40 days ahead: green. Yesterday: red "expired". Clear it: nothing shown.

## 3. Offline, outage and uptime (start early)

- [ ] On the Pi: `sudo systemctl stop wg-quick@bld`. Note the time.
- [ ] About 3 minutes later the site shows **Offline** on the Fleet and Sites pages.
- [ ] About 10 minutes later you get an email "Offline: BLD Test" (the original alert, unchanged).
- [ ] `sudo systemctl start wg-quick@bld`. Within a minute: Online again; an email "Back online"; the History card shows a new outage (from the last heartbeat to the return), and 30-day uptime is just under 100%.

## 4. Health alerts (portal)

Use the real Pi where you can; simulate the rest. Edit `/opt/bld-portal-test/.env`, add the
line below, then `systemctl restart bld-portal-test`. Remove the line and restart afterwards.

- [ ] **Hot:** add `TEMP_ALERT_C=40`. Within about a minute: the site page shows a red flash "Temp: CPU at 6x C", the fleet row shows a red `temp` badge, and you get "Running hot: BLD Test". Not repeated each minute. Remove the line and restart: "Resolved: Running hot" arrives and the badge goes.
- [ ] **Storage:** add `DISK_ALERT_PCT=20` (the Pi is at about 23%). Same pattern: "Storage almost full", then "Resolved".
- [ ] **Player down:** on the Pi `sudo systemctl stop fppd`. After about 12 minutes: "Show player stopped: BLD Test". `sudo systemctl start fppd`: "Resolved". (Controls on the Pi shows the player down meanwhile.)
- [ ] **Controller event (simulated):** on the Pi run

  ```bash
  curl -s -X POST http://10.98.0.1:8091/api/heartbeat -H 'Content-Type: application/json' \
    -d '{"fppd":"running","events":[{"kind":"missed_show","key":"manual-1","detail":"Manual test event"}]}'
  ```

  Expect `{"ok":true}` and an email "Missed show: BLD Test" saying "reported: Manual test event". Run it again: no second email.
- [ ] **Under-voltage (simulated; a healthy Pi cannot produce it):** on the relay run the block below. A "Power problem: BLD Test" email arrives and the site page shows the red flash. The next real heartbeat (under a minute) reports power OK, the next health pass clears it and you get "Resolved".

  ```bash
  tp - <<'EOF'
  import json
  from datetime import timedelta
  from portal import create_app, health
  from portal.models import Site, SiteSample, db, utcnow
  app = create_app()
  with app.app_context():
      s = Site.query.filter_by(slug="bldtest").first()
      for m in (40, 20):
          db.session.add(SiteSample(site_id=s.id, at=utcnow() - timedelta(minutes=m), undervoltage=True, fppd="running"))
      rep = json.loads(s.last_report); rep["undervoltage"] = True; s.last_report = json.dumps(rep)
      db.session.commit()
      health.check_all()
  EOF
  ```
- [ ] **Assigned users:** Users -> assign `ryann@blacklightdesigns.com` to `bldtest` if not already, tick "Also alert everyone with an account assigned" and Save, then repeat one alert above: the email also goes to ryann (look for `[TEST for ryann@...]`). Untick afterwards.

## 5. Remote actions (portal -> Pi)

Site page -> Remote actions. Each result shows in the table below the buttons within about a minute.

- [ ] **Restart the control panel**: result `ok - Restarting the control panel`; the Pi UI is back within about 15 seconds (reload; you may need to sign in again).
- [ ] **Restart the show player**: `ok - Show player restarted`; Pi Controls shows it recovering.
- [ ] **Stop effects and clear lights**: `ok`. (Start a Color or Effect on the Pi first so you see it stop.)
- [ ] **Back up its settings now**: `ok - Sent ~180 KB`; the Settings backups card lists it.
- [ ] Press **Back up its settings now** twice quickly: the second says it is already waiting.
- [ ] **Reboot** without typing the address: refused with a message. Type `bldtest`, press Reboot: `ok - Rebooting`; the Pi comes back and reports within about 3 minutes (the tunnel starts at boot).
- [ ] **Update the control panel software** (the Pi's tree has uncommitted work, so this is expected to **fail safely** now): result `failed - git pull failed: ...` and nothing on the Pi changes. After you commit/merge and clean the Pi's working tree, repeat: expect `Updated abc1234 -> def5678; restarting`, the UI restarts, the revision on the site page changes and **behind** clears.
- [ ] **Restore** (do this on the test Pi only; a copy of its code and settings from before the deploy is at `/home/fpp/fpp-ui-prefleet-20261026-2316.tgz`): (1) take a backup, (2) on the Pi change Settings -> Site name (add " X") and save, (3) in the portal choose that backup, type `bldtest`, press Restore. Expect `ok - Restored; a reboot is recommended`, the site name back to the original, and FPP showing its "reboot required" banner. Then reboot from the portal.
- [ ] A customer account cannot see or use any of this (section 7).
- [ ] **Owner switch:** on the Pi, Settings -> Remote Access -> untick "Allow BLD to run maintenance actions". Queue "Stop effects": result `failed - Maintenance actions are switched off on this controller`. Tick it again.
- [ ] Queue an action while the Pi is off the tunnel (stop `wg-quick@bld` for a minute): it waits; after 30 minutes unanswered it shows `failed - Not collected by the controller in time`. (Or just revoke the site to see waiting actions cancelled; do not do that on the live site.)
- [ ] Settings -> **Activity log** on the Pi lists every remote action (`remote_action - restart_ui` and so on, actor `system`).

## 6. Settings backups (Pi -> portal)

- [ ] Pi: Settings -> Remote Access shows two new options. Maintenance is ticked, backup is not.
- [ ] Tick **Send a settings backup to BLD every night**: a backup is sent straight away and the line "Last sent ..." appears; the portal lists it.
- [ ] Portal: **Download** it; the zip opens and contains `ui/backup.json` (and `controller/config-backup.json`). **Delete** one: it disappears.
- [ ] On the relay the stored file is not readable: `strings /opt/bld-portal-test/instance/backups/*/*.enc | head` shows no JSON.
- [ ] Only the newest 7 are kept (push 8 with the "Back up its settings now" button over time, or trust the unit test).
- [ ] The nightly run: set the last-sent time back (`sqlite3` on the Pi: `update app_setting set value='0' where key='remote_backup_at';` in `instance/fpp_ui.db`) and either wait for 02:00-04:59 Pi time or just confirm the unit test. Untick the option: nothing further is sent.

## 7. Customer view and reports (portal)

- [ ] Sign in as a customer assigned to `bldtest` (ryann or seldenblack; email code). The Sites card shows Now, Next show, Last show and 30-day uptime. No temperature or power (those are for admins/resellers). No Fleet link; opening `/admin/fleet` gives "Not allowed".
- [ ] **Monthly report.** Seed September history, then press the button. On the relay:

  ```bash
  tp - <<'EOF'
  from datetime import datetime
  from portal import create_app
  from portal.models import Site, SiteOutage, SiteSample, db
  app = create_app()
  with app.app_context():
      s = Site.query.filter_by(slug="bldtest").first()
      s.alert_emails = "ryann@blacklightdesigns.com"; s.monthly_report = True
      db.session.add(SiteSample(site_id=s.id, at=datetime(2026, 9, 1)))
      db.session.add(SiteOutage(site_id=s.id, started_at=datetime(2026, 9, 10, 1), ended_at=datetime(2026, 9, 10, 3)))
      db.session.commit()
  EOF
  ```

  Then **Send last month's report** on the site page. Expect two emails: "Monthly report: BLD Test" (to ryann, 99.7x% available, 1 outage, 2 h) and "Monthly fleet report: September 2026" (to admins). The button always sends; the automatic run (the first monitor pass of a month) sends once per site per month, which the unit tests cover.
- [ ] Clean up: delete those two rows and clear Alert addresses if you do not want them.

## 8. Pi: Show tools (Controls page)

- [ ] Controls has a collapsed **Show tools** panel under the schedule card. Open it on desktop and phone: the buttons wrap, text readable, no overflow.
- [ ] **Check my show**: a list with green ticks for Show player, Next show, Playlist contents, Clock, Power, Storage; a yellow note for "Show alerts" if email alerts are off. Stop fppd and check again: a single red line "The show player is not responding".
- [ ] **Pause for 2 hours**: a yellow banner "The schedule is paused until ..."; Resume schedule appears. In Schedule, the entries are shown disabled; trying to add or edit an entry gives "The schedule is paused until ... Resume it first". **Resume schedule**: banner gone, entries enabled exactly as before (compare with the Schedule page you saw earlier).
- [ ] **Skip tonight's show** (confirm dialog): the schedule is paused until the noon after the next show and what is playing stops. Resume manually afterwards. Also check the portal site page shows "Schedule: Paused until ..." and the customer dashboard "Schedule paused until ...".
- [ ] Leave a pause running with 1 hour: it resumes by itself on time (check the Schedule page after the hour). Restart the control panel mid-pause: it still resumes on time.
- [ ] **Run [playlist] for 0.5 hours**: starts, repeats; the banner/line says it runs until a time; **Stop timed run** ends it. Let one run out: it stops by itself at the deadline (not if you started something else meanwhile).
- [ ] Scenes and Effects cannot be picked for a timed run (only your show playlists).
- [ ] While a pause or run is active, a Pi reboot leaves the state intact.

## 9. Pi: Staff PIN

- [ ] Settings -> **Staff PIN**: set `1111` (or any PIN different from yours): "Staff PIN saved". Setting the same PIN as your admin PIN is refused.
- [ ] On a phone, sign in with the staff PIN. You land on Controls. The nav shows only Controls and Custom -> Color, Effects (no Playlists, Schedule, Settings gear).
- [ ] Controls: play and stop a playlist, brightness, Show tools (skip tonight, Check my show) all work. Color and Effects: send a color, run and stop an effect work; saving presets and scenes is not offered or is refused.
- [ ] Type these URLs by hand: `/schedule`, `/settings`, `/playlists`: each bounces back to Controls. `.../api/schedule/list` shows "Your PIN does not allow that".
- [ ] Sign in with your admin PIN on another device at the same time: everything works as before.
- [ ] Remove the staff PIN: the old staff PIN no longer signs in.
- [ ] `sudo bash fpp_reset_pin.sh` leaves the staff PIN set (it only clears the admin PIN). Decide whether that is what you want (see the status report).

## 10. Pi: Activity log

- [ ] Settings -> **Activity log** -> Show recent activity. After the steps above it lists logins (admin/staff), failed logins (type a wrong PIN once), schedule changes, pause and resume, remote options, PIN set/cleared, remote actions.
- [ ] Nothing in it shows a PIN or a request body. Playing a playlist or choosing a color does not add lines.
- [ ] After many edits the list stays at the newest 100 on screen (2000 stored).

## 11. Regression on the Pi

- [ ] The saved-colors work that was on the Pi before (editable saved colors, renamable scenes) still works: Color page -> edit a saved color; rename a scene.
- [ ] Run the golden set in `TESTING.md` section 2.
- [ ] A normal show plays through at its scheduled time and the portal's "Last show" updates afterwards.
- [ ] Email alerts: Settings -> Alerts -> send a test email; it arrives.

## 12. The portal's other pages

- [ ] Users, Manage sites (Enrol device, Revoke, Delete) still work. Do **not** revoke or delete `bldtest` unless you want to re-enrol; deleting a site now also removes its history and backups.
- [ ] Audit log shows `site_command`, `command_ok`, `backup_downloaded`, `report_sent`.
- [ ] Account page, SSH key, sign in and out as before.

## 13. Reading the numbers

- [ ] After a day or two: the temperature graph draws on the site page; 30-day uptime is sensible.
- [ ] `journalctl -u bld-portal-test -n 50` on the relay shows no "Monitor step ... failed" lines.

## 14. Sign-off

- [ ] Everything above is acceptable, or the problems are written down.
- [ ] Then, in this order (I have committed and pushed nothing): review the diffs, say "commit and open the PRs", then promote the portal to **live** (same deploy without `INSTANCE=test`; the setup script installs `cryptography` itself) and update the real controllers through the portal's Update action or FPP's plugin manager.

## 15. Putting the Pi back on the live portal (when you are done testing)

1. On the Pi: `sudo cp /home/fpp/fpp-remote.conf.live-backup /etc/fpp-remote.conf`
2. Live portal -> Manage sites -> `bldtest` -> **Re-enrol**, copy the command and run it on the Pi (it keeps the same key; the live site comes back Online within a minute).
3. Test portal -> Manage sites -> `bldtest` -> Disable (it was disabled before; I re-enabled it to enrol).
4. To discard the new code on the Pi: stop `fpp-ui`, restore `/home/fpp/fpp-ui-prefleet-20261026-2316.tgz` over the plugin directory, start `fpp-ui`.
