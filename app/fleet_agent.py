"""The controller's side of BLD remote monitoring beyond the basic heartbeat.

* extra fields for the status report (next and last show, paused state, options),
* one-off events for the portal to mail (a missed show, the player down),
* maintenance actions the portal queued, run from an allow-list and reported back.

Nothing here opens a port: the controller asks the portal for work in its own heartbeat.
Maintenance actions can be switched off by the owner (Settings -> Remote Access).
"""
import logging
import subprocess
import threading
import time

from app import db

_logger = logging.getLogger(__name__)

ACTIONS = ("restart_ui", "restart_fppd", "reboot", "update_plugin", "stop_effects",
           "push_backup", "restore_backup")
MAINTENANCE_KEY = "remote_maintenance"      # "0" = refuse maintenance actions; unset = allowed
EVENTS_KEY = "fleet_events"
RESULTS_KEY = "fleet_results"
_PLUGIN_DIR = None

_lock = threading.Lock()
_events = []                                # [{kind, key, detail}] not yet acknowledged


def maintenance_enabled(app):
    from app.models import AppSetting
    with app.app_context():
        row = db.session.get(AppSetting, MAINTENANCE_KEY)
        return not (row is not None and row.value == "0")


# --- events ------------------------------------------------------------------------------
def queue_event(kind, detail):
    """Remember something for the portal to mail. Safe to call from anywhere."""
    key = f"{kind}-{int(time.time())}"
    with _lock:
        if not any(e["kind"] == kind and e["detail"] == detail for e in _events):
            _events.append({"kind": kind, "key": key, "detail": str(detail)[:150]})
        del _events[:-10]


def pending_events():
    with _lock:
        return list(_events)


def ack_events(keys):
    with _lock:
        _events[:] = [e for e in _events if e["key"] not in set(keys)]


# --- results of maintenance actions ------------------------------------------------------------
def _results(app):
    import json
    from app.models import AppSetting
    with app.app_context():
        row = db.session.get(AppSetting, RESULTS_KEY)
        try:
            data = json.loads(row.value) if row is not None and row.value else []
        except ValueError:
            data = []
        return data if isinstance(data, list) else []


def _store_results(app, results):
    import json
    from app.models import AppSetting
    with app.app_context():
        row = db.session.get(AppSetting, RESULTS_KEY)
        text = json.dumps(results[-10:])
        if row is None:
            db.session.add(AppSetting(key=RESULTS_KEY, value=text))
        else:
            row.value = text
        db.session.commit()


def add_result(app, command_id, ok, detail):
    with _lock:
        results = _results(app)
        results = [r for r in results if r.get("id") != command_id]
        results.append({"id": command_id, "ok": bool(ok), "detail": str(detail)[:150]})
        _store_results(app, results)


def pending_results(app):
    with _lock:
        return _results(app)


def ack_results(app, ids):
    with _lock:
        _store_results(app, [r for r in _results(app) if r.get("id") not in set(ids)])


# --- running actions ---------------------------------------------------------------------------------
def _plugin_dir():
    global _PLUGIN_DIR
    if _PLUGIN_DIR is None:
        import os
        _PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return _PLUGIN_DIR


def _sudo(*args, timeout=30):
    return subprocess.run(["sudo", "-n", *args], capture_output=True, text=True, timeout=timeout)


