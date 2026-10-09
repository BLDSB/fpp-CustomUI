"""Network and WiFi-hotspot settings, proxied to FPP's own network API.

FPP owns the network configuration. It regenerates the systemd-networkd files
under /etc/systemd/network at every boot from its own
/home/fpp/media/config/interface.* files, so writing those files behind its
back — or reaching for NetworkManager/nmcli, which FPP 9 does not use — gets
silently overwritten. Everything here therefore goes through FPP's REST API,
which writes the files FPP reads and knows how to apply them.

Two groups of settings live here:

  * per-interface config (DHCP or static, WiFi SSID/PSK, route metric) plus the
    global gateway and DNS. FPP applies these on demand via
    `POST /api/network/interface/<iface>/apply`, which runs `fppinit
    setupNetwork` for that interface;
  * tethering — the fallback hotspot FPP raises when it has no address.
    `EnableTethering`, `TetherSSID` and `TetherPSK` are plain FPP settings with
    no apply step: `maybeEnableTethering()` reads them at boot, so a change
    only takes effect after a reboot. The UI says so rather than pretending
    the hotspot reconfigured itself.

A wrong value here can take the controller off the network entirely, so every
field is validated before it reaches FPP and the page confirms before applying.
"""
import ipaddress
import re

import requests
from flask import Blueprint, current_app, jsonify

from app.auth_utils import login_required
from app.fpp_api import fpp_url
from app.validation import json_object

network_bp = Blueprint("network", __name__)

# Interface names are interpolated into an FPP API URL and, on FPP's side, into
# a config filename — so they are matched against this and then against the
# list of interfaces FPP actually reports before being used.
_IFACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,14}$")

# Tethering modes, as understood by maybeEnableTethering() in
# src/boot/FPPINIT_Network.cpp.
TETHER_MODES = {
    "0": "Automatic — only when nothing else connects",
    "1": "Always on",
    "2": "Disabled",
}

# FPP's own defaults for the hotspot, used when the settings have never been
# written on this controller.
TETHER_DEFAULTS = {"mode": "0", "ssid": "FPP", "psk": "Christmas"}

# The hotspot address is hardcoded in FPP, not configurable — shown so nobody
# has to guess what to type into a phone.
TETHER_ADDRESS = "192.168.8.1"

_SHORT_TIMEOUT = 8      # reads
_SAVE_TIMEOUT = 15      # config writes
_APPLY_TIMEOUT = 45     # `fppinit setupNetwork` restarts the interface
_SCAN_TIMEOUT = 45      # a WiFi scan takes several seconds per band


class FppError(Exception):
    """FPP could not be reached, or answered with something unusable."""


