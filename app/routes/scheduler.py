import re

import requests
from flask import Blueprint, current_app, jsonify, render_template, request

from app.auth_utils import login_required

scheduler_bp = Blueprint("scheduler", __name__)

_TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")
SOLAR_TIMES = {"Dawn", "SunRise", "SunSet", "Dusk"}


def _fpp_base():
    return current_app.config.get("FPP_BASE_URL", "http://localhost/api")


def _load_schedule():
    """Fetch schedule from FPP. Returns a list of entry dicts."""
    resp = requests.get(f"{_fpp_base()}/schedule", timeout=5)
    resp.raise_for_status()
    data = resp.json()
    # FPP v9 returns {"schedule": [...]}; older versions return the list directly
    if isinstance(data, list):
        return data
    return data.get("schedule", [])


def _save_schedule(entries):
    """POST the full schedule back to FPP and reload it."""
    resp = requests.post(f"{_fpp_base()}/schedule", json=entries, timeout=5)
    resp.raise_for_status()
    try:
        requests.post(f"{_fpp_base()}/schedule/reload", timeout=3)
    except Exception:
        pass
    return entries


def _validate(data):
    """Validate a schedule entry payload. Returns (entry_dict, error_str)."""
    playlist = str(data.get("playlist", "")).strip()
    command  = str(data.get("command",  "")).strip()
    args     = data.get("args", [])

    if not playlist and not command:
        return None, "playlist or command is required"

    try:
        day = int(data.get("day", 0))
        if not ((0 <= day <= 15) or (256 <= day <= 32512)):
            raise ValueError
    except (TypeError, ValueError):
        return None, "day must be a valid FPP day index or bitmask"

    start_time = str(data.get("startTime", "")).strip()
    if start_time not in SOLAR_TIMES and not _TIME_RE.match(start_time):
        return None, "startTime must be HH:MM:SS or a solar label"

    end_time = str(data.get("endTime", "")).strip()
    if end_time not in SOLAR_TIMES and not _TIME_RE.match(end_time):
        return None, "endTime must be HH:MM:SS or a solar label"

    try:
        start_offset = int(data.get("startTimeOffset", 0))
    except (TypeError, ValueError):
        return None, "startTimeOffset must be an integer"

    try:
        end_offset = int(data.get("endTimeOffset", 0))
    except (TypeError, ValueError):
        return None, "endTimeOffset must be an integer"

    try:
        repeat = int(data.get("repeat", 0))
        if repeat not in (0, 1):
            raise ValueError
    except (TypeError, ValueError):
        return None, "repeat must be 0 or 1"

    try:
        enabled = int(data.get("enabled", 1))
        if enabled not in (0, 1):
            raise ValueError
    except (TypeError, ValueError):
        return None, "enabled must be 0 or 1"

    try:
        stop_type = int(data.get("stopType", 0))
        if stop_type not in (0, 1, 2):
            raise ValueError
    except (TypeError, ValueError):
        return None, "stopType must be 0 (Graceful), 1 (Hard Stop), or 2 (Immediate)"

    entry = {
        "enabled":         enabled,
        "playlist":        playlist,
        "startTime":       start_time,
        "endTime":         end_time,
        "repeat":          repeat,
        "day":             day,
        "stopType":        stop_type,
        "startTimeOffset": start_offset,
        "endTimeOffset":   end_offset,
    }
    if command:
        entry["command"] = command
        entry["args"] = args if isinstance(args, list) else []

    return entry, None


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

@scheduler_bp.get("/schedule")
@login_required
def schedule_page():
    return render_template("schedule.html")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@scheduler_bp.get("/api/schedule/list")
@login_required
def list_schedule():
    try:
        return jsonify({"entries": _load_schedule()})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@scheduler_bp.post("/api/schedule/entry")
@login_required
def add_entry():
    fields, error = _validate(request.get_json(silent=True) or {})
    if error:
        return jsonify({"error": error}), 400
    try:
        entries = _load_schedule()
        entries.append(fields)
        _save_schedule(entries)
        return jsonify({"ok": True, "entries": entries}), 201
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@scheduler_bp.put("/api/schedule/entry/<int:idx>")
@login_required
def update_entry(idx):
    fields, error = _validate(request.get_json(silent=True) or {})
    if error:
        return jsonify({"error": error}), 400
    try:
        entries = _load_schedule()
        if idx < 0 or idx >= len(entries):
            return jsonify({"error": "Entry not found"}), 404
        entries[idx] = fields
        _save_schedule(entries)
        return jsonify({"ok": True, "entries": entries})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@scheduler_bp.delete("/api/schedule/entry/<int:idx>")
@login_required
def delete_entry(idx):
    try:
        entries = _load_schedule()
        if idx < 0 or idx >= len(entries):
            return jsonify({"error": "Entry not found"}), 404
        entries.pop(idx)
        _save_schedule(entries)
        return jsonify({"ok": True, "entries": entries})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
