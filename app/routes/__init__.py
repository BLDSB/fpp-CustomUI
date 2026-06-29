import requests
from flask import Blueprint, current_app, jsonify, render_template, request

from app import db
from app.auth_utils import login_required
from app.models import AppSetting

main = Blueprint("main", __name__)

_BRIGHTNESS_DESCRIPTION = "Global Brightness"
_BRIGHTNESS_COUNT = 524288  # covers all practical channel counts


def _fpp(path):
    return f"{current_app.config['FPP_BASE_URL']}{path}"


@main.route("/")
@login_required
def index():
    return render_template("home.html")


@main.route("/controls")
@login_required
def controls():
    setting = db.session.get(AppSetting, "brightness")
    brightness = int(setting.value) if setting and setting.value else 100
    return render_template("index.html", brightness=brightness)


@main.get("/api/brightness")
@login_required
def get_brightness():
    setting = db.session.get(AppSetting, "brightness")
    return jsonify({"brightness": int(setting.value) if setting and setting.value else 100})


@main.post("/api/brightness")
@login_required
def set_brightness():
    data = request.get_json(silent=True) or {}
    try:
        value = int(data.get("brightness", 100))
        if not 0 <= value <= 100:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "brightness must be 0–100"}), 400

    # Read existing processors, replace our brightness entry, write back
    try:
        resp = requests.get(_fpp("/channel/output/processors"), timeout=5)
        resp.raise_for_status()
        body = resp.json()
    except Exception:
        body = {"outputProcessors": []}

    processors = [
        p for p in (body.get("outputProcessors") or [])
        if p.get("description") != _BRIGHTNESS_DESCRIPTION
    ]
    processors.insert(0, {
        "type": "Brightness",
        "active": 1,
        "description": _BRIGHTNESS_DESCRIPTION,
        "start": 1,
        "count": _BRIGHTNESS_COUNT,
        "brightness": value,
        "gamma": 1.0,
    })
    payload = {"outputProcessors": processors}

    try:
        r = requests.post(_fpp("/channel/output/processors"), json=payload, timeout=5)
        r.raise_for_status()
    except Exception as exc:
        return jsonify({"error": f"FPP error: {exc}"}), 502

    # Persist in AppSettings so the slider restores on next page load
    setting = db.session.get(AppSetting, "brightness")
    if setting:
        setting.value = str(value)
    else:
        db.session.add(AppSetting(key="brightness", value=str(value)))
    db.session.commit()

    return jsonify({"ok": True, "brightness": value})


# Additional route modules are registered in app/__init__.py as blueprints.
