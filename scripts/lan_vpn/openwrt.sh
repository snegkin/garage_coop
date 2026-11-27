#!/bin/sh
# Роутер кооператива (OpenWrt) — клиент VPN до сервера сайта.
# Запускать на роутере от root:
#   sh openwrt.sh <ключ сервера> <IP или домен сервера> [адрес в туннеле, по умолчанию 10.77.0.2]
# Адрес в туннеле у каждого роутера свой (10.77.0.2, .3, …) — тот же, что
# в lan_vpn_peers (deployment/ansible) или в server.sh peer.
# Ничего существующего не меняет: добавляет интерфейс wgcoop, зону vpn с
# доступом в lan и подмену адреса (устройства видят запросы сервера как от
# роутера — так ответ дойдёт даже у камер со странным шлюзом).
# Копии настроек: /etc/config/{network,firewall}.bak-<дата>.
set -eu
SERVER_KEY="${1:?ключ сервера}"; SERVER_IP="${2:?IP сервера}"; ADDR="${3:-10.77.0.2}"; PORT=51820

if ! command -v wg >/dev/null; then
    opkg update
    opkg install kmod-wireguard wireguard-tools
fi
STAMP=$(date +%F-%H%M)
cp /etc/config/network /etc/config/network.bak-$STAMP
cp /etc/config/firewall /etc/config/firewall.bak-$STAMP

if ! uci -q get network.wgcoop >/dev/null; then
    PRIV=$(wg genkey)
    uci set network.wgcoop=interface
    uci set network.wgcoop.proto=wireguard
    uci set network.wgcoop.private_key="$PRIV"
    uci add_list network.wgcoop.addresses=$ADDR/24
    uci set network.wgserver=wireguard_wgcoop
    uci set network.wgserver.description=site-server
    uci set network.wgserver.public_key="$SERVER_KEY"
    uci set network.wgserver.endpoint_host="$SERVER_IP"
    uci set network.wgserver.endpoint_port=$PORT
    uci set network.wgserver.persistent_keepalive=25
    uci set network.wgserver.route_allowed_ips=1
    uci add_list network.wgserver.allowed_ips=10.77.0.1/32
    uci commit network

    z=$(uci add firewall zone)
    uci set firewall.$z.name=vpn
    uci set firewall.$z.input=ACCEPT
    uci set firewall.$z.output=ACCEPT
    uci set firewall.$z.forward=REJECT
    uci add_list firewall.$z.network=wgcoop
    f=$(uci add firewall forwarding)
    uci set firewall.$f.src=vpn
    uci set firewall.$f.dest=lan
    n=$(uci add firewall nat)
    uci set firewall.$n.name=VPN-to-LAN
    uci set firewall.$n.src=lan
    uci set firewall.$n.src_ip=10.77.0.0/24
    uci set firewall.$n.target=MASQUERADE
    uci set firewall.$n.proto=all
    uci commit firewall
fi
ifup wgcoop
/etc/init.d/firewall reload
echo "Ключ роутера: $(uci get network.wgcoop.private_key | wg pubkey)"
