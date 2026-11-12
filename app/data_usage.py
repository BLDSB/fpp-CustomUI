"""Data usage readout: how much this controller sends and receives over its internet link.

Useful when a controller sits on a cellular hotspot with a monthly allowance. The kernel
keeps running byte counters per network interface; they restart from zero at every boot, so
this module samples them once a minute and adds up the *differences* into per-day totals
that survive reboots.

Two figures are kept per day:

* ``link``   - everything on the interface that carries the default route (the hotspot
               connection), which is what the carrier actually bills;
* ``tunnel`` - the part of that which went through the BLD remote-access tunnel.

Counters are only a measure of this controller's own traffic. Anything else on the hotspot
(a phone, a laptop) is invisible here, and the carrier's own figure is the final word.
Totals are saved every few minutes rather than every sample to spare the SD card; a crash
loses at most those few minutes of the controller's own traffic.
"""
import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta

from app import db

_logger = logging.getLogger(__name__)

SETTING_KEY = "data_usage"
TUNNEL_IFACE = "bld"                    # wg-quick@bld, see deploy/fpp-remote.sh
KEEP_DAYS = 62
SAVE_EVERY_SECONDS = 300
_NET_ROOT = "/sys/class/net"
_ROUTE_FILE = "/proc/net/route"

_lock = threading.Lock()
_state = None                           # loaded lazily from the database
_last_save = 0.0


def default_interface():
    """Name of the interface that carries the default route, or None."""
    try:
        with open(_ROUTE_FILE) as fh:
            rows = fh.read().splitlines()[1:]
    except OSError:
        return None
    best = None
    for row in rows:
        cols = row.split()
        if len(cols) < 7 or cols[1] != "00000000":
            continue
        try:
            metric = int(cols[6])
        except ValueError:
            continue
        if best is None or metric < best[0]:
            best = (metric, cols[0])
    return best[1] if best else None


def read_counters(iface):
    """(received, sent) bytes since boot for an interface, or None if it has none."""
    if not iface or "/" in iface or iface.startswith("."):
        return None
    try:
        out = []
        for name in ("rx_bytes", "tx_bytes"):
            with open(os.path.join(_NET_ROOT, iface, "statistics", name)) as fh:
                out.append(int(fh.read().strip()))
        return out[0], out[1]
    except (OSError, ValueError):
        return None


def _empty():
    return {"link_iface": "", "last": {}, "days": {}}


def _load(app):
    from app.models import AppSetting
    with app.app_context():
        row = db.session.get(AppSetting, SETTING_KEY)
        try:
            data = json.loads(row.value) if row is not None and row.value else {}
        except ValueError:
            data = {}
    state = _empty()
    if isinstance(data, dict):
        state["link_iface"] = str(data.get("link_iface") or "")
        if isinstance(data.get("last"), dict):
            state["last"] = data["last"]
        if isinstance(data.get("days"), dict):
            state["days"] = data["days"]
    return state


def _save(app, state):
    from app.models import AppSetting
    text = json.dumps(state, separators=(",", ":"))
    with app.app_context():
        row = db.session.get(AppSetting, SETTING_KEY)
        if row is None:
            db.session.add(AppSetting(key=SETTING_KEY, value=text))
        else:
            row.value = text
        db.session.commit()


def _delta(state, key, counters):
    """Bytes (received, sent) since the previous reading of ``key``. The first reading, and
    a reading after the counter went backwards (a reboot), count from the counter itself
    or from nothing as appropriate, so traffic is never counted twice."""
    rx, tx = counters
    prev = state["last"].get(key)
    state["last"][key] = [rx, tx]
    if not prev or len(prev) != 2:
        return 0, 0                     # nothing to compare with yet
    drx = rx - prev[0] if rx >= prev[0] else rx
    dtx = tx - prev[1] if tx >= prev[1] else tx
    return max(drx, 0), max(dtx, 0)


def _add(state, day, kind, drx, dtx):
    if not (drx or dtx):
        return
    entry = state["days"].setdefault(day, {})
    cur = entry.get(kind) or [0, 0]
    entry[kind] = [cur[0] + drx, cur[1] + dtx]


def sample(app, now=None, force_save=False):
    """Take one reading and fold it into today's totals. Never raises."""
    global _state, _last_save
    try:
        now = now or datetime.now()
        with _lock:
            if _state is None:
                _state = _load(app)
            state = _state
            day = now.strftime("%Y-%m-%d")

            iface = default_interface()
            if iface:
                if iface != state["link_iface"]:
                    # The connection moved (cable to Wi-Fi, say): start a fresh baseline.
                    state["last"].pop("link", None)
                    state["link_iface"] = iface
                counters = read_counters(iface)
                if counters:
                    _add(state, day, "link", *_delta(state, "link", counters))
            counters = read_counters(TUNNEL_IFACE)
            if counters:
                _add(state, day, "tunnel", *_delta(state, "tunnel", counters))
            else:
                state["last"].pop("tunnel", None)

            cutoff = (now.date() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
            for old in [d for d in state["days"] if d < cutoff]:
                del state["days"][old]

            mono = time.monotonic()
            if force_save or mono - _last_save >= SAVE_EVERY_SECONDS:
                _last_save = mono
                _save(app, state)
    except Exception:
        _logger.exception("Data usage sample failed")


def flush(app):
    """Write the in-memory totals now (used before reading them back)."""
    global _last_save
    with _lock:
        if _state is not None:
            _last_save = time.monotonic()
            _save(app, _state)


def _sum(days, keys, kind):
    rx = tx = 0
    for key in keys:
        pair = (days.get(key) or {}).get(kind)
        if pair:
            rx += pair[0]
            tx += pair[1]
    return {"rx": rx, "tx": tx, "total": rx + tx}


def summary(app, today=None):
    """What the Settings card shows."""
    global _state
    today = today or date.today()
    with _lock:
        if _state is None:
            _state = _load(app)
        days = json.loads(json.dumps(_state["days"]))
        iface = _state["link_iface"]

    def span(n):
        return [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]

    month = [k for k in days if k.startswith(today.strftime("%Y-%m"))]
    periods = {"today": span(1), "week": span(7), "thirty": span(30), "month": month}
    out = {"interface": iface, "available": bool(iface)}
    for name, keys in periods.items():
        out[name] = {"link": _sum(days, keys, "link"), "tunnel": _sum(days, keys, "tunnel")}
    out["daily"] = [{"day": key, "total": _sum(days, [key], "link")["total"]}
                    for key in reversed(span(14))]
    return out


def reset_for_tests():
    global _state, _last_save
    with _lock:
        _state = None
        _last_save = 0.0


def clear(app):
    """Forget all totals (the owner starting a new billing month, say)."""
    global _state, _last_save
    with _lock:
        _state = _empty()
        _last_save = time.monotonic()
        _save(app, _state)
