import calendar
import re
from datetime import date, datetime

import requests
from flask import Blueprint, jsonify, render_template

from app.auth_utils import login_required
from app.fpp_api import fpp_error_text, fpp_url
from app.models import Holiday
from app.validation import json_object

scheduler_bp = Blueprint("scheduler", __name__)

_TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")
SOLAR_TIMES = {"Dawn", "SunRise", "SunSet", "Dusk"}

# FPP always wants a date range on an entry; these bounds mean "no restriction".
DEFAULT_START_DATE = "2000-01-01"
DEFAULT_END_DATE = "2099-12-31"


def _parse_date(value):
    """Return a YYYY-MM-DD string, DEFAULT-able "" for blank, or None if invalid."""
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _load_schedule():
    """Fetch schedule from FPP. Returns a list of entry dicts."""
    resp = requests.get(fpp_url("/schedule"), timeout=5)
    resp.raise_for_status()
    data = resp.json()
    # FPP v9 returns {"schedule": [...]}; older versions return the list directly
    if isinstance(data, list):
        return data
    return data.get("schedule", [])


def _save_schedule(entries):
    """POST the full schedule back to FPP and reload it."""
    resp = requests.post(fpp_url("/schedule"), json=entries, timeout=5)
    resp.raise_for_status()
    try:
        requests.post(fpp_url("/schedule/reload"), timeout=3)
    except Exception:
        pass
    return entries


# ---------------------------------------------------------------------------
# Holidays: named yearly month/day ranges that entries can link to
# ---------------------------------------------------------------------------

def _clamped(year, month, day):
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def resolve_holiday_dates(holiday, today=None):
    """Return (startDate, endDate) strings for the current/next occurrence.

    A range whose end month/day is before its start spans New Year. We pick
    the occurrence that has not yet finished, so a finished season rolls
    forward to next year.
    """
    today = today or date.today()
    spans = (holiday.end_month, holiday.end_day) < (holiday.start_month, holiday.start_day)
    for start_year in (today.year - 1, today.year, today.year + 1):
        start = _clamped(start_year, holiday.start_month, holiday.start_day)
        end = _clamped(start_year + (1 if spans else 0), holiday.end_month, holiday.end_day)
        if end >= today:
            return start.isoformat(), end.isoformat()
    raise RuntimeError("unreachable")  # pragma: no cover


def sync_holiday_entries(entries=None, rename=None, delete_name=None):
    """Re-apply holiday dates to linked entries and save if anything changed.

    rename=(old, new) rewrites the link; delete_name unlinks (dates kept).
    Returns (entries, changed_count).
    """
    if entries is None:
        entries = _load_schedule()
    holidays = {h.name: h for h in Holiday.query.all()}
    changed = 0
    for entry in entries:
        name = entry.get("holiday")
        if not name:
            continue
        if rename and name == rename[0]:
            name = entry["holiday"] = rename[1]
            changed += 1
        if delete_name and name == delete_name:
            del entry["holiday"]
            changed += 1
            continue
        holiday = holidays.get(name)
        if not holiday:
            continue
        start, end = resolve_holiday_dates(holiday)
        if entry.get("startDate") != start or entry.get("endDate") != end:
            entry["startDate"], entry["endDate"] = start, end
            changed += 1
    if changed:
        _save_schedule(entries)
    return entries, changed


def _int_field(data, key, default, is_valid, message):
    """``(int, None)`` for ``data[key]``, or ``(None, message)`` if it is not a valid int."""
    try:
        value = int(data.get(key, default))
        if not is_valid(value):
            raise ValueError
    except (TypeError, ValueError):
        return None, message
    return value, None


def _time_field(data, key):
    """``(time string, None)`` if ``data[key]`` is HH:MM:SS or a solar label."""
    value = str(data.get(key, "")).strip()
    if value not in SOLAR_TIMES and not _TIME_RE.match(value):
        return None, f"{key} must be HH:MM:SS or a solar label"
    return value, None


def _resolve_entry_dates(data, holiday_name):
    """``(start_date, end_date, error)`` — from the named holiday, else the payload."""
    if holiday_name:
        holiday = Holiday.query.filter_by(name=holiday_name).first()
        if not holiday:
            return None, None, f"Unknown holiday: {holiday_name}"
        start_date, end_date = resolve_holiday_dates(holiday)
        return start_date, end_date, None
    start_date = _parse_date(data.get("startDate"))
    end_date = _parse_date(data.get("endDate"))
    if start_date is None or end_date is None:
        return None, None, "startDate and endDate must be YYYY-MM-DD or empty"
    start_date = start_date or DEFAULT_START_DATE
    end_date = end_date or DEFAULT_END_DATE
    if start_date > end_date:
        return None, None, "startDate must not be after endDate"
    return start_date, end_date, None


def _valid_day(value):
    return (0 <= value <= 15) or (256 <= value <= 32512)


def _valid_offset(minutes):
    # FPP stores solar offsets in MINUTES (see GetTimeFromSun in ScheduleEntry.cpp);
    # anything past a day pushes the computed time out of range and FPP silently
    # falls back to 8AM/8PM.
    return -1439 <= minutes <= 1439


