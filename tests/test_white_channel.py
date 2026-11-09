"""White channel for RGBW zones.

FPP's overlay models only reach the first three channels of a node, and a second
overlay model over the white channels would overwrite the colors, so white is
held by FPP's channel-range overlay instead (see app/white_layer.py).  These
tests cover which channels that is, when it is set and when it is cleared, and
that RGB-only zones simply ignore white.
"""
import io
import json
import threading

import pytest

from app import db, white_layer
from app.models import SavedColor, ZoneLayout, ZoneMember, rgbw_models, white_channels

# Zone 1 is RGBW at channel 1 (6 nodes); Zone 2 is RGB; Zone 3 is two RGBW members.
Z1 = {4, 8, 12, 16, 20, 24}
Z31 = {46, 50, 54, 58, 62, 66}
Z32 = {70, 74, 78, 82, 86, 90}


@pytest.fixture
def client(app_factory, fpp):
    app = app_factory()
    # The startup sync thread rewrites playlists through the same in-memory
    # connection.  Seeding rows below while it runs can lose part of a commit,
    # so let it finish first.
    for thread in threading.enumerate():
        if thread.name == "fpp-startup-sync":
            thread.join(timeout=10)
    client = app.test_client()
    with client.session_transaction() as s:
        s["logged_in"] = True
    client.app = app
    return client


def _layout(slot, channels_per_node, start, nodes=6, data=None):
    return ZoneLayout(
        slot=slot, source_name=f"M{slot}", width=nodes, height=1, node_count=nodes,
        start_channel=start, channel_count=nodes * channels_per_node,
        channels_per_node=channels_per_node,
        data=data or ",".join(str(n) for n in range(1, nodes + 1)),
    )


def _member(slot, position, channels_per_node, start, nodes=6):
    return ZoneMember(
        slot=slot, position=position, source_name=f"M{slot}.{position}", width=nodes,
        height=1, node_count=nodes, start_channel=start,
        channel_count=nodes * channels_per_node, channels_per_node=channels_per_node,
        data=",".join(str(n) for n in range(1, nodes + 1)),
    )


@pytest.fixture
def rgbw(client, fpp):
    with client.app.app_context():
        db.session.add_all([
            _layout(1, 4, 1), _layout(2, 3, 25),
            _member(3, 1, 4, 43), _member(3, 2, 4, 67),
        ])
        db.session.commit()
    fpp.models += ["Zone 3.1", "Zone 3.2"]
    return client


def send(client, model, white=None, hex="#102030"):
    body = {"hex": hex, "model": model}
    if white is not None:
        body["white"] = white
    return client.post("/colors/send", json=body)


def ranges(fpp, channels):
    """The forced value of each channel in ``channels`` (None when not forced)."""
    return {fpp.ranges.get(c) for c in channels}


# ── Which channels ───────────────────────────────────────────────────────────

def test_white_is_the_last_channel_of_each_node(rgbw):
    with rgbw.app.app_context():
        found = white_channels(["Zone 1", "Zone 2", "Zone 3.1", "Zone 3.2"])
        assert {k: set(v) for k, v in found.items()} == {
            "Zone 1": Z1, "Zone 3.1": Z31, "Zone 3.2": Z32,      # Zone 2 is RGB: none
        }
        assert rgbw_models() == {"Zone 1", "Zone 3.1", "Zone 3.2"}


def test_a_layout_with_holes_only_yields_placed_nodes(client):
    with client.app.app_context():
        db.session.add(_layout(1, 4, 101, nodes=5, data="1,,3;,5,"))
        db.session.commit()
        # nodes 1, 3, 5 of a model starting at channel 101
        assert white_channels(["Zone 1"]) == {"Zone 1": [104, 112, 120]}


# ── Colors ───────────────────────────────────────────────────────────────────

def test_color_with_white_holds_the_white_channels(rgbw, fpp):
    assert send(rgbw, "Zone 1", 200).status_code == 200
    assert fpp.overlay_state["Zone 1"]["RGB"] == [16, 32, 48]
    assert fpp.lit() == {"Zone 1"}                  # no extra overlay models
    assert ranges(fpp, Z1) == {200}
    assert set(fpp.ranges) == Z1                    # and nothing else is forced


