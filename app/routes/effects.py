import hmac
import json
from urllib.parse import quote

import requests
from flask import Blueprint, current_app, jsonify, render_template, request

from app import db
from app.auth_utils import login_required
from app.models import EffectPreset, get_all_zones

effects_bp = Blueprint("effects", __name__)


def _fpp(path):
    return f"{current_app.config['FPP_BASE_URL']}{path}"


def _playlist_name(preset_name):
    return f"Effect - {preset_name}"


def _str_list(value):
    """Coerce a JSON payload field to a list of non-empty strings, or None if
    it isn't list-shaped at all (so callers can reject it)."""
    if value is None:
        return []
    if not isinstance(value, list):
        return None
    return [str(v) for v in value if isinstance(v, (str, int, float)) and str(v).strip()]


def _send_effect(models, effect, args, multisync, systems):
    """Push an 'Overlay Model Effect' command to FPP. Returns (ok, error)."""
    command = {
        "command": "Overlay Model Effect",
        "multisyncCommand": multisync,
        "multisyncHosts": ",".join(systems) if multisync else "",
        "args": [",".join(models), "Enabled", effect] + [str(a) for a in args],
    }
    try:
        resp = requests.post(_fpp("/command"), json=command, timeout=10)
        resp.raise_for_status()
        return True, None
    except requests.RequestException as exc:
        current_app.logger.error("FPP run effect error: %s", exc)
        return False, str(exc)


def _run_preset(preset):
    """Fire the effect stored in a preset."""
    d = preset.to_dict()
    return _send_effect(d["models"], d["effect_name"], d["args"], d["multisync"], d["systems"])


def _write_effect_playlist(preset):
    """Register an FPP playlist that runs this preset.

    Mirrors the scene playlist structure in app/routes/scenes.py, which is the
    shape FPP is known to play correctly: the URL command in mainPlaylist fires
    the effect through Flask, the 10-second pause keeps FPP's player active
    (and gives the entry a non-zero duration), and leadOut clears the overlay
    when the playlist is stopped gracefully — e.g. when the scheduler reaches
    the entry's endTime with stopType=Graceful.
    """
    token = current_app.config.get("INTERNAL_TOKEN", "")
    apply_url = f"http://localhost:5000/internal/effect/{preset.id}/apply?token={token}"

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
        "name": _playlist_name(preset.name),
        "version": 4,
        "repeat": 1,
        "loopCount": 0,
        "desc": "FPP UI Effect",
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
            _fpp(f"/playlist/{_playlist_name(preset.name)}"),
            json=playlist_def,
            timeout=5,
        ).raise_for_status()
    except requests.RequestException as exc:
        current_app.logger.warning(
            "Could not register FPP playlist for effect preset %d: %s", preset.id, exc
        )


def _delete_effect_playlist(preset):
    try:
        requests.delete(_fpp(f"/playlist/{_playlist_name(preset.name)}"), timeout=5)
    except requests.RequestException:
        pass


@effects_bp.get("/effects")
@login_required
def effects_page():
    zones = [z.to_dict() for z in get_all_zones() if z.slot != 0 and not z.hidden]
    return render_template("effects.html", zones=zones)


@effects_bp.get("/api/effects/list")
@login_required
def list_effects():
    try:
        resp = requests.get(_fpp("/overlays/effects"), timeout=10)
        resp.raise_for_status()
        effects = resp.json()
    except Exception as exc:
        current_app.logger.error("FPP list effects error: %s", exc)
        return jsonify({"error": str(exc)}), 502

    if not isinstance(effects, list):
        current_app.logger.warning("FPP /overlays/effects returned non-list payload")
        effects = []

    MUSIC_NOTE_CHARS = set("♩♪♫♬")

    builtin, wled = [], []
    for e in effects:
        if not isinstance(e, str):
            continue
        if e == "Stop Effects":
            continue
        if any(c in e for c in MUSIC_NOTE_CHARS):
            continue
        if e.startswith("WLED - "):
            wled.append(e)
        else:
            builtin.append(e)

    return jsonify({"builtin": sorted(builtin), "wled": sorted(wled)})


@effects_bp.get("/api/effects/args/<path:effect_name>")
@login_required
def get_effect_args(effect_name):
    # <path:> lets slashes through — don't forward traversal-shaped names to FPP.
    if "/" in effect_name or "\\" in effect_name or ".." in effect_name:
        return jsonify({"error": "Invalid effect name"}), 400
    try:
        resp = requests.get(
            _fpp(f"/overlays/effects/{quote(effect_name, safe='')}"), timeout=10
        )
        resp.raise_for_status()
        return jsonify(resp.json())
    except Exception as exc:
        current_app.logger.error("FPP effect args error for %r: %s", effect_name, exc)
        return jsonify({"error": str(exc)}), 502


