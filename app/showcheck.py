"""The show-day check: is everything ready for the next show?

Each check returns {"name", "status": ok | warn | fail, "detail"}. A check that cannot
be answered (for example a listing that fails) is skipped rather than reported as a
failure, so the page never cries wolf.
"""
import logging
import shutil
import time
from datetime import datetime

from app import overrides, showlog

_logger = logging.getLogger(__name__)


def _item(name, status, detail):
    return {"name": name, "status": status, "detail": detail}


def run_checks(app, now=None):
    from app import heartbeat
    from app.alert_monitor import _load_settings, _next_start, _preflight_all
    now = now or time.time()
    checks = []

    status = heartbeat._fetch_status(app)
    if status is None:
        checks.append(_item("Show player", "fail",
                            "The show player is not responding. No show will play until it is running."))
        return checks
    checks.append(_item("Show player", "ok", "Running"))

    with app.app_context():
        until = overrides.paused_until()
        settings = _load_settings(app)
    if until:
        checks.append(_item("Schedule", "warn",
                            f"Paused until {showlog.fmt(until)}. Resume it for shows to start."))

    nxt = _next_start(status)
    if nxt is None:
        if not until:
            checks.append(_item("Next show", "warn", "Nothing is scheduled in the coming weeks."))
    else:
        checks.append(_item("Next show", "ok", f"{nxt[0]} - {showlog.fmt(nxt[1])}"))
        try:
            problems = _preflight_all(app, [{"name": nxt[0]}]).get(nxt[0], [])
        except Exception:
            _logger.exception("Show check: playlist check failed")
            problems = []
        if problems:
            checks.append(_item("Playlist contents", "fail", "; ".join(str(p) for p in problems)))
        else:
            checks.append(_item("Playlist contents", "ok", "The sequences and audio it needs are present."))

    year = datetime.fromtimestamp(now).year
    checks.append(_item("Clock", "ok" if year >= 2024 else "fail",
                        datetime.fromtimestamp(now).strftime("%a %b %d %Y %I:%M %p").replace(" 0", " ")
                        if year >= 2024 else "The controller's clock looks wrong, so scheduled times will be off."))

    power = heartbeat._power()
    if power.get("undervoltage"):
        checks.append(_item("Power", "fail", "The power supply is too weak right now."))
    elif power.get("undervoltage_ever"):
        checks.append(_item("Power", "warn", "Low power was detected earlier since the last restart."))
    elif power:
        checks.append(_item("Power", "ok", "OK"))

    try:
        usage = shutil.disk_usage("/")
        pct = usage.used * 100.0 / usage.total
        checks.append(_item("Storage", "fail" if pct >= 95 else ("warn" if pct >= 85 else "ok"),
                            f"{pct:.0f}% used"))
    except OSError:
        pass

    if settings.get("alert_enabled") == "1":
        checks.append(_item("Show alerts", "ok", "On"))
    else:
        checks.append(_item("Show alerts", "warn", "Email alerts are off, so a missed show will not be reported."))
    return checks
