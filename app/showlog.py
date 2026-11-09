"""Remember which playlists have run, from the monitor's status polls.

Gives the remote status report its "last show" and a count of recent playlist runs.
A run starts when a different playlist is playing than the one last seen, and ends
when nothing is playing. Poll resolution is a minute, so very short runs can be missed.
"""
import logging
import time
from datetime import datetime

from app import db

_logger = logging.getLogger(__name__)
KEEP_SECONDS = 90 * 86400
WINDOW_SECONDS = 30 * 86400


def _playing_name(status):
    """The playlist playing right now, or ''."""
    if not status:
        return ""
    playing = str(status.get("status_name", "")).lower() == "playing" or status.get("status") == 1
    name = ((status.get("current_playlist") or {}).get("playlist") or "").strip()
    return name if playing else ""


def observe(app, status, now=None):
    """Record a change in what is playing. Call from the monitor every poll."""
    if status is None:                      # fppd unreachable: say nothing either way
        return
    from app.models import ShowLog
    now = int(now or time.time())
    name = _playing_name(status)
    try:
        with app.app_context():
            open_row = ShowLog.query.filter_by(ended_at=None).order_by(ShowLog.id.desc()).first()
            if open_row is not None and open_row.playlist == name:
                return
            if open_row is not None:
                open_row.ended_at = now
            if name:
                db.session.add(ShowLog(playlist=name[:128], started_at=now))
            ShowLog.query.filter(ShowLog.started_at < now - KEEP_SECONDS).delete()
            db.session.commit()
    except Exception:
        db.session.rollback()
        _logger.exception("Show log update failed")


def fmt(epoch):
    return datetime.fromtimestamp(epoch).strftime("%a %b %d %I:%M %p").replace(" 0", " ")


def summary(app, now=None):
    """{'last_show': 'Evening - Fri Oct 23 6:00 PM', 'shows_30d': 12}, empty when none yet."""
    from app.models import ShowLog
    now = int(now or time.time())
    out = {}
    with app.app_context():
        last = ShowLog.query.order_by(ShowLog.started_at.desc()).first()
        if last is not None:
            out["last_show"] = f"{last.playlist} - {fmt(last.started_at)}"
        out["shows_30d"] = ShowLog.query.filter(ShowLog.started_at >= now - WINDOW_SECONDS).count()
    return out
