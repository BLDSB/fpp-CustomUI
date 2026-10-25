"""Scenes built from a saved color follow later edits of that color."""
import io
import json

import pytest


@pytest.fixture
def client(app_factory, fpp):
    client = app_factory().test_client()
    with client.session_transaction() as s:
        s["logged_in"] = True
    return client


def save_color(client, name="Red", hex_val="#ff0000"):
    resp = client.post("/colors/save", json={"name": name, "hex": hex_val})
    assert resp.status_code == 201
    return resp.get_json()


def make_linked_scene(client, color, name="Warm", zones=("Zone 1", "Zone 2")):
    resp = client.post("/api/scenes", json={
        "name": name,
        "zones": {z: color["hex_value"] for z in zones},
        "saved_colors": {z: color["saved_color_id"] for z in zones},
    })
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()


def scene_hexes(client):
    return {s["name"]: [z["hex_color"] for z in s["zones"]] for s in client.get("/api/scenes").get_json()}


def test_editing_a_saved_color_updates_linked_scenes(client, fpp):
    color = save_color(client)
    make_linked_scene(client, color)

    resp = client.put(f"/colors/buttons/{color['id']}", json={"hex": "#cc0033"})
    assert resp.status_code == 200
    assert resp.get_json()["scenes"] == ["Warm"]

    assert scene_hexes(client) == {"Warm": ["#cc0033", "#cc0033"]}
    assert client.get("/colors/buttons").get_json()[0]["hex_value"] == "#cc0033"


def test_edited_color_is_what_the_scene_plays(client, fpp):
    color = save_color(client)
    scene = make_linked_scene(client, color, zones=("Zone 1",))
    client.put(f"/colors/buttons/{color['id']}", json={"hex": "#00ff00"})

    assert client.post(f"/api/scenes/{scene['id']}/apply").status_code == 200
    assert fpp.overlay_state["Zone 1"]["RGB"] == [0, 255, 0]


def test_scene_from_a_hand_picked_color_is_not_linked(client, fpp):
    color = save_color(client)
    # The client claims a link, but the zone's color is not the saved one.
    resp = client.post("/api/scenes", json={
        "name": "Tweaked",
        "zones": {"Zone 1": "#fe0000"},
        "saved_colors": {"Zone 1": color["saved_color_id"]},
    })
    assert resp.get_json()["zones"][0]["saved_color_id"] is None

    client.put(f"/colors/buttons/{color['id']}", json={"hex": "#0000ff"})
    assert scene_hexes(client) == {"Tweaked": ["#fe0000"]}


def test_edit_rejects_bad_hex_and_unknown_button(client, fpp):
    color = save_color(client)
    assert client.put(f"/colors/buttons/{color['id']}", json={"hex": "red"}).status_code == 400
    assert client.put("/colors/buttons/999", json={"hex": "#000000"}).status_code == 404


def test_deleting_a_color_in_use_needs_confirmation(client, fpp):
    color = save_color(client)
    make_linked_scene(client, color)

    refused = client.delete(f"/colors/buttons/{color['id']}")
    assert refused.status_code == 409
    assert refused.get_json()["scenes"] == ["Warm"]
    assert len(client.get("/colors/buttons").get_json()) == 1

    assert client.delete(f"/colors/buttons/{color['id']}?detach=1").status_code == 200
    assert client.get("/colors/buttons").get_json() == []
    # The scene keeps its shade but no longer follows anything.
    zones = client.get("/api/scenes").get_json()[0]["zones"]
    assert [(z["hex_color"], z["saved_color_id"]) for z in zones] == [("#ff0000", None)] * 2


def test_deleting_an_unused_color_needs_no_confirmation(client, fpp):
    color = save_color(client)
    assert client.delete(f"/colors/buttons/{color['id']}").status_code == 200


def test_backup_restore_keeps_the_link(client, fpp):
    color = save_color(client)
    make_linked_scene(client, color)
    backup = client.get("/api/backup").data

    restored = client.post("/api/restore", data={"file": (io.BytesIO(backup), "b.json")},
                           content_type="multipart/form-data")
    assert restored.status_code == 200, restored.get_json()

    new_button = client.get("/colors/buttons").get_json()[0]
    client.put(f"/colors/buttons/{new_button['id']}", json={"hex": "#112233"})
    assert scene_hexes(client) == {"Warm": ["#112233", "#112233"]}


def test_restoring_an_older_backup_without_links(client, fpp):
    color = save_color(client)
    make_linked_scene(client, color)
    payload = json.loads(client.get("/api/backup").data)
    for scene in payload["scenes"]:
        for zone in scene["zones"]:
            del zone["saved_color_id"]

    restored = client.post("/api/restore",
                           data={"file": (io.BytesIO(json.dumps(payload).encode()), "old.json")},
                           content_type="multipart/form-data")
    assert restored.status_code == 200
    zones = client.get("/api/scenes").get_json()[0]["zones"]
    assert all(z["saved_color_id"] is None and z["hex_color"] == "#ff0000" for z in zones)


def test_editing_can_rename_the_color(client, fpp):
    color = save_color(client)
    resp = client.put(f"/colors/buttons/{color['id']}", json={"hex": "#ff0000", "name": "Crimson"})
    assert resp.status_code == 200
    assert resp.get_json()["label"] == "Crimson"
    assert client.get("/colors/buttons").get_json()[0]["label"] == "Crimson"
    assert client.put(f"/colors/buttons/{color['id']}", json={"hex": "#ff0000", "name": " "}).status_code == 400


def test_renaming_a_scene_moves_its_playlist_and_schedule(client, fpp):
    scene = make_linked_scene(client, save_color(client), name="Old Name")
    fpp.schedule = [{"playlist": "Scene - Old Name", "enabled": 1},
                    {"playlist": "Other", "enabled": 1}]

    resp = client.put(f"/api/scenes/{scene['id']}", json={"name": "New Name"})
    assert resp.status_code == 200
    assert resp.get_json()["name"] == "New Name"
    assert "Scene - New Name" in fpp.playlists
    assert "Scene - Old Name" not in fpp.playlists
    assert [e["playlist"] for e in fpp.schedule] == ["Scene - New Name", "Other"]
    # Zones and links survive the rename.
    assert len(resp.get_json()["zones"]) == 2


def test_rename_rejects_duplicates_and_bad_names(client, fpp):
    color = save_color(client)
    a = make_linked_scene(client, color, name="A")
    make_linked_scene(client, color, name="B")
    assert client.put(f"/api/scenes/{a['id']}", json={"name": "B"}).status_code == 409
    assert client.put(f"/api/scenes/{a['id']}", json={"name": "a/b"}).status_code == 400
    assert client.put(f"/api/scenes/{a['id']}", json={"name": ""}).status_code == 400
    assert client.put("/api/scenes/999", json={"name": "Z"}).status_code == 404
    # Re-saving its own name is not a clash.
    assert client.put(f"/api/scenes/{a['id']}", json={"name": "A"}).status_code == 200
