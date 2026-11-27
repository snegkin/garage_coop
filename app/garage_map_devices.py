"""
Схема территории: связь с сетевыми устройствами локальной сети кооператива.

Сайт видит локальную сеть территории только если сервер до неё
дотягивается (VPN до роутера) и подсеть перечислена в
GARAGE_MAP_LAN_SUBNETS (config.py). Тогда:
  - /net-status — пинг всех объектов схемы с IP из этих подсетей
    (на схеме — отметка «не в сети»);
  - камеры и регистраторы Xiongmai настраиваются из карточки по DVRIP
    (app/dvrip.py): сеть/DHCP, время, пароль, перезагрузка. Логин и пароль
    устройства хранятся в объекте схемы (пароль — зашифрован, как у
    регистраторов видеонаблюдения). Изменения пишутся в аудит.
"""
import concurrent.futures
import ipaddress
import shutil
import socket
import subprocess

from flask import current_app, jsonify, render_template, request

from . import audit, database
from .auth import roles_required
from .bank_api import crypto
from .dvrip import DEFAULT_PORT, DvripClient, DvripError
from .garage_map import _get_object, bp
from .i18n import translate as _
from .models import GarageMapObject, RoleEnum

DEVICE_KINDS = ("camera", "dvr")  # у них настройки по DVRIP


def lan_subnets() -> list:
    raw = current_app.config.get("GARAGE_MAP_LAN_SUBNETS") or ""
    nets = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            try:
                nets.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                current_app.logger.warning("GARAGE_MAP_LAN_SUBNETS: неверная подсеть %r", part)
    return nets


def object_ip(obj: GarageMapObject) -> str | None:
    if not obj.ip_address:
        return None
    try:
        return str(ipaddress.ip_interface(obj.ip_address).ip)
    except ValueError:
        return None


def reachable_ip(obj: GarageMapObject, nets=None) -> str | None:
    """IP объекта, если он в подсети, которую сайт видит."""
    ip = object_ip(obj)
    nets = lan_subnets() if nets is None else nets
    if ip and any(ipaddress.ip_address(ip) in net for net in nets):
        return ip
    return None


def _ping(ip: str) -> bool:
    ping = shutil.which("ping")
    if ping:
        try:
            return subprocess.run([ping, "-c", "1", "-W", "1", ip], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=3).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    for port in (80, 554, DEFAULT_PORT):  # без ping — хотя бы один открытый порт
        try:
            with socket.create_connection((ip, port), timeout=1):
                return True
        except OSError:
            continue
    return False


@bp.route("/net-status")
@roles_required(RoleEnum.BOARD)
def net_status():
    nets = lan_subnets()
    if not nets:
        return jsonify(ok=True, enabled=False, status={})
    targets = {}
    for obj in database.db_session.query(GarageMapObject).filter(GarageMapObject.ip_address.isnot(None)).all():
        ip = reachable_ip(obj, nets)
        if ip:
            targets[obj.id] = ip
    status = {}
    if targets:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(32, len(targets))) as pool:
            for obj_id, up in zip(targets, pool.map(_ping, targets.values())):
                status[obj_id] = up
    return jsonify(ok=True, enabled=True, status=status)


def _client(obj: GarageMapObject, ip: str) -> DvripClient:
    password = crypto.decrypt(obj.device_password_encrypted) if obj.device_password_encrypted else ""
    return DvripClient(ip, obj.device_port or DEFAULT_PORT, obj.device_username or "admin", password or "")


def _device_context(obj: GarageMapObject):
    ip = reachable_ip(obj)
    ctx = {"obj": obj, "ip": ip, "object_ip": object_ip(obj), "lan_enabled": bool(lan_subnets()),
           "info": None, "net": None, "error": None}
    if ip:
        try:
            with _client(obj, ip) as dev:
                ctx["info"] = dev.system_info()
                ctx["net"] = dev.network()
        except DvripError as exc:
            ctx["error"] = str(exc)
    return ctx


@bp.route("/object/<int:object_id>/device")
@roles_required(RoleEnum.BOARD)
def object_device(object_id):
    obj = _get_object(object_id)
    if obj.kind not in DEVICE_KINDS:
        return jsonify(ok=False, error=_("Это устройство не настраивается с сайта.")), 400
    return render_template("garage_map/_device.html", **_device_context(obj))


