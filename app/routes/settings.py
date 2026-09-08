import datetime
import glob
import json
import os
import re
import subprocess
import threading
import time

from flask import Blueprint, Response, current_app, jsonify, render_template, request, url_for

from app import db
from app.auth_utils import login_required
from app.models import (
    OVERLAY_MODELS, AppSetting, ColorButton, CustomPlaylist, CustomPlaylistItem,
    EffectPreset, SavedColor, Scene, SceneZone, Zone, ZoneLayout, get_all_zones,
)
from app import overlay_layout
from app import ui_path as ui_path_mod

settings_bp = Blueprint("settings", __name__)

_ALLOWED_KEYS = {
    "bg_image_url", "logo_url", "site_name",
    "accent_color", "nav_color", "nav_link_color", "text_color",
    "genius_pro_count",
    *{f"genius_pro_url_{i}" for i in range(1, 9)},
    "alert_enabled", "alert_smtp_host", "alert_smtp_port",
    "alert_smtp_user", "alert_smtp_pass",
    "alert_email_from", "alert_delay_minutes",
    "alert_email_to", "alert_email_to_2", "alert_email_to_3",
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
    layouts = {l.slot: l.to_dict() for l in ZoneLayout.query.all()}
    return render_template(
        "settings.html", settings=settings, zones=zones, layouts=layouts,
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

DISPLAY_MAP_PATH = "/home/fpp/media/config/virtualdisplaymap"
OVERLAY_CONFIG_PATH = "/home/fpp/media/config/model-overlays.json"
COMPOSITE_KEY = "__all__"


def _current_overlay_models():
    """Name -> entry from FPP's model-overlays.json, or {} if unreadable."""
    try:
        with open(OVERLAY_CONFIG_PATH) as f:
            data = json.load(f)
        return {
            m.get("Name"): m
            for m in data.get("models", [])
            if isinstance(m, dict) and m.get("Name")
        }
    except Exception:
        return {}


def _derive_from_map():
    """Parse the display map and pair its models with zone slots.

    Models are matched to slots in start-channel order, which is how the zones
    were laid out in the first place.  Slot 0 gets the whole-display composite.
    Returns (entries, error_response_or_None).
    """
    if not os.path.exists(DISPLAY_MAP_PATH):
        return None, (jsonify({
            "error": "No xLights display map found on this controller. "
                     "Upload one from xLights (FPP Connect → Virtual Display Map), "
                     f"or place it at {DISPLAY_MAP_PATH}."
        }), 404)
    try:
        with open(DISPLAY_MAP_PATH) as f:
            models = overlay_layout.parse_display_map(f.read())
    except Exception as exc:
        current_app.logger.warning("Could not read display map: %s", exc)
        return None, (jsonify({"error": f"Could not read the display map: {exc}"}), 500)

    if not models:
        return None, (jsonify({"error": "The display map has no models in it."}), 400)

    entries = []
    grids = [(m, overlay_layout.derive_grid(m)) for m in models]
    grids.sort(key=lambda pair: pair[1]["start_channel"])

    composite = overlay_layout.derive_composite_grid(models)
    if composite:
        entries.append((0, COMPOSITE_KEY, composite))

    for slot, (model, grid) in enumerate(grids, start=1):
        if slot > 15:
            # Only Zone 1-15 exist; anything beyond is reported, not assigned.
            entries.append((None, model["name"], grid))
            continue
        entries.append((slot, model["name"], grid))
    return entries, None


@settings_bp.get("/api/fpp/layout/preview")
@login_required
def layout_preview():
    """Derive matrix geometry from the display map without writing anything."""
    entries, err = _derive_from_map()
    if err:
        return err
    current = _current_overlay_models()

    out = []
    for slot, name, grid in entries:
        model_name = None if slot is None else ("All" if slot == 0 else f"Zone {slot}")
        live = current.get(model_name) or {}
        out.append({
            "slot": slot,
            "source_name": name,
            "fpp_model_name": model_name,
            "is_composite": name == COMPOSITE_KEY,
            "width": grid["width"],
            "height": grid["height"],
            "node_count": grid["node_count"],
            "placed": grid["placed"],
            "collisions": grid["collisions"],
            "start_channel": grid["start_channel"],
            "channel_count": grid["channel_count"],
            "current_start_channel": live.get("StartChannel"),
            "current_channel_count": live.get("ChannelCount"),
            "channel_mismatch": bool(live) and (
                live.get("StartChannel") != grid["start_channel"]
                or live.get("ChannelCount") != grid["channel_count"]
            ),
            "error": overlay_layout.validate_grid(grid, name),
            "mask": overlay_layout.grid_mask(grid["data"]),
        })
    return jsonify({"models": out, "map_path": DISPLAY_MAP_PATH})


@settings_bp.post("/api/fpp/layout/import")
@login_required
def layout_import():
    """Store the confirmed geometry as ZoneLayout rows.

    Each item is either {slot, source_name} — re-derived from the display map —
    or {slot, data, start_channel} for a layout pasted from an xLights custom
    model, the escape hatch for nodes that do not sit on a regular lattice.
    """
    body = request.get_json(silent=True) or {}
    items = body.get("models")
    if not isinstance(items, list) or not items:
        return jsonify({"error": "No models selected"}), 400

    derived = {}
    if any(not item.get("data") for item in items if isinstance(item, dict)):
        entries, err = _derive_from_map()
        if err:
            return err
        derived = {name: grid for _slot, name, grid in entries}

    staged = []
    for item in items:
        if not isinstance(item, dict):
            continue
        slot = item.get("slot")
        if not isinstance(slot, int) or slot < 0 or slot > 15:
            continue
        source_name = str(item.get("source_name") or "")[:64]

        if item.get("data"):
            try:
                grid = overlay_layout.parse_xlights_custom(
                    str(item["data"]),
                    int(item.get("start_channel") or 1),
                    int(item.get("channels_per_node") or 3),
                )
            except (ValueError, TypeError) as exc:
                return jsonify({"error": f"Zone {slot}: {exc}"}), 400
        else:
            grid = derived.get(source_name)
            if grid is None:
                return jsonify({
                    "error": f"Zone {slot}: '{source_name}' is not in the display map."
                }), 400

        err = overlay_layout.validate_grid(grid, f"Zone {slot}")
        if err:
            return jsonify({"error": err}), 400
        staged.append((slot, source_name, grid))

    if not staged:
        return jsonify({"error": "No valid models to import"}), 400

    now = datetime.datetime.utcnow().isoformat() + "Z"
    set_names = bool(body.get("set_names"))
    zones = {z.slot: z for z in get_all_zones()}

    for slot, source_name, grid in staged:
        layout = db.session.get(ZoneLayout, slot)
        if layout is None:
            layout = ZoneLayout(slot=slot)
            db.session.add(layout)
        layout.source_name = source_name
        layout.width = grid["width"]
        layout.height = grid["height"]
        layout.node_count = grid["placed"]
        layout.start_channel = grid["start_channel"]
        layout.channel_count = grid["channel_count"]
        layout.channels_per_node = grid["channels_per_node"]
        layout.data = grid["data"]
        layout.imported_at = now

        if set_names and slot > 0 and source_name and source_name != COMPOSITE_KEY:
            zone = zones.get(slot)
            if zone is not None:
                zone.display_name = source_name[:64]

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("Layout import failed at commit")
        return jsonify({"error": f"Import failed — no changes applied: {exc}"}), 500

    return jsonify({"ok": True, "imported": len(staged)})


@settings_bp.post("/api/fpp/layout/clear")
@login_required
def layout_clear():
    """Drop all stored layouts, returning zones to FPP's rectangular handling."""
    ZoneLayout.query.delete()
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        return jsonify({"error": f"Could not clear layouts: {exc}"}), 500
    return jsonify({"ok": True})


@settings_bp.post("/api/fpp/create-overlay-models")
@login_required
def create_overlay_models():
    config_path = OVERLAY_CONFIG_PATH
    managed_names = OVERLAY_MODELS

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

    prior = {
        m.get("Name"): m
        for m in existing.get("models", [])
        if isinstance(m, dict) and m.get("Name") in managed_names
    }
    layouts = {l.slot: l for l in ZoneLayout.query.all()}

    # Keep any model we don't manage untouched.
    kept = [
        m for m in existing.get("models", [])
        if not (isinstance(m, dict) and m.get("Name") in managed_names)
    ]

    rebuilt = []
    for slot in range(0, 16):
        name = "All" if slot == 0 else f"Zone {slot}"
        layout = layouts.get(slot)
        if layout is not None:
            grid = layout.to_grid()
            err = overlay_layout.validate_grid(grid, name)
            if err:
                current_app.logger.error("Skipping %s: %s", name, err)
            else:
                rebuilt.append(overlay_layout.to_fpp_model(name, grid))
                continue
        if name in prior:
            # No layout — keep the operator's channel data exactly as it is
            # rather than resetting it to a stub.
            rebuilt.append(prior[name])
        elif slot > 0:
            rebuilt.append({
                "Name": name,
                "Type": "Channel",
                "StartChannel": 1,
                "ChannelCount": 3,
                "ChannelCountPerNode": 3,
                "StringCount": 1,
                "StrandsPerString": 1,
                "Orientation": "horizontal",
                "StartCorner": "TL",
                "xLights": False,
            })

    existing["models"] = kept + rebuilt

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
        return jsonify({"ok": True, "warning": "Models written, but the player did not restart — restart the controller manually."})

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


def trigger_reboot(delay=2, reason="the settings page"):
    """Reboot the Raspberry Pi this plugin runs on, after `delay` seconds.

    systemd tears the service down the moment the command lands, so the reboot
    is fired from a background thread and the caller's response gets out first.
    Returns None once the reboot is scheduled, or an error string.

    Shared with the backup restore, which reboots at the end so FPP picks up
    the configuration it was just handed.
    """
    # Check passwordless sudo up front — otherwise the thread would fail
    # silently and the page would claim a reboot that never happened.
    try:
        probe = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=5)
    except Exception as exc:
        current_app.logger.error("Could not check sudo before reboot: %s", exc)
        return f"Could not run the reboot command: {exc}"
    if probe.returncode != 0:
        return ("This user cannot reboot without a password — "
                "reboot from the controller's own menu or over SSH.")

    def _reboot():
        # Long enough for the response to reach the browser.
        time.sleep(delay)
        try:
            subprocess.run(["sudo", "-n", "systemctl", "reboot"], timeout=20)
        except Exception:
            try:
                subprocess.run(["sudo", "-n", "shutdown", "-r", "now"], timeout=20)
            except Exception:
                pass

    current_app.logger.warning("Reboot requested from %s", reason)
    threading.Thread(target=_reboot, daemon=True).start()
    return None


@settings_bp.post("/api/system/reboot")
@login_required
def system_reboot():
    error = trigger_reboot()
    if error:
        return jsonify({"error": error}), 500
    return jsonify({"ok": True})


# ── Backup / Restore ──────────────────────────────────────────────────────────

def build_ui_payload():
    """This plugin's database as the version-4 backup payload.

    Shared with the full-archive builder in app/fpp_backup.py, which embeds
    the same document at ui/backup.json so one restore path handles both.
    """
    return {
        "version": 4,
        "exported_at": datetime.datetime.utcnow().isoformat() + "Z",
        "settings": {s.key: s.value for s in AppSetting.query.all()},
        "zones": [
            {"slot": z.slot, "display_name": z.display_name, "hidden": z.hidden}
            for z in Zone.query.order_by(Zone.slot).all()
        ],
        "zone_layouts": [
            l.to_dict(include_data=True)
            for l in ZoneLayout.query.order_by(ZoneLayout.slot).all()
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
        "custom_playlists": [
            c.to_dict() for c in CustomPlaylist.query.order_by(CustomPlaylist.id).all()
        ],
    }


@settings_bp.get("/api/backup")
@login_required
def download_backup():
    ts = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    return Response(
        json.dumps(build_ui_payload(), indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename=fpp-ui-backup-{ts}.json"},
    )


def apply_ui_backup(data):
    """Restore this plugin's database from a version-4 payload.

    Returns None on success or an error string; the whole thing is one
    transaction, so a failure at commit leaves the database untouched.
    Shared with the full-archive restore in app/routes/backup.py.
    """
    if not isinstance(data, dict) or data.get("version") not in (1, 2, 3, 4):
        return "Unsupported backup version"

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

    # Zone layouts — replace entirely when the backup carries them
    if isinstance(data.get("zone_layouts"), list):
        ZoneLayout.query.delete()
        db.session.flush()
        for item in data["zone_layouts"]:
            if not isinstance(item, dict):
                continue
            slot = item.get("slot")
            grid_data = item.get("data")
            if not isinstance(slot, int) or slot < 0 or slot > 15 or not grid_data:
                continue
            try:
                layout = ZoneLayout(
                    slot=slot,
                    source_name=str(item.get("source_name") or "")[:64] or None,
                    width=int(item["width"]),
                    height=int(item["height"]),
                    node_count=int(item["node_count"]),
                    start_channel=int(item["start_channel"]),
                    channel_count=int(item["channel_count"]),
                    channels_per_node=int(item.get("channels_per_node") or 3),
                    data=str(grid_data),
                    imported_at=str(item.get("imported_at") or "")[:32] or None,
                )
            except (KeyError, TypeError, ValueError):
                current_app.logger.warning("Restore: skipping malformed layout for slot %r", slot)
                continue
            err = overlay_layout.validate_grid(layout.to_grid(), f"Zone {slot}")
            if err:
                current_app.logger.warning("Restore: %s", err)
                continue
            db.session.add(layout)

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

    from app.routes.custom_playlists import _write_custom_playlist
    from app.routes.effects import _write_effect_playlist
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
        new_preset = EffectPreset(
            name=name,
            effect_name=effect_name,
            models_json=json.dumps(_as_list(p.get("models"))),
            args_json=json.dumps(_as_list(p.get("args"))),
        )
        db.session.add(new_preset)
        db.session.flush()
        try:
            _write_effect_playlist(new_preset)
        except Exception as exc:
            current_app.logger.warning("Could not write effect playlist for '%s': %s", name, exc)

    # Custom playlists — replace entirely. Items are rebuilt by name lookup
    # because scene and preset ids are reassigned by the wipe-and-recreate above.
    scenes_by_name = {s.name: s.id for s in Scene.query.all()}
    presets_by_name = {p.name: p.id for p in EffectPreset.query.all()}

    CustomPlaylistItem.query.delete()
    CustomPlaylist.query.delete()
    db.session.flush()

    for c in (data.get("custom_playlists") or []):
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()[:64]
        if not name or "/" in name or "\\" in name or ".." in name:
            continue
        new_cp = CustomPlaylist(
            name=name,
            repeat=bool(c.get("repeat", True)),
            random=bool(c.get("random", False)),
        )
        restored_items = []

        for pos, raw in enumerate(c.get("items") or []):
            if not isinstance(raw, dict):
                continue
            item_type = raw.get("item_type")
            if item_type not in CustomPlaylistItem.ITEM_TYPES:
                continue
            try:
                duration = max(1, min(int(raw.get("duration") or 30), 86400))
            except (TypeError, ValueError):
                duration = 30

            ref_id, ref_name = None, None
            if item_type in ("scene", "effect"):
                lookup = scenes_by_name if item_type == "scene" else presets_by_name
                ref_id = lookup.get(str(raw.get("label") or ""))
                if ref_id is None:
                    current_app.logger.warning(
                        "Restore: playlist '%s' references missing %s %r — skipping item",
                        name, item_type, raw.get("label"),
                    )
                    continue
            elif item_type == "sequence":
                ref_name = str(raw.get("ref_name") or "").strip()[:255]
                if not ref_name:
                    continue

            restored_items.append(CustomPlaylistItem(
                position=pos, item_type=item_type,
                ref_id=ref_id, ref_name=ref_name, duration=duration,
            ))

        # Assign through the relationship so _write_custom_playlist sees the
        # items without depending on a lazy reload mid-transaction.
        new_cp.items = restored_items
        db.session.add(new_cp)
        db.session.flush()
        try:
            _write_custom_playlist(new_cp)
        except Exception as exc:
            current_app.logger.warning("Could not write custom playlist '%s': %s", name, exc)

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("Backup restore failed at commit")
        return f"Restore failed — no changes applied: {exc}"
    return None


@settings_bp.post("/api/restore")
@login_required
def restore_backup():
    """Restore from a backup file.

    Accepts both the plain-JSON backups this endpoint has always taken and
    the newer full-archive zip, so an operator can drop either on the same
    button without having to know which one they have.
    """
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file provided"}), 400

    raw = file.read()
    if raw[:2] == b"PK":
        from app.routes.backup import apply_archive_bytes
        return apply_archive_bytes(raw)

    try:
        data = json.loads(raw)
    except Exception:
        return jsonify({"error": "Invalid JSON file"}), 400

    error = apply_ui_backup(data)
    if error:
        status = 400 if error == "Unsupported backup version" else 500
        return jsonify({"error": error}), status
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
    ok, detail = send_test_email(current_app._get_current_object())
    if ok:
        return jsonify({"ok": True, "sent_to": detail})
    return jsonify({"error": detail}), 502


@settings_bp.get("/api/alerts/state")
@login_required
def alert_state():
    """What the monitor is watching. A successful test email only proves SMTP
    works, so without this there is no way to tell an armed monitor from one
    that is silently watching nothing."""
    from app.alert_monitor import monitor_state
    return jsonify(monitor_state(current_app._get_current_object()))
