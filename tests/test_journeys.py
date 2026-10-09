"""End-to-end happy paths through the real routes, with a fake FPP behind them.

Each test reads like a person using the app: first-run setup, sign in, make a
scene, play it, schedule it, back it up and get it back. If one of these fails
after an edit, something users do every day is broken.
"""
import json
from unittest import mock

import bcrypt
import pytest

from app.models import AppSetting, Scene


@pytest.fixture
def pin_hash():
    return bcrypt.hashpw(b"4321", bcrypt.gensalt(4)).decode()


@pytest.fixture
def client(app_factory, fpp):
    app = app_factory()
    client = app.test_client()
    with client.session_transaction() as s:
        s["logged_in"] = True
    client.app = app
    return client


def make_scene(client, name="Warm White", zones=None):
    zones = zones or {"Zone 1": "#ff8800", "Zone 2": "#0000ff"}
    resp = client.post("/api/scenes", json={"name": name, "zones": zones})
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()


# ── 1. First run and sign-in ─────────────────────────────────────────────────

def test_first_run_setup_then_login(app_factory, fpp, pin_hash):
    app = app_factory(provisioned=False)
    client = app.test_client()

    # Any page funnels a fresh install to the setup screen.
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/setup")

    # Claiming it sets the PIN (the .env write is stubbed so the test never
    # touches a real .env) and signs the owner in.
    def fake_write(key, value):
        app.config[key] = value

    with mock.patch("app.routes.auth.write_env_key", side_effect=fake_write):
        resp = client.post("/setup", data={"pin": "4321", "confirm": "4321"})
    assert resp.status_code == 302
    assert client.get("/api/zones").status_code == 200          # session works
    assert bcrypt.checkpw(b"4321", app.config["ADMIN_PASSWORD_HASH"].encode())

    # The setup screen is gone once claimed.
    assert client.get("/setup", follow_redirects=False).status_code == 302


def test_setup_rejects_mismatched_pins(app_factory, fpp):
    app = app_factory(provisioned=False)
    client = app.test_client()
    resp = client.post("/setup", data={"pin": "1234", "confirm": "9999"})
    assert resp.status_code == 200
    assert b"did not match" in resp.data
    assert app.config["ADMIN_PASSWORD_HASH"] == ""


def test_login_logout_and_wrong_pin(app_factory, fpp, pin_hash):
    from app.routes.auth import _failed_logins
    _failed_logins.clear()
    app = app_factory()
    app.config["ADMIN_PASSWORD_HASH"] = pin_hash
    client = app.test_client()

    assert client.get("/api/zones").status_code == 302          # logged out

    bad = client.post("/login", data={"password": "0000"})
    assert b"Invalid PIN" in bad.data
    assert client.get("/api/zones").status_code == 302

    good = client.post("/login", data={"password": "4321"})
    assert good.status_code == 302
    assert client.get("/api/zones").status_code == 200

    client.get("/logout")
    assert client.get("/api/zones").status_code == 302


def test_repeated_wrong_pins_lock_the_client_out(app_factory, fpp, pin_hash):
    from app.routes.auth import _FREE_ATTEMPTS, _failed_logins
    _failed_logins.clear()
    app = app_factory()
    app.config["ADMIN_PASSWORD_HASH"] = pin_hash
    client = app.test_client()
    for _ in range(_FREE_ATTEMPTS + 1):
        client.post("/login", data={"password": "0000"})
    locked = client.post("/login", data={"password": "4321"})      # even the right PIN
    assert locked.status_code == 429
    _failed_logins.clear()


# ── 2. Core action: build a scene, play it, see it on the controller ─────────

def test_create_and_apply_scene_lights_the_zones(client, fpp):
    scene = make_scene(client)

    # Saving a scene registers its FPP playlist so it can be scheduled.
    assert "Scene - Warm White" in fpp.playlists
    main = fpp.playlists["Scene - Warm White"]["mainPlaylist"]
    assert f"/internal/scene/{scene['id']}/apply?token=tok-abc" in main[0]["args"][0]

    listed = client.get("/api/scenes").get_json()
    assert [s["name"] for s in listed] == ["Warm White"]

    resp = client.post(f"/api/scenes/{scene['id']}/apply")
    assert resp.status_code == 200
    assert fpp.lit() == {"Zone 1", "Zone 2"}
    assert fpp.overlay_state["Zone 1"]["RGB"] == [255, 136, 0]
    assert fpp.overlay_state["Zone 2"]["RGB"] == [0, 0, 255]
    # Applying stops whatever was playing first.
    assert ("GET", "/playlists/stop", None) in fpp.calls


