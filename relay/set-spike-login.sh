#!/usr/bin/env bash
# Prompts for a temporary login (password hidden), stores only its hash, then
# installs /root/Caddyfile.spike and reloads Caddy. Run as root on the relay.
set -euo pipefail
read -rp "Temporary username: " U
read -rsp "Temporary password: " P
echo
[ -n "$U" ] && [ -n "$P" ] || { echo "Empty value, nothing changed."; exit 1; }
H=$(caddy hash-password --plaintext "$P")
umask 037
printf 'SPIKE_USER=%s\nSPIKE_HASH=%s\n' "$U" "$H" > /etc/caddy/spike.env
chgrp caddy /etc/caddy/spike.env; chmod 640 /etc/caddy/spike.env
cat > /etc/systemd/system/caddy.service.d/spike.conf <<'CONF'
[Service]
EnvironmentFile=/etc/caddy/spike.env
CONF
systemctl daemon-reload
cp /root/Caddyfile.spike /etc/caddy/Caddyfile
systemctl restart caddy
sleep 3
systemctl is-active caddy
