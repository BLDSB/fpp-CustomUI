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

# ADMIN_PASSWORD_HASH is intentionally left empty (chosen at first run).

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

# ── 2b. Master PIN — one per install, generated once on first install/upgrade ──
# Covers fresh installs and upgrades of installs that predate the master PIN.
# An existing MASTER_PIN_HASH is never touched, so re-running this is safe. The
# PIN itself is printed once on the closing banner and is not stored anywhere.
MASTER_PIN=$("$PLUGIN_DIR/venv/bin/python" - "$PLUGIN_DIR/.env" << 'PYEOF'
import sys, secrets
import bcrypt
from dotenv import dotenv_values, set_key

env_path = sys.argv[1]
if not dotenv_values(env_path).get('MASTER_PIN_HASH'):
    pin = f"{secrets.randbelow(10000):04d}"
    hashed = bcrypt.hashpw(pin.encode(), bcrypt.gensalt()).decode()
    set_key(env_path, 'MASTER_PIN_HASH', hashed, quote_mode='never')
    print(pin)
PYEOF
)
if [ -n "$MASTER_PIN" ]; then
    echo "✓ Master PIN generated (shown at the end of the install)."
else
    echo "✓ Master PIN already set — keeping it."
fi
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
# cp inherits mktemp's 0600, which leaves the unit unreadable to anyone but
# root — systemd copes, but `systemctl cat` and `systemd-analyze verify` do not.
chmod 644 "$SERVICE_DEST"
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

# ── 6. FPP WiFi tethering fallback ────────────────────────────────────────────
# FPP's default tethering mode ("if no connection") walks every wired interface
# and refuses to start the rescue AP whenever one has carrier. A venue with a
# live switch but no working DHCP therefore leaves the controller with no
# address and no AP — unreachable until someone drives out with a card reader.
# Force tethering on so there is always a way in.
#
# Applied once per controller: the marker means a deliberate later change on
# FPP's Network page is never undone by a plugin upgrade. Takes effect at the
# next boot.
FPP_SETTINGS="/home/fpp/media/settings"
TETHER_MARKER="/var/lib/fpp-ui/tethering-applied"

if [ -f "$TETHER_MARKER" ]; then
    echo "✓ Tethering already configured on this controller — leaving as-is."
elif [ ! -f "$FPP_SETTINGS" ]; then
    echo "⚠ $FPP_SETTINGS not found — skipping tethering fallback."
else
    # Lines in FPP's settings file are `Key = "value"` (WriteSettingToFile() in
    # /opt/fpp/www/common.php). Drop any existing key, then append ours.
    sed -i '/^EnableTethering[[:space:]]*=/d' "$FPP_SETTINGS"
    echo 'EnableTethering = "1"' >> "$FPP_SETTINGS"
    chown fpp:fpp "$FPP_SETTINGS"
    mkdir -p "$(dirname "$TETHER_MARKER")"
    : > "$TETHER_MARKER"
    echo "✓ WiFi tethering enabled — SSID \"FPP\" at http://192.168.8.1/"
fi
echo ""

# ── 7. Fix ownership (venv and new files created as root → hand back to fpp) ──
chown -R fpp:fpp "$PLUGIN_DIR"
echo ""

# ── 8. Start (or restart) the service now that files/ownership are final ─────
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
if [ -n "$MASTER_PIN" ]; then
    echo "╔══════════════════════════════════════════════════════╗"
    echo "║  MASTER PIN — record it now, it is shown only once   ║"
    echo "║                                                      ║"
    printf "║%-54s║
" "  $MASTER_PIN"
    echo "║                                                      ║"
    echo "║  It always logs in, even if the admin PIN is changed.║"
    echo "║  Change it later from Settings (master login only).  ║"
    echo "╚══════════════════════════════════════════════════════╝"
    echo ""
fi