@bp.route("/object/<int:object_id>/device/credentials", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def object_device_credentials(object_id):
    obj = _get_object(object_id)
    username = (request.form.get("username") or "").strip()[:100] or None
    port = request.form.get("port") or ""
    try:
        port = int(port) if port else None
        if port is not None and not 0 < port < 65536:
            raise ValueError
    except ValueError:
        return jsonify(ok=False, error=_("Неверный порт.")), 400
    obj.device_username = username
    obj.device_port = port
    if request.form.get("password_set") == "1":
        password = request.form.get("password") or ""
        obj.device_password_encrypted = crypto.encrypt(password) if password else None
    audit.record("garage_map.device_credentials", f"Схема: изменён доступ к устройству «{obj.title or obj.id}»",
                 entity_type="garage_map_object", entity_id=obj.id)
    database.db_session.commit()
    return jsonify(ok=True)


def _device_action(object_id, action):
    """Общая обвязка действий с устройством: проверки, соединение, аудит."""
    obj = _get_object(object_id)
    ip = reachable_ip(obj)
    if obj.kind not in DEVICE_KINDS or not ip:
        return jsonify(ok=False, error=_("Сайт не видит это устройство в сети.")), 400
    try:
        with _client(obj, ip) as dev:
            message, summary = action(obj, dev)
    except DvripError as exc:
        return jsonify(ok=False, error=str(exc)), 502
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    if summary:
        audit.record("garage_map.device_change", f"Схема: «{obj.title or obj.id}» ({ip}) — {summary}",
                     entity_type="garage_map_object", entity_id=obj.id)
    database.db_session.commit()
    return jsonify(ok=True, message=message)


def _parse_ip(value: str, label: str) -> str:
    try:
        return str(ipaddress.IPv4Address((value or "").strip()))
    except ValueError:
        raise ValueError(_("Неверное значение: {label}.", label=label)) from None


@bp.route("/object/<int:object_id>/device/network", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def object_device_network(object_id):
    def action(obj, dev):
        dhcp = request.form.get("dhcp") == "1"
        ip = mask = gateway = None
        if not dhcp:
            ip = _parse_ip(request.form.get("ip"), _("IP-адрес"))
            mask = _parse_ip(request.form.get("mask"), _("маска"))
            gateway = _parse_ip(request.form.get("gateway"), _("шлюз"))
            prefix = ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
            obj.ip_address = f"{ip}/{prefix}"  # на схеме — новый адрес
        try:
            dev.set_network(dhcp, ip, mask, gateway)
        except DvripError as exc:
            # устройство могло применить настройки и сразу уйти из сети
            raise DvripError(_("Связь прервалась при записи ({error}). Проверьте, в сети ли устройство.",
                               error=str(exc))) from exc
        summary = "сеть: DHCP" if dhcp else f"сеть: статический {ip}/{mask}, шлюз {gateway}"
        return _("Настройки сети записаны. Устройство может переподключиться через несколько секунд."), summary
    return _device_action(object_id, action)


@bp.route("/object/<int:object_id>/device/time", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def object_device_time(object_id):
    def action(obj, dev):
        dev.set_time()
        return _("Время на устройстве выставлено по серверу."), None
    return _device_action(object_id, action)


@bp.route("/object/<int:object_id>/device/reboot", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def object_device_reboot(object_id):
    def action(obj, dev):
        dev.reboot()
        return _("Устройство перезагружается, это займёт около минуты."), "перезагрузка"
    return _device_action(object_id, action)


@bp.route("/object/<int:object_id>/device/password", methods=["POST"])
@roles_required(RoleEnum.BOARD)
def object_device_password(object_id):
    def action(obj, dev):
        new = request.form.get("new_password") or ""
        if len(new) < 6:
            raise ValueError(_("Пароль — не короче 6 символов."))
        if new != (request.form.get("new_password2") or ""):
            raise ValueError(_("Пароли не совпадают."))
        dev.change_password(new)
        obj.device_password_encrypted = crypto.encrypt(new)
        return _("Пароль изменён и сохранён. Не забудьте сменить его и в канале регистратора."), "смена пароля"
    return _device_action(object_id, action)
