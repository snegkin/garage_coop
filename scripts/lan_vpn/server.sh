#!/bin/sh
# VPN (WireGuard) от сервера сайта до роутера кооператива — чтобы схема
# территории видела локальную сеть (в сети ли устройства, настройка камер).
# Запускать на сервере от root.
#
#   1) sh server.sh init            — ставит WireGuard, создаёт ключи и
#                                     интерфейс wg-coop, печатает ключ сервера;
#   2) на роутере: sh openwrt.sh <ключ сервера> <IP сервера>  — печатает ключ роутера;
#   3) sh server.sh peer <ключ роутера> <подсеть кооператива, напр. 192.168.2.0/24>
#
# Потом в env кооператива: GARAGE_MAP_LAN_SUBNETS=192.168.2.0/24 и перезапуск сервиса.
set -eu
CONF=/etc/wireguard/wg-coop.conf
PORT=51820

case "${1:-}" in
init)
    command -v wg >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y wireguard-tools
    umask 077
    mkdir -p /etc/wireguard
    [ -f /etc/wireguard/wg-coop.key ] || wg genkey > /etc/wireguard/wg-coop.key
    if [ ! -f "$CONF" ]; then
        cat > "$CONF" <<CONF
[Interface]
Address = 10.77.0.1/24
ListenPort = $PORT
PostUp = wg set %i private-key /etc/wireguard/wg-coop.key
CONF
    fi
    command -v ufw >/dev/null && ufw allow $PORT/udp comment "WireGuard wg-coop"
    systemctl enable --now wg-quick@wg-coop
    echo "Ключ сервера: $(wg pubkey < /etc/wireguard/wg-coop.key)"
    ;;
peer)
    KEY="${2:?ключ роутера}"; NET="${3:?подсеть кооператива}"
    grep -q "$KEY" "$CONF" && { echo "Этот роутер уже добавлен"; exit 0; }
    cp "$CONF" "$CONF.bak-$(date +%F-%H%M)"
    cat >> "$CONF" <<CONF

[Peer]
# роутер кооператива
PublicKey = $KEY
AllowedIPs = 10.77.0.2/32, $NET
CONF
    systemctl restart wg-quick@wg-coop
    sleep 5
    wg show wg-coop
    ;;
*)
    echo "использование: $0 init | peer <ключ роутера> <подсеть>"; exit 1 ;;
esac
