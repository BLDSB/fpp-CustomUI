#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# FPP Custom Web UI — one-shot setup script
#
# Run from the project root on the Raspberry Pi:
#   bash scripts/setup.sh
#
# What it does:
#   1. Creates a Python venv and installs dependencies
#   2. Generates a .env file with random SECRET_KEY and INTERNAL_TOKEN
#   3. Prompts for an admin password and stores its bcrypt hash in .env
#   4. Installs and enables the systemd service
#
# Safe to re-run — it skips steps that are already done.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Resolve the project directory (where this script lives/../) ───────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║        FPP Custom Web UI — Setup                     ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "Project directory: $PROJECT_DIR"
echo ""

# ── 1. Python venv ─────────────────────────────────────────────────────────────
if [ ! -d "$PROJECT_DIR/venv" ]; then
    echo "▶ Creating Python virtual environment..."
    python3 -m venv "$PROJECT_DIR/venv"
else
    echo "✓ Virtual environment already exists."
fi

echo "▶ Installing / upgrading dependencies..."
"$PROJECT_DIR/venv/bin/pip" install --quiet --upgrade pip
"$PROJECT_DIR/venv/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt"
echo "✓ Dependencies installed."
echo ""

# ── 2. .env file ──────────────────────────────────────────────────────────────
if [ -f "$PROJECT_DIR/.env" ]; then
    echo "✓ .env already exists — skipping generation."
    echo "  (Delete .env and re-run to regenerate secrets.)"
else
    echo "▶ Generating .env from .env.example..."
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"

    # Generate SECRET_KEY
    SECRET_KEY=$("$PROJECT_DIR/venv/bin/python" -c "import secrets; print(secrets.token_hex(32))")
    sed -i "s|SECRET_KEY=replace-with-a-strong-random-value|SECRET_KEY=$SECRET_KEY|" "$PROJECT_DIR/.env"

    # Generate INTERNAL_TOKEN
    INTERNAL_TOKEN=$("$PROJECT_DIR/venv/bin/python" -c "import secrets; print(secrets.token_hex(24))")
    sed -i "s|INTERNAL_TOKEN=\$|INTERNAL_TOKEN=$INTERNAL_TOKEN|" "$PROJECT_DIR/.env"

    echo "✓ .env created with generated SECRET_KEY and INTERNAL_TOKEN."
fi
echo ""

# ── 3. Admin password ─────────────────────────────────────────────────────────
# Check if ADMIN_PASSWORD_HASH is already set in .env
if grep -q "^ADMIN_PASSWORD_HASH=.\+" "$PROJECT_DIR/.env"; then
    echo "✓ ADMIN_PASSWORD_HASH already set in .env — skipping."
