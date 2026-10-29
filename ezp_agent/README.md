# Агент ЭЗП (Почта России, API ИС ЭПС)

Программа для компьютера председателя, где вставлен Рутокен с КЭП
кооператива. Сайт ставит заказные письма в очередь, а агент:
- забирает их,
- подписывает PDF (`cryptcp`),
- отправляет в Почту через ГОСТ-TLS туннель (`stunnel` КриптоПро),
- возвращает на сайт номер запроса, статус и ШПИ.

Подробности — в докстринге `ezp_agent.py` и в `app/postal_letters.py`.

Почему не на сервере сайта: ключ КЭП от ФНС неэкспортируемый, он есть только
на токене. Токен нужен дважды: для подписи PDF и для TLS-рукопожатия с Почтой.
Отдельный TLS-сертификат не нужен — КЭП от ФНС подходит: в нём есть EKU
`1.3.6.1.5.5.7.3.2` (Client Authentication) и встроенная лицензия КриптоПро CSP.

## Состав

| Файл | Куда на ПК |
|---|---|
| `ezp_agent.py` | `/opt/ezp-agent/ezp_agent.py` |
| `ezp_agent.example.ini` | `/etc/ezp-agent/ezp_agent.ini` (заполнить) |
| `stunnel.conf.example` | `/etc/ezp-agent/stunnel.conf` |
| `ezp-agent.service`, `ezp-stunnel.service` | `/etc/systemd/system/` |

## Установка

Пути утилит КриптоПро ниже — `/opt/cprocsp/bin/amd64/`.

1. **КриптоПро CSP** (уже есть) и пакет **`cprocsp-stunnel`** из того же
   дистрибутива КриптоПро (`dpkg -i cprocsp-stunnel*.deb`). Проверка:
   `ls /opt/cprocsp/sbin/amd64/stunnel*`. Также нужны `python3`,
   `python3-requests` и `pcscd` (служба смарт-карт).

2. **Пользователь агента** и каталоги:
   ```bash
   sudo useradd --system --create-home --home-dir /var/lib/ezp-agent-home ezp-agent
   sudo install -d -m 0750 -o root -g ezp-agent /etc/ezp-agent
   sudo install -d /opt/ezp-agent
   sudo install -m 0755 ezp_agent.py /opt/ezp-agent/
   ```
   Хранилище сертификатов КриптоПро у каждого пользователя своё, поэтому
   сертификат ставится именно от `ezp-agent`. Домашний каталог ему нужен для
   этого хранилища.

3. **Контейнер ключа на токене** — найти полное имя:
   ```bash
   sudo -u ezp-agent /opt/cprocsp/bin/amd64/csptest -keyset -enum_containers -verifycontext -fqcn
   ```
   Строка вида `\\.\Aktiv Rutoken lite 00 00\SCARD\rutoken_lt_…\…`.

4. **Установить сертификат в хранилище My пользователя `ezp-agent`** со
   ссылкой на контейнер и проверить:
   ```bash
   sudo -u ezp-agent /opt/cprocsp/bin/amd64/certmgr -inst -container '\\.\Aktiv Rutoken lite 00 00\SCARD\…'
   sudo -u ezp-agent /opt/cprocsp/bin/amd64/certmgr -list
   ```
   В выводе должны быть `SHA1 Thumbprint : 32c441ef…` и `PrivateKey Link : Yes`.

   Для `-errchain` должна строиться цепочка до корня:

   | Звено | Файл | SHA1 | Откуда |
   |---|---|---|---|
   | клиент «ГМ "ВОСХОД"» | `gm-voshod.cer` | `32c441ef…` | — |
   | промежуточный «Федеральная налоговая служба» | `CA_FNS_Russia_2025_01.crt` | `2fef1770…` | *CA cert URL* в сертификате: `http://pki.tax.gov.ru/crt/ca_fns_russia_2025_01.crt` |
   | корневой ГОСТ «Минцифры России», до 2040 | `Mincifry_Rossii_root_guc2022.cer` | `2f0cb09b…` | `http://crl.gosuslugi.ru/cdp/guc2022.crt` |

   Все три файла лежат в `instance/coop_certs/` сайта. Ставить их в
   хранилища **компьютера** (`mRoot`/`mCA`), а не пользователя: тогда их
   видит и `ezp-agent`. Делается один раз:
   ```bash
   sudo /opt/cprocsp/bin/amd64/certmgr -inst -store mRoot -file Mincifry_Rossii_root_guc2022.cer
   sudo /opt/cprocsp/bin/amd64/certmgr -inst -store mCA   -file CA_FNS_Russia_2025_01.crt
   # проверка: цепочка строится (Certificate chains are checked)
   sudo -u ezp-agent /opt/cprocsp/bin/amd64/cryptcp -copycert -thumbprint 32c441ef7c63a951ab66b4ef585bfe05fe6fdbd7 \
     -u -errchain -df /tmp/chaincheck.cer </dev/null
   ```
   `Russian_Trusted_Root_CA`/`Russian_Trusted_Sub_CA` — это RSA-сертификаты
   НУЦ Минцифры для HTTPS-сайтов. В ГОСТ-цепочку КЭП они не входят, для ЭЗП
   не нужны.

