import calendar
import re
from datetime import date, datetime

import requests
from flask import Blueprint, current_app, jsonify, render_template, request

from app.auth_utils import login_required
from app.models import Holiday

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
        return None, "day must be a valid day index or bitmask"

    start_time = str(data.get("startTime", "")).strip()
    if start_time not in SOLAR_TIMES and not _TIME_RE.match(start_time):
        return None, "startTime must be HH:MM:SS or a solar label"

    end_time = str(data.get("endTime", "")).strip()
    if end_time not in SOLAR_TIMES and not _TIME_RE.match(end_time):
        return None, "endTime must be HH:MM:SS or a solar label"

    # FPP stores solar offsets in MINUTES (see GetTimeFromSun in ScheduleEntry.cpp);
    # anything past a day pushes the computed time out of range and FPP silently
    # falls back to 8AM/8PM.
    try:
        start_offset = int(data.get("startTimeOffset", 0))
        if not -1439 <= start_offset <= 1439:
            raise ValueError
    except (TypeError, ValueError):
        return None, "startTimeOffset must be a number of minutes between -1439 and 1439"

    try:
        end_offset = int(data.get("endTimeOffset", 0))
        if not -1439 <= end_offset <= 1439:
            raise ValueError
    except (TypeError, ValueError):
        return None, "endTimeOffset must be a number of minutes between -1439 and 1439"

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

    holiday_name = str(data.get("holiday", "")).strip()
    if holiday_name:
        holiday = Holiday.query.filter_by(name=holiday_name).first()
        if not holiday:
            return None, f"Unknown holiday: {holiday_name}"
        start_date, end_date = resolve_holiday_dates(holiday)
    else:
        start_date = _parse_date(data.get("startDate"))
        end_date = _parse_date(data.get("endDate"))
        if start_date is None or end_date is None:
            return None, "startDate and endDate must be YYYY-MM-DD or empty"
        start_date = start_date or DEFAULT_START_DATE
        end_date = end_date or DEFAULT_END_DATE
        if start_date > end_date:
            return None, "startDate must not be after endDate"

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


@scheduler_bp.post("/api/schedule/entry/<int:idx>/move")
@login_required
def move_entry(idx):
    """Move an entry up/down the list. FPP gives earlier entries higher priority."""
    direction = (request.get_json(silent=True) or {}).get("direction")
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
        resp = requests.get(f"{_fpp_base()}/fppd/schedule", timeout=5)
        resp.raise_for_status()
        sched = resp.json().get("schedule", {})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

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