else
    echo "▶ Set admin password"
    echo "  This is the password you will use to log in to the web UI."
    echo ""
    while true; do
        read -r -s -p "  Enter password: " PASSWORD
        echo ""
        read -r -s -p "  Confirm password: " PASSWORD2
        echo ""
        if [ "$PASSWORD" = "$PASSWORD2" ]; then
            break
        fi
        echo "  ✗ Passwords do not match. Try again."
        echo ""
    done

    HASH=$("$PROJECT_DIR/venv/bin/python" -c "
import bcrypt, sys
pw = sys.argv[1].encode()
print(bcrypt.hashpw(pw, bcrypt.gensalt()).decode())
" "$PASSWORD")

    # Replace the empty ADMIN_PASSWORD_HASH= line
    sed -i "s|^ADMIN_PASSWORD_HASH=\$|ADMIN_PASSWORD_HASH=$HASH|" "$PROJECT_DIR/.env"
    echo "✓ Password hash written to .env."
fi
echo ""

# ── 4. FPP settings reminder ──────────────────────────────────────────────────
echo "────────────────────────────────────────────────────────"
echo "  If FPP is not running on this same device, update:"
echo ""
echo "  FPP_BASE_URL — FPP API URL (default: http://localhost/api)"
echo ""
echo "  Edit with:  nano $PROJECT_DIR/.env"
echo "────────────────────────────────────────────────────────"
echo ""

# ── 5. Systemd service ────────────────────────────────────────────────────────
SERVICE_SRC="$PROJECT_DIR/deploy/fpp-ui.service"
SERVICE_DEST="/etc/systemd/system/fpp-ui.service"

# Patch the service file with the actual project directory before installing
TMP_SERVICE=$(mktemp)
sed "s|/home/fpp/fpp-ui|$PROJECT_DIR|g" "$SERVICE_SRC" > "$TMP_SERVICE"

if [ -f "$SERVICE_DEST" ]; then
    echo "✓ Systemd service already installed."
    echo "  To reinstall: sudo cp $TMP_SERVICE $SERVICE_DEST && sudo systemctl daemon-reload"
else
    echo "▶ Installing systemd service..."
    if sudo cp "$TMP_SERVICE" "$SERVICE_DEST" && sudo systemctl daemon-reload; then
        sudo systemctl enable fpp-ui
        sudo systemctl restart fpp-ui
        echo "✓ Service installed, enabled, and started."
    else
        echo "  ✗ Could not install service (no sudo?). To install manually:"
        echo "    sudo cp $TMP_SERVICE $SERVICE_DEST"
        echo "    sudo systemctl daemon-reload"
        echo "    sudo systemctl enable fpp-ui"
        echo "    sudo systemctl start fpp-ui"
    fi
fi
rm -f "$TMP_SERVICE"
echo ""

# ── 6. Apache2 reverse proxy ──────────────────────────────────────────────────
# The proxy config is a template — fpp-ui-set-path renders it for the path this
# install is served at (UI_PATH in .env, default CustomUI).
UI_PATH="CustomUI"
if [ -f "$PROJECT_DIR/.env" ] && grep -qE '^UI_PATH=' "$PROJECT_DIR/.env"; then
    EXISTING=$(grep -E '^UI_PATH=' "$PROJECT_DIR/.env" | tail -1 | cut -d= -f2- | tr -d '"'"'"' \r')
    if printf '%s' "$EXISTING" | grep -qE '^[A-Za-z0-9_-]{1,32}$'; then
        UI_PATH="$EXISTING"
    fi
fi

SETPATH_BIN="/usr/local/sbin/fpp-ui-set-path"
echo "▶ Installing Apache2 reverse proxy (http://<pi-ip>/$UI_PATH)..."

install_proxy() {
    sudo sed "s|__PLUGIN_DIR__|$PROJECT_DIR|g" \
        "$PROJECT_DIR/deploy/fpp-ui-set-path.sh" | sudo tee "$SETPATH_BIN" > /dev/null || return 1
    sudo chown root:root "$SETPATH_BIN" || return 1
    sudo chmod 755 "$SETPATH_BIN" || return 1
    sudo a2enmod proxy proxy_http headers > /dev/null 2>&1 || true
    sudo "$SETPATH_BIN" "$UI_PATH" || return 1

    # Let the web UI change the path later without a password prompt.
    echo "fpp ALL=(root) NOPASSWD: $SETPATH_BIN" | sudo tee /etc/sudoers.d/fpp-ui-set-path > /dev/null
    sudo chmod 0440 /etc/sudoers.d/fpp-ui-set-path
    sudo visudo -cf /etc/sudoers.d/fpp-ui-set-path > /dev/null 2>&1 \
        || sudo rm -f /etc/sudoers.d/fpp-ui-set-path
    return 0
}

if install_proxy; then
    echo "✓ Apache2 config installed and reloaded."
else
    echo "  ✗ Could not configure Apache2 (no sudo?). To install manually:"
    echo "    sudo sed 's|__PLUGIN_DIR__|$PROJECT_DIR|g' $PROJECT_DIR/deploy/fpp-ui-set-path.sh > $SETPATH_BIN"
    echo "    sudo chmod 755 $SETPATH_BIN"
    echo "    sudo a2enmod proxy proxy_http headers"
    echo "    sudo $SETPATH_BIN $UI_PATH"
fi
echo ""

# ── Done ──────────────────────────────────────────────────────────────────────
PI_IP=$(hostname -I | awk '{print $1}')
echo "╔══════════════════════════════════════════════════════╗"
echo "║  Setup complete!                                     ║"
echo "║                                                      ║"
BANNER="  Open http://$PI_IP/$UI_PATH in a browser"
echo "║$(printf '%-54s' "$BANNER")║"
echo "║  (Also available at http://$PI_IP:5000)       ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