5. **Открытая часть сертификата в PEM** — для `stunnel` и для Почты:
   ```bash
   sudo -u ezp-agent /opt/cprocsp/bin/amd64/certmgr -export -dest /tmp/client.cer \
     -container '\\.\Aktiv Rutoken lite 00 00\SCARD\…' -pem
   sudo install -m 0644 /tmp/client.cer /etc/ezp-agent/client.cer
   ```
   Тот же файл уже лежит в репозитории сайта: `instance/coop_certs/gm-voshod.cer`.

6. **Письмо в Почту** — ezp.support-r00@russianpost.ru (п. 6.12.1.4
   инструкции). Приложить три сертификата из таблицы в п. 4, только
   открытые части: `gm-voshod.cer`, `CA_FNS_Russia_2025_01.crt`,
   `Mincifry_Rossii_root_guc2022.cer`.

   Попросить внести их в белый список и выдать тестовую пару ID/token.

7. **Конфиги:**
   ```bash
   sudo install -m 0640 -o root -g ezp-agent ezp_agent.example.ini /etc/ezp-agent/ezp_agent.ini
   sudo install -m 0640 -o root -g ezp-agent stunnel.conf.example  /etc/ezp-agent/stunnel.conf
   ```
   В `ezp_agent.ini` заполнить:
   - адрес сайта;
   - токен агента — сайт → «Заказные письма» → «Настройки API Почты» → «Выпустить токен агента»;
   - ID/token от Почты;
   - ПИН Рутокена.

   На сайте в тех же настройках указать тот же ID отправителя.

8. **Службы:**
   ```bash
   sudo install -m 0644 ezp-stunnel.service ezp-agent.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now ezp-stunnel ezp-agent
   journalctl -u ezp-stunnel -u ezp-agent -f
   ```

## Проверка и интеграционное тестирование

```bash
# канал до Почты: должен прийти статус UNKNOWN
sudo -u ezp-agent python3 /opt/ezp-agent/ezp_agent.py --config /etc/ezp-agent/ezp_agent.ini check
```

Перед продуктивом Почта просит три тестовых письма (п. 6.12.1.8).
1. Скачать комплект на сайте: «Настройки API Почты» → «Скачать тестовый
   комплект». В нём три папки `testN/` с `letter.pdf` и `commonInfo.json`.
2. Отправить каждое письмо:
   ```bash
   sudo -u ezp-agent python3 /opt/ezp-agent/ezp_agent.py --config /etc/ezp-agent/ezp_agent.ini \
     send-file --common-info test1/commonInfo.json --pdf test1/letter.pdf
   # → req_…; через несколько минут:
   sudo -u ezp-agent python3 /opt/ezp-agent/ezp_agent.py --config /etc/ezp-agent/ezp_agent.ini status req_…
   ```
3. Когда придёт финальный статус (`ONLINE_DELIVERED`, `DELIVERED_TO_PRINT`
   или `FAILED`), отправить в поддержку Почты requestCode и ШПИ.

Переход на продуктив:
1. В `stunnel.conf` указать `connect = api.zakaznoe.pochta.ru:3080`.
2. В `ezp_agent.ini` вписать боевые ID/token, на сайте — боевой ID отправителя.
3. Выполнить `systemctl restart ezp-stunnel ezp-agent` и снова `check`.

## Про подпись

`cryptcp -signf -detached -fext .sig -thumbprint … -cert -strict` даёт
`letter.pdf.sig` — отсоединённую подпись PKCS#7 в DER, как требует п. 7.4.
Ключа `-der` у `cryptcp` нет: DER и так по умолчанию, `-base64` включил бы
Base64. Без `-fext .sig` файл назывался бы `letter.pdf.sgn`.

## Безопасность

ПИН Рутокена лежит в `/etc/ezp-agent/ezp_agent.ini` (`root:ezp-agent 0640`).
Пока токен вставлен, любой с правами root или пользователя `ezp-agent` может
подписывать от имени кооператива. Сам агент подписывает только письма,
которые выдал сайт по его токену.

Когда агент не нужен, токен можно вынимать: письма подождут в очереди. Если
агент больше часа не выходит на связь, сайт показывает предупреждение на
странице писем.
