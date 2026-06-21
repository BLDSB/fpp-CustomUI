import json

import requests
from flask import Blueprint, current_app, jsonify, render_template, request

from app import db
from app.auth_utils import login_required
from app.models import EffectPreset, get_all_zones

effects_bp = Blueprint("effects", __name__)


def _fpp(path):
    return f"{current_app.config['FPP_BASE_URL']}{path}"


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
        return jsonify({"error": str(exc)}), 502

    builtin, wled = [], []
    for e in effects:
        if e == "Stop Effects":
            continue
        if e.startswith("WLED - "):
            wled.append(e)
        else:
            builtin.append(e)

    return jsonify({"builtin": sorted(builtin), "wled": sorted(wled)})


@effects_bp.get("/api/effects/args/<path:effect_name>")
@login_required
def get_effect_args(effect_name):
    try:
        resp = requests.get(_fpp(f"/overlays/effects/{effect_name}"), timeout=10)
        resp.raise_for_status()
        return jsonify(resp.json())
    except Exception as exc:
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
    models    = data.get("models", [])
    effect    = str(data.get("effect", "")).strip()
    args      = data.get("args", [])
    multisync = bool(data.get("multisync", False))
    systems   = data.get("systems", [])

    if not models:
        return jsonify({"error": "No zones selected"}), 400
    if not effect:
        return jsonify({"error": "No effect selected"}), 400

    command = {
        "command": "Overlay Model Effect",
        "multisyncCommand": multisync,
        "multisyncHosts": ",".join(systems) if multisync else "",
        "args": [",".join(models), "Enabled", effect] + [str(a) for a in args],
    }
    try:
        resp = requests.post(_fpp("/command"), json=command, timeout=10)
        resp.raise_for_status()
        return jsonify({"ok": True})
    except Exception as exc:
        current_app.logger.error("FPP run effect error: %s", exc)
        return jsonify({"error": f"Could not run effect: {exc}"}), 502


@effects_bp.post("/api/effects/stop")
@login_required
def stop_effect():
    data      = request.get_json(silent=True) or {}
    models    = data.get("models", [])
    multisync = bool(data.get("multisync", False))
    systems   = data.get("systems", [])

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

    preset = EffectPreset(
        name=name,
        effect_name=str(data.get("effect_name", ""))[:128],
        models_json=json.dumps(data.get("models", [])),
        args_json=json.dumps(data.get("args", [])),
        multisync=bool(data.get("multisync", False)),
        systems_json=json.dumps(data.get("systems", [])),
    )
    db.session.add(preset)
    db.session.commit()
    return jsonify(preset.to_dict()), 201


@effects_bp.delete("/api/effects/presets/<int:preset_id>")
@login_required
def delete_preset(preset_id):
    preset = db.session.get(EffectPreset, preset_id)
    if not preset:
        return jsonify({"error": "Not found"}), 404
    db.session.delete(preset)
    db.session.commit()
    return jsonify({"ok": True})
