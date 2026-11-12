#!/usr/bin/env python3
"""
Агент ЭЗП — отправка заказных писем кооператива через API ИС ЭПС Почты
России («Система-Система», POST /api/v2.3/letters). Работает на компьютере
председателя, где вставлен Рутокен с КЭП кооператива: ключ с токена нужен
и для подписи PDF (cryptcp), и для двустороннего ГОСТ-TLS до Почты
(stunnel КриптоПро, слушает локально, см. stunnel.conf.example). Сайт
кооператива живёт на VPS без токена, поэтому агент сам ходит к нему за
работой (pull — компьютер за NAT, сайт до него не достучится):

  1. GET  <сайт>/postal-letters/agent/jobs — письма из очереди (с готовыми
     метаданными commonInfo) и письма, у которых пора спросить статус;
  2. скачивает PDF, подписывает: cryptcp -signf -detached → letter.pdf.sig
     (PKCS#7, DER — формат по умолчанию у cryptcp, ключа -der у него нет);
  3. POST <stunnel>/api/v2.3/letters (multipart: commonInfo, letter.pdf,
     letter.pdf.sig), Basic id:token отправителя от Почты;
  4. POST <сайт>/postal-letters/agent/jobs/<id>/result — requestCode;
  5. GET <stunnel>/api/v2.3/letters/<requestCode>/status → POST на сайт
     …/jobs/<id>/status.

ID и токен отправителя от Почты есть только здесь, в конфиге агента (по
умолчанию /etc/ezp-agent/ezp_agent.ini, см. ezp_agent.example.ini) — на
сайт они не уходят. Сайту агент предъявляет свой токен, выпущенный
председателем на странице «Настройки API Почты».

Принято Почтой, но сайт не узнал (обрыв связи после отправки) — такое
письмо при следующем проходе снова пришло бы в очереди. Чтобы не слать его
второй раз, requestCode каждого отправленного письма сначала пишется в
локальный журнал (state_dir/sent.json), и при повторе агент только
пересылает сайту уже известный requestCode.

Отказ из-за самого письма (400 от Почты) — письмо на сайте становится «Не
доставлено» с текстом ошибки. Всё, что не про письмо — сеть, 5xx, 401/403
(не тот токен, сертификат не в белом списке), не вставлен токен/ошибка
подписи — письмо остаётся в очереди, проход прерывается, повтор в
следующем.

Команды:
  ezp_agent.py run [--loop]   один проход / демон (systemd: ezp-agent.service)
  ezp_agent.py check          проверка канала до Почты: статус письма «1»
                              должен прийти UNKNOWN (п. 6.12.1.5 инструкции)
  ezp_agent.py send-file --common-info commonInfo.json --pdf letter.pdf
                              отправить письмо из тестового комплекта сайта
  ezp_agent.py status <requestCode>
"""
import argparse
import base64
import configparser
import contextlib
import datetime as dt
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading

import requests

DEFAULT_CONFIG = "/etc/ezp-agent/ezp_agent.ini"
REQUEST_TIMEOUT = 60
SIGN_TIMEOUT = 120


def log(message: str) -> None:
    print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


class AgentError(Exception):
    """Сбой не из-за конкретного письма — проход прерывается, письмо
    остаётся в очереди до следующего."""


class Config:
    def __init__(self, path: str):
        parser = configparser.ConfigParser(interpolation=None)
        if not parser.read(path, encoding="utf-8"):
            raise SystemExit(f"Не найден конфиг {path}")
        self.site_url = parser.get("site", "url").rstrip("/")
        self.agent_token = parser.get("site", "agent_token")
        self.ezp_id = parser.get("ezp", "id")
        self.ezp_token = parser.get("ezp", "token")
        self.ezp_url = parser.get("ezp", "url", fallback="http://127.0.0.1:3080").rstrip("/")
        self.cryptcp = parser.get("crypto", "cryptcp", fallback="/opt/cprocsp/bin/amd64/cryptcp")
        self.thumbprint = parser.get("crypto", "thumbprint")
        self.pin = parser.get("crypto", "pin", fallback="") or None
        # -errchain: не построилась цепочка до корня ФНС — ошибка, а не
        # вопрос в консоли, на котором демон повис бы навсегда.
        self.sign_extra_args = parser.get("crypto", "sign_extra_args", fallback="-errchain").split()
        self.poll_interval = parser.getint("agent", "poll_interval", fallback=300)
        self.state_dir = parser.get("agent", "state_dir", fallback="/var/lib/ezp-agent")
        self.lock_file = parser.get("agent", "lock_file", fallback="/run/ezp-agent/ezp_agent.lock")


# --- подпись ---------------------------------------------------------------

def cryptcp_sign_args(cfg: Config, pdf_path: str, out_dir: str) -> list[str]:
    """Отсоединённая подпись PKCS#7 в DER (п. 7.4 инструкции): -cert —
    сертификат подписанта внутри подписи, чтобы Почта могла её проверить;
    -strict — каноничный DER вместо BER."""
    args = [
        cfg.cryptcp, "-signf", "-detached", "-dir", out_dir, "-fext", ".sig",
        "-thumbprint", cfg.thumbprint, "-cert", "-strict", *cfg.sign_extra_args,
    ]
    if cfg.pin:
        args += ["-pin", cfg.pin]
    args.append(pdf_path)
    return args


