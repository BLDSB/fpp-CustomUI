"""BLD fleet features on the controller: activity log, staff PIN, schedule pause and timed
runs, show log, remote maintenance actions, settings backups and the show check."""
import io
import json
import time
import zipfile
from datetime import datetime
from unittest import mock

import bcrypt
import pytest

from app import audit, fleet_agent, overrides, remote_backup, showlog
from app.models import AppSetting, AuditLog, ShowLog
from app.security import STAFF_ALLOWED

from conftest import _Response


@pytest.fixture
def pin_hash():
    return bcrypt.hashpw(b"4321", bcrypt.gensalt(4)).decode()


@pytest.fixture
def app(app_factory, pin_hash):
    app = app_factory()
    app.config["ADMIN_PASSWORD_HASH"] = pin_hash
    return app


@pytest.fixture
def client(app, fpp):
    client = app.test_client()
    with client.session_transaction() as s:
        s["logged_in"] = True
    client.app = app
    return client


def setting(app, key):
    with app.app_context():
        from app import db
        row = db.session.get(AppSetting, key)
        return row.value if row else None


def put_setting(app, key, value):
    with app.app_context():
        from app import db
        row = db.session.get(AppSetting, key)
        if row is None:
            db.session.add(AppSetting(key=key, value=value))
        else:
            row.value = value
        db.session.commit()


@pytest.fixture
def env_writes():
    """Stub the .env write (as the journey tests do) and apply it to the live config."""
    from flask import current_app

    def fake(key, value):
        current_app.config[key] = value
    with mock.patch("app.routes.auth.write_env_key", side_effect=fake) as m:
        yield m


# --- activity log ---------------------------------------------------------------------------
def test_changes_are_logged_but_pins_and_bodies_are_not(client, app, fpp):
    client.post("/api/schedule/entry", json={"playlist": "Evening", "startTime": "18:00:00",
                                              "endTime": "23:00:00", "day": 7})
    client.post("/api/holidays", json={"name": "Secret-Name-123", "start_month": 12, "start_day": 1,
                                       "end_month": 12, "end_day": 31})
    with app.app_context():
        rows = AuditLog.query.all()
        text = json.dumps([[r.actor, r.action, r.detail] for r in rows])
    assert "POST /api/schedule/entry" in text and "POST /api/holidays" in text
    assert "Secret-Name-123" not in text and "Evening" not in text


def test_reading_and_playing_are_not_logged(client, app, fpp):
    client.get("/api/playlists")
    client.get("/api/schedule/list")
    client.post("/api/playlists/stop")
    with app.app_context():
        assert AuditLog.query.count() == 0


def test_failed_changes_are_not_logged(client, app, fpp):
    client.post("/api/schedule/entry", json={"nothing": "valid"})
    with app.app_context():
        assert AuditLog.query.count() == 0


def test_logins_good_and_bad_are_logged_with_the_role(app, fpp):
    client = app.test_client()
    client.post("/login", data={"password": "0000"})
    client.post("/login", data={"password": "4321"})
    with app.app_context():
        pairs = [(r.action, r.detail) for r in AuditLog.query.order_by(AuditLog.id)]
    assert pairs == [("login_failed", ""), ("login", "admin")]


def test_the_log_is_trimmed_and_served_newest_first(client, app):
    with app.app_context():
        for i in range(audit.KEEP_ROWS + 60):
            audit.record("change", f"n{i}")
        audit._prune()
        assert AuditLog.query.count() == audit.KEEP_ROWS
    entries = client.get("/api/audit").get_json()["entries"]
    assert len(entries) == 100 and entries[0]["detail"] == f"n{audit.KEEP_ROWS + 59}"


# --- staff PIN ------------------------------------------------------------------------------------
def test_the_staff_pin_must_differ_from_the_admin_pin(client, env_writes):
    assert client.post("/api/set-staff-pin", json={"new_pin": "4321"}).status_code == 400
    assert client.post("/api/set-staff-pin", json={"new_pin": "12"}).status_code == 400
    assert client.post("/api/set-staff-pin", json={"new_pin": "1111"}).status_code == 200
    assert client.get("/api/staff-pin").get_json() == {"set": True}
    assert client.post("/api/clear-staff-pin").status_code == 200


