#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# fpp_reset_pin.sh — clear the saved PIN(s) and re-run first-run setup.
#
# For when the PIN to a headless/kiosk controller is lost. Clears the stored
# hashes so the UI drops back to the setup page, where the first visitor on the
# local network chooses a new PIN.
#
# Usage:  sudo bash fpp_reset_pin.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$PLUGIN_DIR/.env"

if [ "$(id -u)" -ne 0 ]; then
    echo "This script restarts the fpp-ui service — please run it with sudo:"
    echo "    sudo bash $0"
    exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
    echo "✗ No .env found at $ENV_FILE — is the plugin installed?"
    exit 1
fi

echo "▶ Clearing saved PINs in $ENV_FILE ..."

sed -i 's|^ADMIN_PASSWORD_HASH=.*|ADMIN_PASSWORD_HASH=|' "$ENV_FILE"
sed -i 's|^MASTER_PIN_HASH=.*|MASTER_PIN_HASH=|' "$ENV_FILE"

# Older installs predate MASTER_PIN_HASH — add it so the key always exists.
grep -q '^MASTER_PIN_HASH=' "$ENV_FILE" || echo 'MASTER_PIN_HASH=' >> "$ENV_FILE"

# sed -i rewrites the file as root; the service runs as fpp and must be able to
# write its own PIN back, so hand ownership back (and keep secrets owner-only).
chown fpp:fpp "$ENV_FILE"
chmod 600 "$ENV_FILE"

systemctl restart fpp-ui
sleep 2

PI_IP=$(hostname -I | awk '{print $1}')

# Report the path this install is actually served at, not the default.
UI_PATH="CustomUI"
if grep -qE '^UI_PATH=' "$ENV_FILE"; then
    CONFIGURED=$(grep -E '^UI_PATH=' "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -dc 'A-Za-z0-9_-')
    [ -n "$CONFIGURED" ] && UI_PATH="$CONFIGURED"
fi

if systemctl is-active --quiet fpp-ui; then
    echo ""
    echo "✓ PIN cleared and service restarted."
    echo ""
    echo "  Open http://$PI_IP/$UI_PATH to choose a new PIN."
    echo ""
else
    echo "✗ fpp-ui did not come back up. Check: journalctl -u fpp-ui -n 30"
    exit 1
fi