def sign_pdf(cfg: Config, pdf_path: str) -> str:
    """Подписывает PDF, возвращает путь к letter.pdf.sig рядом с ним."""
    out_dir = os.path.dirname(pdf_path)
    sig_path = pdf_path + ".sig"
    try:
        result = subprocess.run(
            cryptcp_sign_args(cfg, pdf_path, out_dir), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=SIGN_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise AgentError(f"cryptcp не запустился: {e}") from e
    if result.returncode != 0 or not os.path.exists(sig_path):
        output = (result.stdout + result.stderr).strip()[-2000:]
        raise AgentError(f"Подпись не удалась (код {result.returncode}), Рутокен вставлен? {output}")
    return sig_path


# --- Почта (через локальный stunnel) -------------------------------------

def _ezp_headers(cfg: Config) -> dict:
    pair = base64.b64encode(f"{cfg.ezp_id}:{cfg.ezp_token}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {pair}"}


def letter_multipart(common_info: dict, pdf_bytes: bytes, sig_bytes: bytes) -> dict:
    """Части запроса POST /letters: имена частей letter.pdf/letter.pdf.sig —
    те же, что в commonInfo.attachments; метаданные — UTF-8 без BOM."""
    return {
        "commonInfo": ("commonInfo.json", json.dumps(common_info, ensure_ascii=False).encode("utf-8"), "application/json"),
        "letter.pdf": ("letter.pdf", pdf_bytes, "application/octet-stream"),
        "letter.pdf.sig": ("letter.pdf.sig", sig_bytes, "application/octet-stream"),
    }


class LetterRejected(Exception):
    """Почта отказала из-за самого письма (400) — его надо исправлять."""


def _parse_ezp_response(resp: requests.Response) -> dict:
    """Ответ ИС ЭПС {"code": …, "message": …, "data": {…}} → data."""
    try:
        body = resp.json()
    except ValueError:
        body = {}
    message = body.get("message") or resp.text[:500]
    if resp.status_code == 400:
        raise LetterRejected(f"Почта: некорректный запрос — {message}")
    if resp.status_code in (401, 403):
        raise AgentError(f"Почта: доступ запрещён ({resp.status_code}) — проверьте ID/token и белый список сертификата. {message}")
    if resp.status_code != 200 or not isinstance(body.get("data"), dict):
        raise AgentError(f"Почта: HTTP {resp.status_code} — {message}")
    return body["data"]


def ezp_send_letter(cfg: Config, common_info: dict, pdf_path: str, sig_path: str) -> str:
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()
    with open(sig_path, "rb") as f:
        sig_bytes = f.read()
    try:
        resp = requests.post(
            f"{cfg.ezp_url}/api/v2.3/letters", headers=_ezp_headers(cfg),
            files=letter_multipart(common_info, pdf_bytes, sig_bytes), timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        raise AgentError(f"Почта недоступна (stunnel запущен?): {e}") from e
    request_code = _parse_ezp_response(resp).get("requestCode")
    if not request_code:
        raise AgentError(f"Почта не вернула requestCode: {resp.text[:500]}")
    return request_code


def ezp_status(cfg: Config, request_code: str) -> dict:
    try:
        resp = requests.get(
            f"{cfg.ezp_url}/api/v2.3/letters/{request_code}/status", headers=_ezp_headers(cfg), timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        raise AgentError(f"Почта недоступна (stunnel запущен?): {e}") from e
    try:
        return _parse_ezp_response(resp)
    except LetterRejected as e:
        raise AgentError(str(e)) from e


# --- сайт кооператива -------------------------------------------------------

class Site:
    def __init__(self, cfg: Config):
        self.base = cfg.site_url
        self.headers = {"Authorization": f"Bearer {cfg.agent_token}"}

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        try:
            resp = requests.request(method, self.base + path, headers=self.headers, timeout=REQUEST_TIMEOUT, **kwargs)
        except requests.RequestException as e:
            raise AgentError(f"Сайт недоступен: {e}") from e
        if resp.status_code == 401:
            raise AgentError("Сайт не принял токен агента — выпустите новый в «Настройки API Почты» и впишите в конфиг")
        return resp

    def jobs(self) -> dict:
        resp = self._request("GET", "/postal-letters/agent/jobs")
        if resp.status_code != 200:
            raise AgentError(f"Сайт: HTTP {resp.status_code} на запрос заданий")
        return resp.json()

    def download(self, path: str, dest: str) -> None:
        resp = self._request("GET", path)
        if resp.status_code != 200:
            raise AgentError(f"Сайт: HTTP {resp.status_code} при скачивании PDF")
        with open(dest, "wb") as f:
            f.write(resp.content)

    def post(self, path: str, payload: dict) -> None:
        resp = self._request("POST", path, json=payload)
        if resp.status_code not in (200, 404, 409):  # 404/409 — письмо уже удалено/не в очереди, сообщать некуда
            raise AgentError(f"Сайт: HTTP {resp.status_code} на {path}")


# --- журнал отправленного (защита от повторной отправки) --------------------

class SentJournal:
    def __init__(self, state_dir: str):
        self.path = os.path.join(state_dir, "sent.json")
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
        except FileNotFoundError:
            self.data = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def put(self, key: str, request_code: str) -> None:
        self.data[key] = request_code
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)


def journal_key(cfg: Config, job: dict) -> str:
    """Ключ — сайт + documentId письма: тот же, по которому Почта ловит дубли."""
    return f"{cfg.site_url}|{job['common_info']['document']['id']}"


# --- проход -----------------------------------------------------------------

def process_send(cfg: Config, site: Site, journal: SentJournal, job: dict) -> None:
    result_path = f"/postal-letters/agent/jobs/{job['id']}/result"
    key = journal_key(cfg, job)
    known = journal.get(key)
    if known:
        log(f"Письмо {job['reg_number']} уже отправлено ранее ({known}) — повторно сообщаю сайту")
        site.post(result_path, {"request_code": known})
        return
    tmp_dir = tempfile.mkdtemp(prefix="ezp-")
    try:
        pdf_path = os.path.join(tmp_dir, "letter.pdf")
        site.download(job["pdf_path"], pdf_path)
        sig_path = sign_pdf(cfg, pdf_path)
        try:
            request_code = ezp_send_letter(cfg, job["common_info"], pdf_path, sig_path)
        except LetterRejected as e:
            log(f"Письмо {job['reg_number']} отклонено: {e}")
            site.post(result_path, {"error": str(e)})
            return
        journal.put(key, request_code)
        log(f"Письмо {job['reg_number']} отправлено: {request_code}")
        site.post(result_path, {"request_code": request_code})
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def process_poll(cfg: Config, site: Site, item: dict) -> None:
    data = ezp_status(cfg, item["request_code"])
    log(f"Статус {item['request_code']}: {data.get('type')}{' ' + data['mailId'] if data.get('mailId') else ''}")
    site.post(f"/postal-letters/agent/jobs/{item['id']}/status", data)


def run_once(cfg: Config) -> None:
    site = Site(cfg)
    journal = SentJournal(cfg.state_dir)
    jobs = site.jobs()
    for job in jobs.get("send", []):
        process_send(cfg, site, journal, job)
    for item in jobs.get("poll", []):
        process_poll(cfg, site, item)


@contextlib.contextmanager
def single_instance(lock_file: str):
    os.makedirs(os.path.dirname(lock_file), exist_ok=True)
    with open(lock_file, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Агент уже запущен (занят " + lock_file + ")")
        yield


def cmd_run(cfg: Config, loop: bool) -> int:
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    with single_instance(cfg.lock_file):
        while True:
            try:
                run_once(cfg)
            except AgentError as e:
                log(f"Ошибка: {e}")
                if not loop:
                    return 1
            if not loop or stop.wait(cfg.poll_interval):
                return 0


# --- ручные команды -----------------------------------------------------------

def cmd_check(cfg: Config) -> int:
    data = ezp_status(cfg, "1")
    print(json.dumps(data, ensure_ascii=False))
    if data.get("type") == "UNKNOWN":
        print("Канал до Почты работает.")
        return 0
    print("Ожидался статус UNKNOWN — проверьте настройки.")
    return 1


def cmd_send_file(cfg: Config, common_info_path: str, pdf_path: str) -> int:
    with open(common_info_path, encoding="utf-8") as f:
        common_info = json.load(f)
    tmp_dir = tempfile.mkdtemp(prefix="ezp-")
    try:
        tmp_pdf = os.path.join(tmp_dir, "letter.pdf")
        shutil.copyfile(pdf_path, tmp_pdf)
        sig_path = sign_pdf(cfg, tmp_pdf)
        request_code = ezp_send_letter(cfg, common_info, tmp_pdf, sig_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    print(request_code)
    return 0


def cmd_status(cfg: Config, request_code: str) -> int:
    print(json.dumps(ezp_status(cfg, request_code), ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Агент ЭЗП: подпись и отправка заказных писем через API ИС ЭПС")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command")
    run_p = sub.add_parser("run", help="один проход (или демон с --loop)")
    run_p.add_argument("--loop", action="store_true")
    sub.add_parser("check", help="проверить канал до Почты")
    send_p = sub.add_parser("send-file", help="отправить письмо из файлов (тестовый комплект)")
    send_p.add_argument("--common-info", required=True)
    send_p.add_argument("--pdf", required=True)
    status_p = sub.add_parser("status", help="статус письма по requestCode")
    status_p.add_argument("request_code")
    args = parser.parse_args(argv)

    cfg = Config(args.config)
    try:
        if args.command == "check":
            return cmd_check(cfg)
        if args.command == "send-file":
            return cmd_send_file(cfg, args.common_info, args.pdf)
        if args.command == "status":
            return cmd_status(cfg, args.request_code)
        return cmd_run(cfg, loop=getattr(args, "loop", False))
    except (AgentError, LetterRejected) as e:
        log(f"Ошибка: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