def test_white_zero_still_holds_the_channels_off(rgbw, fpp):
    send(rgbw, "Zone 1", 200)
    send(rgbw, "Zone 1", 0)
    assert ranges(fpp, Z1) == {0}


def test_rgb_only_zone_ignores_white(rgbw, fpp):
    assert send(rgbw, "Zone 2", 200).status_code == 200
    assert fpp.lit() == {"Zone 2"}
    assert fpp.ranges == {}


def test_color_without_white_leaves_the_white_channel_alone(rgbw, fpp):
    assert send(rgbw, "Zone 1").status_code == 200
    assert fpp.ranges == {}
    assert not any("/overlays/range" in path for _m, path, _j in fpp.calls)


@pytest.mark.parametrize("bad", [256, -1, "200", 1.5, True, [1]])
def test_bad_white_is_rejected_before_anything_is_sent(rgbw, fpp, bad):
    before = len(fpp.calls)
    assert send(rgbw, "Zone 1", bad).status_code == 400
    assert not any("/overlays/" in path for _m, path, _j in fpp.calls[before:])


def test_grouped_zone_holds_every_members_white(rgbw, fpp):
    assert send(rgbw, "Zone 3", 90).status_code == 200
    assert fpp.lit() == {"Zone 3.1", "Zone 3.2"}
    assert ranges(fpp, Z31 | Z32) == {90}
    assert not (set(fpp.ranges) & Z1)


def test_zones_sent_one_after_another_keep_their_own_white(rgbw, fpp):
    send(rgbw, "Zone 1", 200)
    send(rgbw, "Zone 3", 90)
    send(rgbw, "Zone 2")                            # a plain zone must not disturb them
    assert ranges(fpp, Z1) == {200}
    assert ranges(fpp, Z31 | Z32) == {90}


def test_whole_display_holds_white_on_every_rgbw_zone(rgbw, fpp):
    # Mixed 3- and 4-channel zones, no real All model: All is virtual.
    assert send(rgbw, "All", 50, "#ff0000").status_code == 200
    assert fpp.lit() == {"Zone 1", "Zone 2", "Zone 3.1", "Zone 3.2"}
    assert ranges(fpp, Z1 | Z31 | Z32) == {50}


def test_a_single_zone_after_a_virtual_all_takes_the_rest_of_its_white_down(rgbw, fpp):
    send(rgbw, "All", 50)
    send(rgbw, "Zone 1", 200)
    assert fpp.lit() == {"Zone 1"}
    assert set(fpp.ranges) == Z1 and ranges(fpp, Z1) == {200}


def test_whole_display_replaces_what_the_zones_held(rgbw, fpp):
    send(rgbw, "Zone 1", 200)
    send(rgbw, "All", 10)
    assert ranges(fpp, Z1 | Z31 | Z32) == {10}


def test_dropping_never_deletes_a_range_one_at_a_time(rgbw, fpp):
    """fppd turns deleting a range it does not hold into a bogus range, so the
    layer clears everything and sets the survivors again instead."""
    send(rgbw, "Zone 1", 200)
    send(rgbw, "Zone 3", 90)
    with rgbw.app.app_context():
        white_layer.drop(["Zone 1"])
    assert set(fpp.ranges) == Z31 | Z32
    assert not any(path.endswith("/delete") for _m, path, _j in fpp.calls)


def test_clearing_takes_the_white_down_with_the_colors(rgbw, fpp):
    send(rgbw, "Zone 1", 200)
    assert rgbw.post("/colors/stop").status_code == 200
    assert fpp.lit() == set()
    assert fpp.ranges == {}


def test_an_rgb_only_show_never_pays_for_the_white_layer(client, fpp):
    with client.app.app_context():
        db.session.add(_layout(1, 3, 1))
        db.session.commit()
    client.post("/colors/stop")
    assert not any("/overlays/range" in path for _m, path, _j in fpp.calls)


