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


def _stored_brightness():
    """Saved brightness, clamped to 0-100; 100 if unset, corrupt, or DB down."""
    try:
        setting = db.session.get(AppSetting, "brightness")
    except Exception:
        current_app.logger.exception("Could not read stored brightness — using 100")
        return 100
    if setting and setting.value:
        try:
            return max(0, min(100, int(setting.value)))
        except (TypeError, ValueError):
            current_app.logger.warning(
                "Stored brightness %r is not a number — using 100", setting.value
            )
    return 100


@main.route("/controls")
@login_required
def controls():
    return render_template("index.html", brightness=_stored_brightness())


@main.get("/api/brightness")
@login_required
def get_brightness():
    return jsonify({"brightness": _stored_brightness()})


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

    # Read existing processors, replace our brightness entry, write back.
    # If the read fails we must NOT write back an empty list — that would
    # silently delete every other output processor configured in FPP.
    try:
        resp = requests.get(_fpp("/channel/output/processors"), timeout=5)
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            raise ValueError(f"unexpected processors payload: {type(body).__name__}")
    except Exception as exc:
        current_app.logger.error("Could not read FPP output processors: %s", exc)
        return jsonify({"error": "Could not read FPP output processors"}), 502

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
