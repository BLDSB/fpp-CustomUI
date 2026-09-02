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
    echo "✓ Apache proxy config removed."
fi

# Remove the CSP override block we injected into FPP's VirtualHost
VHOST_CONF="/etc/apache2/sites-enabled/000-default.conf"
if [ -f "$VHOST_CONF" ] && grep -qF "# BEGIN fpp-CustomUI CSP override" "$VHOST_CONF"; then
    python3 - "$VHOST_CONF" << 'PYEOF'
import sys, re
conf = open(sys.argv[1]).read()
conf = re.sub(r'\s*# BEGIN fpp-CustomUI CSP override.*?# END fpp-CustomUI CSP override', '', conf, flags=re.DOTALL)
open(sys.argv[1], 'w').write(conf)
print('  ✓ FPP VirtualHost CSP patch removed.')
PYEOF
fi

# Remove the path helper and its sudoers rule
if [ -f "/usr/local/sbin/fpp-ui-set-path" ]; then
    rm -f /usr/local/sbin/fpp-ui-set-path
    echo "✓ Path helper removed."
fi
if [ -f "/etc/sudoers.d/fpp-ui-set-path" ]; then
    rm -f /etc/sudoers.d/fpp-ui-set-path
    echo "✓ Sudoers rule removed."
fi
if [ -f "/etc/logrotate.d/fpp-ui" ]; then
    rm -f /etc/logrotate.d/fpp-ui
    echo "✓ Log rotation config removed."
fi

# Graceful reload is enough to drop our config, and a failure here must not
# abort the uninstall (set -e) — FPP still removes the plugin directory next.
systemctl reload apache2 2>/dev/null || service apache2 reload || \
    echo "⚠ Could not reload Apache — reload it manually: sudo systemctl reload apache2"

echo ""
echo "Uninstall complete. FPP will now remove the plugin directory."
echo ""