def _fpp_get(path, timeout=_SHORT_TIMEOUT):
    try:
        resp = requests.get(fpp_url(path), timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        current_app.logger.error("FPP GET %s failed: %s", path, exc)
        raise FppError(f"Could not read {path} from the controller: {exc}")


def _fpp_post(path, payload, timeout=_SAVE_TIMEOUT):
    try:
        resp = requests.post(fpp_url(path), json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        current_app.logger.error("FPP POST %s failed: %s", path, exc)
        raise FppError(f"The controller rejected the change: {exc}")


def _fpp_status_ok(body, what):
    """Raise unless FPP's `status` field says the call worked.

    FPP answers 200 with `{"status": "ERROR: ..."}` — or with a bare sentence
    like "Unable to create file for interface" — rather than an HTTP error, so
    the body has to be read or a failed write reads as a success.
    """
    status = str((body or {}).get("status", "")).strip()
    if status.upper() == "OK":
        return
    detail = status.split(":", 1)[-1].strip() if status else "no status returned"
    raise FppError(f"{what}: {detail}")


# ── Validation ────────────────────────────────────────────────────────────────

def _valid_ip(value):
    try:
        ipaddress.IPv4Address(value)
        return True
    except ValueError:
        return False


def _valid_netmask(value):
    """A contiguous IPv4 mask, and not 0.0.0.0."""
    try:
        network = ipaddress.IPv4Network(f"0.0.0.0/{value}")
    except ValueError:
        return False
    return network.prefixlen > 0


def _netmask_from_prefix(prefixlen):
    return str(ipaddress.IPv4Network(f"0.0.0.0/{prefixlen}").netmask)


def _check_psk(psk, label):
    """WPA pre-shared keys are 8–63 characters; an empty one means open."""
    if psk and not 8 <= len(psk) <= 63:
        return f"{label} must be 8–63 characters (WPA requires at least 8)."
    return None


def _check_ssid(ssid, label, required=False):
    if not ssid:
        return f"{label} is required." if required else None
    if len(ssid.encode("utf-8")) > 32:
        return f"{label} is too long — 32 characters maximum."
    return None


def _str(data, key, limit=128):
    return str(data.get(key) or "").strip()[:limit]


# ── Reading the current configuration ─────────────────────────────────────────

def _known_interfaces():
    """Interface names FPP reports, lowest-cost call, for validating input."""
    body = _fpp_get("/network/interface")
    if not isinstance(body, list):
        raise FppError("The controller returned an unexpected interface list.")
    return [
        rec.get("ifname") for rec in body
        if isinstance(rec, dict) and rec.get("ifname")
    ]


def _resolve_iface(iface):
    """The interface name as FPP spells it, or an error string."""
    if not _IFACE_RE.match(iface or ""):
        return None, "That is not a valid interface name."
    if iface == "lo":
        return None, "The loopback interface cannot be configured."
    try:
        known = _known_interfaces()
    except FppError as exc:
        return None, str(exc)
    if iface not in known:
        return None, f"This controller has no interface called '{iface}'."
    return iface, None


def _interface_view(record):
    """One interface, flattened into what the settings page needs."""
    name = record.get("ifname", "")
    flags = record.get("flags") or []
    config = record.get("config") or {}

    address, netmask = "", ""
    for info in record.get("addr_info") or []:
        if info.get("family") == "inet" and info.get("local"):
            address = info["local"]
            try:
                netmask = _netmask_from_prefix(int(info.get("prefixlen", 32)))
            except (TypeError, ValueError):
                netmask = ""
            break

    wifi = record.get("wifi") or {}

    return {
        "name": name,
        "wireless": name.startswith("wl"),
        # LOWER_UP is the carrier: "configured and up" is not the same as
        # "something is actually plugged in", and that distinction is the whole
        # reason tethering sometimes refuses to start.
        "up": "UP" in flags,
        "carrier": "LOWER_UP" in flags,
        "current_address": address,
        "current_netmask": netmask,
        "signal": wifi.get("level"),
        "proto": (config.get("PROTO") or "dhcp").lower(),
        "address": config.get("ADDRESS") or "",
        "netmask": config.get("NETMASK") or "",
        "ssid": config.get("SSID") or "",
        "psk": config.get("PSK") or "",
        "backup_ssid": config.get("BACKUPSSID") or "",
        "backup_psk": config.get("BACKUPPSK") or "",
        "hidden": str(config.get("HIDDEN") or "0") == "1",
        "wpa3": str(config.get("WPA3") or "0") == "1",
        "backup_hidden": str(config.get("BACKUPHIDDEN") or "0") == "1",
        "backup_wpa3": str(config.get("BACKUPWPA3") or "0") == "1",
        "route_metric": config.get("ROUTEMETRIC") or "",
        "configured": bool(config),
    }


def _current_ssid(iface):
    """The SSID a wireless interface is associated with right now, if any."""
    try:
        detail = _fpp_get(f"/network/interface/{iface}")
    except FppError:
        return ""
    return (detail or {}).get("CurrentSSID", "") if isinstance(detail, dict) else ""


def _tethering_setting(key, default):
    """One FPP setting's value, falling back to FPP's own default."""
    try:
        body = _fpp_get(f"/settings/{key}")
    except FppError:
        return default
    if not isinstance(body, dict):
        return default
    value = body.get("value")
    if value in (None, ""):
        value = body.get("default")
    return default if value in (None, "") else str(value)


@network_bp.get("/api/network")
@login_required
def get_network():
    """Everything the Network cards on the settings page render from."""
    try:
        raw = _fpp_get("/network/interface")
    except FppError as exc:
        return jsonify({"error": str(exc)}), 502
    if not isinstance(raw, list):
        return jsonify({"error": "The controller returned an unexpected interface list."}), 502

    interfaces = []
    for record in raw:
        if not isinstance(record, dict) or not record.get("ifname"):
            continue
        view = _interface_view(record)
        if view["wireless"]:
            view["current_ssid"] = _current_ssid(view["name"])
        interfaces.append(view)

    # DNS and the gateway are global on FPP, not per-interface. Neither is
    # fatal if it is missing — an unconfigured controller has no such file.
    try:
        dns = _fpp_get("/network/dns")
    except FppError:
        dns = {}
    try:
        gateway = _fpp_get("/network/gateway")
    except FppError:
        gateway = {}

    return jsonify({
        "interfaces": interfaces,
        "dns1": (dns or {}).get("DNS1", "") if isinstance(dns, dict) else "",
        "dns2": (dns or {}).get("DNS2", "") if isinstance(dns, dict) else "",
        "gateway": (gateway or {}).get("GATEWAY", "") if isinstance(gateway, dict) else "",
        "tethering": {
            "mode": _tethering_setting("EnableTethering", TETHER_DEFAULTS["mode"]),
            "ssid": _tethering_setting("TetherSSID", TETHER_DEFAULTS["ssid"]),
            "psk": _tethering_setting("TetherPSK", TETHER_DEFAULTS["psk"]),
        },
        "tether_address": TETHER_ADDRESS,
    })


# ── Writing ───────────────────────────────────────────────────────────────────

# Keys FPP writes into an interface file that this page has no field for. They
# are set from FPP's own network page (a DHCP server handing out addresses on a
# port, IP forwarding between ports) and would be dropped on the floor by a
# save from here, because FPP rewrites the file from the posted document rather
# than merging into it.
_PASSTHROUGH_KEYS = ("DHCPSERVER", "DHCPOFFSET", "DHCPPOOLSIZE", "IPFORWARDING")


def _carry_forward(iface, payload):
    """Copy settings this page doesn't show from the interface's current config."""
    try:
        existing = _fpp_get(f"/network/interface/{iface}")
    except FppError:
        # Better to save what was asked for than to refuse over settings that
        # most controllers never use.
        current_app.logger.warning("Could not read %s before saving it", iface)
        return
    if not isinstance(existing, dict):
        return

    for key in _PASSTHROUGH_KEYS:
        if existing.get(key) in (None, ""):
            continue
        try:
            payload[key] = int(existing[key])
        except (TypeError, ValueError):
            current_app.logger.warning(
                "Ignoring non-numeric %s=%r on %s", key, existing[key], iface
            )

    # FPP deletes the static-lease file when DHCPSERVER is on and no leases are
    # posted, so an existing reservation list has to come back with the save.
    leases = existing.get("StaticLeases")
    if payload.get("DHCPSERVER") and isinstance(leases, dict) and leases:
        payload["Leases"] = leases


def _static_fields(data):
    """``(ADDRESS/NETMASK dict, error)`` for a static-IP save."""
    address = _str(data, "address", 45)
    netmask = _str(data, "netmask", 45)
    if not _valid_ip(address):
        return None, "Enter a valid IP address, e.g. 192.168.1.50."
    if not _valid_netmask(netmask):
        return None, "Enter a valid subnet mask, e.g. 255.255.255.0."
    return {"ADDRESS": address, "NETMASK": netmask}, None


def _wireless_fields(data):
    """``(SSID/PSK/... dict, error)`` for a wireless interface save."""
    ssid = _str(data, "ssid", 64)
    psk = _str(data, "psk", 64)
    backup_ssid = _str(data, "backup_ssid", 64)
    backup_psk = _str(data, "backup_psk", 64)
    for problem in (
        _check_ssid(ssid, "Network name (SSID)"),
        _check_psk(psk, "The WiFi password"),
        _check_ssid(backup_ssid, "Backup network name"),
        _check_psk(backup_psk, "The backup WiFi password"),
    ):
        if problem:
            return None, problem
    return {
        "SSID": ssid,
        "PSK": psk,
        "HIDDEN": 1 if data.get("hidden") else 0,
        "WPA3": 1 if data.get("wpa3") else 0,
        "BACKUPSSID": backup_ssid,
        "BACKUPPSK": backup_psk,
        "BACKUPHIDDEN": 1 if data.get("backup_hidden") else 0,
        "BACKUPWPA3": 1 if data.get("backup_wpa3") else 0,
    }, None


def _route_metric_field(data):
    """``(ROUTEMETRIC dict — empty when unset, error)``."""
    metric = _str(data, "route_metric", 8)
    if not metric:
        return {}, None
    try:
        metric_value = int(metric)
        if not 0 <= metric_value <= 9999:
            raise ValueError
    except ValueError:
        return None, "Route metric must be a number from 0 to 9999."
    return {"ROUTEMETRIC": metric_value}, None


def _build_interface_payload(iface, data):
    """The full document FPP expects for ``iface``, or ``(None, error)``."""
    proto = _str(data, "proto", 16).lower()
    if proto not in ("dhcp", "static"):
        return None, "Addressing must be either DHCP or Static."

    payload = {"INTERFACE": iface, "PROTO": proto}
    sections = []
    if proto == "static":
        sections.append(_static_fields)
    if iface.startswith("wl"):
        sections.append(_wireless_fields)
    sections.append(_route_metric_field)

    for section in sections:
        fields, error = section(data)
        if error:
            return None, error
        payload.update(fields)
    return payload, None


@network_bp.post("/api/network/interface/<iface>")
@login_required
def save_interface(iface):
    """Write one interface's configuration, and optionally apply it.

    FPP rewrites the whole interface file from the posted document, so every
    field it understands has to be sent every time — anything left out is
    dropped from the file, not merged.
    """
    iface, error = _resolve_iface(iface)
    if error:
        return jsonify({"error": error}), 400

    data = json_object()
    payload, error = _build_interface_payload(iface, data)
    if error:
        return jsonify({"error": error}), 400

    _carry_forward(iface, payload)

    try:
        body = _fpp_post(f"/network/interface/{iface}", payload)
        _fpp_status_ok(body, "The controller could not save the interface")
    except FppError as exc:
        return jsonify({"error": str(exc)}), 502

    if not data.get("apply"):
        return jsonify({"ok": True, "applied": False})

    # Applying restarts the interface. If that is the interface this request
    # arrived on, the response never reaches the browser — the page treats a
    # dropped connection here as "probably applied", which is the honest
    # reading, rather than reporting a failure.
    try:
        applied = _fpp_post(f"/network/interface/{iface}/apply", {}, timeout=_APPLY_TIMEOUT)
        _fpp_status_ok(applied, "The controller could not apply the settings")
    except FppError as exc:
        return jsonify({
            "error": f"Saved, but applying failed — reboot to pick the settings up. ({exc})"
        }), 502

    return jsonify({"ok": True, "applied": True})


@network_bp.post("/api/network/dns")
@login_required
def save_dns():
    data = json_object()
    dns1 = _str(data, "dns1", 45)
    dns2 = _str(data, "dns2", 45)
    for value, label in ((dns1, "Primary DNS"), (dns2, "Secondary DNS")):
        if value and not _valid_ip(value):
            return jsonify({"error": f"{label} must be a valid IP address, or blank."}), 400

    try:
        body = _fpp_post("/network/dns", {"DNS1": dns1, "DNS2": dns2})
        _fpp_status_ok(body, "The controller could not save DNS")
    except FppError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"ok": True})


