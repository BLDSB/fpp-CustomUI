import datetime
import glob
import json
import os
import re
import subprocess

from flask import Blueprint, Response, current_app, jsonify, render_template, request, url_for

from app import db
from app.auth_utils import login_required
from app.models import (
    OVERLAY_MODELS, AppSetting, ColorButton, EffectPreset, SavedColor, Scene,
    SceneZone, Zone, get_all_zones,
)
from app import ui_path as ui_path_mod

settings_bp = Blueprint("settings", __name__)

_ALLOWED_KEYS = {
    "bg_image_url", "logo_url", "site_name",
    "accent_color", "nav_color", "nav_link_color", "text_color",
    "genius_pro_count",
    *{f"genius_pro_url_{i}" for i in range(1, 9)},
    "alert_enabled", "alert_smtp_host", "alert_smtp_port",
    "alert_smtp_user", "alert_smtp_pass",
    "alert_email_from", "alert_email_to", "alert_delay_minutes",
}
_URL_RE    = re.compile(r"^https?://", re.IGNORECASE)
_COLOR_RE  = re.compile(r"^#[0-9a-fA-F]{6}$")


def _validate_url(val):
    return bool(_URL_RE.match(val)) or val.startswith("/")


@settings_bp.get("/settings")
@login_required
def settings_page():
    settings = {s.key: s.value for s in AppSetting.query.all()}
    # Show the working URL for uploads, not one anchored to an old path.
    for key in ("logo_url", "bg_image_url"):
        if settings.get(key):
            settings[key] = ui_path_mod.reanchor_upload_url(settings[key])
    zones = [z.to_dict() for z in get_all_zones() if z.slot != 0]
    return render_template(
        "settings.html", settings=settings, zones=zones,
        ui_path=ui_path_mod.current_path(),
        reserved_paths=sorted(ui_path_mod.RESERVED_PATHS),
    )


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

        # Numeric settings feed the alert monitor thread — reject garbage here
        # so a typo can't silently break alerting every poll cycle.
        if key in ("alert_smtp_port", "alert_delay_minutes", "genius_pro_count") and value:
            try:
                n = int(value)
            except (TypeError, ValueError):
                return jsonify({"error": f"'{key}' must be a number"}), 400
            if key == "alert_smtp_port" and not 1 <= n <= 65535:
                return jsonify({"error": "SMTP port must be 1–65535"}), 400
            if key == "alert_delay_minutes" and not 1 <= n <= 1440:
                return jsonify({"error": "Alert delay must be 1–1440 minutes"}), 400
            if key == "genius_pro_count" and not 0 <= n <= 8:
                return jsonify({"error": "Controller count must be 0–8"}), 400

        if key.startswith("genius_pro_url_") and value and not _URL_RE.match(value):
            return jsonify({"error": f"'{key}' must start with http:// or https://"}), 400

        setting = db.session.get(AppSetting, key)
        if setting is None:
            db.session.add(AppSetting(key=key, value=value))
        else:
            setting.value = value

    db.session.commit()
    return jsonify({"ok": True})


_ALLOWED_IMAGE_TYPES = {"logo", "bg"}
_ALLOWED_IMAGE_EXTS  = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}


@settings_bp.post("/api/upload/image")
@login_required
def upload_image():
    image_type = request.args.get("type", "")
    if image_type not in _ALLOWED_IMAGE_TYPES:
        return jsonify({"error": "type must be 'logo' or 'bg'"}), 400

    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file provided"}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in _ALLOWED_IMAGE_EXTS:
        return jsonify({"error": f"Unsupported file type. Use: {', '.join(sorted(_ALLOWED_IMAGE_EXTS))}"}), 400

    upload_dir = os.path.join(current_app.static_folder, "uploads")
    filename = f"{image_type}{ext}"
    try:
        os.makedirs(upload_dir, exist_ok=True)
        # Remove any previous upload for this slot (different extension)
        for old in glob.glob(os.path.join(upload_dir, f"{image_type}.*")):
            os.remove(old)
        file.save(os.path.join(upload_dir, filename))
    except OSError as exc:
        current_app.logger.error("Image upload failed: %s", exc)
        return jsonify({"error": "Could not save image — disk full or uploads folder not writable?"}), 500

    return jsonify({"url": url_for("static", filename=f"uploads/{filename}")})


@settings_bp.get("/api/upload/image/<image_type>")
@login_required
def delete_image(image_type):
    """DELETE isn't used — kept as a no-op placeholder; actual delete is via save_settings."""
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


# ── FPP integration ───────────────────────────────────────────────────────────

