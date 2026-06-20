import hmac
import re

import requests
from flask import Blueprint, current_app, jsonify, request

from app import db
from app.auth_utils import login_required
from app.models import OVERLAY_MODELS, Scene, SceneZone

scenes_bp = Blueprint("scenes", __name__)

_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _fpp(path):
    return f"{current_app.config['FPP_BASE_URL']}{path}"


def _hex_to_rgb(hex_color):
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _playlist_name(scene_name):
    return f"Scene - {scene_name}"


def _write_scene_files(scene):
    """Register the scene playlist with FPP.

    leadIn applies the scene colors once via Flask.  Pixel overlay models are a
    persistent layer — colors stay set until explicitly cleared, so no loop is needed.
    mainPlaylist is a simple 10-second repeating pause that keeps FPP's player
    active without consuming resources.
    leadOut disables all overlay models when FPP stops the playlist gracefully
    (i.e. when the scheduler reaches the entry's endTime with stopType=Graceful).
    """
    token = current_app.config.get("INTERNAL_TOKEN", "")
    apply_url = f"http://localhost:5000/internal/scene/{scene.id}/apply?token={token}"

    def url_cmd(u):
        return {"type": "command", "enabled": 1, "command": "URL",
                "args": [u, "GET", ""], "startDelay": 0, "endDelay": 0}

    def overlay_effect(model, state, action):
        return {"type": "command", "enabled": 1, "command": "Overlay Model Effect",
                "args": [model, state, action], "startDelay": 0, "endDelay": 0}

    def pause_item(d):
        return {"type": "pause", "enabled": 1, "duration": d,
                "startDelay": 0, "endDelay": 0}

    playlist_def = {
        "name": _playlist_name(scene.name),
        "version": 4,
        "repeat": 1,
        "loopCount": 0,
        "desc": "FPP UI Scene",
        "random": 0,
        "empty": False,
        "leadIn": [],
        "mainPlaylist": [
            url_cmd(apply_url),
            pause_item(10),
        ],
        "leadOut": [
            pause_item(3),
            overlay_effect("--All Models--", "Enabled", "Stop Effects"),
        ],
    }
    try:
        requests.post(
            _fpp(f"/playlist/{_playlist_name(scene.name)}"),
            json=playlist_def,
            timeout=5,
        )
    except requests.RequestException as exc:
        current_app.logger.warning("Could not register FPP playlist for scene %d: %s", scene.id, exc)


def _delete_scene_files(scene):
    try:
        requests.delete(_fpp(f"/playlist/{_playlist_name(scene.name)}"), timeout=5)
    except requests.RequestException:
        pass


def _set_scene_colors(scene):
    """Enable overlay models and fill colors for each zone. Does not stop playback."""
    errors = []
    for zone in scene.zones:
        r, g, b = _hex_to_rgb(zone.hex_color)
        try:
            requests.put(
                _fpp(f"/overlays/model/{zone.fpp_model}/state"),
                json={"State": 1},
                timeout=5,
            ).raise_for_status()
            requests.put(
                _fpp(f"/overlays/model/{zone.fpp_model}/fill"),
                json={"RGB": [r, g, b]},
                timeout=5,
            ).raise_for_status()
        except requests.RequestException as exc:
            current_app.logger.error("Scene %d apply error for %s: %s", scene.id, zone.fpp_model, exc)
            errors.append(zone.fpp_model)
    return len(errors) == 0, errors


def _apply_scene(scene):
    """Stop playback, clear all overlays, then set each zone stored in the scene."""
    try:
        requests.get(_fpp("/playlists/stop"), timeout=5)
    except requests.RequestException:
        pass

    for model in OVERLAY_MODELS:
        try:
            requests.put(_fpp(f"/overlays/model/{model}/state"), json={"State": 0}, timeout=3)
        except requests.RequestException:
            pass

    return _set_scene_colors(scene)


@scenes_bp.get("/api/scenes")
@login_required
def list_scenes():
    return jsonify([s.to_dict() for s in Scene.query.order_by(Scene.id).all()])


@scenes_bp.post("/api/scenes")
@login_required
def create_scene():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    zones = data.get("zones", {})

    if not name or len(name) > 64:
        return jsonify({"error": "Name required (max 64 chars)"}), 400
    if Scene.query.filter_by(name=name).first():
        return jsonify({"error": "A scene with that name already exists"}), 409
    if not zones or not isinstance(zones, dict):
        return jsonify({"error": "No zone colors provided"}), 400

    scene = Scene(name=name)
    db.session.add(scene)
    db.session.flush()

    for fpp_model, hex_color in zones.items():
        if fpp_model not in OVERLAY_MODELS:
            continue
        if not _HEX_RE.match(str(hex_color)):
            continue
        db.session.add(SceneZone(scene_id=scene.id, fpp_model=fpp_model, hex_color=hex_color))

    db.session.commit()
    _write_scene_files(scene)
    return jsonify(scene.to_dict()), 201


@scenes_bp.delete("/api/scenes/<int:scene_id>")
@login_required
def delete_scene(scene_id):
    scene = db.session.get(Scene, scene_id)
    if not scene:
        return jsonify({"error": "Not found"}), 404
    _delete_scene_files(scene)
    db.session.delete(scene)
    db.session.commit()
    return jsonify({"ok": True})


@scenes_bp.post("/api/scenes/<int:scene_id>/apply")
@login_required
def apply_scene(scene_id):
    scene = db.session.get(Scene, scene_id)
    if not scene:
        return jsonify({"error": "Not found"}), 404
    ok, errors = _apply_scene(scene)
    if not ok:
        return jsonify({"error": f"Partial apply — failed zones: {', '.join(errors)}"}), 502
    return jsonify({"ok": True, "zones": [z.to_dict() for z in scene.zones]})


@scenes_bp.get("/internal/scene/<int:scene_id>/apply")
def internal_apply_scene(scene_id):
    """Token-authenticated endpoint for FPP playlists to trigger a scene."""
    token = request.args.get("token", "")
    internal_token = current_app.config.get("INTERNAL_TOKEN", "")

    if not internal_token:
        return jsonify({"error": "Internal token not configured"}), 503
    if not hmac.compare_digest(token, internal_token):
        return jsonify({"error": "Forbidden"}), 403

    scene = db.session.get(Scene, scene_id)
    if not scene:
        return jsonify({"error": "Scene not found"}), 404

    ok, errors = _set_scene_colors(scene)
    if not ok:
        return jsonify({"error": f"Partial apply — failed: {', '.join(errors)}"}), 502
    return jsonify({"ok": True})


