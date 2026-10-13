#!/bin/sh
# Writes /etc/wireguard/bld.conf from the Pi's own private key and brings the
# tunnel up. Args: <tunnel-ip> <hub-public-key> <hub-endpoint>. Run as root.
set -eu
IP=$1; HUB_PUB=$2; ENDPOINT=$3
umask 077
cat > /etc/wireguard/bld.conf <<CONF
[Interface]
PrivateKey = $(cat /etc/wireguard/bld.key)
Address = $IP/32

[Peer]
PublicKey = $HUB_PUB
Endpoint = $ENDPOINT
AllowedIPs = 10.99.0.1/32
PersistentKeepalive = 25
CONF
systemctl enable wg-quick@bld >/dev/null 2>&1
systemctl restart wg-quick@bld
