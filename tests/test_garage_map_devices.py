"""Связь схемы территории с устройствами: клиент DVRIP (app/dvrip.py) против
поддельной камеры и маршруты карточки (app/garage_map_devices.py)."""
import json
import socket
import struct
import threading

import pytest

from app import dvrip
from app.bank_api import crypto
from app.models import AuditLog, GarageMapObject, RoleEnum

from tests.conftest import login, make_user

HEADER = struct.Struct("<BB2xII2xHI")


class FakeCamera:
    """Камера Xiongmai в миниатюре: вход, SystemInfo, NetCommon/NetDHCP,
    время, перезагрузка, смена пароля. Принимает соединения в потоке."""

    def __init__(self, password=""):
        self.password_hash = dvrip.sofia_hash(password)
        self.common = {"HostIP": dvrip.ip_to_hex("192.168.2.244"), "Submask": dvrip.ip_to_hex("255.255.255.0"),
                       "GateWay": dvrip.ip_to_hex("192.168.2.1"), "MAC": "00:12:33:69:cc:78", "HostName": "cam4"}
        self.dhcp = [{"Enable": False, "Interface": "eth0"}, {"Enable": False, "Interface": "eth1"}]
        self.time = None
        self.rebooted = False
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(5)
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _recv(self, conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _handle(self, conn):
        with conn:
            while True:
                head = self._recv(conn, HEADER.size)
                if head is None:
                    return
                _h, _v, _sess, seq, msg, length = HEADER.unpack(head)
                req = json.loads(self._recv(conn, length)[:-2])
                reply = self._answer(msg, req)
                body = json.dumps(reply).encode() + b"\x0a\x00"
                conn.sendall(HEADER.pack(0xFF, 1, 0x1234, seq, msg + 1, len(body)) + body)

    def _answer(self, msg, req):
        name = req.get("Name")
        if msg == dvrip.MSG_LOGIN:
            ok = req["PassWord"] == self.password_hash
            return {"Ret": 100 if ok else 106, "SessionID": "0x00001234", "AliveInterval": 20}
        if msg == dvrip.MSG_LOGOUT:
            return {"Ret": 100}
        if msg == dvrip.MSG_SYSINFO:
            return {"Ret": 100, "Name": name, name: {"HardWare": "IPC_XM530", "SoftWareVersion": "V5.00.R02", "SerialNo": "abc"}}
        if msg == dvrip.MSG_CONFIG_GET:
            value = {"NetWork.NetCommon": self.common, "NetWork.NetDHCP": self.dhcp}[name]
            return {"Ret": 100, "Name": name, name: value}
        if msg == dvrip.MSG_CONFIG_SET:
            if name == "NetWork.NetCommon":
                self.common = req[name]
            elif name == "NetWork.NetDHCP":
                self.dhcp = req[name]
            return {"Ret": 100}
        if msg == dvrip.MSG_OP:
            if name == "OPTimeSetting":
                self.time = req[name]
            elif name == "OPMachine":
                self.rebooted = req[name] == {"Action": "Reboot"}
            return {"Ret": 100}
        if msg == dvrip.MSG_PASSWORD:
            if req["PassWord"] != self.password_hash:
                return {"Ret": 203}
            self.password_hash = req["NewPassWord"]
            return {"Ret": 100}
        return {"Ret": 102}

    def close(self):
        self.server.close()


@pytest.fixture
def camera():
    cam = FakeCamera()
    yield cam
    cam.close()


def test_ip_hex_roundtrip_and_hash():
    assert dvrip.ip_to_hex("192.168.2.244") == "0xF402A8C0"
    assert dvrip.hex_to_ip("0xF402A8C0") == "192.168.2.244"
    assert dvrip.sofia_hash("") == "tlJwpbo6"  # известный хеш пустого пароля Xiongmai


def test_client_reads_and_switches_to_dhcp(camera):
    with dvrip.DvripClient("127.0.0.1", camera.port) as dev:
        assert dev.system_info()["HardWare"] == "IPC_XM530"
        net = dev.network()
        assert (net["ip"], net["mask"], net["gateway"], net["dhcp"]) == ("192.168.2.244", "255.255.255.0", "192.168.2.1", False)
        dev.set_network(True)
    assert camera.dhcp[0] == {"Enable": True, "Interface": "eth0"} and camera.dhcp[1]["Enable"] is False

    with pytest.raises(dvrip.DvripError, match="неверный логин"):
        with dvrip.DvripClient("127.0.0.1", camera.port, password="wrong"):
            pass


def _setup(db, client, app, camera, **fields):
    app.config["GARAGE_MAP_LAN_SUBNETS"] = "127.0.0.0/8"
    obj = GarageMapObject(kind="camera", x=1, y=1, title="cam4", ip_address="127.0.0.1/8",
                          device_username="admin", device_port=camera.port, **fields)
    db.add(obj)
    make_user(db, "dev_board", "pass12345", role=RoleEnum.BOARD)
    db.commit()
    login(client, "dev_board", "pass12345")
    return obj


def test_device_card_and_network_change(db, client, app, camera):
    obj = _setup(db, client, app, camera)
    html = client.get(f"/garages/map/object/{obj.id}/device").get_data(as_text=True)
    assert "IPC_XM530" in html and "192.168.2.244" in html and "статический" in html

    resp = client.post(f"/garages/map/object/{obj.id}/device/network", data={"dhcp": "0", "ip": "127.0.0.1",
                                                                            "mask": "255.0.0.0", "gateway": "127.0.0.254"})
    assert resp.get_json()["ok"]
    assert camera.common["HostIP"] == dvrip.ip_to_hex("127.0.0.1") and camera.dhcp[0]["Enable"] is False
    resp = client.post(f"/garages/map/object/{obj.id}/device/network", data={"dhcp": "1"})
    assert resp.get_json()["ok"] and camera.dhcp[0]["Enable"] is True
    assert db.query(AuditLog).filter(AuditLog.action == "garage_map.device_change").count() == 2

    bad = client.post(f"/garages/map/object/{obj.id}/device/network", data={"dhcp": "0", "ip": "1.2.3", "mask": "x"})
    assert bad.status_code == 400


def test_device_time_reboot_password_and_credentials(db, client, app, camera):
    obj = _setup(db, client, app, camera)
    assert client.post(f"/garages/map/object/{obj.id}/device/time").get_json()["ok"] and camera.time
    assert client.post(f"/garages/map/object/{obj.id}/device/reboot").get_json()["ok"] and camera.rebooted

    short = client.post(f"/garages/map/object/{obj.id}/device/password", data={"new_password": "123", "new_password2": "123"})
    assert short.status_code == 400
    ok = client.post(f"/garages/map/object/{obj.id}/device/password",
                     data={"new_password": "s3cret!", "new_password2": "s3cret!"})
    assert ok.get_json()["ok"]
    db.expire_all()
    assert crypto.decrypt(db.get(GarageMapObject, obj.id).device_password_encrypted) == "s3cret!"
    assert camera.password_hash == dvrip.sofia_hash("s3cret!")

    # смена сохранённого доступа: password_set=0 пароль не трогает, =1 — заменяет
    client.post(f"/garages/map/object/{obj.id}/device/credentials", data={"username": "op", "port": "", "password_set": "0"})
    db.expire_all()
    saved = db.get(GarageMapObject, obj.id)
    assert saved.device_username == "op" and saved.device_port is None and crypto.decrypt(saved.device_password_encrypted) == "s3cret!"
    assert client.post(f"/garages/map/object/{obj.id}/device/credentials", data={"port": "99999"}).status_code == 400


def test_device_unreachable_without_lan(db, client, app, camera):
    obj = _setup(db, client, app, camera)
    app.config["GARAGE_MAP_LAN_SUBNETS"] = ""
    assert "нужен VPN" in client.get(f"/garages/map/object/{obj.id}/device").get_data(as_text=True)
    assert client.post(f"/garages/map/object/{obj.id}/device/reboot").status_code == 400
    assert client.get("/garages/map/net-status").get_json() == {"ok": True, "enabled": False, "status": {}}


def test_net_status_pings_only_lan_objects(db, client, app, camera, monkeypatch):
    obj = _setup(db, client, app, camera)
    outside = GarageMapObject(kind="wifi", x=1, y=1, ip_address="192.168.1.1/24")
    db.add(outside)
    db.commit()
    from app import garage_map_devices
    monkeypatch.setattr(garage_map_devices, "_ping", lambda ip: ip == "127.0.0.1")
    res = client.get("/garages/map/net-status").get_json()
    assert res["enabled"] and res["status"] == {str(obj.id): True}