def test_scene_validation_and_duplicates(client, fpp):
    make_scene(client)
    assert client.post("/api/scenes", json={"name": "Warm White", "zones": {"Zone 1": "#ffffff"}}).status_code == 409
    assert client.post("/api/scenes", json={"name": "", "zones": {"Zone 1": "#ffffff"}}).status_code == 400
    assert client.post("/api/scenes", json={"name": "a/b", "zones": {"Zone 1": "#ffffff"}}).status_code == 400
    assert client.post("/api/scenes", json={"name": "Bad", "zones": {"Zone 1": "red"}}).status_code == 400
    assert client.post("/api/scenes", json={"name": "Bad", "zones": {"Nowhere": "#ffffff"}}).status_code == 400


def test_delete_scene_removes_it_everywhere(client, fpp):
    scene = make_scene(client)
    assert client.delete(f"/api/scenes/{scene['id']}").status_code == 200
    assert client.get("/api/scenes").get_json() == []
    assert "Scene - Warm White" not in fpp.playlists
    assert client.delete(f"/api/scenes/{scene['id']}").status_code == 404


def test_fpp_playing_a_scene_playlist_reaches_the_internal_endpoint(client, fpp):
    """What FPP's own scheduler does: call back with the token, no session."""
    scene = make_scene(client)
    anonymous = client.application.test_client()
    url = f"/internal/scene/{scene['id']}/apply"
    assert anonymous.get(url).status_code == 403
    assert anonymous.get(url + "?token=wrong").status_code == 403
    assert anonymous.get(url + "?token=tok-abc&clear=1").status_code == 200
    assert fpp.lit() == {"Zone 1", "Zone 2"}


# ── 3. Playlists: stop, status, custom playlist built from a scene ───────────

def test_controls_page_status_and_stop(client, fpp):
    fpp.status = {"status_name": "playing", "current_playlist": {"playlist": "Show"}}
    assert client.get("/api/fppd/status").get_json()["current_playlist"]["playlist"] == "Show"
    assert client.post("/api/playlists/stop").status_code == 200
    # The stop invalidated the status cache, so the page sees the new state at once.
    assert client.get("/api/fppd/status").get_json()["current_playlist"]["playlist"] == ""


def test_build_a_custom_playlist_from_a_scene(client, fpp):
    scene = make_scene(client)
    resp = client.post("/api/custom-playlists", json={
        "name": "Evening",
        "repeat": True,
        "items": [
            {"item_type": "scene", "ref_id": scene["id"], "duration": 20},
            {"item_type": "pause", "duration": 5},
        ],
    })
    assert resp.status_code == 201, resp.get_json()
    built = resp.get_json()
    assert [i["label"] for i in built["items"]] == ["Warm White", "Pause"]

    pushed = fpp.playlists["Evening"]["mainPlaylist"]
    assert any("/internal/scene/" in str(e.get("args")) and "clear=1" in str(e.get("args")) for e in pushed)

    names = client.get("/api/custom-playlists?summary=1").get_json()
    assert [n["name"] for n in names] == ["Evening"]

    assert client.delete(f"/api/custom-playlists/{built['id']}").status_code == 200
    assert "Evening" not in fpp.playlists


# ── 4. Scheduling ────────────────────────────────────────────────────────────

ENTRY = {"playlist": "Scene - Warm White", "day": 7, "startTime": "18:00:00", "endTime": "22:00:00",
         "startDate": "2026-11-01", "endDate": "2026-12-31"}