@network_bp.post("/api/network/gateway")
@login_required
def save_gateway():
    data = json_object()
    gateway = _str(data, "gateway", 45)
    if gateway and not _valid_ip(gateway):
        return jsonify({"error": "The gateway must be a valid IP address, or blank."}), 400

    try:
        body = _fpp_post("/network/gateway", {"GATEWAY": gateway})
        _fpp_status_ok(body, "The controller could not save the gateway")
    except FppError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"ok": True})


@network_bp.get("/api/network/wifi/scan/<iface>")
@login_required
def wifi_scan(iface):
    """Networks in range, so an SSID can be picked rather than typed."""
    iface, error = _resolve_iface(iface)
    if error:
        return jsonify({"error": error}), 400
    if not iface.startswith("wl"):
        return jsonify({"error": f"{iface} is not a wireless interface."}), 400

    try:
        body = _fpp_get(f"/network/wifi/scan/{iface}", timeout=_SCAN_TIMEOUT)
    except FppError as exc:
        return jsonify({"error": str(exc)}), 502
    if not isinstance(body, dict):
        return jsonify({"error": "The controller returned an unexpected scan result."}), 502

    # Strongest first, one entry per SSID: the same network on two bands or two
    # access points is one choice as far as this page is concerned.
    best = {}
    for entry in body.get("networks") or []:
        ssid = (entry.get("SSID") or "").strip()
        if not ssid:
            continue
        try:
            signal = float(str(entry.get("signal", "")).split()[0])
        except (ValueError, IndexError):
            signal = -999.0
        if ssid not in best or signal > best[ssid]["signal"]:
            best[ssid] = {
                "ssid": ssid,
                "signal": signal,
                "encrypted": bool(entry.get("encrypted")),
            }

    return jsonify({
        "networks": sorted(best.values(), key=lambda n: -n["signal"]),
        "message": body.get("message", ""),
    })


