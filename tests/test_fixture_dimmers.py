"""DMX fixtures with a master dimmer.

Their color channels sit inside a wider block, so a display map points overlay
models straight at the red channels and names the dimmer channels, which the app
holds at full only while it is lighting those fixtures (see app/white_layer.py).
"""
import json

import pytest

from app import db, overlay_layout as ol, white_layer
from app.models import FixtureDimmer, ZoneLayout, ZoneMember
from app.routes import settings

# Two 6-channel fixtures (dimmer first) in one model, then two 8-channel ones,
# which can only be a model each.
MAP = """# Model: 'Pars', 2 nodes
# Dimmers: 1,7
0,0,0,1,3,RGB,5
10,0,0,7,3,RGB,5
# Model: 'Wash 1', 1 nodes
# Dimmers: 13
20,0,0,13,3,RGB,5
# Model: 'Wash 2', 1 nodes
# Dimmers: 21
30,0,0,21,3,RGB,5
"""


@pytest.fixture
def client(app_factory, fpp, monkeypatch, tmp_path):
    path = tmp_path / "virtualdisplaymap"
    path.write_text(MAP)
    monkeypatch.setattr(settings, "DISPLAY_MAP_PATH", str(path))
    app = app_factory(settle=True)
    client = app.test_client()
    with client.session_transaction() as s:
        s["logged_in"] = True
    client.app = app
    fpp.models += ["Zone 2.1", "Zone 2.2"]
    return client


@pytest.fixture
def imported(client):
    res = client.post("/api/fpp/layout/import", json={"replace": True, "models": [
        {"slot": 1, "source_name": "Pars"},
        {"slot": 2, "source_name": "Wash 1"},
        {"slot": 2, "source_name": "Wash 2"},
    ]})
    assert res.status_code == 200, res.get_json()
    return client


def send(client, model):
    return client.post("/colors/send", json={"hex": "#102030", "model": model})


def test_color_channels_are_the_real_ones(imported):
    with imported.app.app_context():
        pars = db.session.get(ZoneLayout, 1)
        # red of fixture 1 is channel 2; node 3 is channel 8, red of fixture 2
        assert (pars.start_channel, pars.channel_count, pars.data) == (2, 9, "1,3")
        assert [m.start_channel for m in ZoneMember.query.order_by(ZoneMember.position)] == [14, 22]


def test_models_that_do_not_share_a_lattice_get_no_whole_display_model():
    models = ol.parse_display_map(MAP)
    assert models[0]["dimmers"] == [1, 7]
    assert ol.derive_composite_grid(models) is None


def test_a_misaligned_model_is_refused_rather_than_driven_wrongly():
    grid = ol.derive_grid(ol.parse_display_map(
        "# Model: 'Bad', 2 nodes\n0,0,0,1,3\n10,0,0,9,3\n")[0])
    assert "multiples of 3" in ol.validate_grid(grid, "Bad")


def test_color_raises_only_that_zones_dimmers(imported, fpp):
    assert send(imported, "Zone 1").status_code == 200
    assert fpp.ranges == {1: 255, 7: 255}


def test_all_raises_every_dimmer_and_stop_releases_them(imported, fpp):
    assert send(imported, "All").status_code == 200
    assert fpp.ranges == {1: 255, 7: 255, 13: 255, 21: 255}
    assert imported.post("/colors/stop").status_code == 200
    assert fpp.ranges == {}


def test_switching_all_off_for_one_zone_releases_the_others(imported, fpp):
    send(imported, "All")
    send(imported, "Zone 1")
    assert fpp.ranges == {1: 255, 7: 255}


def test_scene_holds_dimmers_and_releasing_overlays_lets_go(imported, fpp):
    scene = imported.post("/api/scenes", json={
        "name": "Dusk", "zones": {"Zone 2": "#ff0000"},
    })
    assert scene.status_code == 201, scene.get_json()
    assert imported.post(f"/api/scenes/{scene.get_json()['id']}/apply").status_code == 200
    assert fpp.ranges == {13: 255, 21: 255}
    imported.post("/api/overlays/release")
    assert fpp.ranges == {}


def test_effect_holds_dimmers_and_stopping_it_lets_go(imported, fpp):
    res = imported.post("/api/effects/run",
                        json={"models": ["Zone 1"], "effect": "Bars", "args": []})
    assert res.status_code == 200, res.get_json()
    assert fpp.ranges == {1: 255, 7: 255}
    imported.post("/api/effects/stop", json={"models": ["Zone 1"]})
    assert fpp.ranges == {}


def test_dimmers_are_released_when_fpp_switches_the_model_off(imported, fpp):
    send(imported, "Zone 1")
    send(imported, "Zone 2")
    for name in ("Zone 2.1", "Zone 2.2"):
        fpp.overlay_state[name]["State"] = 0      # e.g. a playlist's lead-out
    with imported.app.app_context():
        white_layer.release_switched_off()
    assert fpp.ranges == {1: 255, 7: 255}


def test_dimmers_survive_a_backup_and_restore(imported):
    backup = imported.get("/api/backup").get_json()
    assert [d["channel"] for d in backup["fixture_dimmers"]] == [1, 7, 13, 21]
    imported.post("/api/fpp/layout/clear")
    with imported.app.app_context():
        assert FixtureDimmer.query.count() == 0
        assert settings.apply_ui_backup(json.loads(json.dumps(backup))) is None
        assert FixtureDimmer.query.count() == 4


def test_a_map_can_choose_each_models_zone(client, monkeypatch, tmp_path):
    path = tmp_path / "zoned"
    path.write_text(MAP.replace("'Pars', 2 nodes\n", "'Pars', 2 nodes\n# Zone: 2\n")
                    .replace("1 nodes\n", "1 nodes\n# Zone: 1\n"))
    monkeypatch.setattr(settings, "DISPLAY_MAP_PATH", str(path))
    found = client.get("/api/fpp/layout/preview").get_json()["models"]
    assert [(m["source_name"], m["slot"], m["group_label"]) for m in found] == [
        ("Pars", 2, "Pars"), ("Wash 1", 1, "Wash"), ("Wash 2", 1, "Wash"),
    ]


def test_a_partly_zoned_map_falls_back_to_suggestions():
    models = ol.parse_display_map(MAP.replace("2 nodes\n", "2 nodes\n# Zone: 2\n"))
    assert ol.mapped_groups(models, 15) is None