@effects_bp.get("/api/effects/fonts")
@login_required
def get_fonts():
    try:
        resp = requests.get(_fpp("/overlays/fonts"), timeout=10)
        resp.raise_for_status()
        return jsonify(resp.json())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@effects_bp.get("/api/effects/systems")
@login_required
def get_systems():
    try:
        resp = requests.get(_fpp("/fppd/multiSyncSystems"), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        systems = [
            {"hostname": s.get("hostname") or s.get("address", ""),
             "address": s.get("address", "")}
            for s in (data.get("systems") or [])
            if s.get("address")
        ]
        return jsonify(systems)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@effects_bp.post("/api/effects/run")
@login_required
def run_effect():
    data = request.get_json(silent=True) or {}
    models    = _str_list(data.get("models"))
    effect    = str(data.get("effect", "")).strip()
    args      = data.get("args", [])
    multisync = bool(data.get("multisync", False))
    systems   = _str_list(data.get("systems")) or []

    if not isinstance(args, list):
        return jsonify({"error": "args must be a list"}), 400
    if not models:
        return jsonify({"error": "No zones selected"}), 400
    if not effect:
        return jsonify({"error": "No effect selected"}), 400

    ok, error = _send_effect(models, effect, args, multisync, systems)
    if not ok:
        return jsonify({"error": f"Could not run effect: {error}"}), 502
    return jsonify({"ok": True})


@effects_bp.post("/api/effects/stop")
@login_required
def stop_effect():
    data      = request.get_json(silent=True) or {}
    models    = _str_list(data.get("models")) or []
    multisync = bool(data.get("multisync", False))
    systems   = _str_list(data.get("systems")) or []

    model_str = ",".join(models) if models else "All"
    command = {
        "command": "Overlay Model Effect",
        "multisyncCommand": multisync,
        "multisyncHosts": ",".join(systems) if multisync else "",
        "args": [model_str, "Enabled", "Stop Effects"],
    }
    try:
        resp = requests.post(_fpp("/command"), json=command, timeout=10)
        resp.raise_for_status()
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": f"Could not stop effects: {exc}"}), 502


@effects_bp.get("/api/effects/presets")
@login_required
def list_presets():
    return jsonify([p.to_dict() for p in EffectPreset.query.order_by(EffectPreset.id).all()])


@effects_bp.post("/api/effects/presets")
@login_required
def save_preset():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()[:64]
    if not name:
        return jsonify({"error": "Name required"}), 400
    # The preset name becomes an FPP playlist name, so it has to be unique
    # and safe to use in a URL path.
    if any(ch in name for ch in ("/", "\\")) or ".." in name:
        return jsonify({"error": "Name cannot contain slashes or .."}), 400
    if EffectPreset.query.filter_by(name=name).first():
        return jsonify({"error": "A preset with that name already exists"}), 409

    models  = _str_list(data.get("models"))
    systems = _str_list(data.get("systems"))
    args    = data.get("args", [])
    if models is None or systems is None or not isinstance(args, list):
        return jsonify({"error": "models, args and systems must be lists"}), 400

    preset = EffectPreset(
        name=name,
        effect_name=str(data.get("effect_name", ""))[:128],
        models_json=json.dumps(models),
        args_json=json.dumps(args),
        multisync=bool(data.get("multisync", False)),
        systems_json=json.dumps(systems),
    )
    db.session.add(preset)
    db.session.commit()
    _write_effect_playlist(preset)
    return jsonify(preset.to_dict()), 201


@effects_bp.delete("/api/effects/presets/<int:preset_id>")
@login_required
def delete_preset(preset_id):
    preset = db.session.get(EffectPreset, preset_id)
    if not preset:
        return jsonify({"error": "Not found"}), 404
    _delete_effect_playlist(preset)
    db.session.delete(preset)
    db.session.commit()
    return jsonify({"ok": True})


@effects_bp.get("/internal/effect/<int:preset_id>/apply")
def internal_apply_effect(preset_id):
    """Token-authenticated endpoint for FPP playlists to trigger an effect preset."""
    token = request.args.get("token", "")
    internal_token = current_app.config.get("INTERNAL_TOKEN", "")

    if not internal_token:
        return jsonify({"error": "Internal token not configured"}), 503
    if not hmac.compare_digest(token, internal_token):
        return jsonify({"error": "Forbidden"}), 403

    preset = db.session.get(EffectPreset, preset_id)
    if not preset:
        return jsonify({"error": "Preset not found"}), 404

    ok, error = _run_preset(preset)
    if not ok:
        return jsonify({"error": f"Could not run effect: {error}"}), 502
    return jsonify({"ok": True})
