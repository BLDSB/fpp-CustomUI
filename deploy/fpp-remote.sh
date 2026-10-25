#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# fpp-remote — join or leave the BLD remote-access tunnel.
#
#   sudo fpp-remote status
#   sudo fpp-remote enroll <code>      code from the BLD portal (single use)
#   sudo fpp-remote disconnect
#
# Installed to /usr/local/sbin/fpp-remote by fpp_install.sh. Runs as root; the
# Settings page calls it through a narrow sudo rule. This is a trust boundary, so:
#   * the portal address comes from /etc/fpp-remote.conf (root-owned), never from
#     the caller, so a pasted code can only ever be sent to the real portal;
#   * the code is re-validated here, and every value the portal returns is
#     checked before it reaches a config file;
#   * output is one JSON object on stdout; failures are one line on stderr.
#
# The WireGuard private key is made on this device and never leaves it.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

CONF_FILE="/etc/fpp-remote.conf"
STATE_DIR="/var/lib/fpp-remote"
STATE="$STATE_DIR/state.json"
IFACE="bld"
WG_CONF="/etc/wireguard/${IFACE}.conf"
WG_KEY="/etc/wireguard/bld.key"
DEFAULT_PORTAL="https://portal.bldlightingcontrol.com"

die() { echo "$*" >&2; exit 1; }

portal_url() {
    local url=""
    if [ -f "$CONF_FILE" ]; then
        url="$(grep -E '^PORTAL_URL=' "$CONF_FILE" | tail -1 | cut -d= -f2- | tr -d "\"' \r")"
    fi
    if printf '%s' "$url" | grep -qE '^https://[A-Za-z0-9.-]+$'; then
        printf '%s' "$url"
    else
        printf '%s' "$DEFAULT_PORTAL"
    fi
}

# ── status ─────────────────────────────────────────────────────────────────────
cmd_status() {
    local enrolled=0 age=-1
    [ -f "$WG_CONF" ] && enrolled=1
    if [ "$enrolled" = 1 ]; then
        local hs
        hs="$(wg show "$IFACE" latest-handshakes 2>/dev/null | awk '{print $2}' | head -1 || true)"
        if [ -n "${hs:-}" ] && [ "$hs" != "0" ]; then
            age=$(( $(date +%s) - hs ))
        fi
    fi
    ENROLLED="$enrolled" AGE="$age" STATE_FILE="$STATE" python3 - <<'PY'
import json, os

state = {}
try:
    with open(os.environ["STATE_FILE"]) as fh:
        state = json.load(fh)
except (OSError, ValueError):
    pass
age = int(os.environ["AGE"])
print(json.dumps({
    "enrolled": os.environ["ENROLLED"] == "1",
    "connected": 0 <= age < 180,          # keepalive is 25 s, so >3 min means it is down
    "handshake_age": age if age >= 0 else None,
    "slug": state.get("slug"),
    "address": state.get("address"),
    "ui_path": state.get("ui_path"),
}))
PY
}