def test_the_staff_pin_signs_in_as_staff_and_the_admin_pin_still_wins(app, fpp, env_writes):
    admin = app.test_client()
    with admin.session_transaction() as s:
        s["logged_in"] = True
    admin.post("/api/set-staff-pin", json={"new_pin": "1111"})
    staff = app.test_client()
    r = staff.post("/login", data={"password": "1111"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/controls")
    with staff.session_transaction() as s:
        assert s["is_staff"] is True and s["is_master"] is False
    full = app.test_client()
    full.post("/login", data={"password": "4321"})
    with full.session_transaction() as s:
        assert s["is_staff"] is False


def sample_url(app, rule):
    values = {a: (1 if type(rule._converters[a]).__name__ == "IntegerConverter" else "x")
              for a in rule.arguments}
    return rule.build(values, append_unknown=False)[1]


def test_staff_can_reach_only_the_allowed_routes(app, fpp):
    """Walk every route as a staff session: anything not on the allow-list must be refused
    before its view runs, and everything on it must get through the gate."""
    seen_blocked, seen_allowed = 0, 0
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static" or rule.rule.startswith("/internal/"):
            continue
        method = next(m for m in ("GET", "POST", "PUT", "DELETE") if m in rule.methods)
        url = sample_url(app, rule)
        client = app.test_client()
        with client.session_transaction() as s:
            s["logged_in"], s["is_staff"] = True, True
        resp = client.open(url, method=method, json={} if method != "GET" else None)
        blocked = resp.status_code == 403 and (resp.get_json(silent=True) or {}).get("staff_blocked")
        redirected = resp.status_code == 302 and resp.headers["Location"].endswith("/controls")
        if rule.endpoint in STAFF_ALLOWED:
            assert not blocked, f"{rule.endpoint} should be open to staff"
            seen_allowed += 1
        else:
            assert blocked or redirected, f"{rule.endpoint} is reachable by staff ({resp.status_code})"
            seen_blocked += 1
    assert seen_blocked > 30 and seen_allowed > 20


def test_every_allowed_endpoint_actually_exists(app):
    assert STAFF_ALLOWED <= {r.endpoint for r in app.url_map.iter_rules()}


def test_admin_sessions_are_unaffected_by_the_gate(client, fpp):
    assert client.get("/settings").status_code == 200
    assert client.get("/schedule").status_code == 200


def test_staff_do_not_see_the_admin_links(app, fpp):
    client = app.test_client()
    with client.session_transaction() as s:
        s["logged_in"], s["is_staff"] = True, True
    page = client.get("/controls").get_data(as_text=True)
    assert ">Schedule</a>" not in page and 'title="Settings"' not in page and ">Playlists</a>" not in page
    assert ">Controls</a>" in page and ">Color</a>" in page


# --- schedule pause ---------------------------------------------------------------------------------------
ENTRY = {"enabled": 1, "playlist": "Evening", "startTime": "18:00:00", "endTime": "23:00:00",
         "day": 7, "repeat": 1, "stopType": 0, "startDate": "2000-01-01", "endDate": "2099-12-31"}


def test_pausing_disables_every_entry_and_resuming_restores_them_exactly(client, app, fpp):
    fpp.schedule = [dict(ENTRY), {**ENTRY, "playlist": "Late", "enabled": 0}]
    original = [dict(e) for e in fpp.schedule]
    r = client.post("/api/override/pause", json={"hours": 3})
    assert r.status_code == 200 and r.get_json()["paused"] is True
    assert [e["enabled"] for e in fpp.schedule] == [0, 0]
    assert client.get("/api/override").get_json()["paused"] is True
    assert client.post("/api/override/resume").status_code == 200
    assert fpp.schedule == original
    assert client.get("/api/override").get_json()["paused"] is False


def test_pausing_twice_moves_the_end_and_keeps_the_originals(client, app, fpp):
    fpp.schedule = [dict(ENTRY)]
    client.post("/api/override/pause", json={"hours": 3})
    client.post("/api/override/pause", json={"hours": 6})
    client.post("/api/override/resume")
    assert fpp.schedule[0]["enabled"] == 1


def test_schedule_edits_are_refused_while_paused(client, fpp):
    fpp.schedule = [dict(ENTRY)]
    client.post("/api/override/pause", json={"hours": 3})
    new = {"playlist": "X", "startTime": "18:00:00", "endTime": "19:00:00", "day": 7}
    assert client.post("/api/schedule/entry", json=new).status_code == 409
    assert client.put("/api/schedule/entry/0", json=new).status_code == 409
    assert client.delete("/api/schedule/entry/0").status_code == 409
    assert client.post("/api/schedule/entry/0/move", json={"direction": "down"}).status_code == 409
    client.post("/api/override/resume")
    assert client.post("/api/schedule/entry", json=new).status_code == 201


def test_a_failed_save_leaves_the_schedule_alone_and_not_paused(client, app, fpp):
    fpp.schedule = [dict(ENTRY)]
    orig = fpp.handle

    def failing(method, url, **kw):
        if method == "POST" and url.endswith("/schedule"):
            return _Response({}, 500)
        return orig(method, url, **kw)
    fpp.handle = failing
    assert client.post("/api/override/pause", json={"hours": 2}).status_code == 400
    assert client.get("/api/override").get_json()["paused"] is False
    assert fpp.schedule[0]["enabled"] == 1


def test_pause_validates_the_length(client, fpp):
    assert client.post("/api/override/pause", json={"hours": 0}).status_code == 400
    assert client.post("/api/override/pause", json={"hours": 500}).status_code == 400
    assert client.post("/api/override/pause", json={}).status_code == 400


def test_the_timer_resumes_the_schedule_when_the_pause_runs_out(client, app, fpp):
    fpp.schedule = [dict(ENTRY)]
    client.post("/api/override/pause", json={"hours": 1})
    overrides.tick(app, {}, now=time.time() + 1800)
    assert fpp.schedule[0]["enabled"] == 0            # not yet
    overrides.tick(app, {}, now=time.time() + 7200)
    assert fpp.schedule[0]["enabled"] == 1
    assert client.get("/api/override").get_json()["paused"] is False


def test_skip_tonight_covers_the_evening_and_ends_at_noon():
    evening = datetime(2026, 10, 26, 18, 0).timestamp()
    now = datetime(2026, 10, 26, 14, 0).timestamp()
    until = overrides.tonight_until({"scheduler": {"nextPlaylist": {"scheduledStartTime": evening}}}, now)
    assert datetime.fromtimestamp(until) == datetime(2026, 10, 27, 12, 0)
    after_midnight = datetime(2026, 10, 27, 0, 30).timestamp()
    until = overrides.tonight_until({"scheduler": {"nextPlaylist": {"scheduledStartTime": after_midnight}}}, now)
    assert datetime.fromtimestamp(until) == datetime(2026, 10, 27, 12, 0)
    playing = overrides.tonight_until({"status_name": "playing"}, datetime(2026, 10, 26, 20, 0).timestamp())
    assert datetime.fromtimestamp(playing) == datetime(2026, 10, 27, 12, 0)
    unknown = overrides.tonight_until({}, datetime(2026, 10, 26, 11, 30).timestamp())
    assert unknown - datetime(2026, 10, 26, 11, 30).timestamp() >= 3600


def test_skip_tonight_through_the_api_stops_what_is_playing(client, fpp):
    fpp.schedule = [dict(ENTRY)]
    fpp.status = {"status_name": "playing", "current_playlist": {"playlist": "Evening"}}
    r = client.post("/api/override/pause", json={"mode": "tonight", "stop_now": True})
    assert r.status_code == 200
    assert ("GET", "/playlists/stop", None) in fpp.calls


# --- timed run ---------------------------------------------------------------------------------------------
def fake_start(fpp):
    orig = fpp.handle

    def handle(method, url, **kw):
        if "/command/Start%20Playlist/" in url:
            name = url.split("/command/Start%20Playlist/")[1].split("/")[0].replace("%20", " ")
            fpp.calls.append((method, "/start", name))
            fpp.status = {"status_name": "playing", "current_playlist": {"playlist": name}}
            return _Response({})
        return orig(method, url, **kw)
    fpp.handle = handle


def test_a_timed_run_starts_the_playlist_and_stops_it_at_the_deadline(client, app, fpp):
    fake_start(fpp)
    assert client.post("/api/override/run", json={"playlist": "Evening", "hours": 2}).status_code == 200
    assert ("GET", "/start", "Evening") in fpp.calls
    assert client.get("/api/override").get_json()["run"]["playlist"] == "Evening"
    fpp.calls.clear()
    overrides.tick(app, fpp.status, now=time.time() + 3600)
    assert ("GET", "/playlists/stop", None) not in fpp.calls
    overrides.tick(app, fpp.status, now=time.time() + 3 * 3600)
    assert ("GET", "/playlists/stop", None) in fpp.calls
    assert client.get("/api/override").get_json()["run"] is None


def test_a_timed_run_does_not_stop_something_else_that_took_over(client, app, fpp):
    fake_start(fpp)
    client.post("/api/override/run", json={"playlist": "Evening", "hours": 1})
    fpp.calls.clear()
    other = {"status_name": "playing", "current_playlist": {"playlist": "Manual"}}
    overrides.tick(app, other, now=time.time() + 7200)
    assert ("GET", "/playlists/stop", None) not in fpp.calls


def test_timed_run_rejects_scene_playlists_and_bad_input(client, fpp):
    assert client.post("/api/override/run", json={"playlist": "Scene - Warm", "hours": 1}).status_code == 400
    assert client.post("/api/override/run", json={"playlist": "", "hours": 1}).status_code == 400
    assert client.post("/api/override/run", json={"playlist": "A", "hours": 0}).status_code == 400
    assert client.post("/api/override/run", json={"playlist": "../x", "hours": 1}).status_code == 400


# --- show log ---------------------------------------------------------------------------------------------
def test_the_show_log_tracks_runs_and_counts_them(app):
    playing = lambda name: {"status_name": "playing", "current_playlist": {"playlist": name}}
    idle = {"status_name": "idle", "current_playlist": {"playlist": ""}}
    now = int(time.time())
    showlog.observe(app, playing("Evening"), now - 7200)
    showlog.observe(app, playing("Evening"), now - 7100)          # same run
    showlog.observe(app, idle, now - 3600)
    showlog.observe(app, playing("Late"), now - 600)
    showlog.observe(app, None, now - 300)                          # fppd unreachable: ignored
    with app.app_context():
        rows = ShowLog.query.order_by(ShowLog.id).all()
        assert [(r.playlist, r.ended_at is not None) for r in rows] == [("Evening", True), ("Late", False)]
    out = showlog.summary(app, now)
    assert out["shows_30d"] == 2 and out["last_show"].startswith("Late - ")


def test_old_runs_are_not_counted(app):
    with app.app_context():
        from app import db
        db.session.add(ShowLog(playlist="Old", started_at=int(time.time()) - 40 * 86400))
        db.session.commit()
    assert showlog.summary(app)["shows_30d"] == 0


# --- report extras and heartbeat round trip ---------------------------------------------------------------
def test_report_extras_carry_options_next_show_and_pause(app):
    put_setting(app, "remote_backup", "1")
    status = {"scheduler": {"nextPlaylist": {"playlistName": "Evening",
                                             "scheduledStartTime": int(datetime(2026, 12, 1, 18, 0).timestamp())}}}
    extras = fleet_agent.report_extras(app, status)
    assert extras["maintenance"] is True and extras["backup_opt_in"] is True
    assert extras["next_show"] == "Evening - Tue Dec 1 6:00 PM"
    put_setting(app, "remote_maintenance", "0")
    assert fleet_agent.report_extras(app, None)["maintenance"] is False


def test_events_are_resent_until_the_portal_accepts_them(app):
    from app import heartbeat
    fleet_agent._events.clear()
    fleet_agent.queue_event("missed_show", "Evening was due but is not playing")
    fleet_agent.queue_event("missed_show", "Evening was due but is not playing")        # de-duplicated
    assert len(fleet_agent.pending_events()) == 1
    bodies = []

    def post(url, data=None, **kw):
        bodies.append(json.loads(data))
        if len(bodies) == 1:
            raise OSError("tunnel down")
        return mock.Mock(raise_for_status=lambda: None, json=lambda: {"ok": True})
    with mock.patch.object(heartbeat, "_fetch_status", return_value=None), \
            mock.patch("app.heartbeat.requests.post", side_effect=post):
        assert heartbeat.send(app) is False
        assert heartbeat.send(app) is True
        assert heartbeat.send(app) is True
    assert bodies[0]["events"] == bodies[1]["events"] and "events" not in bodies[2]
    assert bodies[1]["events"][0]["kind"] == "missed_show"


# --- maintenance actions -----------------------------------------------------------------------------------
def run_action(app, action, arg="", **patches):
    results = []
    with mock.patch.object(fleet_agent, "add_result", lambda a, i, ok, d: results.append((ok, d))):
        with mock.patch.object(fleet_agent, "_sudo", return_value=mock.Mock(returncode=0)) as sudo:
            fleet_agent.execute(app, 7, action, arg, report=lambda: None)
    return results, sudo


def test_only_listed_actions_run(app):
    results, sudo = run_action(app, "rm_everything")
    assert results == [(False, "Not an allowed action")] and not sudo.called


def test_the_owner_can_switch_maintenance_off(app):
    put_setting(app, "remote_maintenance", "0")
    results, sudo = run_action(app, "restart_fppd")
    assert results[0][0] is False and "switched off" in results[0][1] and not sudo.called


def test_restart_fppd_uses_systemctl(app):
    results, sudo = run_action(app, "restart_fppd")
    assert results[0][0] is True
    sudo.assert_called_with("systemctl", "restart", "fppd", timeout=60)


def test_restarting_the_ui_reports_before_it_restarts(app):
    order = []
    with mock.patch.object(fleet_agent, "add_result", lambda a, i, ok, d: order.append("result")), \
            mock.patch.object(fleet_agent, "_sudo", side_effect=lambda *a, **k: order.append("restart")), \
            mock.patch.object(fleet_agent.time, "sleep"):
        fleet_agent.execute(app, 1, "restart_ui", report=lambda: order.append("reported"))
    assert order == ["result", "reported", "restart"]


def test_stop_effects_clears_the_overlays(app, fpp):
    results, _ = run_action(app, "stop_effects")
    assert results == [(True, "Effects stopped and lights cleared")]


def test_a_failing_action_is_reported_not_raised(app):
    with mock.patch.object(fleet_agent, "_run", side_effect=RuntimeError("boom")), \
            mock.patch.object(fleet_agent, "add_result") as add:
        fleet_agent.execute(app, 3, "restart_fppd")
    assert add.call_args[0][2] is False


def test_update_reports_up_to_date_without_restarting(app):
    seq = iter(["abc1234\n", "", "abc1234\n"])

    def git(*a, **k):
        out = next(seq)
        return mock.Mock(returncode=0, stdout=out, stderr="")
    with mock.patch.object(fleet_agent.subprocess, "run", side_effect=git):
        assert fleet_agent._update_plugin(app) == (True, "Already up to date (abc1234)")


def test_update_that_changes_code_asks_for_the_installer(app):
    seq = iter(["abc1234\n", "", "def5678\n"])
    with mock.patch.object(fleet_agent.subprocess, "run",
                           side_effect=lambda *a, **k: mock.Mock(returncode=0, stdout=next(seq), stderr="")):
        ok, detail = fleet_agent._update_plugin(app)
    assert ok and "abc1234 -> def5678" in detail and "restarting" in detail


def test_update_failure_is_reported(app):
    seq = iter([mock.Mock(returncode=0, stdout="abc\n", stderr=""),
                mock.Mock(returncode=1, stdout="", stderr="fatal: not possible to fast-forward\n")])
    with mock.patch.object(fleet_agent.subprocess, "run", side_effect=lambda *a, **k: next(seq)):
        ok, detail = fleet_agent._update_plugin(app)
    assert not ok and "fast-forward" in detail


def test_results_survive_a_restart_and_are_acknowledged(app):
    fleet_agent.add_result(app, 5, True, "done")
    fleet_agent.add_result(app, 6, False, "no")
    assert [r["id"] for r in fleet_agent.pending_results(app)] == [5, 6]
    fleet_agent.ack_results(app, [5])
    assert [r["id"] for r in fleet_agent.pending_results(app)] == [6]
    fleet_agent.ack_results(app, [6])


def test_commands_from_the_portal_are_started_at_most_three_at_a_time(app):
    started = []
    with mock.patch.object(fleet_agent.threading, "Thread") as thread:
        thread.side_effect = lambda target, args, **kw: started.append(args[2]) or mock.Mock()
        fleet_agent.handle_commands(app, [{"id": i, "action": "stop_effects"} for i in range(1, 6)]
                                    + [{"bad": 1}])
    assert started == ["stop_effects"] * 3


def test_a_command_arrives_in_the_heartbeat_reply_and_is_run(app):
    from app import heartbeat
    handled = []
    with mock.patch.object(heartbeat, "_fetch_status", return_value=None), \
            mock.patch("app.heartbeat.requests.post",
                       return_value=mock.Mock(raise_for_status=lambda: None,
                                              json=lambda: {"ok": True, "commands": [{"id": 9, "action": "restart_ui"}]})), \
            mock.patch.object(fleet_agent, "handle_commands", side_effect=lambda a, c, report=None: handled.append(c)):
        assert heartbeat.send(app) is True
    assert handled == [[{"id": 9, "action": "restart_ui"}]]


# --- settings backups ----------------------------------------------------------------------------------------
def tiny_zip(content=b"x"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ui/backup.json", content)
    return buf.getvalue()


def test_a_backup_is_sent_to_the_portal_and_remembered(app):
    with mock.patch.object(remote_backup, "build_archive", return_value=tiny_zip()), \
            mock.patch("app.remote_backup.requests.post") as post:
        assert remote_backup.push(app)[0] is True
    assert post.call_args[0][0].endswith("/api/backup")
    assert remote_backup.last_backup(app) > 0


def test_an_oversized_backup_is_not_sent(app):
    with mock.patch.object(remote_backup, "build_archive", return_value=b"x" * (remote_backup.MAX_BYTES + 1)), \
            mock.patch("app.remote_backup.requests.post") as post:
        ok, detail = remote_backup.push(app)
    assert not ok and "too large" in detail and not post.called


def test_a_failed_upload_is_reported_not_raised(app):
    with mock.patch.object(remote_backup, "build_archive", return_value=tiny_zip()), \
            mock.patch("app.remote_backup.requests.post", side_effect=OSError("down")):
        assert remote_backup.push(app)[0] is False


def test_nightly_backups_need_the_owners_opt_in_and_run_once_at_night(app):
    night = datetime(2026, 10, 27, 3, 0).timestamp()
    noon = datetime(2026, 10, 27, 12, 0).timestamp()
    with mock.patch.object(remote_backup, "push", return_value=(True, "")) as push:
        assert remote_backup.maybe_nightly(app, night) is None            # not opted in
        put_setting(app, "remote_backup", "1")
        assert remote_backup.maybe_nightly(app, night) is not None         # first ever: runs
        put_setting(app, "remote_backup_at", str(int(night)))
        assert remote_backup.maybe_nightly(app, night + 3600) is None      # already done today
        assert remote_backup.maybe_nightly(app, night + 86400 - 600) is not None
        put_setting(app, "remote_backup_at", str(int(night) - 86400))
        assert remote_backup.maybe_nightly(app, noon) is None              # not in the night window
    assert push.call_count == 2


def test_a_restore_fetches_applies_and_reports(app):
    ok_json = {"ok": True, "log": [], "reboot_recommended": True}
    with mock.patch("app.remote_backup.requests.get",
                    return_value=mock.Mock(raise_for_status=lambda: None, content=tiny_zip())), \
            mock.patch("app.routes.backup.apply_archive_bytes",
                       return_value=mock.Mock(get_json=lambda: ok_json)):
        ok, detail = remote_backup.restore(app, 12)
    assert ok and "reboot is recommended" in detail


def test_a_restore_that_cannot_download_fails_cleanly(app):
    with mock.patch("app.remote_backup.requests.get", side_effect=OSError("no")):
        assert remote_backup.restore(app, 12)[0] is False


def test_a_restore_with_step_errors_says_which(app):
    body = {"ok": False, "log": [{"step": "ui_database", "status": "error"}]}
    with mock.patch("app.remote_backup.requests.get",
                    return_value=mock.Mock(raise_for_status=lambda: None, content=b"z")), \
            mock.patch("app.routes.backup.apply_archive_bytes", return_value=mock.Mock(get_json=lambda: body)):
        ok, detail = remote_backup.restore(app, 1)
    assert not ok and "ui_database" in detail


# --- options API ------------------------------------------------------------------------------------------------
def test_remote_options_default_to_maintenance_on_and_backup_off(client):
    assert client.get("/api/remote/options").get_json() == {"maintenance": True, "backup": False, "backup_at": ""}


def test_changing_the_options_is_saved_and_logged(client, app):
    with mock.patch("app.remote_backup.push"):
        assert client.post("/api/remote/options", json={"maintenance": False, "backup": True}).status_code == 200
    assert client.get("/api/remote/options").get_json()["maintenance"] is False
    assert client.get("/api/remote/options").get_json()["backup"] is True
    with app.app_context():
        assert AuditLog.query.filter(AuditLog.detail.contains("/api/remote/options")).count() == 1


# --- show check ---------------------------------------------------------------------------------------------------------
def test_the_show_check_reports_a_dead_player(client, fpp):
    with mock.patch("app.heartbeat._fetch_status", return_value=None):
        checks = client.get("/api/showcheck").get_json()["checks"]
    assert checks[0]["status"] == "fail" and len(checks) == 1


def test_the_show_check_warns_when_nothing_is_scheduled_and_alerts_are_off(client, fpp):
    with mock.patch("app.heartbeat._power", return_value={}):
        checks = {c["name"]: c for c in client.get("/api/showcheck").get_json()["checks"]}
    assert checks["Show player"]["status"] == "ok"
    assert checks["Next show"]["status"] == "warn"
    assert checks["Show alerts"]["status"] == "warn"
    assert checks["Clock"]["status"] == "ok"


def test_the_show_check_notices_a_paused_schedule_and_a_known_next_show(client, app, fpp):
    fpp.schedule = [dict(ENTRY)]
    fpp.status = {"status_name": "idle", "current_playlist": {"playlist": ""},
                  "scheduler": {"nextPlaylist": {"playlistName": "Evening",
                                                 "scheduledStartTime": int(time.time()) + 3600}}}
    client.post("/api/override/pause", json={"hours": 2})
    with mock.patch("app.heartbeat._power", return_value={"undervoltage": False, "undervoltage_ever": True}), \
            mock.patch("app.alert_monitor._preflight_all", return_value={"Evening": ["Sequence 'a.fseq' is missing"]}):
        checks = {c["name"]: c for c in client.get("/api/showcheck").get_json()["checks"]}
    assert checks["Schedule"]["status"] == "warn"
    assert checks["Next show"]["status"] == "ok"
    assert checks["Playlist contents"]["status"] == "fail"
    assert checks["Power"]["status"] == "warn"


# --- housekeeping hook ---------------------------------------------------------------------------------------------------
def test_the_monitor_poll_updates_the_show_log_even_with_alerts_off(app, fpp):
    from app import alert_monitor
    fpp.status = {"status_name": "playing", "current_playlist": {"playlist": "Evening"}}
    alert_monitor._process(app)
    with app.app_context():
        assert [r.playlist for r in ShowLog.query.all()] == ["Evening"]


def test_a_missed_show_is_queued_for_the_portal(app):
    from app import alert_monitor
    fleet_agent._events.clear()
    with mock.patch.object(alert_monitor, "_send_email", return_value=True):
        alert_monitor._alert_not_playing({}, "Evening")
        alert_monitor._alert_fppd_down({}, "Evening")
    assert [e["kind"] for e in fleet_agent.pending_events()] == ["missed_show", "fppd_down"]
    fleet_agent._events.clear()
