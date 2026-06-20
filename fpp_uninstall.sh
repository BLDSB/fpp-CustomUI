#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# fpp_uninstall.sh — FPP plugin uninstaller for Custom Web UI
#
# Run automatically as root by FPP before the plugin directory is deleted.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║        FPP Custom Web UI — Uninstall                 ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ── 1. Stop and remove systemd service ────────────────────────────────────────
if systemctl is-active --quiet fpp-ui 2>/dev/null; then
    systemctl stop fpp-ui
    echo "✓ Service stopped."
fi
if systemctl is-enabled --quiet fpp-ui 2>/dev/null; then
    systemctl disable fpp-ui
    echo "✓ Service disabled."
fi
if [ -f "/etc/systemd/system/fpp-ui.service" ]; then
    rm -f /etc/systemd/system/fpp-ui.service
    systemctl daemon-reload
    echo "✓ Service file removed."
fi

# ── 2. Remove Apache reverse proxy ────────────────────────────────────────────
if [ -f "/etc/apache2/conf-enabled/99-fpp-ui.conf" ]; then
    rm -f /etc/apache2/conf-enabled/99-fpp-ui.conf
    service apache2 restart
    echo "✓ Apache proxy config removed."
fi

echo ""
echo "Uninstall complete. FPP will now remove the plugin directory."
echo ""
