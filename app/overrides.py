"""Temporary overrides of the schedule: pause it (skip tonight), or run a playlist for a while.

Pausing works by switching every schedule entry off in fppd and remembering the
originals; resuming puts them back exactly as they were. The remembered copy lives in the
database, so a restart in the middle of a pause still resumes on time. While paused,
schedule edits are refused (they would be lost when the schedule is restored).
"""
import json
import logging
import time
from datetime import datetime, timedelta
from urllib.parse import quote

import requests

from app import db
from app.fpp_api import fpp_url

_logger = logging.getLogger(__name__)

PAUSE_KEY = "schedule_pause"       # {"until": epoch, "saved": [entries]}
RUN_KEY = "override_run"           # {"playlist": name, "until": epoch}
MAX_HOURS = 72


class OverrideError(Exception):
    pass


def _get(key):
    from app.models import AppSetting
    row = db.session.get(AppSetting, key)
    if row is None or not row.value:
        return None
    try:
        value = json.loads(row.value)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _set(key, value):
    from app.models import AppSetting
    row = db.session.get(AppSetting, key)
    text = None if value is None else json.dumps(value)
    if row is None:
        db.session.add(AppSetting(key=key, value=text))
    else:
        row.value = text
    db.session.commit()


def paused_until():
    """Epoch the schedule resumes, or None when it is not paused."""
    state = _get(PAUSE_KEY)
    return int(state["until"]) if state and "until" in state else None


def is_paused():
    return paused_until() is not None


def _fmt(epoch):
    return datetime.fromtimestamp(epoch).strftime("%a %b %d %I:%M %p").replace(" 0", " ")


def state():
    pause, run = _get(PAUSE_KEY), _get(RUN_KEY)
    return {
        "paused": bool(pause),
        "paused_until": int(pause["until"]) if pause else None,
        "paused_until_text": _fmt(int(pause["until"])) if pause else "",
        "run": ({"playlist": run["playlist"], "until": int(run["until"]),
                 "until_text": _fmt(int(run["until"]))} if run else None),
    }


def next_noon_after(ref_epoch):
    """The first 12:00 (controller local time) after ``ref_epoch``."""
    ref = datetime.fromtimestamp(ref_epoch)
    noon = ref.replace(hour=12, minute=0, second=0, microsecond=0)
    if noon <= ref:
        noon += timedelta(days=1)
    return int(noon.timestamp())


def tonight_until(status, now=None):
    """When a "skip tonight" pause should end: noon after the next show starts (or after now
    when one is playing), so the whole evening is covered. A day from now if nothing is known."""
    now = int(now or time.time())
    sched = (status or {}).get("scheduler") or {}
    playing = str((status or {}).get("status_name", "")).lower() == "playing"
    nxt = (sched.get("nextPlaylist") or {}).get("scheduledStartTime")
    try:
        nxt = int(nxt)
    except (TypeError, ValueError):
        nxt = 0
    ref = now if playing or not nxt or nxt <= now else nxt
    until = next_noon_after(ref)
    return until if until - now >= 3600 else until + 86400


def pause(until, stop_now=False):
    """Switch the schedule off until ``until`` (epoch). Raises OverrideError."""
    now = time.time()
    if until <= now or until - now > MAX_HOURS * 3600:
        raise OverrideError(f"Choose a time up to {MAX_HOURS} hours from now.")
    from app.routes.scheduler import _load_schedule, _save_schedule
    existing = _get(PAUSE_KEY)
    if existing:                                    # already paused: just move the end
        existing["until"] = int(until)
        _set(PAUSE_KEY, existing)
    else:
        try:
            saved = _load_schedule()
        except Exception as exc:
            raise OverrideError("Could not read the schedule from the player.") from exc
        _set(PAUSE_KEY, {"until": int(until), "saved": saved})   # remembered before anything changes
        try:
            _save_schedule([{**entry, "enabled": 0} for entry in saved])
        except Exception as exc:
            _set(PAUSE_KEY, None)
            raise OverrideError("The player did not accept the change; the schedule is unchanged.") from exc
    if stop_now:
        from app.routes.playlists import _reset_overlays, _stop_current
        _stop_current()
        _reset_overlays()


def resume():
    """Restore the schedule. Raises OverrideError if the player cannot be reached (it is retried)."""
    pause_state = _get(PAUSE_KEY)
    if not pause_state:
        return
    from app.routes.scheduler import _save_schedule
    try:
        _save_schedule(pause_state.get("saved") or [])
    except Exception as exc:
        raise OverrideError("Could not restore the schedule; will retry.") from exc
    _set(PAUSE_KEY, None)


def run_for(playlist, hours):
    """Play ``playlist`` now, repeating, and stop it after ``hours``."""
    if not 0 < hours <= MAX_HOURS:
        raise OverrideError(f"Choose between a few minutes and {MAX_HOURS} hours.")
    if playlist.startswith(("Scene - ", "Effect - ")) or "/" in playlist or ".." in playlist:
        raise OverrideError("Choose one of your show playlists.")
    from app.routes.playlists import _reset_overlays, _stop_current
    _stop_current()
    _reset_overlays()
    try:
        resp = requests.get(
            fpp_url(f"/command/Start%20Playlist/{quote(playlist, safe='')}/true/false"), timeout=5)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise OverrideError("Could not start the playlist.") from exc
    _set(RUN_KEY, {"playlist": playlist, "until": int(time.time() + hours * 3600)})


def cancel_run(stop=True):
    if _get(RUN_KEY) is None:
        return
    _set(RUN_KEY, None)
    if stop:
        from app.routes.playlists import _reset_overlays, _stop_current
        _stop_current()
        _reset_overlays()


def tick(app, status=None, now=None):
    """Once a minute from the monitor: end a pause or a timed run that has run out."""
    now = now or time.time()
    try:
        with app.app_context():
            until = paused_until()
            if until is not None and now >= until:
                try:
                    resume()
                except OverrideError as exc:
                    _logger.warning("%s", exc)
            run = _get(RUN_KEY)
            if run and now >= int(run["until"]):
                playing = ((status or {}).get("current_playlist") or {}).get("playlist")
                cancel_run(stop=(playing == run["playlist"]))
    except Exception:
        db.session.rollback()
        _logger.exception("Override timer failed")