def _validate(data):
    """Validate a schedule entry payload. Returns (entry_dict, error_str)."""
    playlist = str(data.get("playlist", "")).strip()
    command  = str(data.get("command",  "")).strip()
    args     = data.get("args", [])

    if not playlist and not command:
        return None, "playlist or command is required"

    day, err = _int_field(data, "day", 0, _valid_day,
                          "day must be a valid day index or bitmask")
    if err:
        return None, err

    start_time, err = _time_field(data, "startTime")
    if err:
        return None, err
    end_time, err = _time_field(data, "endTime")
    if err:
        return None, err

    start_offset, err = _int_field(
        data, "startTimeOffset", 0, _valid_offset,
        "startTimeOffset must be a number of minutes between -1439 and 1439")
    if err:
        return None, err
    end_offset, err = _int_field(
        data, "endTimeOffset", 0, _valid_offset,
        "endTimeOffset must be a number of minutes between -1439 and 1439")
    if err:
        return None, err

    repeat, err = _int_field(data, "repeat", 0, lambda v: v in (0, 1), "repeat must be 0 or 1")
    if err:
        return None, err
    enabled, err = _int_field(data, "enabled", 1, lambda v: v in (0, 1), "enabled must be 0 or 1")
    if err:
        return None, err
    stop_type, err = _int_field(
        data, "stopType", 0, lambda v: v in (0, 1, 2),
        "stopType must be 0 (Graceful), 1 (Hard Stop), or 2 (Immediate)")
    if err:
        return None, err

    holiday_name = str(data.get("holiday", "")).strip()
    start_date, end_date, err = _resolve_entry_dates(data, holiday_name)
    if err:
        return None, err

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
        "startDate":       start_date,
        "endDate":         end_date,
    }
    if holiday_name:
        entry["holiday"] = holiday_name
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
        entries, _ = sync_holiday_entries()
        return jsonify({"entries": entries})
    except Exception as exc:
        return jsonify({"error": fpp_error_text(exc)}), 502


@scheduler_bp.post("/api/schedule/entry")
@login_required
def add_entry():
    fields, error = _validate(json_object())
    if error:
        return jsonify({"error": error}), 400
    try:
        entries = _load_schedule()
        entries.append(fields)
        _save_schedule(entries)
        return jsonify({"ok": True, "entries": entries}), 201
    except Exception as exc:
        return jsonify({"error": fpp_error_text(exc)}), 502


@scheduler_bp.put("/api/schedule/entry/<int:idx>")
@login_required
def update_entry(idx):
    fields, error = _validate(json_object())
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
        return jsonify({"error": fpp_error_text(exc)}), 502


@scheduler_bp.post("/api/schedule/entry/<int:idx>/move")
@login_required
def move_entry(idx):
    """Move an entry up/down the list. FPP gives earlier entries higher priority."""
    direction = (json_object()).get("direction")
    if direction not in ("up", "down", "top", "bottom"):
        return jsonify({"error": "direction must be up, down, top or bottom"}), 400
    try:
        entries = _load_schedule()
        if idx < 0 or idx >= len(entries):
            return jsonify({"error": "Entry not found"}), 404
        target = {"up": idx - 1, "down": idx + 1, "top": 0, "bottom": len(entries) - 1}[direction]
        target = max(0, min(target, len(entries) - 1))
        if target != idx:
            entries.insert(target, entries.pop(idx))
            _save_schedule(entries)
        return jsonify({"ok": True, "entries": entries})
    except Exception as exc:
        return jsonify({"error": fpp_error_text(exc)}), 502


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
        return jsonify({"error": fpp_error_text(exc)}), 502


# ---------------------------------------------------------------------------
# Upcoming-events preview (the Controls page "Schedule Preview" popup)
# ---------------------------------------------------------------------------

def _clock(dt):
    return dt.strftime("%I:%M %p").lstrip("0")


@scheduler_bp.get("/api/schedule/preview")
@login_required
def schedule_preview():
    """What fppd will actually run over its look-ahead window (28 days by default).

    fppd has already resolved solar times, day rules, date ranges and holidays
    into `items`, so we only reshape them; see the note on /api/schedule in the
    project docs for why we never recompute this from the raw entries.
    """
    try:
        resp = requests.get(fpp_url("/fppd/schedule"), timeout=5)
        resp.raise_for_status()
        sched = resp.json().get("schedule", {})
    except Exception as exc:
        return jsonify({"error": fpp_error_text(exc)}), 502

    entries = {e.get("id"): e for e in sched.get("entries", [])}
    running = []  # (end epoch, priority) for playlists that have started and not ended
    events = []
    for item in sched.get("items", []):
        start = int(item.get("startTime", 0))
        start_dt = datetime.fromtimestamp(start)
        entry = entries.get(item.get("id"), {})
        args = item.get("args") or []
        event = {
            "date":  start_dt.strftime("%Y-%m-%d"),
            "start": _clock(start_dt),
            "name":  str(args[0]) if args else "",
        }
        if item.get("command") == "Start Playlist":
            end = int(item.get("endTime", 0))
            priority = item.get("priority", 0)
            running = [r for r in running if r[0] > start]
            skipped = bool(running) and priority >= running[-1][1]
            if not skipped:
                running.append((end, priority))
            event.update({
                "kind":    "playlist",
                "end":     _clock(datetime.fromtimestamp(end)),
                "endDate": datetime.fromtimestamp(end).strftime("%Y-%m-%d"),
                "repeat":  entry.get("repeat") == 1,
                "stop":    entry.get("stopTypeStr", ""),
                "skipped": skipped,
            })
        else:
            event.update({
                "kind": "command",
                "name": " | ".join([str(item.get("command", ""))] + [str(a) for a in args]),
            })
        events.append(event)

    return jsonify({
        "enabled":  sched.get("enabled", 1) != 0,
        "days":     sched.get("scheduleDistance", 28),
        "extends":  bool(sched.get("schedulesExtendBeyondDistance")),
        "events":   events,
    })