@network_bp.post("/api/network/tethering")
@login_required
def save_tethering():
    """Write the hotspot settings. They are read at boot, so a reboot applies them."""
    data = json_object()

    mode = _str(data, "mode", 2)
    if mode not in TETHER_MODES:
        return jsonify({"error": "Pick one of the listed tethering modes."}), 400

    ssid = _str(data, "ssid", 64)
    psk = _str(data, "psk", 64)
    problem = _check_ssid(ssid, "Hotspot name (SSID)", required=True)
    if problem:
        return jsonify({"error": problem}), 400
    # FPP's hotspot is always WPA-protected, so an empty key would produce an
    # AP nothing can join rather than an open one.
    if not 8 <= len(psk) <= 63:
        return jsonify({"error": "The hotspot password must be 8–63 characters."}), 400

    for key, value in (("TetherSSID", ssid), ("TetherPSK", psk), ("EnableTethering", mode)):
        try:
            resp = requests.put(
                fpp_url(f"/settings/{key}"),
                data=value.encode("utf-8"),
                headers={"Content-Type": "text/plain"},
                timeout=_SAVE_TIMEOUT,
            )
            resp.raise_for_status()
        except Exception as exc:
            current_app.logger.error("Could not write FPP setting %s: %s", key, exc)
            return jsonify({"error": f"Could not save '{key}' on the controller: {exc}"}), 502

    return jsonify({"ok": True})
