"""Activity log: a record of who changed what on this controller.

Logins and PIN changes are recorded explicitly; everything else is picked up by one
after-request hook that notes every successful change made through the settings,
schedule and maintenance endpoints. Day-to-day operation (playing a playlist, picking a
color) is deliberately not logged. The log never stores PINs or request bodies.
"""
import logging
import time

from flask import has_request_context, request, session

from app import db

_logger = logging.getLogger(__name__)

KEEP_ROWS = 2000
_PRUNE_EVERY = 50
_writes = 0

# Paths (as Flask sees them, without the URL prefix) whose successful changes are logged.
AUDITED_PREFIXES = (
    "/api/schedule", "/api/holidays", "/api/settings", "/api/zones", "/api/upload",
    "/api/change-", "/api/set-", "/api/clear-", "/api/reset-terms", "/api/ui-path",
    "/api/remote", "/api/restore", "/api/system", "/api/genius", "/api/alerts",
    "/api/network", "/api/fpp", "/api/custom-playlists", "/api/override",
)
_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
# Read-only calls that happen to be POSTs.
_QUIET = ("/api/alerts/test", "/api/restore/chunk", "/api/fpp/layout/preview")


def actor():
    if not has_request_context():
        return "system"
    if session.get("is_staff"):
        return "staff"
    if session.get("is_master"):
        return "master"
    return "admin" if session.get("logged_in") else "anonymous"


def record(action, detail=""):
    """Add one line to the log. Never raises: auditing must not break the action."""
    global _writes
    from app.models import AuditLog
    try:
        ip = (request.remote_addr or "") if has_request_context() else ""
        db.session.add(AuditLog(at=int(time.time()), actor=actor(), action=str(action)[:60],
                                detail=str(detail)[:200], ip=ip[:64]))
        db.session.commit()
        _writes += 1
        if _writes % _PRUNE_EVERY == 0:
            _prune()
    except Exception:
        db.session.rollback()
        _logger.exception("Could not write the activity log")


def _prune():
    from app.models import AuditLog
    cutoff = db.session.query(AuditLog.id).order_by(AuditLog.id.desc()).offset(KEEP_ROWS).first()
    if cutoff:
        AuditLog.query.filter(AuditLog.id <= cutoff[0]).delete()
        db.session.commit()


def recent(limit=100):
    from app.models import AuditLog
    rows = AuditLog.query.order_by(AuditLog.id.desc()).limit(max(1, min(limit, 500))).all()
    return [{"at": r.at, "actor": r.actor, "action": r.action, "detail": r.detail, "ip": r.ip}
            for r in rows]


def init_audit(app):
    @app.after_request
    def _log_changes(response):
        if (request.method in _UNSAFE and response.status_code < 400
                and request.path.startswith(AUDITED_PREFIXES)
                and not request.path.startswith(_QUIET)):
            record("change", f"{request.method} {request.path}")
        return response