def test_a_failed_white_request_is_reported(rgbw, fpp, monkeypatch):
    import requests

    def boom(channels, level):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(white_layer, "_set", boom)
    assert send(rgbw, "Zone 1", 200).status_code == 502


# ── Effects ──────────────────────────────────────────────────────────────────

def test_starting_an_effect_drops_that_zones_white_only(rgbw, fpp):
    send(rgbw, "Zone 1", 200)
    send(rgbw, "Zone 3", 90)
    resp = rgbw.post("/api/effects/run", json={"models": ["Zone 1"], "effect": "Color Wash", "args": []})
    assert resp.status_code == 200, resp.get_json()
    assert set(fpp.ranges) == Z31 | Z32


# ── Page and saved colors ────────────────────────────────────────────────────

def test_the_colors_page_offers_white_only_on_rgbw_zones(rgbw):
    import re
    html = rgbw.get("/colors").get_data(as_text=True)
    assert 'id="white-input"' in html
    flags = dict(re.findall(r'data-model="([^"]+)"\s+data-white="([01])"', html))
    assert flags["Zone 1"] == "1" and flags["Zone 3"] == "1"
    assert flags["Zone 2"] == "0" and flags["Zone 4"] == "0"


def test_saved_colors_keep_their_white(rgbw):
    resp = rgbw.post("/colors/save", json={"name": "Warm", "hex": "#ff8800", "white": 120})
    assert resp.status_code == 201 and resp.get_json()["white"] == 120
    rgbw.post("/colors/save", json={"name": "Plain", "hex": "#0000ff"})
    listed = {b["label"]: b["white"] for b in rgbw.get("/colors/buttons").get_json()}
    assert listed == {"Warm": 120, "Plain": None}
    assert rgbw.post("/colors/save", json={"name": "X", "hex": "#0000ff", "white": 999}).status_code == 400


# ── Scenes ───────────────────────────────────────────────────────────────────

def _scene(client, name, zones, whites=None):
    body = {"name": name, "zones": zones}
    if whites is not None:
        body["whites"] = whites
    resp = client.post("/api/scenes", json=body)
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()


def test_scene_remembers_and_applies_white(rgbw, fpp):
    scene = _scene(rgbw, "Cool", {"Zone 1": "#001020", "Zone 2": "#102030"},
                   {"Zone 1": 255, "Zone 2": 40})
    assert {z["fpp_model"]: z["white"] for z in scene["zones"]} == {"Zone 1": 255, "Zone 2": 40}

    assert rgbw.post(f"/api/scenes/{scene['id']}/apply").status_code == 200
    assert fpp.lit() == {"Zone 1", "Zone 2"}
    assert set(fpp.ranges) == Z1 and ranges(fpp, Z1) == {255}     # Zone 2 has no white channel


def test_scene_saved_without_white_leaves_the_channel_alone(rgbw, fpp):
    scene = _scene(rgbw, "Old", {"Zone 1": "#001020"})
    assert scene["zones"][0]["white"] is None
    assert rgbw.post(f"/api/scenes/{scene['id']}/apply").status_code == 200
    assert fpp.ranges == {}


def test_applying_a_scene_clears_the_previous_scenes_white(rgbw, fpp):
    bright = _scene(rgbw, "Bright", {"Zone 1": "#000000"}, {"Zone 1": 255})
    dim = _scene(rgbw, "Dim", {"Zone 1": "#000000"})
    rgbw.post(f"/api/scenes/{bright['id']}/apply")
    assert ranges(fpp, Z1) == {255}
    rgbw.post(f"/api/scenes/{dim['id']}/apply")
    assert fpp.ranges == {}


