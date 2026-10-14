#!/usr/bin/env bash
# Prompts for the Cloudflare API token (input hidden) and stores it where the
# Caddy unit reads it. The token never appears in shell history or chat.
set -euo pipefail
read -rsp "Paste the Cloudflare API token, then press Enter: " T
echo
[ -n "$T" ] || { echo "Empty token, nothing saved."; exit 1; }
umask 037
printf 'CLOUDFLARE_API_TOKEN=%s\n' "$T" > /etc/caddy/cloudflare.env
chgrp caddy /etc/caddy/cloudflare.env
chmod 640 /etc/caddy/cloudflare.env
echo "Saved to /etc/caddy/cloudflare.env"
