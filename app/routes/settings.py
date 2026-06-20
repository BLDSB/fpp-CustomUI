import re

from flask import Blueprint, jsonify, render_template, request

from app import db
from app.auth_utils import login_required
from app.models import AppSetting, Zone, get_all_zones

settings_bp = Blueprint("settings", __name__)

_ALLOWED_KEYS = {"bg_image_url", "logo_url", "site_name", "accent_color", "nav_color", "nav_link_color", "text_color"}
_URL_RE    = re.compile(r"^https?://", re.IGNORECASE)
_COLOR_RE  = re.compile(r"^#[0-9a-fA-F]{6}$")


def _validate_url(val):
    return bool(_URL_RE.match(val)) or val.startswith("/")


@settings_bp.get("/settings")
@login_required
def settings_page():
    settings = {s.key: s.value for s in AppSetting.query.all()}
    zones = [z.to_dict() for z in get_all_zones() if z.slot != 0]
    return render_template("settings.html", settings=settings, zones=zones)


@settings_bp.post("/api/settings")
@login_required
def save_settings():
    data = request.get_json(silent=True) or {}
    for key, raw_value in data.items():
        if key not in _ALLOWED_KEYS:
            continue

        value = str(raw_value).strip() if raw_value else None

        if key in ("bg_image_url", "logo_url"):
            # Treat "none" as an explicit clear
            if value and value.lower() == "none":
                value = None
            if value:
                if not _validate_url(value):
                    return jsonify({"error": f"Invalid URL for '{key}' — must start with http(s)://"}), 400
                if len(value) > 500:
                    return jsonify({"error": f"URL for '{key}' is too long"}), 400

        if key == "site_name" and value:
            value = value[:64]

        if key in ("accent_color", "nav_color", "nav_link_color", "text_color") and value:
            if not _COLOR_RE.match(value):
                return jsonify({"error": f"Invalid color for '{key}' — must be a 6-digit hex color like #e94560"}), 400

        setting = db.session.get(AppSetting, key)
        if setting is None:
            db.session.add(AppSetting(key=key, value=value))
        else:
            setting.value = value

    db.session.commit()
    return jsonify({"ok": True})


@settings_bp.get("/api/zones")
@login_required
def get_zones():
    return jsonify([z.to_dict() for z in get_all_zones() if z.slot != 0])


@settings_bp.post("/api/zones")
@login_required
def save_zones():
    """Accept a list of {slot, display_name, hidden} objects."""
    data = request.get_json(silent=True)
    if not isinstance(data, list):
        return jsonify({"error": "Expected a list"}), 400

    existing = {z.slot: z for z in Zone.query.all()}

    for item in data:
        slot = item.get("slot")
        if not isinstance(slot, int) or slot < 0 or slot > 15:
            continue
        name = str(item.get("display_name") or "").strip()
        if not name or len(name) > 64:
            continue
        hidden = bool(item.get("hidden", False))

        if slot in existing:
            existing[slot].display_name = name
            existing[slot].hidden = hidden
        else:
            default_name = "All" if slot == 0 else f"Zone {slot}"
            db.session.add(Zone(slot=slot, display_name=name or default_name, hidden=hidden))

    db.session.commit()
    return jsonify({"ok": True})
