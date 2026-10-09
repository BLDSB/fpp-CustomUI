from flask import Blueprint, jsonify

from app import db
from app.auth_utils import login_required
from app.fpp_api import fpp_error_text
from app.models import Holiday
from app.routes.scheduler import sync_holiday_entries
from app.validation import json_object, str_field

holidays_bp = Blueprint("holidays", __name__)

_DUP = "duplicate"
_DAYS_IN_MONTH = {2: 29, 4: 30, 6: 30, 9: 30, 11: 30}  # Feb 29 allowed; clamped when resolved


def _parse(data, current_id=None):
    """Return (fields, error)."""
    name = str_field(data, "name")
    if not name or len(name) > 64:
        return None, "Name required (max 64 chars)"
    if "/" in name or "\\" in name or ".." in name:
        return None, "Name cannot contain slashes or .."
    try:
        sm, sd = int(data["start_month"]), int(data["start_day"])
        em, ed = int(data["end_month"]), int(data["end_day"])
    except (KeyError, TypeError, ValueError):
        return None, "Start and end month/day are required"
    for m, d in ((sm, sd), (em, ed)):
        if not 1 <= m <= 12 or not 1 <= d <= _DAYS_IN_MONTH.get(m, 31):
            return None, "Invalid month/day"
    dup = Holiday.query.filter_by(name=name).first()
    if dup and dup.id != current_id:
        return None, _DUP
    return dict(name=name, start_month=sm, start_day=sd, end_month=em, end_day=ed), None


def _err(error):
    if error == _DUP:
        return jsonify({"error": "A holiday with that name already exists"}), 409
    return jsonify({"error": error}), 400


@holidays_bp.get("/api/holidays")
@login_required
def list_holidays():
    rows = Holiday.query.order_by(Holiday.start_month, Holiday.start_day).all()
    return jsonify({"holidays": [h.to_dict() for h in rows]})


@holidays_bp.post("/api/holidays")
@login_required
def create_holiday():
    fields, error = _parse(json_object())
    if error:
        return _err(error)
    h = Holiday(**fields)
    db.session.add(h)
    db.session.commit()
    return jsonify(h.to_dict()), 201


@holidays_bp.put("/api/holidays/<int:hid>")
@login_required
def update_holiday(hid):
    h = db.session.get(Holiday, hid)
    if not h:
        return jsonify({"error": "Holiday not found"}), 404
    fields, error = _parse(json_object(), current_id=hid)
    if error:
        return _err(error)
    old_name = h.name
    for k, v in fields.items():
        setattr(h, k, v)
    db.session.commit()
    try:
        _, changed = sync_holiday_entries(
            rename=(old_name, h.name) if old_name != h.name else None)
    except Exception as exc:
        return jsonify({"error": f"Saved, but updating the schedule failed: {fpp_error_text(exc)}"}), 502
    return jsonify({**h.to_dict(), "updated_entries": changed})


@holidays_bp.delete("/api/holidays/<int:hid>")
@login_required
def delete_holiday(hid):
    h = db.session.get(Holiday, hid)
    if not h:
        return jsonify({"error": "Holiday not found"}), 404
    name = h.name
    db.session.delete(h)
    db.session.commit()
    try:
        sync_holiday_entries(delete_name=name)
    except Exception as exc:
        return jsonify({"error": f"Deleted, but updating the schedule failed: {fpp_error_text(exc)}"}), 502
    return jsonify({"ok": True})
