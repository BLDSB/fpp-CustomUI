# Status report: fleet features (overnight build, 26-27 Oct 2026)

## Where things stand

* **Built:** everything in my recommendation list except the two items below. Details in section 1.
* **Tests:** portal 232 pass, plugin 247 pass (258 on the Pi's tree, which also carries the saved-colors work). Live end-to-end check (`deploy/e2e_check.py`) passed 14/14.
* **Deployed:** test portal `https://testportal.bldlightingcontrol.com` and the Pi at 10.27.200.89. **The live portal, Caddy, the live WireGuard hub and every customer site were not touched.**
* **Not committed, not pushed, no PRs** (your rule: hardware first). The work is on local branches `feat/fleet-agent` (plugin, on top of `feat/test-portal-hub-address`) and `feat/fleet-monitoring` (portal). Copies of both working trees are in `D:\Code\fleet-features-backup\`.
* **Not done:** passkeys (deferred, reason below) and any visual check in a real browser.
* **Needs your attention first:** the Pi at .89 had to be moved from the live portal to the test portal (decision 1).

Open `MANUAL_TEST_PLAN.md` to start; `AUTOMATED_TEST_PLAN.md` for the commands.

## 1. What was built

| Recommendation | Built as |
|---|---|
| Fleet dashboard | Admin -> **Fleet**: status, now/next, health, plugin version with **behind** badge, 30-day uptime, monitoring-paid date; filters; relay health card |
| History | Samples every 5 min (90 days) and an outage log; 30-day uptime per site; outage list and temperature graph on the site page |
| Health alerts | Under-voltage (sustained), storage, temperature, show player down; mail once, remind daily, mail when resolved; the controller's own missed-show and player-down events now reach the portal |
| Customer status page | Customers see now playing, next show, last show, 30-day uptime |
| Monthly report | Per site, opt-in, to the site's address list (plus assigned users if ticked) and a digest to admins; "send now" button |
| Alert assigned users | Per-site option, off by default |
| Remote actions | Restart panel, restart show player, stop effects, back up now, reboot, update software, restore backup. Queue in the portal, controller collects it in its heartbeat (no inbound connection), reports back, audited, 30-minute expiry. Reboot/update/restore need the site address typed |
| Update status | "Behind" badge using GitHub's branch head, looked up hourly; pin with `LATEST_PLUGIN_REV` |
| Remote config backup | Controller sends a zip of settings (never media) nightly **only if its owner opts in**, or on demand; stored encrypted; download, delete, restore |
| Relay self-check | Hourly: certificate expiry and disk; mail admins daily while wrong |
| Billing hook | "Monitoring paid until" date per site with warnings (no payments) |
| Enrol/fleet view | Fleet filters (not enrolled, offline, needs attention) |
| Skip tonight / timed run | Controls -> **Show tools**: skip tonight, pause for N hours, resume, run a playlist for N hours |
| Show-day check | **Check my show** button: player, next show, playlist files, clock, power, storage, alerts |
| Controls-only PIN | Optional **Staff PIN** (Controls, Color, Effects, Show tools only) |
| Activity log | Settings -> Activity log: logins, failed logins, settings/schedule/PIN changes, remote actions |
| Docs | Plugin README, user manual (HTML), TESTING.md, `.env.example`, portal README |

## 2. Questions I had and what I decided

1. **Which portal is the .89 Pi on?** It was enrolled on the **live** portal as `bldtest` ("BLD Test Production"), its config pointed at the live portal, and the test portal's `bldtest` site was **disabled**. Your memory note says it stays on the test portal. I could not ask, and these features can only be tried end to end against the test portal, so I **moved it**: re-enabled the test site, issued a one-hour code and enrolled the Pi (same WireGuard key). Side effect: the live `bldtest` site is Offline, so you will get an "Offline: BLD Test Production" email from the live portal about ten minutes after I did this, then a daily reminder. To undo, follow section 15 of the manual plan (the old config is saved on the Pi as `/home/fpp/fpp-remote.conf.live-backup`). *Question for you: should .89 live on the test portal from now on?*
2. **The Pi already had unmerged work** (the editable saved colors and renamable scenes, from the `Forest FPP-colors` worktree) as uncommitted changes. I did not overwrite it: I layered that branch's exact patch onto my tree, ran the combined suite (258 pass), backed up the Pi's whole plugin folder to `/home/fpp/fpp-ui-prefleet-20261026-2316.tgz` first, then deployed. Both sets of changes are on the Pi.
3. **Commit and PR rules:** I followed "deploy first, commit after you approve". Nothing is committed. The deploys are hand-copied (a tarball over the Pi's working tree; a copy of the working tree on the test portal). The test portal's previous folder is at `/root/bld-portal-test.bak-*` on the relay.
4. **Passkeys: deferred.** They change how customers and you sign in, need a physical authenticator to test properly, and I did not want to ship an untested change to the 2FA gate while you were asleep. Suggest a dedicated PR with you at the keyboard.
5. **Scene scheduling: not built, because it already works.** My earlier note was wrong: every scene is already a playlist named "Scene - ..." and appears in the Schedule page's playlist drop-down. Possible polish: group that drop-down into Playlists / Scenes / Effects.
6. **Billing:** only a "monitoring paid until" date with warnings. No payment provider.
7. **Remote maintenance defaults to ON; the nightly settings backup defaults to OFF.** Maintenance is no more than the terminal access admins already have. A backup contains the PIN hashes and mail settings, so it is opt-in. Both are switches on the Pi's Remote Access card.
8. **The heartbeat now carries more** than the Terms list: `maintenance`, `backup_opt_in`, `shows_30d`, `next_show`, `last_show`, `paused_until`, `backup_at`. None is sensitive, but the Terms scope test had to be updated on purpose. **The Terms (revision 3) are still awaiting attorney review and do not mention remote maintenance actions or settings backups.** Please get that looked at before these go to customers.
9. **Backup encryption** uses a key derived from the portal's `SECRET_KEY`. Rotating that key makes old backups unreadable. A stronger scheme (a separate key file) is a small follow-up.
10. **Monthly report is opt-in per site** (off by default, including existing sites), because it emails customers. I changed this late in the night from "on" after thinking about Nov 1 surprising live customers.
11. **Health alerts go to the same people as offline alerts** (admins plus the site's extra addresses). If a customer is listed there, they will start getting "Power problem"/"Storage almost full" emails on live. Say so if you want technical alerts admins-only.
12. **"Shows" is really "playlist runs"** (the monitor polls once a minute and counts changes of playing playlist, including manual ones and scenes). The report wording says "playlist runs".
13. **Skip tonight** = pause until the first noon after the next show starts (or after now if one is playing), and stop what is playing. Staff PINs may use it, since that is when staff need it.
14. **Staff PIN** is an allow-list (a test walks every route). `fpp_reset_pin.sh` does not clear it, and settings backups do not carry it. The Color and Effects pages may still show Save buttons to staff; the server refuses them.
15. **Controller events** (missed show, player down) are produced by the existing alert monitor, so they only reach the portal when email alerts are switched on on that controller.
16. **Updating through the portal** runs `git pull --ff-only` as the panel's user and then the installer. It fails safely on a controller with uncommitted changes, which includes the Pi at .89 right now.

## 3. Verified, and not verified

Verified on the real Pi and test portal: heartbeat with the new fields, history sample, the "behind" badge, a **back up now** action (177 KB stored encrypted), a **restart panel** action (result reported before the restart), the pages rendering from real data for admin and customer, the customer's 403 on Fleet, and the relay's GitHub lookup.

**Not exercised on hardware** (deliberately, to avoid breaking the test unit unattended): restart show player, stop effects, reboot, update software, restore backup, pause/skip/timed run against the real scheduler, staff PIN on a phone, email wording, the offline/outage/health alert emails, the nightly backup window, the monthly report. All are covered by unit tests with fakes, and listed in the manual plan.

**Not looked at in a browser:** I rendered the pages and syntax-checked every inline script, but did not see them. Expect to find cosmetic things (spacing, the Show tools panel on a phone, the fleet table on a small screen).

## 4. Things you may trip over

* The portal's site page lists one past outage (the time the Pi was on the live portal) while 30-day uptime says 100%: uptime ignores time before the first heartbeat, the outage list does not. Cosmetic.
* The Pi's UI path is `/ui` and its hostname is still "Republic".
* `docs/user-manual/Custom-UI-User-Manual.pdf` was **not** regenerated; the HTML has the new sections without screenshots.
* The portal needs the `cryptography` package (added to `requirements.txt`; the setup script installs it).
* Live promotion is the same deploy without `INSTANCE=test`; I did not run it.

## 5. Suggested next steps

1. Work through the manual plan, starting the offline test (section 3) first because it needs waiting.
2. Tell me what you want changed.
3. When happy: "commit and open the PRs" (two repos), then live portal, then the controllers.
