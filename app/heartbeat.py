"""Heartbeat to the BLD remote-access hub.

While this controller is enrolled (see app/remote_access.py) it sends a small status
report once a minute over the WireGuard tunnel, so BLD can show whether the site is
online and email an alert when it goes quiet. The address is the hub's tunnel
address, reachable only through the tunnel, and WireGuard itself proves which
controller is speaking, so there is no password to store.

What is sent is deliberately small and matches what the Terms describe: whether the
show player is up, the playlist that is running, software versions, CPU temperature,
power condition, storage use and uptime. No lighting content, no credentials.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time

import requests

from app.alert_monitor import _fetch_status

_logger = logging.getLogger(__name__)

STATE_FILE = "/var/lib/fpp-remote/state.json"
HUB_URL = "http://10.99.0.1:8081/api/heartbeat"
INTERVAL_SECONDS = 60
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_thread = None
_plugin_rev = None
_last_warning = 0.0


def hub_url():
    """The hub's heartbeat address. Enrolment records the hub's tunnel address and port in
    the state file (the BLD test portal uses a different range from the live one); units
    enrolled before that was recorded, and any odd value, get the live hub."""
    try:
        with open(STATE_FILE) as fh:
            state = json.load(fh)
        ip, port = str(state.get("hub_ip", "")), int(state.get("heartbeat_port", 0))
        if re.fullmatch(r"10\.\d{1,3}\.0\.1", ip) and 1024 <= port <= 65535:
            return f"http://{ip}:{port}/api/heartbeat"
    except (OSError, ValueError, TypeError):
        pass
    return HUB_URL


def enrolled():
    """True once the controller has joined the tunnel (the helper writes this file)."""
    return os.path.exists(STATE_FILE)


def _read_number(path, scale=1.0):
    try:
        with open(path) as fh:
            return float(fh.read().split()[0]) / scale
    except (OSError, ValueError, IndexError):
        return None


def _cpu_temp():
    return _read_number("/sys/class/thermal/thermal_zone0/temp", 1000.0)


def _uptime():
    return _read_number("/proc/uptime")


def _disk_pct():
    try:
        usage = shutil.disk_usage("/")
        return round(usage.used * 100.0 / usage.total, 1)
    except OSError:
        return None


def _power():
    """Raspberry Pi power/thermal flags from vcgencmd, or {} where it is unavailable."""
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True,
                             timeout=3).stdout
        match = re.search(r"0x[0-9a-fA-F]+", out)
        if not match:
            return {}
        value = int(match.group(0), 16)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    return {"throttled": f"0x{value:x}",
            "undervoltage": bool(value & 0x1),               # right now
            "undervoltage_ever": bool(value & 0x10000)}      # at some point since boot


def _plugin_revision():
    global _plugin_rev
    if _plugin_rev is None:
        try:
            _plugin_rev = subprocess.run(["git", "-C", _PLUGIN_DIR, "rev-parse", "--short", "HEAD"],
                                         capture_output=True, text=True, timeout=5
                                         ).stdout.strip() or "unknown"
        except (OSError, subprocess.SubprocessError):
            _plugin_rev = "unknown"
    return _plugin_rev


def collect(app):
    """The report. Missing readings are simply left out."""
    status = _fetch_status(app)
    report = {"plugin": _plugin_revision()}
    if status is None:
        report["fppd"] = "down"
    else:
        report["fppd"] = str(status.get("fppd") or "unknown")
        report["state"] = str(status.get("status_name") or "")
        playlist = (status.get("current_playlist") or {}).get("playlist")
        report["playlist"] = str(playlist or "")
        if status.get("version"):
            report["fpp"] = str(status["version"])
    for key, value in (("cpu_temp_c", _cpu_temp()), ("uptime_s", _uptime()),
                       ("disk_pct", _disk_pct())):
        if value is not None:
            report[key] = round(value, 1)
    report.update(_power())
    return report


def send(app):
    """One heartbeat. Returns True if the hub accepted it; never raises."""
    global _last_warning
    url = app.config.get("HEARTBEAT_URL") or hub_url()
    try:
        resp = requests.post(url, data=json.dumps(collect(app)), timeout=5,
                             headers={"Content-Type": "application/json"})
        resp.raise_for_status()
        return True
    except Exception as exc:
        # A down tunnel is expected whenever the network is; say so at most every half hour.
        if time.monotonic() - _last_warning > 1800:
            _last_warning = time.monotonic()
            _logger.warning("Heartbeat not delivered: %s", exc)
        return False


def _loop(app):
    time.sleep(75)                          # after the alert monitor, once everything is up
    while True:
        try:
            if enrolled():
                send(app)
        except Exception:
            _logger.exception("Heartbeat unexpected error")
        time.sleep(INTERVAL_SECONDS)


def start_heartbeat(app):
    global _thread
    if _thread is not None:
        return
    _thread = threading.Thread(target=_loop, args=(app,), daemon=True, name="heartbeat")
    _thread.start()
    _logger.info("Heartbeat started")
