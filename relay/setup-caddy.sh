#!/usr/bin/env bash
# Install Caddy (official repo) plus the Cloudflare DNS plugin on the relay.
# Caddy is enabled but not started until /etc/caddy/cloudflare.env holds the
# API token (CLOUDFLARE_API_TOKEN=...), because the wildcard certificate is
# issued with the DNS-01 challenge. Idempotent; run as root.
set -euo pipefail

apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gpg
if [ ! -f /usr/share/keyrings/caddy-stable-archive-keyring.gpg ]; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        > /etc/apt/sources.list.d/caddy-stable.list
fi
apt-get update -qq
apt-get install -y -qq caddy

if ! caddy list-modules | grep -q dns.providers.cloudflare; then
    systemctl stop caddy || true
    caddy add-package github.com/caddy-dns/cloudflare
fi

install -d -m 750 -o root -g caddy /etc/caddy
if [ ! -f /etc/caddy/cloudflare.env ]; then
    install -m 640 -o root -g caddy /dev/null /etc/caddy/cloudflare.env
fi

# Pass the token to Caddy through the unit, not the Caddyfile.
install -d /etc/systemd/system/caddy.service.d
cat > /etc/systemd/system/caddy.service.d/cloudflare.conf <<'CONF'
[Service]
EnvironmentFile=/etc/caddy/cloudflare.env
CONF
systemctl daemon-reload
systemctl enable caddy
systemctl stop caddy || true

ufw allow 80/tcp
ufw allow 443/tcp
echo "== caddy installed (not started) =="
caddy version
