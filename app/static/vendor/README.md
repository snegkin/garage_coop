# Сторонние библиотеки фронтенда

Раздаются с нашего сервера, а не с CDN: сайт не зависит от доступности
стороннего сервиса, а браузеры посетителей не обращаются к нему (их адреса
никуда не уходят). Каждый пакет лежит в папке `<пакет>-<версия>/` — путь
меняется вместе с версией, поэтому файлы отдаются с кэшем на год (см.
`app/__init__.py: _App.get_send_file_max_age`; если статику отдаёт nginx,
то же самое — `location /static/vendor/ { expires 1y; }`).

| Папка | Пакет npm | Где используется |
|---|---|---|
| `bootstrap-5.3.3` | `bootstrap@5.3.3` (`dist/css/bootstrap.min.css`, `dist/js/bootstrap.bundle.min.js`) | все страницы (`base.html`) |
| `bootstrap-icons-1.13.2` | `bootstrap-icons@1.13.2` (`font/bootstrap-icons.min.css`, `font/fonts/*`) | все страницы, иконки `bi bi-*` |
| `chart.js-4.4.4` | `chart.js@4.4.4` (`dist/chart.umd.js` — уже минифицирован) | дашборд, лицевые счета, мониторинг электропитания |
| `chartjs-adapter-date-fns-3.0.0` | `chartjs-adapter-date-fns@3.0.0` | там же, где Chart.js |
| `chartjs-plugin-zoom-2.0.1` | `chartjs-plugin-zoom@2.0.1` | мониторинг электропитания |
| `hammerjs-2.0.8` | `hammerjs@2.0.8` | мониторинг электропитания (жесты для zoom) |
| `mermaid-11.17.2` | `mermaid@11.17.2` (`dist/mermaid.min.js` — однофайловая сборка) | схема контрольных счётчиков |

Лицензии (MIT) — рядом с файлами, в каждой папке. Карты исходников
(`*.map`) не копировались: браузер запрашивает их только при открытых
инструментах разработчика.

## Обновление

Сборщика нет и он не нужен: файлы кладутся как есть из официального
пакета npm, проверенного по контрольной сумме реестра.

```sh
name=bootstrap; ver=5.3.4
meta=$(curl -s https://registry.npmjs.org/$name/$ver)
curl -so pkg.tgz "$(echo "$meta" | python3 -c 'import sys,json;print(json.load(sys.stdin)["dist"]["tarball"])')"
echo "$meta" | python3 -c 'import sys,json;print(json.load(sys.stdin)["dist"]["integrity"])'
echo "sha512-$(openssl dgst -sha512 -binary pkg.tgz | base64 -w0)"   # должно совпасть с строкой выше
tar -xzf pkg.tgz   # → package/
```

Новые файлы — в новую папку `<пакет>-<новая версия>/`, затем поменять путь
в шаблонах (`grep -rn "vendor/<пакет>-" app/templates`) и удалить старую папку.
