# Testing

Two layers: an automated suite that runs in about two seconds on any machine,
and a short manual "golden set" to run on the Pi after a change. Run the
automated suite after **every** edit — human or AI — and the golden set before
anything is merged or left on a controller.

## 1. Automated suite

```bash
pip install pytest pyflakes     # one-time; the app's own deps are in requirements.txt
python -m pytest -q tests       # expect: all passed, ~2 s
python -m pyflakes app tests    # expect: only the known "app.models imported but unused" line
```

The suite never touches a real controller or your `.env`: `tests/conftest.py`
pins every environment variable (so `load_dotenv()` cannot pull in live PIN
hashes), uses an in-memory database, and replaces FPP's REST API with an
in-memory fake (`FakeFPP`). Nothing is written outside `tests/`.

| File | What it protects |
|---|---|
| `tests/test_helpers.py` | Pure logic: FPP playlist shape (the hardware-tuned structure), overlay geometry and zone grouping, holiday date rollover, schedule validation, alert recipients and day rules, PIN checks and login lockout, URL/color helpers |
| `tests/test_journeys.py` | Real routes, end to end: first-run setup → sign in/out/lockout → create, apply, delete a scene (checks the exact colors sent to FPP) → FPP's token callback → custom playlist → schedule add/reorder/delete → holiday-linked entry → settings → backup and restore → every page loads |
| `tests/test_security.py` | CSRF origin check, security headers, every route requires a session, token handling, image upload content checks, rate limits, wrong-typed JSON |
| `tests/test_performance.py` | Query counts (no N+1), paging limits, FK indexes, the shared status cache |
| `tests/test_upcoming.py` | Upcoming-show reminder timing and wording |

If a test fails after an edit, treat it as "the edit changed behavior" first and
"the test is out of date" second. Update a test only when the behavior change was
intended — and say so in the commit message.

Adding a test: use the `client` fixture pattern in `test_journeys.py` (a signed-in
client plus the `fpp` fake). Read `fpp.calls` to assert what was sent to the
controller, `fpp.playlists` / `fpp.schedule` / `fpp.overlay_state` for what it holds.

## 2. Golden set (manual, on the Pi)

Roughly ten minutes. Use the dev box. Tick each line; any failure means stop and
look at `journalctl -u fpp-ui -n 50` (or `fpp-ui.log`).

**Deploy sanity**
- [ ] `systemctl is-active fpp-ui` says `active`, and the UI loads at `http://<pi>/<UI_PATH>/` (a 503 for the first few seconds after a restart is normal)
- [ ] Log has no traceback since the restart
- [ ] Hard-refresh the browser after any template change

**Sign-in**
- [ ] Wrong PIN is refused; the right PIN signs in; Log out returns to the login page
- [ ] After five wrong PINs the next attempt (even the right PIN) says to wait

**Controls page**
- [ ] The playlist cards and sequences list appear; the status line shows what FPP is doing
- [ ] Play a playlist → status shows Playing within ~5 s → Stop → status returns to idle
- [ ] Brightness slider moves and the value sticks after a reload
- [ ] Leave the tab in the background for a minute, come back: status refreshes immediately

**Colors and scenes**
- [ ] Pick a color and send it to a zone: the real lights change to that color
- [ ] Save it as a scene; the scene shows in the list; applying it lights the same zones
- [ ] Delete the scene; it disappears, and so does its `Scene - …` playlist on FPP's Playlists page

**Effects**
- [ ] The effects list loads; run one on a zone; Stop clears it
- [ ] Save a preset; it appears and runs

**Playlists (builder)**
- [ ] Build a playlist from a scene + a pause; save; it appears on the Controls page and plays through both items
- [ ] Delete it; it is gone from FPP too

**Schedule**
- [ ] Add an entry with a time range; it appears in FPP's own Scheduler page
- [ ] Reorder it, then delete it
- [ ] A holiday-linked entry gets the holiday's dates; entering a bad time shows an error and saves nothing
- [ ] "Schedule Preview" opens and lists upcoming starts

**Settings**
- [ ] Change the site name/accent color; it applies after save and survives a reload
- [ ] Upload a PNG logo: it shows. Uploading a `.svg` containing `<script>` (or a text file renamed `.png`) is refused
- [ ] Zones: rename a zone and hide one; both stick
- [ ] Layout import → "Create overlay models" completes and fppd restarts (only on the dev box)
- [ ] Network: the interfaces list loads (don't save changes you can't undo over the same link)

**Backup and restore**
- [ ] Download a full backup (zip); it downloads and opens
- [ ] Restore it on the dev box: the log shows each step ok, scenes/playlists/settings are intact, and the controller reboots when asked

**Alerts (if SMTP is configured)**
- [ ] "Send test email" arrives; the monitor status card shows a recent "Last checked" time

**Security spot checks**
- [ ] While signed out, `http://<pi>/<UI_PATH>/api/scenes` redirects to login rather than returning data
- [ ] `curl -X POST -H "Origin: http://evil.example" http://<pi>/<UI_PATH>/api/playlists/stop` returns 403
- [ ] If the UI is reached through a proxy such as Dataplicity and saves return "Cross-site request blocked", add the proxy's host to `TRUSTED_ORIGINS` in `.env`

## 3. After an AI-assisted change

1. `python -m pytest -q tests` and `python -m pyflakes app tests`
2. Skim `git diff --stat`: did it touch files the request did not mention?
3. Deploy to the dev Pi, restart `fpp-ui`, run the part of the golden set for the area that changed (all of it for anything touching `auth`, `backup`, `settings`, or `fpp_playlist.py`)
4. Only then commit

Known quirks, so they are not mistaken for regressions: the startup log warns
`Could not write Turn Off Lights preset` on a machine with no FPP install (expected
off-controller), and `pyflakes` reports `app.models imported but unused` in
`app/__init__.py` (the import registers the tables).
