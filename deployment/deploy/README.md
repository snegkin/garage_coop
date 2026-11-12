# Обновление сайта из git (deploy.sh)

`deploy.sh` выкатывает на сервер свежий код из ветки на GitHub. Запускается
**на своём компьютере**: подключается к серверу по SSH и всё делает там
одним сеансом. Сервер с нуля поднимает Ansible-плейбук
[`deployment/ansible`](../ansible/README.md), после него нужен только
`deploy.sh`.

```bash
git push origin master                                # на сервер попадает только запушенное
deployment/deploy.sh                  # обновить (спросит подтверждение)
deployment/deploy.sh -y               # без вопроса
deployment/deploy.sh --force          # перезапустить, даже если код не менялся
deployment/deploy.sh maintenance on   # заглушка вручную (off, status)
```

Если запустить `deploy.sh` двойным щелчком из файлового менеджера, он сам
откроет окно терминала, спросит подтверждение и после выкатки подождёт
Enter, чтобы можно было прочитать итог. Общая часть с `setup.sh`
лежит в `deployment/in_terminal.sh`.

## Конфиг

Ветка, сервер, логин, каталог и сервисы задаются в `deploy.conf`. Это файл
в синтаксисе shell, коммитить его не нужно (в нём может быть пароль).
Образец со всеми пояснениями — [`deploy.conf.example`](deploy.conf.example).
Скрипт ищет конфиг в таком порядке и сообщает, какой взял:

1. `-c ПУТЬ`;
2. переменная окружения `DEPLOY_CONFIG`;
3. `deployment/deploy/deploy.conf` (файл в `.gitignore`);
4. `~/.config/garage-coop/deploy.conf`.

Ansible-плейбук создаёт конфиг сам, если файла ещё нет. Куда его
записать, задаёт `deploy_conf_path` в `vars.yml`. Вручную:

```bash
cp deployment/deploy/deploy.conf.example deployment/deploy/deploy.conf
chmod 600 deployment/deploy/deploy.conf
```

Вход лучше по SSH-ключу. С паролем (`DEPLOY_PASSWORD`) нужен `sshpass`, и
этот же пароль используется для `sudo` на сервере. При входе по ключу
перезапуск сервиса идёт через `sudo` без пароля, правило в
`/etc/sudoers.d/` ставит плейбук:
`coop ALL=(root) NOPASSWD: /usr/bin/systemctl restart garage-coop`.

## Что происходит при обновлении

| Шаг | Если здесь ошибка |
|---|---|
| Предупреждение, если в локальной ветке есть незапушенные коммиты. Подтверждение | — |
| `git fetch` ветки. Локальные правки в коде на сервере — отказ | ничего не менялось |
| Копирование `certs/` на сервер (см. «Сертификаты» ниже) | ничего не менялось |
| Код уже на этом коммите — выход (кроме `--force`) | — |
| Заглушка «Ведутся технические работы» (флаг `maintenance.on`) | заглушка снимается |
| Бэкап БД (`scripts/backup_db.sh`, `BACKUP_BEFORE=1`) | заглушка снимается |
| `git merge --ff-only`; `pip install`, если менялся `requirements.txt`; `scripts/vendor.sh verify` | код возвращается на прежний коммит, заглушка снимается: работает прежняя версия |
| `systemctl restart` сервисов (миграции БД накатываются при старте) и ожидание ответа без 5xx по `HEALTH_URLS` до `HEALTH_TIMEOUT` секунд | **заглушка остаётся** |
| Заглушка снимается | — |

Ошибка при перезапуске оставляет заглушку, потому что миграции могли
пройти, а откатывать код без отката БД опаснее. Разбираться так:

```bash
sudo journalctl -u garage-coop -n 100        # на сервере; логи — instance/logs/app-*.log
deployment/deploy.sh maintenance off
```

Пароль не попадает ни в командную строку (`sshpass -e` берёт его из
окружения), ни на диск сервера: удалённый скрипт передаётся через stdin
SSH.

## Заглушка

- [`maintenance.html`](maintenance.html) — сама страница, без внешних
  ресурсов, логотип из `/static/`.
- [`nginx-maintenance.conf`](nginx-maintenance.conf) — фрагмент для
  `server { }`. nginx отдаёт заглушку с кодом 503 и `Retry-After`, пока
  есть флаг `maintenance.on`, а также при 502/504, когда gunicorn
  перезапускается или упал. В конфиг сайта от Ansible фрагмент уже встроен.
- `maintenance.on` — файл-флаг. Его ставит и снимает `deploy.sh`, в git
  он не попадает.

Поисковики заглушку не индексируют, а агент ЭЗП и другие клиенты API
получают ошибку, а не «успешный» HTML.

## Несколько кооперативов с одного кода

В `SERVICES` перечислите сервисы через пробел, в `HEALTH_URLS` — адреса
для проверки в том же порядке. В `BACKUP_ENV_FILES` укажите `.env` каждого
кооператива, чтобы перед обновлением сохранилась каждая БД: `ФАЙЛ` — бэкап
под `APP_USER`, `ПОЛЬЗОВАТЕЛЬ:ФАЙЛ` — под пользователем того кооператива
(через sudo). Кооперативы из `coop_extra_instances` плейбук сам вписывает в
новый `deploy.conf` и ставит для них правила sudo.

## Сертификаты

API обоих банков отдают TLS-сертификаты российских УЦ, которых нет в
стандартном наборе Python (certifi). Без доверенных корней соединение
падает с `self-signed certificate in certificate chain`. Эти файлы в git не
хранятся: они лежат локально в `deployment/deploy/bundles/` (каталог в
`.gitignore`), и `deploy.sh` при каждом запуске копирует их на сервер в
`$APP_DIR/instance/bundles/` (`BANK_CA_BUNDLES_FOLDER` основного
кооператива), даже если код не менялся. Перезапуск для этого не нужен.
Файлы, удалённые локально, на сервере остаются — удалять вручную. Для
локального запуска нужны те же файлы в `instance/bundles/`.

| Файл | Банк | Что внутри |
|---|---|---|
| `bundle.pem` | Сбербанк (`SBERBANK_API_CA_BUNDLE`) | цепочка УЦ Сбера из архива банка, ссылки — в `config.py` |
| `russian_trusted_root_ca.pem` | Модульбанк (`MODULBANK_API_CA_BUNDLE`) | корень НУЦ Минцифры, Russian Trusted Root CA, до 2032 года |

Корень Минцифры можно взять на <https://www.gosuslugi.ru/crt> или из
системного хранилища, если он там уже установлен. Сверьте отпечаток:

```bash
openssl x509 -in deployment/deploy/bundles/russian_trusted_root_ca.pem -noout -fingerprint -sha256
# SHA256 Fingerprint=D2:6D:2D:02:31:B7:C3:9F:92:CC:73:85:12:BA:54:10:35:19:E4:40:5D:68:B5:BD:70:3E:97:88:CA:8E:CF:31
```
