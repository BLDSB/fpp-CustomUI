"""Pause the schedule, run a playlist for a while, and check the show is ready."""
import time

from flask import Blueprint, current_app, jsonify

from app import overrides
from app.alert_monitor import _fetch_status
from app.auth_utils import login_required
from app.validation import json_object

override_bp = Blueprint("overrides", __name__)


def _hours(data, default=None):
    try:
        hours = float(data.get("hours", default))
    except (TypeError, ValueError):
        return None
    return hours if hours > 0 else None


@override_bp.get("/api/override")
@login_required
def get_override():
    return jsonify(overrides.state())


@override_bp.post("/api/override/pause")
@login_required
def pause():
    data = json_object()
    stop_now = bool(data.get("stop_now", False))
    if data.get("mode") == "tonight":
        until = overrides.tonight_until(_fetch_status(current_app._get_current_object()))
    else:
        hours = _hours(data)
        if hours is None:
            return jsonify({"error": "Choose how many hours to pause for."}), 400
        until = int(time.time() + hours * 3600)
    try:
        overrides.pause(until, stop_now=stop_now)
    except overrides.OverrideError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, **overrides.state()})


@override_bp.post("/api/override/resume")
@login_required
def resume():
    try:
        overrides.resume()
    except overrides.OverrideError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"ok": True, **overrides.state()})


@override_bp.post("/api/override/run")
@login_required
def run():
    data = json_object()
    hours = _hours(data)
    playlist = str(data.get("playlist") or "").strip()
    if hours is None or not playlist:
        return jsonify({"error": "Choose a playlist and how many hours."}), 400
    try:
        overrides.run_for(playlist, hours)
    except overrides.OverrideError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, **overrides.state()})


@override_bp.post("/api/override/run/cancel")
@login_required
def cancel_run():
    overrides.cancel_run(stop=True)
    return jsonify({"ok": True, **overrides.state()})


@override_bp.get("/api/showcheck")
@login_required
def showcheck():
    from app import showcheck as checks
    return jsonify({"checks": checks.run_checks(current_app._get_current_object())})