def test_schedule_add_reorder_and_delete(client, fpp):
    assert client.post("/api/schedule/entry", json=ENTRY).status_code == 201
    second = dict(ENTRY, playlist="Other")
    assert client.post("/api/schedule/entry", json=second).status_code == 201
    assert [e["playlist"] for e in fpp.schedule] == ["Scene - Warm White", "Other"]

    assert client.post("/api/schedule/entry/1/move", json={"direction": "up"}).status_code == 200
    assert [e["playlist"] for e in fpp.schedule] == ["Other", "Scene - Warm White"]

    assert client.delete("/api/schedule/entry/0").status_code == 200
    assert [e["playlist"] for e in fpp.schedule] == ["Scene - Warm White"]
    assert client.delete("/api/schedule/entry/9").status_code == 404


def test_schedule_rejects_a_bad_entry_without_touching_fpp(client, fpp):
    resp = client.post("/api/schedule/entry", json=dict(ENTRY, startTime="nope"))
    assert resp.status_code == 400
    assert fpp.schedule == []


def test_holiday_drives_linked_schedule_entries(client, fpp):
    resp = client.post("/api/holidays", json={
        "name": "Winter", "start_month": 12, "start_day": 1, "end_month": 1, "end_day": 5})
    assert resp.status_code in (200, 201), resp.get_json()
    entry = dict(ENTRY, holiday="Winter")
    del entry["startDate"], entry["endDate"]
    assert client.post("/api/schedule/entry", json=entry).status_code == 201
    saved = fpp.schedule[0]
    assert saved["holiday"] == "Winter"
    assert saved["startDate"][5:] == "12-01" and saved["endDate"][5:] == "01-05"


# ── 5. Settings and backup ───────────────────────────────────────────────────

def test_settings_round_trip_and_validation(client, fpp):
    assert client.post("/api/settings", json={"site_name": "Backpack"}).status_code == 200
    with client.application.app_context():
        from app import db
        assert db.session.get(AppSetting, "site_name").value == "Backpack"
    assert client.post("/api/settings", json={"accent_color": "not-a-color"}).status_code == 400
    assert client.post("/api/settings", json={"alert_smtp_port": "99999"}).status_code == 400


def test_backup_and_restore_brings_a_deleted_scene_back(client, fpp):
    scene = make_scene(client, "Keepsake", {"Zone 1": "#123456"})
    backup = client.get("/api/backup")
    assert backup.status_code == 200
    payload = json.loads(backup.data)
    assert payload["version"] == 4
    assert [s["name"] for s in payload["scenes"]] == ["Keepsake"]

    client.delete(f"/api/scenes/{scene['id']}")
    assert client.get("/api/scenes").get_json() == []

    import io
    restored = client.post("/api/restore", data={"file": (io.BytesIO(backup.data), "backup.json")},
                           content_type="multipart/form-data")
    assert restored.status_code == 200, restored.get_json()
    again = client.get("/api/scenes").get_json()
    assert [(s["name"], s["zones"][0]["hex_color"]) for s in again] == [("Keepsake", "#123456")]
    assert "Scene - Keepsake" in fpp.playlists            # FPP playlist regenerated too


def test_restore_rejects_garbage(client, fpp):
    import io
    resp = client.post("/api/restore", data={"file": (io.BytesIO(b"not json"), "x.json")},
                       content_type="multipart/form-data")
    assert resp.status_code == 400
    resp = client.post("/api/restore", data={"file": (io.BytesIO(b'{"version": 99}'), "x.json")},
                       content_type="multipart/form-data")
    assert resp.status_code == 400


# ── 6. Pages render ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/", "/colors", "/effects", "/schedule", "/playlists", "/settings"])
def test_every_main_page_loads_when_signed_in(client, fpp, path):
    resp = client.get(path)
    assert resp.status_code == 200, path
    assert b"<html" in resp.data.lower()


def test_login_page_loads_when_signed_out(app_factory, fpp):
    resp = app_factory().test_client().get("/login")
    assert resp.status_code == 200
    assert b"password" in resp.data.lower()


def test_scene_count_matches_database(client, fpp):
    for i in range(3):
        make_scene(client, f"S{i}")
    with client.application.app_context():
        assert Scene.query.count() == 3


# ── 7. FPP ignoring the first switch-on ──────────────────────────────────────
# A real controller sometimes answers 200 to "switch this model on" and then
# does nothing, which leaves the lights dark with no error anywhere. The app
# has to notice and send it again.