# ── enroll ─────────────────────────────────────────────────────────────────────
cmd_enroll() {
    local code="${1:-}"
    printf '%s' "$code" | grep -qE '^[A-Za-z0-9_-]{20,64}$' \
        || die "That does not look like an enrolment code. Copy it again from the portal."

    local need=""
    command -v wg >/dev/null 2>&1 || need="$need wireguard-tools"
    command -v curl >/dev/null 2>&1 || need="$need curl"
    command -v python3 >/dev/null 2>&1 || need="$need python3"
    if [ -n "$need" ]; then
        apt-get update -qq >/dev/null 2>&1 || true
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $need >/dev/null 2>&1 \
            || die "Could not install$need. Is the controller online?"
    fi

    umask 077
    install -d -m 700 /etc/wireguard
    [ -s "$WG_KEY" ] || wg genkey > "$WG_KEY"
    local pub
    pub="$(wg pubkey < "$WG_KEY")"

    local portal resp
    portal="$(portal_url)"
    resp="$(curl -sS --proto '=https' --max-time 30 -H 'Content-Type: application/json' \
            -d "$(printf '{"token":"%s","public_key":"%s"}' "$code" "$pub")" \
            -w '\n%{http_code}' "$portal/api/enroll" 2>/dev/null)" \
        || die "Could not reach the portal. Check this controller's internet connection."
    local status="${resp##*$'\n'}"
    resp="${resp%$'\n'*}"
    if [ "$status" != "200" ]; then
        local msg
        msg="$(printf '%s' "$resp" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("error",""))' 2>/dev/null || true)"
        die "${msg:-The portal did not accept that code (HTTP $status).}"
    fi

    # Validate every field before it goes near a config file.
    local fields
    fields="$(printf '%s' "$resp" | python3 -c '
import json, re, shlex, sys
d = json.load(sys.stdin)
rules = {
    "slug": r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?",
    "tunnel_ip": r"10\.\d{1,3}\.0\.(?:[2-9]|[1-9]\d|1\d\d|2[0-4]\d|25[0-4])",
    "hub_public_key": r"[A-Za-z0-9+/]{43}=",
    "endpoint": r"[A-Za-z0-9.-]{1,100}:\d{2,5}",
    "ui_path": r"/[A-Za-z0-9_-]{1,32}",
    "site_domain": r"[A-Za-z0-9.-]{3,100}",
    "hub_ip": r"10\.\d{1,3}\.0\.1",
    "heartbeat_port": r"\d{4,5}",
}
# A portal that predates these two fields is the live one.
defaults = {"hub_ip": "10.99.0.1", "heartbeat_port": "8081"}
for key, pattern in rules.items():
    value = str(d.get(key, defaults.get(key, "")))
    if not re.fullmatch(pattern, value):
        sys.exit("bad value for " + key)
    print("R_%s=%s" % (key.upper(), shlex.quote(value)))
' 2>/dev/null)" || die "The portal sent settings this controller does not trust. Nothing was changed."
    eval "$fields"

    cat > "$WG_CONF" <<CONFIG
[Interface]
PrivateKey = $(cat "$WG_KEY")
Address = ${R_TUNNEL_IP}/32

[Peer]
PublicKey = ${R_HUB_PUBLIC_KEY}
Endpoint = ${R_ENDPOINT}
AllowedIPs = ${R_HUB_IP}/32
PersistentKeepalive = 25
CONFIG
    chmod 600 "$WG_CONF"

    install -d /etc/systemd/system/wg-quick@${IFACE}.service.d
    cat > /etc/systemd/system/wg-quick@${IFACE}.service.d/retry.conf <<UNIT
[Service]
Restart=on-failure
RestartSec=15
UNIT
    systemctl daemon-reload
    systemctl enable "wg-quick@${IFACE}" >/dev/null 2>&1
    systemctl restart "wg-quick@${IFACE}" || die "The tunnel could not start. Check the controller's network."

    local address="https://${R_SLUG}.${R_SITE_DOMAIN}"
    install -d -m 755 "$STATE_DIR"
    SLUG="$R_SLUG" ADDRESS="$address" TUNNEL="$R_TUNNEL_IP" HUB="$R_HUB_IP" HBPORT="$R_HEARTBEAT_PORT" UIPATH="$R_UI_PATH" STATE_FILE="$STATE" \
        python3 - <<'PY'
import json, os
with open(os.environ["STATE_FILE"], "w") as fh:
    json.dump({"slug": os.environ["SLUG"], "address": os.environ["ADDRESS"],
               "tunnel_ip": os.environ["TUNNEL"], "hub_ip": os.environ["HUB"],
               "heartbeat_port": int(os.environ["HBPORT"]), "ui_path": os.environ["UIPATH"]}, fh)
os.chmod(os.environ["STATE_FILE"], 0o644)
PY

    # The relay applies new devices within about ten seconds; give it a little longer.
    local connected=false hs
    for _ in $(seq 1 25); do
        hs="$(wg show "$IFACE" latest-handshakes 2>/dev/null | awk '{print $2}' | head -1 || true)"
        if [ -n "${hs:-}" ] && [ "$hs" != "0" ]; then connected=true; break; fi
        sleep 2
    done

    ADDRESS="$address" SLUG="$R_SLUG" UIPATH="$R_UI_PATH" CONNECTED="$connected" python3 - <<'PY'
import json, os
print(json.dumps({"ok": True, "slug": os.environ["SLUG"], "address": os.environ["ADDRESS"],
                  "ui_path": os.environ["UIPATH"],
                  "connected": os.environ["CONNECTED"] == "true"}))
PY
}

# ── disconnect ───────────────────────────────────────────────────────────────────
cmd_disconnect() {
    systemctl disable --now "wg-quick@${IFACE}" >/dev/null 2>&1 || true
    rm -f "$WG_CONF" "$STATE"
    echo '{"ok": true}'
}

case "${1:-}" in
    status)     cmd_status ;;
    enroll)     shift; cmd_enroll "${1:-}" ;;
    disconnect) cmd_disconnect ;;
    *)          die "Usage: fpp-remote status | enroll <code> | disconnect" ;;
esac
