#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# fpp_install.sh — FPP plugin installer / upgrader for Custom Web UI
#
# Run automatically by FPP after:
#   • git clone  (fresh install)
#   • git pull   (upgrade)
#
# This script is invoked as root by FPP's plugin system.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║        FPP Custom Web UI — Install / Upgrade         ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "Plugin directory: $PLUGIN_DIR"

# ── 1. Python venv ─────────────────────────────────────────────────────────────
if [ ! -d "$PLUGIN_DIR/venv" ]; then
    echo "▶ Creating Python virtual environment..."
    python3 -m venv "$PLUGIN_DIR/venv"
else
    echo "✓ Virtual environment already exists — upgrading dependencies."
fi

# FPP runs this script as root but leaves HOME=/home/fpp, so pip finds a cache
# directory owned by fpp, warns, and disables caching. Point it at a root-owned
# cache outside the plugin dir (which is chowned to fpp at step 6) instead.
export PIP_CACHE_DIR=/var/cache/fpp-ui-pip
mkdir -p "$PIP_CACHE_DIR" 2>/dev/null || true   # cache is an optimisation, never fatal

echo "▶ Installing / upgrading Python dependencies..."
"$PLUGIN_DIR/venv/bin/pip" install --quiet --upgrade pip
"$PLUGIN_DIR/venv/bin/pip" install --quiet -r "$PLUGIN_DIR/requirements.txt"
echo "✓ Python dependencies installed."
echo ""

# ── 2. .env — only on first install ──────────────────────────────
if [ ! -f "$PLUGIN_DIR/.env" ]; then
    echo "▶ Generating .env..."

    # No admin PIN is created here. The controller ships unprovisioned and the
    # first visitor on the local network chooses a PIN via the setup page — so
    # no credential ever has to be read off this output and typed in by hand.
    "$PLUGIN_DIR/venv/bin/python" - "$PLUGIN_DIR" << 'PYEOF'
import sys, os, secrets

plugin_dir = sys.argv[1]

with open(os.path.join(plugin_dir, '.env.example')) as f:
    content = f.read()

content = content.replace('SECRET_KEY=replace-with-a-strong-random-value',
                           f'SECRET_KEY={secrets.token_hex(32)}')
content = content.replace('INTERNAL_TOKEN=',
                           f'INTERNAL_TOKEN={secrets.token_hex(24)}')

# ADMIN_PASSWORD_HASH and MASTER_PIN_HASH are intentionally left empty.

with open(os.path.join(plugin_dir, '.env'), 'w') as f:
    f.write(content)
PYEOF

    echo "✓ .env created."
else
    echo "✓ .env already exists — keeping existing settings."
fi
# .env holds the session secret and internal token — keep it owner-only.
chmod 600 "$PLUGIN_DIR/.env"
echo ""

# ── 3. Systemd service ────────────────────────────────────────────────────────
# Installed and enabled here; restarted at the END of this script, after file
# ownership is handed back to fpp — restarting first would race the chown and
# a restart failure would abort the rest of the install half-done.
SERVICE_DEST="/etc/systemd/system/fpp-ui.service"
TMP_SERVICE=$(mktemp)
sed "s|/home/fpp/fpp-ui|$PLUGIN_DIR|g" "$PLUGIN_DIR/deploy/fpp-ui.service" > "$TMP_SERVICE"

cp "$TMP_SERVICE" "$SERVICE_DEST"
rm -f "$TMP_SERVICE"
systemctl daemon-reload
systemctl enable fpp-ui
echo "✓ Systemd service installed."

# Rotate fpp-ui.log so it can never fill the SD card on a long-running unit.
if [ -d /etc/logrotate.d ]; then
    sed "s|/home/fpp/fpp-ui|$PLUGIN_DIR|g" "$PLUGIN_DIR/deploy/fpp-ui.logrotate" \
        > /etc/logrotate.d/fpp-ui
    echo "✓ Log rotation installed."
else
    echo "⚠ /etc/logrotate.d not found — fpp-ui.log will grow unbounded."
fi
echo ""

# ── 4. URL path for this install ──────────────────────────────────────────────
# Each deployment can be served at its own path (e.g. /cityname, /bankname).
# The choice lives in .env so it survives `git pull` upgrades. Fresh installs
# start at /CustomUI; the path is then set from the first-run setup page, the
# Settings page, or `sudo fpp-ui-set-path <name>`.
UI_PATH="CustomUI"
if [ -f "$PLUGIN_DIR/.env" ] && grep -qE '^UI_PATH=' "$PLUGIN_DIR/.env"; then
    EXISTING=$(grep -E '^UI_PATH=' "$PLUGIN_DIR/.env" | tail -1 | cut -d= -f2- | tr -d '"'"'"' \r')
    if printf '%s' "$EXISTING" | grep -qE '^[A-Za-z0-9_-]{1,32}$'; then
        UI_PATH="$EXISTING"
    else
        echo "⚠ Ignoring invalid UI_PATH in .env — falling back to /CustomUI."
    fi
fi

# ── 5. Apache2 reverse proxy ──────────────────────────────────────────────────
a2enmod proxy proxy_http headers > /dev/null 2>&1 || true

# Install the path helper as root-owned, outside the plugin directory (which is
# chowned to fpp below) so that granting fpp sudo on it is not a way to become
# root by editing it.
SETPATH_BIN="/usr/local/sbin/fpp-ui-set-path"
sed "s|__PLUGIN_DIR__|$PLUGIN_DIR|g" "$PLUGIN_DIR/deploy/fpp-ui-set-path.sh" > "$SETPATH_BIN"
chown root:root "$SETPATH_BIN"
chmod 755 "$SETPATH_BIN"

# Let the web UI (running as fpp) change the path without a password prompt.
SUDOERS="/etc/sudoers.d/fpp-ui-set-path"
echo "fpp ALL=(root) NOPASSWD: $SETPATH_BIN" > "$SUDOERS"
chmod 0440 "$SUDOERS"
if ! visudo -cf "$SUDOERS" > /dev/null 2>&1; then
    rm -f "$SUDOERS"
    echo "⚠ Could not install sudoers rule — path changes will need SSH."
fi

# Renders the proxy config, re-scopes the CSP override, and reloads Apache.
"$SETPATH_BIN" "$UI_PATH"
echo ""

# ── 6. Fix ownership (venv and new files created as root → hand back to fpp) ──
chown -R fpp:fpp "$PLUGIN_DIR"
echo ""

# ── 7. Start (or restart) the service now that files/ownership are final ─────
if systemctl restart fpp-ui; then
    echo "✓ fpp-ui service started."
else
    echo "⚠ fpp-ui did not start — check: journalctl -u fpp-ui -n 30"
    echo "  (Install finished; the service will retry after: systemctl restart fpp-ui)"
fi
echo ""

# Network may not be up yet on a fresh boot — never let the banner kill the install.
PI_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
PI_IP="${PI_IP:-<pi-ip>}"
BANNER="  Open http://$PI_IP/$UI_PATH in a browser"
echo "╔══════════════════════════════════════════════════════╗"
echo "║  Installation complete!                              ║"
echo "║                                                      ║"
printf "║%-54s║\n" "$BANNER"
echo "║  to choose your PIN and finish setup.                ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