@settings_bp.post("/api/fpp/create-overlay-models")
@login_required
def create_overlay_models():
    config_path = "/home/fpp/media/config/model-overlays.json"
    zone_names = {f"Zone {i}" for i in range(1, 16)}

    try:
        if os.path.exists(config_path):
            with open(config_path) as f:
                existing = json.load(f)
        else:
            existing = {"models": [], "autoCreate": True}
    except Exception as exc:
        current_app.logger.warning(
            "model-overlays.json unreadable (%s) — rebuilding zone models from scratch", exc
        )
        existing = {"models": [], "autoCreate": True}

    # Keep any non-Zone-1-15 models; replace Zone entries with fresh stubs
    kept = [m for m in existing.get("models", []) if m.get("Name") not in zone_names]
    new_zones = [
        {
            "Name": f"Zone {i}",
            "Type": "Channel",
            "StartChannel": 1,
            "ChannelCount": 3,
            "ChannelCountPerNode": 3,
            "StringCount": 1,
            "StrandsPerString": 1,
            "Orientation": "horizontal",
            "StartCorner": "TL",
            "xLights": False,
        }
        for i in range(1, 16)
    ]
    existing["models"] = kept + new_zones

    # Atomic write so a crash mid-write can't corrupt FPP's own model config.
    tmp_path = config_path + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(existing, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, config_path)
    except Exception as exc:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        current_app.logger.error("Could not write model-overlays.json: %s", exc)
        return jsonify({"error": f"Could not write config: {exc}"}), 500

    # fppd must restart to pick up the new model-overlays.json
    try:
        subprocess.run(["sudo", "systemctl", "restart", "fppd"], timeout=15, check=True)
    except Exception as exc:
        current_app.logger.warning("Could not restart fppd: %s", exc)
        return jsonify({"ok": True, "warning": "Models written, but fppd did not restart — restart FPP manually."})

    return jsonify({"ok": True})


@settings_bp.post("/api/genius/reboot")
@login_required
def genius_reboot():
    import requests as req
    idx = request.args.get("controller", "1")
    try:
        idx = max(1, min(8, int(idx)))
    except ValueError:
        idx = 1

    setting = db.session.get(AppSetting, f"genius_pro_url_{idx}")
    # Fall back to legacy key for slot 1
    if idx == 1 and (not setting or not setting.value):
        setting = db.session.get(AppSetting, "genius_pro_url")
    base_url = (setting.value or "").rstrip("/") if setting else ""
    if not base_url:
        return jsonify({"error": f"Controller {idx} URL is not configured in Settings"}), 400
    if not _URL_RE.match(base_url):
        return jsonify({"error": f"Controller {idx} URL must start with http:// or https://"}), 400
    try:
        resp = req.get(f"{base_url}/api/reboot", timeout=8)
        data = resp.json()
        if not data.get("success"):
            return jsonify({"error": "Reboot command not acknowledged"}), 502
    except Exception as exc:
        return jsonify({"error": f"Could not reach controller {idx}: {exc}"}), 502
    return jsonify({"ok": True})


# ── Backup / Restore ──────────────────────────────────────────────────────────