def activation_calls(fpp, model="All"):
    return [c for c in fpp.calls
            if c[0] == "PUT" and c[1] == f"/overlays/model/{model}/state" and (c[2] or {}).get("State") == 1]


def test_scene_survives_fpp_ignoring_the_first_switch_on(client, fpp):
    scene = make_scene(client, "Quirky", {"Zone 1": "#ff0000", "Zone 2": "#00ff00"})
    fpp.ignore_first_on_after_stop = True

    resp = client.post(f"/api/scenes/{scene['id']}/apply")

    assert resp.status_code == 200, resp.get_json()
    assert fpp.lit() == {"Zone 1", "Zone 2"}                 # both really on
    assert fpp.overlay_state["Zone 2"]["RGB"] == [0, 255, 0]
    assert len(activation_calls(fpp, "Zone 1")) == 2          # one retry for the dropped one


def test_scene_apply_does_not_retry_when_everything_switched_on(client, fpp):
    scene = make_scene(client, "Calm", {"Zone 1": "#ff0000"})
    client.post(f"/api/scenes/{scene['id']}/apply")
    assert len(activation_calls(fpp, "Zone 1")) == 1


def test_scene_reports_a_model_that_never_switches_on(client, fpp):
    scene = make_scene(client, "Stuck", {"Zone 1": "#ff0000", "Zone 2": "#00ff00"})
    fpp.never_on = {"Zone 2"}

    resp = client.post(f"/api/scenes/{scene['id']}/apply")

    assert resp.status_code == 502
    assert "Zone 2" in resp.get_json()["error"]
    assert "Zone 1" not in resp.get_json()["error"]
    assert len(activation_calls(fpp, "Zone 2")) == 3         # first try + two retries, then it gives up


def test_internal_scene_call_from_fpp_also_recovers(client, fpp):
    scene = make_scene(client, "Scheduled", {"Zone 1": "#0000ff"})
    fpp.ignore_first_on_after_stop = True
    resp = client.application.test_client().get(f"/internal/scene/{scene['id']}/apply?token=tok-abc&clear=1")
    assert resp.status_code == 200
    assert fpp.lit() == {"Zone 1"}


def test_color_picker_survives_fpp_ignoring_the_first_switch_on(client, fpp):
    fpp.ignore_first_on_after_stop = True
    client.post("/api/playlists/stop")                         # arms the quirk via the reset
    fpp._armed = True
    resp = client.post("/colors/send", json={"hex": "#336699", "model": "Zone 3"})
    assert resp.status_code == 200, resp.get_json()
    assert fpp.lit() == {"Zone 3"}
    assert fpp.overlay_state["Zone 3"]["RGB"] == [51, 102, 153]


def test_color_picker_says_so_when_the_model_will_not_switch_on(client, fpp):
    fpp.never_on = {"Zone 3"}
    resp = client.post("/colors/send", json={"hex": "#336699", "model": "Zone 3"})
    assert resp.status_code == 502
    assert "Zone 3" in resp.get_json()["error"]


def test_effect_survives_fpp_ignoring_the_first_start(client, fpp):
    fpp.ignore_first_on_after_stop = True
    client.post("/api/playlists/stop")
    fpp._armed = True
    resp = client.post("/api/effects/run", json={"effect": "Bars", "models": ["Zone 1"], "args": []})
    assert resp.status_code == 200, resp.get_json()
    assert fpp.lit() == {"Zone 1"}
    effect_calls = [c for c in fpp.calls if c[0] == "POST" and c[1] == "/command"
                    and (c[2] or {}).get("args", [None, None, None])[2] == "Bars"]
    assert len(effect_calls) == 2


def test_apply_still_succeeds_when_the_read_back_is_unavailable(client, fpp):
    """If FPP cannot be asked, trust the original requests rather than failing the click."""
    scene = make_scene(client, "Blind", {"Zone 1": "#ff0000"})
    fpp.models_unreadable = True
    resp = client.post(f"/api/scenes/{scene['id']}/apply")
    assert resp.status_code == 200
    assert len(activation_calls(fpp, "Zone 1")) == 1