def _update_plugin(app):
    """Pull the newest code; if it changed, run the installer detached (it restarts this service)."""
    d = _plugin_dir()
    before = subprocess.run(["git", "-C", d, "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True, timeout=10).stdout.strip()
    pull = subprocess.run(["git", "-C", d, "pull", "--ff-only"], capture_output=True, text=True,
                          timeout=120)
    if pull.returncode != 0:
        return False, "git pull failed: " + ((pull.stderr or pull.stdout).strip().splitlines() or ["?"])[-1]
    after = subprocess.run(["git", "-C", d, "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=10).stdout.strip()
    if before == after:
        return True, f"Already up to date ({after})"
    return True, f"Updated {before} -> {after}; restarting"


def _run(app, action, arg):
    """Do one action. Returns (ok, detail, after) where ``after`` is a callable to run
    once the result has been reported (for actions that restart this very service)."""
    if action == "restart_ui":
        return True, "Restarting the control panel", lambda: _sudo("systemctl", "restart", "fpp-ui")
    if action == "restart_fppd":
        result = _sudo("systemctl", "restart", "fppd", timeout=60)
        return result.returncode == 0, ("Show player restarted" if result.returncode == 0
                                        else "Could not restart the show player"), None
    if action == "reboot":
        def reboot():
            from app.routes.settings import trigger_reboot
            with app.app_context():
                trigger_reboot(delay=2, reason="BLD remote maintenance")
        return True, "Rebooting", reboot
    if action == "stop_effects":
        from app.routes.playlists import _reset_overlays
        with app.app_context():
            _reset_overlays()
        return True, "Effects stopped and lights cleared", None
    if action == "push_backup":
        from app import remote_backup
        ok, detail = remote_backup.push(app)
        return ok, detail, None
    if action == "restore_backup":
        from app import remote_backup
        if not str(arg).isdigit():
            return False, "No backup chosen", None
        ok, detail = remote_backup.restore(app, int(arg))
        return ok, detail, None
    if action == "update_plugin":
        ok, detail = _update_plugin(app)
        after = None
        if ok and "restarting" in detail:
            def run_installer():
                _sudo("systemd-run", "--no-block", f"--unit=fpp-ui-update-{int(time.time())}",
                      "bash", f"{_plugin_dir()}/fpp_install.sh")
            after = run_installer
        return ok, detail, after
    return False, "Unknown action", None


def execute(app, command_id, action, arg="", report=None):
    """Run one queued action and record how it went. ``report`` is called after the result is
    stored and before any restart, so the portal hears about it from this same process."""
    from app import audit
    with app.app_context():
        audit.record("remote_action", f"{action} {arg}".strip())
    if action not in ACTIONS:
        add_result(app, command_id, False, "Not an allowed action")
        return
    if not maintenance_enabled(app):
        add_result(app, command_id, False, "Maintenance actions are switched off on this controller")
        return
    try:
        ok, detail, after = _run(app, action, arg)
    except Exception as exc:
        _logger.exception("Remote action %s failed", action)
        ok, detail, after = False, f"Failed: {type(exc).__name__}", None
    add_result(app, command_id, ok, detail)
    if after is not None:
        if report is not None:
            report()
        time.sleep(2)
        after()


def handle_commands(app, commands, report=None):
    """Start the actions the portal sent, each in its own thread, at most three."""
    for cmd in (commands if isinstance(commands, list) else [])[:3]:
        try:
            cid, action = int(cmd["id"]), str(cmd["action"])
        except (KeyError, TypeError, ValueError):
            continue
        threading.Thread(target=execute, args=(app, cid, action, str(cmd.get("arg", ""))),
                         kwargs={"report": report}, daemon=True, name=f"remote-{action}").start()


# --- status report extras ------------------------------------------------------------------------------------
def report_extras(app, status):
    """Extra fields for the heartbeat. ``status`` is fppd's status or None."""
    from app import overrides, remote_backup, showlog
    out = {}
    try:
        with app.app_context():
            out["maintenance"] = maintenance_enabled(app)
            out["backup_opt_in"] = remote_backup.opted_in(app)
            last = remote_backup.last_backup(app)
            if last:
                out["backup_at"] = showlog.fmt(last)
            until = overrides.paused_until()
            if until:
                out["paused_until"] = showlog.fmt(until)
            out.update(showlog.summary(app))
        from app.alert_monitor import _next_start
        nxt = _next_start(status) if status else None
        if nxt:
            out["next_show"] = f"{nxt[0]} - {showlog.fmt(nxt[1])}"
    except Exception:
        _logger.exception("Could not build the extra report fields")
    return out
