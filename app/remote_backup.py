"""Configuration backups sent to the BLD portal, and restores fetched from it.

Only the controller's settings go: the UI database, uploaded branding and FPP's own
configuration (never sequences or media). The nightly upload happens only when the
owner has switched it on under Settings -> Remote Access; BLD staff can also ask for
one on demand (a maintenance action). The portal keeps the newest few, encrypted.
"""
import logging
import time

import requests

from app import db

_logger = logging.getLogger(__name__)

OPT_IN_KEY = "remote_backup"            # "1" = send a backup every night
LAST_KEY = "remote_backup_at"           # epoch of the last upload the portal accepted
SECTIONS = {"ui", "uploads", "controller_config"}
MAX_BYTES = 24 * 1024 * 1024
NIGHTLY_HOURS = (2, 3, 4)               # local hours in which the nightly upload may run


def _setting(key, default=""):
    from app.models import AppSetting
    row = db.session.get(AppSetting, key)
    return row.value if row is not None and row.value is not None else default


def _put(key, value):
    from app.models import AppSetting
    row = db.session.get(AppSetting, key)
    if row is None:
        db.session.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    db.session.commit()


def opted_in(app):
    with app.app_context():
        return _setting(OPT_IN_KEY) == "1"


def last_backup(app):
    with app.app_context():
        try:
            return int(_setting(LAST_KEY, "0") or 0)
        except ValueError:
            return 0


def base_url():
    """The portal listener's address, derived from the heartbeat address."""
    from app.heartbeat import hub_url
    return hub_url().rsplit("/api/heartbeat", 1)[0]


def build_archive(app):
    """The settings archive as bytes."""
    from app import fpp_backup
    with app.app_context():
        identity = {}
        for key in ("ADMIN_PASSWORD_HASH", "INTERNAL_TOKEN"):
            if app.config.get(key):
                identity[key] = app.config[key]
        return b"".join(chunk for chunk in fpp_backup.iter_archive(SECTIONS, identity) if chunk)


def push(app):
    """Build and upload one backup. Returns (ok, detail); never raises."""
    try:
        data = build_archive(app)
        if len(data) > MAX_BYTES:
            return False, "The settings backup is too large to send."
        resp = requests.post(base_url() + "/api/backup", data=data, timeout=120,
                             headers={"Content-Type": "application/zip"})
        resp.raise_for_status()
        with app.app_context():
            _put(LAST_KEY, str(int(time.time())))
        return True, f"Sent {len(data) // 1024} KB"
    except Exception as exc:
        _logger.warning("Backup upload failed: %s", exc)
        return False, "Could not send the backup."


def maybe_nightly(app, now=None):
    """Called from the heartbeat loop: upload once a day, at night, if the owner opted in."""
    now = now or time.time()
    if not opted_in(app) or now - last_backup(app) < 23 * 3600:
        return None
    if time.localtime(now).tm_hour not in NIGHTLY_HOURS and last_backup(app) != 0:
        return None
    return push(app)


def restore(app, backup_id):
    """Fetch backup ``backup_id`` from the portal and apply it. Returns (ok, detail)."""
    from app.routes.backup import apply_archive_bytes
    try:
        resp = requests.get(f"{base_url()}/api/backup/{int(backup_id)}", timeout=120)
        resp.raise_for_status()
        raw = resp.content
    except Exception as exc:
        _logger.warning("Backup download failed: %s", exc)
        return False, "Could not fetch the backup from the portal."
    try:
        with app.app_context():
            result = apply_archive_bytes(raw)
            response = result[0] if isinstance(result, tuple) else result
            body = response.get_json() or {}
    except Exception:
        _logger.exception("Backup restore failed")
        return False, "The restore failed; see the controller log."
    if not body.get("ok"):
        bad = [e["step"] for e in body.get("log", []) if e.get("status") == "error"]
        return False, "Restore finished with errors: " + ", ".join(bad) if bad else "Restore failed."
    return True, "Restored" + ("; a reboot is recommended" if body.get("reboot_recommended") else "")
