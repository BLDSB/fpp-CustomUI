import requests
from urllib.parse import quote
from flask import Blueprint, current_app, jsonify, request

from app.auth_utils import login_required
from app.models import OVERLAY_MODELS

playlists_bp = Blueprint("playlists", __name__)


def _fpp(path):
    return f"{current_app.config['FPP_BASE_URL']}{path}"


def _stop_current():
    """Stop whatever FPP is currently playing."""
    try:
        requests.get(_fpp("/playlists/stop"), timeout=5)
    except requests.RequestException:
        pass


def _clear_all_overlays():
    """Deactivate every overlay model so a playlist has full channel control."""
    for model in OVERLAY_MODELS:
        try:
            requests.put(
                _fpp(f"/overlays/model/{model}/state"),
                json={"State": 0},
                timeout=3,
            )
        except requests.RequestException:
            pass


@playlists_bp.get("/api/playlists")
@login_required
def list_playlists():
    """Return the playlist names known to FPP."""
    try:
        resp = requests.get(_fpp("/playlists"), timeout=5)
        resp.raise_for_status()
        data = resp.json()
        # FPP may return a bare list or {"playlists": [...]}
        playlists = data if isinstance(data, list) else data.get("playlists", [])
        return jsonify({"playlists": sorted(playlists)})
    except requests.RequestException as exc:
        current_app.logger.error("FPP list playlists error: %s", exc)
        return jsonify({"error": "Could not reach FPP"}), 502


@playlists_bp.post("/api/playlists/<name>/play")
@login_required
def play_playlist(name):
    """Start a named playlist on FPP.

    Scene playlists (name starts with 'Scene - ') are applied directly by
    our Flask app rather than via FPP, because FPP skips command-only playlists
    that have total_duration=0.  Non-scene playlists go to FPP normally.
    """
    if "/" in name or "\\" in name or ".." in name:
        return jsonify({"error": "Invalid playlist name"}), 400

    # Scene playlists: look up the scene in the DB and apply it directly.
    if name.startswith("Scene - "):
        scene_name = name[len("Scene - "):]
        from app.models import Scene
        from app.routes.scenes import _apply_scene
        scene = Scene.query.filter_by(name=scene_name).first()
        if scene:
            _stop_current()
            ok, errors = _apply_scene(scene)
            if not ok:
                return jsonify({"error": f"Failed zones: {', '.join(errors)}"}), 502
            return jsonify({"ok": True})

    # Regular playlist: stop current, clear overlays, then start via FPP.
    _stop_current()
    _clear_all_overlays()

    try:
        data = request.get_json(silent=True) or {}
        repeat = bool(data.get("repeat", True))
        repeat_str = "true" if repeat else "false"
        resp = requests.get(_fpp(f"/playlist/{quote(name, safe='')}/start/{repeat_str}"), timeout=5)
        resp.raise_for_status()
        return jsonify({"ok": True})
    except requests.RequestException as exc:
        current_app.logger.error("FPP start playlist '%s' error: %s", name, exc)
        return jsonify({"error": "Could not start playlist"}), 502


@playlists_bp.post("/api/playlists/stop")
@login_required
def stop_playback():
    """Stop FPP playback and deactivate all overlay models."""
    _stop_current()
    _clear_all_overlays()
    return jsonify({"ok": True})


@playlists_bp.get("/api/sequences")
@login_required
def list_sequences():
    """Return the sequence names known to FPP."""
    try:
        resp = requests.get(_fpp("/sequence"), timeout=5)
        resp.raise_for_status()
        data = resp.json()
        sequences = data if isinstance(data, list) else []
        return jsonify({"sequences": sorted(sequences)})
    except requests.RequestException as exc:
        current_app.logger.error("FPP list sequences error: %s", exc)
        return jsonify({"error": "Could not reach FPP"}), 502


@playlists_bp.post("/api/sequences/<name>/play")
@login_required
def play_sequence(name):
    """Play a named sequence by wrapping it in a single-item FPP playlist.

    All sequence playback is routed through the 'Current-Sequence' playlist so that
    stop, loop, and preemption all work via FPP's normal /playlists/stop API.
    The playlist definition is saved to FPP via its own POST API (no direct
    filesystem writes needed).
    """
    if "/" in name or "\\" in name or ".." in name:
        return jsonify({"error": "Invalid sequence name"}), 400

    # Stop whatever is currently playing so the new selection always preempts.
    _stop_current()

    _clear_all_overlays()

    data = request.get_json(silent=True) or {}
    repeat = bool(data.get("repeat", True))
    seq_file = name if name.endswith(".fseq") else f"{name}.fseq"

    # Build a single-sequence playlist and push it to FPP via its REST API.
    playlist_def = {
        "name": "Current-Sequence",
        "version": 4,
        "repeat": 1 if repeat else 0,
        "loopCount": 0,
        "desc": "",
        "random": 0,
        "empty": False,
        "leadIn": [],
        "mainPlaylist": [
            {
                "type": "sequence",
                "enabled": 1,
                "playOnce": 0 if repeat else 1,
                "sequenceName": seq_file,
                "displayMode": "argsOnly",
                "timecode": "Default",
                "duration": 86400,
            }
        ],
        "leadOut": [],
    }

    try:
        resp = requests.post(_fpp("/playlist/Current-Sequence"), json=playlist_def, timeout=5)
        resp.raise_for_status()
    except requests.RequestException as exc:
        current_app.logger.error("Could not save temp sequence playlist: %s", exc)
        return jsonify({"error": "Could not prepare sequence for playback"}), 500

    repeat_str = "true" if repeat else "false"
    try:
        resp = requests.get(_fpp(f"/playlist/Current-Sequence/start/{repeat_str}"), timeout=5)
        resp.raise_for_status()
        return jsonify({"ok": True})
    except requests.RequestException as exc:
        current_app.logger.error("FPP start sequence playlist error: %s", exc)
        return jsonify({"error": "Could not start sequence"}), 502


@playlists_bp.get("/api/fppd/status")
@login_required
def fpp_status():
    """Proxy the FPP daemon status endpoint."""
    try:
        resp = requests.get(_fpp("/fppd/status"), timeout=5)
        resp.raise_for_status()
        return jsonify(resp.json())
    except requests.RequestException as exc:
        current_app.logger.error("FPP status error: %s", exc)
        return jsonify({"error": "Could not reach FPP"}), 502
