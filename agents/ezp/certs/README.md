# Сертификаты для агента ЭЗП

Сами файлы в git не хранятся (`.gitignore`): в клиентском сертификате есть
СНИЛС, ИНН и ФИО председателя. Нужны только открытые части; закрытый ключ
остаётся на Рутокене.

| Файл | Что это | SHA1 | Откуда взять |
|---|---|---|---|
| `gm-voshod.cer` | КЭП кооператива (клиентский сертификат TLS и подписи), до 24.10.2027 | `32c441ef7c63a951ab66b4ef585bfe05fe6fdbd7` | с токена: `certmgr -export -container '…' -dest gm-voshod.cer -pem` |
| `CA_FNS_Russia_2025_01.crt` | промежуточный УЦ «Федеральная налоговая служба», до 2039 | `2fef177002f2b1b95ca1a30e10a50cd38a9866f6` | http://pki.tax.gov.ru/crt/ca_fns_russia_2025_01.crt |
| `Mincifry_Rossii_root_guc2022.cer` | корневой ГОСТ «Минцифры России», до 2040 | `2f0cb09be3550ef17ec4f29c90abd18bfcaad63a` | http://crl.gosuslugi.ru/cdp/guc2022.crt |

Все три отправляются в Почту для белого списка. Цепочка ставится в
хранилища компьютера — см. [README агента](../README.md), шаг 4.
`gm-voshod.cer` копируется в `/etc/ezp-agent/client.cer` для stunnel.

Сертификаты `Russian_Trusted_Root_CA`/`Russian_Trusted_Sub_CA` (RSA, НУЦ
Минцифры для HTTPS) в ГОСТ-цепочку КЭП не входят и не нужны.
