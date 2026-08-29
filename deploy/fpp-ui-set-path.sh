#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# fpp-ui-set-path — change the public URL path of the FPP Custom Web UI.
#
#   sudo fpp-ui-set-path cityname     →  http://<pi-ip>/cityname
#
# Installed to /usr/local/sbin/fpp-ui-set-path by fpp_install.sh, which also
# substitutes PLUGIN_DIR below. Runs as root: it rewrites the Apache proxy
# config, re-scopes the CSP override inside FPP's VirtualHost, records the
# choice in .env so upgrades keep it, and gracefully reloads Apache.
#
# The Flask app calls this via sudo when the path is changed from the setup or
# settings page. The app passes only a bare name — never config text — and the
# name is re-validated here, so a compromised session cannot inject Apache
# directives.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PLUGIN_DIR="__PLUGIN_DIR__"

APACHE_CONF="/etc/apache2/conf-enabled/99-fpp-ui.conf"
VHOST_CONF="/etc/apache2/sites-enabled/000-default.conf"
TEMPLATE="$PLUGIN_DIR/deploy/99-fpp-ui.conf"
ENV_FILE="$PLUGIN_DIR/.env"

# Top-level paths owned by FPP itself or by Apache. Mounting the UI over one of
# these would break the stock FPP interface, so they are refused.
RESERVED="api cgi-bin fpp css js images fonts doc docs php uploads media config
          backups logs tmp plugin plugins sequence playlist playlists effects
          models outputs overlays channel events ws remote proxy system status
          settings cache deps themes i18n icons favicon.ico robots.txt"

die() { echo "✗ $*" >&2; exit 1; }

# ── Validate ──────────────────────────────────────────────────────────────────
NAME="${1:-}"
[ -n "$NAME" ] || die "Usage: fpp-ui-set-path <name>   (e.g. fpp-ui-set-path cityname)"

if ! printf '%s' "$NAME" | grep -qE '^[A-Za-z0-9_-]{1,32}$'; then
    die "Invalid name '$NAME'. Use 1-32 characters: letters, numbers, hyphen, underscore."
fi

LOWER=$(printf '%s' "$NAME" | tr '[:upper:]' '[:lower:]')
for word in $RESERVED; do
    [ "$LOWER" = "$word" ] && die "'$NAME' is reserved by FPP — pick a different name."
done

[ -f "$TEMPLATE" ] || die "Missing proxy template at $TEMPLATE."

# ── Back up what we are about to rewrite, so a bad config can be rolled back ──
BACKUP_DIR=$(mktemp -d)
trap 'rm -rf "$BACKUP_DIR"' EXIT
[ -f "$APACHE_CONF" ] && cp "$APACHE_CONF" "$BACKUP_DIR/proxy.conf"
[ -f "$VHOST_CONF" ]  && cp "$VHOST_CONF"  "$BACKUP_DIR/vhost.conf"

restore() {
    [ -f "$BACKUP_DIR/proxy.conf" ] && cp "$BACKUP_DIR/proxy.conf" "$APACHE_CONF"
    [ -f "$BACKUP_DIR/vhost.conf" ]  && cp "$BACKUP_DIR/vhost.conf"  "$VHOST_CONF"
    return 0
}

# ── 1. Render the reverse-proxy config ───────────────────────────────────────
sed "s|__UI_PATH__|$NAME|g" "$TEMPLATE" > "$APACHE_CONF"

# ── 2. Re-scope the CSP override inside FPP's VirtualHost ────────────────────
# FPP's VirtualHost sets a restrictive Content-Security-Policy that blocks
# external images. The override is <Location>-scoped, so it has to move with
# the path or uploaded/remote images break at the new URL.
if [ -f "$VHOST_CONF" ]; then
    UI_PATH="$NAME" python3 - "$VHOST_CONF" << 'PYEOF'
import os, re, sys

path = os.environ["UI_PATH"]
conf = open(sys.argv[1]).read()

# Drop any block we previously injected (at the old path).
conf = re.sub(
    r'\s*# BEGIN fpp-CustomUI CSP override.*?# END fpp-CustomUI CSP override',
    '', conf, flags=re.DOTALL,
)

block = (
    '  # BEGIN fpp-CustomUI CSP override\n'
    '  <Location "/%s/">\n'
    "    Header set Content-Security-Policy \"default-src 'self'; img-src * data: blob:; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; font-src 'self' data:; object-src 'none';\"\n"
    '  </Location>\n'
    '  # END fpp-CustomUI CSP override'
) % path

if '</VirtualHost>' in conf:
    conf = conf.replace('</VirtualHost>', block + '\n</VirtualHost>', 1)
open(sys.argv[1], 'w').write(conf)
PYEOF
fi

# ── 3. Validate before reloading — a broken config takes FPP's UI down too ───
if ! apachectl configtest > /dev/null 2>&1; then
    restore
    die "Apache rejected the generated config — no changes applied."
fi

# ── 4. Record the choice so `git pull` upgrades keep this path ───────────────
if [ -f "$ENV_FILE" ]; then
    if grep -qE '^UI_PATH=' "$ENV_FILE"; then
        sed -i "s|^UI_PATH=.*|UI_PATH=$NAME|" "$ENV_FILE"
    else
        printf '\n# Public URL path for this install (see fpp-ui-set-path).\nUI_PATH=%s\n' "$NAME" >> "$ENV_FILE"
    fi
    chown fpp:fpp "$ENV_FILE"
fi

# ── 5. Graceful reload — finishes in-flight requests, including the one that
#      may have triggered this change from the web UI.
systemctl reload apache2 2>/dev/null || service apache2 reload

PI_IP=$(hostname -I | awk '{print $1}')
echo "✓ Custom UI is now at http://$PI_IP/$NAME"