def test_a_scene_without_white_replaces_white_left_on_its_zone(rgbw, fpp):
    """The FPP playlist path does not clear first; the zone it names still has to
    end up showing just its own color."""
    bright = _scene(rgbw, "Bright", {"Zone 1": "#000000"}, {"Zone 1": 255})
    old = _scene(rgbw, "Old", {"Zone 1": "#102030"})
    assert rgbw.get(f"/internal/scene/{bright['id']}/apply?token=tok-abc").status_code == 200
    assert ranges(fpp, Z1) == {255}
    assert rgbw.get(f"/internal/scene/{old['id']}/apply?token=tok-abc").status_code == 200
    assert fpp.ranges == {}


def test_scene_from_fpp_playlist_applies_white_too(rgbw, fpp):
    scene = _scene(rgbw, "Show", {"Zone 1": "#001020"}, {"Zone 1": 77})
    assert rgbw.get(f"/internal/scene/{scene['id']}/apply?token=tok-abc&clear=1").status_code == 200
    assert ranges(fpp, Z1) == {77}


def test_scene_survives_fpp_ignoring_the_first_switch_on(rgbw, fpp):
    fpp.ignore_first_on_after_stop = True
    scene = _scene(rgbw, "Flaky", {"Zone 1": "#001020"}, {"Zone 1": 10})
    assert rgbw.post(f"/api/scenes/{scene['id']}/apply").status_code == 200
    assert fpp.lit() == {"Zone 1"} and ranges(fpp, Z1) == {10}


def test_a_scene_reports_white_the_controller_would_not_take(rgbw, fpp, monkeypatch):
    import requests

    def boom(channels, level):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(white_layer, "_set", boom)
    scene = _scene(rgbw, "Cool", {"Zone 1": "#001020"}, {"Zone 1": 255})
    resp = rgbw.post(f"/api/scenes/{scene['id']}/apply")
    assert resp.status_code == 502 and "Zone 1" in resp.get_json()["error"]


# ── Backup, restore and upgrade ──────────────────────────────────────────────

def test_backup_and_restore_carry_white(rgbw, fpp):
    _scene(rgbw, "Keep", {"Zone 1": "#112233"}, {"Zone 1": 66})
    rgbw.post("/colors/save", json={"name": "Warm", "hex": "#ff8800", "white": 120})
    backup = rgbw.get("/api/backup")
    payload = json.loads(backup.data)
    assert payload["scenes"][0]["zones"][0]["white"] == 66
    assert payload["saved_colors"][0]["white"] == 120

    rgbw.delete(f"/api/scenes/{payload['scenes'][0]['id']}")
    restored = rgbw.post("/api/restore", data={"file": (io.BytesIO(backup.data), "b.json")},
                         content_type="multipart/form-data")
    assert restored.status_code == 200, restored.get_json()
    assert rgbw.get("/api/scenes").get_json()[0]["zones"][0]["white"] == 66
    assert rgbw.get("/colors/buttons").get_json()[0]["white"] == 120


def test_restoring_an_old_backup_without_white_still_works(rgbw, fpp):
    old = {"version": 4, "scenes": [{"name": "Old", "zones": [{"fpp_model": "Zone 1", "hex_color": "#112233"}]}],
           "saved_colors": [{"id": 1, "name": "C", "hex_value": "#445566"}], "color_buttons": []}
    restored = rgbw.post("/api/restore", data={"file": (io.BytesIO(json.dumps(old).encode()), "o.json")},
                         content_type="multipart/form-data")
    assert restored.status_code == 200, restored.get_json()
    assert rgbw.get("/api/scenes").get_json()[0]["zones"][0]["white"] is None


def test_upgrading_adds_the_white_columns(client):
    """A controller from before white existed has neither column; startup adds them."""
    from sqlalchemy import text
    from app import _add_missing_columns
    with client.app.app_context():
        for table in ("saved_colors", "scene_zones"):
            db.session.execute(text(f"ALTER TABLE {table} DROP COLUMN white"))
        db.session.commit()
        _add_missing_columns(client.app)
        for table in ("saved_colors", "scene_zones"):
            cols = [r[1] for r in db.session.execute(text(f"PRAGMA table_info({table})"))]
            assert "white" in cols
        db.session.add(SavedColor(name="x", hex_value="#000000"))
        db.session.commit()                          # and the model can use it again
