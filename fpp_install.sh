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

echo "▶ Installing / upgrading Python dependencies..."
"$PLUGIN_DIR/venv/bin/pip" install --quiet --upgrade pip
"$PLUGIN_DIR/venv/bin/pip" install --quiet -r "$PLUGIN_DIR/requirements.txt"
echo "✓ Python dependencies installed."
echo ""

# ── 2. .env — only on first install ───────────────────────────────────────────
if [ ! -f "$PLUGIN_DIR/.env" ]; then
    echo "▶ Generating .env and initial admin password..."

    # Generate a random 12-character alphanumeric password
    INIT_PW=$("$PLUGIN_DIR/venv/bin/python" -c \
        "import secrets, string; \
         print(''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12)))")

    # Use Python to write the .env (avoids shell escaping issues with bcrypt '$' signs)
    "$PLUGIN_DIR/venv/bin/python" - "$PLUGIN_DIR" "$INIT_PW" << 'PYEOF'
import sys, os, bcrypt, secrets

plugin_dir = sys.argv[1]
init_pw    = sys.argv[2]

with open(os.path.join(plugin_dir, '.env.example')) as f:
    content = f.read()

secret_key     = secrets.token_hex(32)
internal_token = secrets.token_hex(24)
pw_hash        = bcrypt.hashpw(init_pw.encode(), bcrypt.gensalt()).decode()

content = content.replace('SECRET_KEY=replace-with-a-strong-random-value',
                           f'SECRET_KEY={secret_key}')
content = content.replace('INTERNAL_TOKEN=',
                           f'INTERNAL_TOKEN={internal_token}')
content = content.replace('ADMIN_PASSWORD_HASH=',
                           f'ADMIN_PASSWORD_HASH={pw_hash}')

with open(os.path.join(plugin_dir, '.env'), 'w') as f:
    f.write(content)
PYEOF

    PI_IP=$(hostname -I | awk '{print $1}')
    echo ""
    echo "┌──────────────────────────────────────────────────────┐"
    echo "│  INITIAL LOGIN PASSWORD: $INIT_PW               │"
    echo "│  URL: http://$PI_IP/CustomUI                    │"
    echo "│  Change this password after first login!             │"
    echo "└──────────────────────────────────────────────────────┘"
    echo ""
    echo "✓ .env created."
else
    echo "✓ .env already exists — keeping existing settings."
fi
echo ""

# ── 3. Systemd service ────────────────────────────────────────────────────────
SERVICE_DEST="/etc/systemd/system/fpp-ui.service"
TMP_SERVICE=$(mktemp)
sed "s|/home/fpp/fpp-ui|$PLUGIN_DIR|g" "$PLUGIN_DIR/deploy/fpp-ui.service" > "$TMP_SERVICE"

cp "$TMP_SERVICE" "$SERVICE_DEST"
rm -f "$TMP_SERVICE"
systemctl daemon-reload
systemctl enable fpp-ui
systemctl restart fpp-ui
echo "✓ Systemd service installed and started."
echo ""

# ── 4. Apache2 reverse proxy ──────────────────────────────────────────────────
APACHE_CONF="/etc/apache2/conf-enabled/99-fpp-ui.conf"
cp "$PLUGIN_DIR/deploy/99-fpp-ui.conf" "$APACHE_CONF"
a2enmod proxy proxy_http headers > /dev/null 2>&1 || true

# FPP's Apache injects a restrictive Content-Security-Policy via its VirtualHost
# config that blocks external images. Inject a <Location> block inside FPP's
# VirtualHost (the only place that can override it) to replace the CSP with one
# that allows external img-src. Idempotent — skipped if marker already present.
VHOST_CONF="/etc/apache2/sites-enabled/000-default.conf"
MARKER="# BEGIN fpp-CustomUI CSP override"
if [ -f "$VHOST_CONF" ] && ! grep -qF "$MARKER" "$VHOST_CONF"; then
    python3 - "$VHOST_CONF" << PYEOF
import sys, re
conf = open(sys.argv[1]).read()
block = '''  $MARKER
  <Location "/CustomUI/">
    Header set Content-Security-Policy "default-src 'self'; img-src * data: blob:; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; connect-src 'self'; font-src 'self' data:; object-src 'none';"
  </Location>
  # END fpp-CustomUI CSP override'''
open(sys.argv[1], 'w').write(conf.replace('</VirtualHost>', block + '\n</VirtualHost>', 1))
print('  ✓ FPP VirtualHost patched for CSP.')
PYEOF
fi

service apache2 restart
echo "✓ Apache reverse proxy configured at /CustomUI."
echo ""

# ── 5. Fix ownership (venv and new files created as root → hand back to fpp) ──
chown -R fpp:fpp "$PLUGIN_DIR"
echo ""

PI_IP=$(hostname -I | awk '{print $1}')
echo "╔══════════════════════════════════════════════════════╗"
echo "║  Installation complete!                              ║"
echo "║                                                      ║"
echo "║  Open http://$PI_IP/CustomUI in a browser     ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