@settings_bp.get("/api/backup")
@login_required
def download_backup():
    data = {
        "version": 1,
        "exported_at": datetime.datetime.utcnow().isoformat() + "Z",
        "settings": {s.key: s.value for s in AppSetting.query.all()},
        "zones": [
            {"slot": z.slot, "display_name": z.display_name, "hidden": z.hidden}
            for z in Zone.query.order_by(Zone.slot).all()
        ],
        "saved_colors": [
            {"id": c.id, "name": c.name, "hex_value": c.hex_value}
            for c in SavedColor.query.all()
        ],
        "color_buttons": [
            {"id": b.id, "label": b.label, "saved_color_id": b.saved_color_id}
            for b in ColorButton.query.all()
        ],
        "scenes": [s.to_dict() for s in Scene.query.all()],
        "effect_presets": [p.to_dict() for p in EffectPreset.query.order_by(EffectPreset.id).all()],
    }
    ts = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    return Response(
        json.dumps(data, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename=fpp-ui-backup-{ts}.json"},
    )


@settings_bp.post("/api/restore")
@login_required
def restore_backup():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file provided"}), 400
    try:
        data = json.loads(file.read())
    except Exception:
        return jsonify({"error": "Invalid JSON file"}), 400

    if data.get("version") not in (1, 2):
        return jsonify({"error": "Unsupported backup version"}), 400

    # Settings — merge (update existing keys, add new ones)
    for key, value in (data.get("settings") or {}).items():
        if key not in _ALLOWED_KEYS:
            continue
        if value is not None:
            value = str(value)
        s = db.session.get(AppSetting, key)
        if s:
            s.value = value
        else:
            db.session.add(AppSetting(key=key, value=value))

    # Zones — update matching slots
    existing_zones = {z.slot: z for z in Zone.query.all()}
    for item in (data.get("zones") or []):
        slot = item.get("slot")
        if not isinstance(slot, int) or slot < 0 or slot > 15:
            continue
        name = str(item.get("display_name") or "").strip()
        hidden = bool(item.get("hidden", False))
        if slot in existing_zones:
            if name:
                existing_zones[slot].display_name = name
            existing_zones[slot].hidden = hidden
        else:
            db.session.add(Zone(slot=slot, display_name=name or ("All" if slot == 0 else f"Zone {slot}"), hidden=hidden))

    # Saved colors + buttons — replace entirely
    ColorButton.query.delete()
    SavedColor.query.delete()
    db.session.flush()

    color_id_map = {}
    for c in (data.get("saved_colors") or []):
        if not isinstance(c, dict):
            continue
        hex_value = str(c.get("hex_value", ""))
        if not _COLOR_RE.match(hex_value):
            current_app.logger.warning("Restore: skipping color with invalid hex %r", hex_value)
            continue
        nc = SavedColor(name=str(c.get("name", ""))[:64], hex_value=hex_value)
        db.session.add(nc)
        db.session.flush()
        color_id_map[c.get("id")] = nc.id

    for b in (data.get("color_buttons") or []):
        new_sid = color_id_map.get(b.get("saved_color_id"))
        if new_sid:
            db.session.add(ColorButton(label=str(b.get("label", ""))[:64], saved_color_id=new_sid))

    # Scenes — replace entirely and regenerate FPP playlists
    Scene.query.delete()
    db.session.flush()

    from app.routes.scenes import _write_scene_files
    seen_scene_names = set()
    for s in (data.get("scenes") or []):
        if not isinstance(s, dict):
            continue
        name = str(s.get("name", "")).strip()[:64]
        # Scene.name is unique — a duplicate in a hand-edited backup would
        # otherwise abort the whole restore at commit time.
        if not name or name in seen_scene_names:
            continue
        seen_scene_names.add(name)
        new_scene = Scene(name=name)
        db.session.add(new_scene)
        db.session.flush()
        for z in (s.get("zones") or []):
            if not isinstance(z, dict):
                continue
            fpp_model = str(z.get("fpp_model", ""))
            hex_color = str(z.get("hex_color", ""))
            # These values are replayed against the FPP API and parsed as hex
            # later — only known models and well-formed colors may be stored.
            if fpp_model not in OVERLAY_MODELS or not _COLOR_RE.match(hex_color):
                current_app.logger.warning(
                    "Restore: skipping invalid zone %r/%r in scene '%s'",
                    fpp_model, hex_color, name,
                )
                continue
            db.session.add(SceneZone(
                scene_id=new_scene.id,
                fpp_model=fpp_model,
                hex_color=hex_color,
            ))
        db.session.flush()
        try:
            _write_scene_files(new_scene)
        except Exception as exc:
            current_app.logger.warning("Could not write scene playlist for '%s': %s", name, exc)

    # Effect presets — replace entirely
    EffectPreset.query.delete()
    db.session.flush()

    def _as_list(v):
        return v if isinstance(v, list) else []

    for p in (data.get("effect_presets") or []):
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()[:64]
        effect_name = str(p.get("effect_name") or "").strip()[:128]
        if not name or not effect_name:
            continue
        db.session.add(EffectPreset(
            name=name,
            effect_name=effect_name,
            models_json=json.dumps(_as_list(p.get("models"))),
            args_json=json.dumps(_as_list(p.get("args"))),
            multisync=bool(p.get("multisync", False)),
            systems_json=json.dumps(_as_list(p.get("systems"))),
        ))

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("Backup restore failed at commit")
        return jsonify({"error": f"Restore failed — no changes applied: {exc}"}), 500
    return jsonify({"ok": True})


# ── Public URL path ───────────────────────────────────────────────────────────

@settings_bp.post("/api/ui-path")
@login_required
def set_ui_path():
    """Move this install to a different URL path (e.g. /cityname).

    Apache is reloaded gracefully, so this response still reaches the browser
    over the old path; the client then navigates to the returned URL.
    """
    data = request.get_json(silent=True) or {}
    name = str(data.get("ui_path") or "").strip()

    if name == ui_path_mod.current_path():
        return jsonify({"ok": True, "url": f"/{name}/", "unchanged": True})

    error = ui_path_mod.apply(name)
    if error:
        return jsonify({"error": error}), 400
    return jsonify({"ok": True, "url": f"/{name}/"})


# ── Alert email ──────────────────────────────────────────────────────────────

@settings_bp.post("/api/alerts/test")
@login_required
def test_alert_email():
    from app.alert_monitor import send_test_email
    ok, err = send_test_email(current_app._get_current_object())
    if ok:
        return jsonify({"ok": True})
    return jsonify({"error": err}), 502
