# Пакеты КриптоПро CSP для агента ЭЗП

Плейбук (`deployment/ansible`, `local_ezp_agent: true`) ставит КриптоПро на
компьютер председателя из этого каталога. Дистрибутив выдаётся на
cryptopro.ru после регистрации, распространять его нельзя. Поэтому `.deb`
лежат только локально, в git их нет (`.gitignore`). Лицензия CSP не
нужна: она встроена в КЭП от ФНС.

Сюда скопировать из дистрибутива `linux-amd64_deb.tgz` (КриптоПро CSP 5.0,
другая сборка тоже подойдёт: плейбук ищет файлы по имени пакета `<пакет>_*.deb`):

| Файл | Зачем |
|---|---|
| `lsb-cprocsp-base_*_all.deb` | основа CSP |
| `lsb-cprocsp-rdr-64_*_amd64.deb` | считыватели |
| `lsb-cprocsp-kc1-64_*_amd64.deb` | провайдер КС1 |
| `lsb-cprocsp-capilite-64_*_amd64.deb` | `cryptcp`, `certmgr`, `csptest` |
| `cprocsp-curl-64_*_amd64.deb` | загрузка CRL/OCSP при проверке цепочки |
| `lsb-cprocsp-ca-certs_*_all.deb` | корневые сертификаты |
| `cprocsp-rdr-pcsc-64_*_amd64.deb` | токены через `pcscd` |
| `cprocsp-rdr-rutoken-64_*_amd64.deb` | Рутокен |
| `cprocsp-stunnel-64_*_amd64.deb` | ГОСТ-TLS туннель до Почты (`stunnel_thread`) |
| `ifd-rutokens_*_amd64.deb` | драйвер Рутокен S (Рутокен lite/ЭЦП работают и через `libccid`) |
| `cprocsp-compat-debian_*_all.deb` | только если в системе нет пакета `lsb-base` |

Это тот же набор, который ставит `install.sh kc1 cprocsp-rdr-pcsc
cprocsp-rdr-rutoken cprocsp-stunnel` из дистрибутива, без GUI-пакетов.

```bash
D=~/Downloads/linux-amd64_deb
for p in lsb-cprocsp-base lsb-cprocsp-rdr-64 lsb-cprocsp-kc1-64 lsb-cprocsp-capilite-64 \
         cprocsp-curl-64 lsb-cprocsp-ca-certs cprocsp-rdr-pcsc-64 cprocsp-rdr-rutoken-64 \
         cprocsp-stunnel-64 ifd-rutokens cprocsp-compat-debian; do
    cp "$D"/${p}_*.deb agents/ezp/cryptopro/
done
```

Каталог — `ezp_cryptopro_dir` в `vars.yml`. Если пакеты уже установлены,
файлы не нужны.
