#!/usr/bin/env bash
# Base setup for the BLD remote-access relay (Debian 13): SSH hardening,
# firewall, fail2ban, swap, and the WireGuard hub. Idempotent; run as root.
# Caddy and the portal are added separately once DNS is in place.
set -euo pipefail

WG_IF=wg0
WG_NET=10.99.0.1/24
WG_PORT=51820

apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq wireguard fail2ban ufw unattended-upgrades

# --- SSH: keys only ---------------------------------------------------------
cat > /etc/ssh/sshd_config.d/00-relay-hardening.conf <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
CONF
sshd -t
systemctl reload ssh

# --- Swap: safety net on a 1 GB box ----------------------------------------
if ! swapon --show | grep -q /swapfile; then
    fallocate -l 1G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# --- Firewall: SSH + WireGuard only until Caddy goes in ---------------------
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow "${WG_PORT}/udp"
ufw --force enable

# --- fail2ban for sshd ------------------------------------------------------
cat > /etc/fail2ban/jail.d/sshd.local <<'CONF'
[sshd]
enabled = true
maxretry = 5
bantime = 1h
CONF
systemctl enable --now fail2ban
systemctl restart fail2ban

# --- WireGuard hub ----------------------------------------------------------
# No IP forwarding: peers can reach the hub, never each other.
umask 077
mkdir -p /etc/wireguard
[ -f /etc/wireguard/server.key ] || wg genkey > /etc/wireguard/server.key
wg pubkey < /etc/wireguard/server.key > /etc/wireguard/server.pub
if [ ! -f /etc/wireguard/${WG_IF}.conf ]; then
    cat > /etc/wireguard/${WG_IF}.conf <<CONF
[Interface]
Address = ${WG_NET}
ListenPort = ${WG_PORT}
PrivateKey = $(cat /etc/wireguard/server.key)
# Peers are added by the enrol tool, one [Peer] per Pi.
CONF
fi
systemctl enable --now wg-quick@${WG_IF}

echo "== relay base ready =="
echo "WireGuard hub public key: $(cat /etc/wireguard/server.pub)"
