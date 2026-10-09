import re

import requests
from flask import Blueprint, current_app, jsonify, request

from app import db
from app.auth_utils import internal_token_error, login_required
from app.fpp_api import ensure_models_active, fpp_url, hex_to_rgb, playlist_url
from app.fpp_playlist import build_playlist_def, scene_entries
from app.models import (
    OVERLAY_MODELS, Scene, SceneZone, all_overlay_models, expand_overlay_models,
)
from app.validation import json_object, page_args, str_field

scenes_bp = Blueprint("scenes", __name__)

_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _playlist_name(scene_name):
    return f"Scene - {scene_name}"


def _write_scene_files(scene):
    """Register the scene playlist with FPP.

    The playlist shape comes from app/fpp_playlist.py; see that module for why
    it must not be restructured.  clear=False keeps a standalone scene playlist
    behaving exactly as it always has — it only touches the zones it names.
    """
    playlist_def = build_playlist_def(
        _playlist_name(scene.name),
        scene_entries(scene.id, 10),
        "FPP UI Scene",
    )
    try:
        requests.post(
            playlist_url(_playlist_name(scene.name)),
            json=playlist_def,
            timeout=5,
        ).raise_for_status()
    except requests.RequestException as exc:
        current_app.logger.warning("Could not register FPP playlist for scene %d: %s", scene.id, exc)


def _delete_scene_files(scene):
    try:
        requests.delete(playlist_url(_playlist_name(scene.name)), timeout=5)
    except requests.RequestException:
        pass


def _reset_overlays():
    """Stop any running effect and deactivate every overlay model.

    Deliberately does NOT stop playback: this runs from inside a playlist entry,
    where hitting /playlists/stop would kill the playlist that is driving it.
    """
    try:
        requests.post(
            fpp_url("/command"),
            json={
                "command": "Overlay Model Effect",
                "multisyncCommand": False,
                "multisyncHosts": "",
                "args": ["--All Models--", "Enabled", "Stop Effects"],
            },
            timeout=5,
        )
    except requests.RequestException:
        pass

    from app.routes.colors import mark_overlays_cleared
    mark_overlays_cleared()
    for model in sorted(all_overlay_models()):
        try:
            requests.put(fpp_url(f"/overlays/model/{model}/state"), json={"State": 0}, timeout=3)
        except requests.RequestException:
            pass


def _put_zone_on(target, rgb):
    """Switch one overlay model on and fill it with ``rgb``."""
    requests.put(
        fpp_url(f"/overlays/model/{target}/state"), json={"State": 1}, timeout=5,
    ).raise_for_status()
    requests.put(
        fpp_url(f"/overlays/model/{target}/fill"), json={"RGB": list(rgb)}, timeout=5,
    ).raise_for_status()


def _set_scene_colors(scene):
    """Enable overlay models and fill colors for each zone. Does not stop playback."""
    errors = []
    colors = {}   # overlay model -> rgb, for the read-back below
    for zone in scene.zones:
        try:
            rgb = hex_to_rgb(zone.hex_color)
        except (ValueError, IndexError, TypeError):
            # Corrupt stored color (e.g. bad restore) - skip this zone rather
            # than aborting the whole scene with a 500.
            current_app.logger.error(
                "Scene %d has invalid color %r for %s - skipping zone",
                scene.id, zone.hex_color, zone.fpp_model,
            )
            errors.append(zone.fpp_model)
            continue
        try:
            for target in expand_overlay_models([zone.fpp_model]):
                _put_zone_on(target, rgb)
                colors[target] = rgb
        except requests.RequestException as exc:
            current_app.logger.error("Scene %d apply error for %s: %s", scene.id, zone.fpp_model, exc)
            errors.append(zone.fpp_model)

    def reactivate(models):
        for target in models:
            try:
                _put_zone_on(target, colors[target])
            except requests.RequestException as exc:
                current_app.logger.error("Scene %d re-apply error for %s: %s", scene.id, target, exc)

    errors += ensure_models_active(list(colors), reactivate)
    return len(errors) == 0, errors


def _apply_scene(scene):
    """Stop playback, clear all overlays, then set each zone stored in the scene."""
    try:
        requests.get(fpp_url("/playlists/stop"), timeout=5)
    except requests.RequestException:
        pass

    _reset_overlays()

    return _set_scene_colors(scene)


@scenes_bp.get("/api/scenes")
@login_required
def list_scenes():
    limit, offset = page_args()
    rows = Scene.query.order_by(Scene.id).limit(limit).offset(offset).all()
    return jsonify([s.to_dict() for s in rows])


@scenes_bp.post("/api/scenes")
@login_required
def create_scene():
    data = json_object()
    name = str_field(data, "name")
    zones = data.get("zones", {})

    if not name or len(name) > 64:
        return jsonify({"error": "Name required (max 64 chars)"}), 400
    # The name becomes an FPP playlist name, which goes into a URL path
    # unencoded — same rule effect presets already enforce.
    if "/" in name or "\\" in name or ".." in name:
        return jsonify({"error": "Name cannot contain slashes or .."}), 400
    if Scene.query.filter_by(name=name).first():
        return jsonify({"error": "A scene with that name already exists"}), 409
    if not zones or not isinstance(zones, dict):
        return jsonify({"error": "No zone colors provided"}), 400

    scene = Scene(name=name)
    db.session.add(scene)
    db.session.flush()

    valid_zones = 0
    for fpp_model, hex_color in zones.items():
        if fpp_model not in OVERLAY_MODELS:
            continue
        if not _HEX_RE.match(str(hex_color)):
            continue
        db.session.add(SceneZone(scene_id=scene.id, fpp_model=fpp_model, hex_color=hex_color))
        valid_zones += 1

    if valid_zones == 0:
        db.session.rollback()
        return jsonify({"error": "No valid zone colors provided"}), 400

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
    """Token-authenticated endpoint for FPP playlists to trigger a scene.

    Must never call _apply_scene() — that stops playback, which would kill the
    playlist calling in here.  ?clear=1 resets the overlay layer first (used by
    built playlists so one item's colors do not bleed into the next); without it
    the scene only touches the zones it names, as it always has.
    """
    denied = internal_token_error()
    if denied:
        return denied

    scene = db.session.get(Scene, scene_id)
    if not scene:
        return jsonify({"error": "Scene not found"}), 404

    if request.args.get("clear") == "1":
        _reset_overlays()

    ok, errors = _set_scene_colors(scene)
    if not ok:
        return jsonify({"error": f"Partial apply — failed: {', '.join(errors)}"}), 502
    return jsonify({"ok": True})
