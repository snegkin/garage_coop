"""
Минимальный клиент протокола DVRIP (Xiongmai / XMEye, «Sofia»): IP-камеры и
регистраторы этого производителя настраиваются по TCP-порту 34567.

Используется схемой территории (app/garage_map.py) для чтения и смены
сетевых настроек камер, времени, пароля и перезагрузки. Формат сверен с
открытой библиотекой python-dvr (OpenIPC): пакет = 20-байтный заголовок
struct "BB2xII2xHI" (0xFF, версия, сессия, номер пакета, код сообщения,
длина) + JSON + b"\\x0a\\x00"; пароль передаётся «sofia-хешем» MD5.
IP-адреса в настройках — "0x%08X" от 4 байт адреса в порядке хоста
(little-endian), так их хранит прошивка.
"""
import datetime as dt
import hashlib
import ipaddress
import json
import socket
import struct

DEFAULT_PORT = 34567
OK_CODES = (100, 515)
HEADER = struct.Struct("<BB2xII2xHI")

MSG_LOGIN = 1000
MSG_LOGOUT = 1001
MSG_SYSINFO = 1020
MSG_CONFIG_SET = 1040
MSG_CONFIG_GET = 1042
MSG_OP = 1450  # OPMachine, OPTimeSetting
MSG_PASSWORD = 1488

ERRORS = {
    101: "неизвестная ошибка", 102: "не поддерживается", 103: "неверный запрос", 104: "пользователь уже вошёл",
    105: "пользователь не вошёл", 106: "неверный логин или пароль", 107: "нет прав", 203: "неверный пароль",
    205: "пользователь заблокирован",
}


class DvripError(Exception):
    pass


def sofia_hash(password: str) -> str:
    digest = hashlib.md5(password.encode("utf-8")).digest()
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    return "".join(chars[(a + b) % 62] for a, b in zip(digest[::2], digest[1::2]))


def ip_to_hex(ip: str) -> str:
    return "0x%08X" % struct.unpack("<I", ipaddress.IPv4Address(ip).packed)[0]


def hex_to_ip(value: str) -> str:
    return str(ipaddress.IPv4Address(struct.pack("<I", int(value, 16))))


class DvripClient:
    def __init__(self, host: str, port: int = DEFAULT_PORT, username: str = "admin", password: str = "",
                 timeout: float = 5.0):
        self.host, self.port, self.username, self.password = host, port, username, password
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.session = 0
        self.sequence = 0

    # --- транспорт
    def _recv_exact(self, length: int) -> bytes:
        buf = bytearray()
        while len(buf) < length:
            chunk = self.sock.recv(length - len(buf))
            if not chunk:
                raise DvripError("устройство закрыло соединение")
            buf.extend(chunk)
        return bytes(buf)

    def _send(self, msg_id: int, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        packet = HEADER.pack(0xFF, 0, self.session, self.sequence, msg_id, len(body) + 2) + body + b"\x0a\x00"
        self.sequence += 1
        try:
            self.sock.sendall(packet)
            _head, _ver, session, _seq, _msg, length = HEADER.unpack(self._recv_exact(HEADER.size))
            data = self._recv_exact(length)
        except (OSError, struct.error) as exc:
            raise DvripError(f"нет связи с устройством: {exc}") from exc
        if session:
            self.session = session
        text = data.rstrip(b"\x00").rstrip(b"\x0a").decode("utf-8", errors="replace")
        try:
            return json.loads(text) if text else {}
        except ValueError as exc:
            raise DvripError("устройство прислало непонятный ответ") from exc

    def _check(self, reply: dict, action: str) -> dict:
        ret = reply.get("Ret")
        if ret not in OK_CODES:
            raise DvripError(f"{action}: {ERRORS.get(ret, f'код {ret}')}")
        return reply

    # --- сессия
    def __enter__(self):
        self.login()
        return self

    def __exit__(self, *exc):
        self.close()

    def login(self):
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise DvripError(f"нет связи с {self.host}:{self.port}: {exc}") from exc
        self.sock.settimeout(self.timeout)
        reply = self._send(MSG_LOGIN, {
            "EncryptType": "MD5", "LoginType": "DVRIP-Web",
            "PassWord": sofia_hash(self.password), "UserName": self.username,
        })
        self._check(reply, "вход")
        if reply.get("SessionID"):
            self.session = int(str(reply["SessionID"]), 16)

    def close(self):
        if self.sock is None:
            return
        try:
            self._send(MSG_LOGOUT, {"Name": "", "SessionID": self._sid()})
        except DvripError:
            pass
        try:
            self.sock.close()
        finally:
            self.sock = None

    def _sid(self) -> str:
        return "0x%08X" % self.session

    # --- запросы
    def get(self, name: str, msg_id: int = MSG_CONFIG_GET):
        reply = self._check(self._send(msg_id, {"Name": name, "SessionID": self._sid()}), f"чтение {name}")
        return reply.get(name)

    def set(self, name: str, value, msg_id: int = MSG_CONFIG_SET):
        return self._check(
            self._send(msg_id, {"Name": name, "SessionID": self._sid(), name: value}), f"запись {name}",
        )

    def system_info(self) -> dict:
        return self.get("SystemInfo", MSG_SYSINFO) or {}

    def network(self) -> dict:
        """Сетевые настройки: ip, mask, gateway, dhcp (bool), mac, hostname."""
        common = self.get("NetWork.NetCommon") or {}
        dhcp = self.get("NetWork.NetDHCP") or []
        eth = next((d for d in dhcp if d.get("Interface") == "eth0"), dhcp[0] if dhcp else {})
        return {
            "ip": hex_to_ip(common["HostIP"]) if common.get("HostIP") else None,
            "mask": hex_to_ip(common["Submask"]) if common.get("Submask") else None,
            "gateway": hex_to_ip(common["GateWay"]) if common.get("GateWay") else None,
            "mac": common.get("MAC"), "hostname": common.get("HostName"),
            "dhcp": bool(eth.get("Enable")), "_common": common, "_dhcp": dhcp,
        }

    def set_network(self, dhcp: bool, ip: str | None = None, mask: str | None = None, gateway: str | None = None):
        """Статический адрес пишется до включения/выключения DHCP: если DHCP
        не выдаст адрес, камера останется на прежнем статическом."""
        current = self.network()
        if ip:
            common = dict(current["_common"])
            common["HostIP"] = ip_to_hex(ip)
            if mask:
                common["Submask"] = ip_to_hex(mask)
            if gateway:
                common["GateWay"] = ip_to_hex(gateway)
            self.set("NetWork.NetCommon", common)
        entries = [dict(d) for d in current["_dhcp"]] or [{"Interface": "eth0", "Enable": dhcp}]
        for entry in entries:
            if entry.get("Interface") == "eth0" or len(entries) == 1:
                entry["Enable"] = dhcp
        self.set("NetWork.NetDHCP", entries)

    def set_time(self, when: dt.datetime | None = None):
        when = when or dt.datetime.now()
        self.set("OPTimeSetting", when.strftime("%Y-%m-%d %H:%M:%S"), MSG_OP)

    def reboot(self):
        self.set("OPMachine", {"Action": "Reboot"}, MSG_OP)

    def change_password(self, new_password: str):
        reply = self._send(MSG_PASSWORD, {
            "EncryptType": "MD5", "NewPassWord": sofia_hash(new_password), "PassWord": sofia_hash(self.password),
            "SessionID": self._sid(), "UserName": self.username,
        })
        self._check(reply, "смена пароля")
        self.password = new_password
