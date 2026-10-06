#!/usr/bin/env bash
# Пересобирает всю статику сайта после правки шаблонов:
#   1) website/static/css/tailwind.css — из classes в templates/**/*.html
#   2) website/static/fonts/fa/*.woff2 + website/static/css/fa-subset.css —
#      сабсет иконок (список — в build-fa-subset.js, править при добавлении новой иконки)
# Оба файла инлайнятся в <head> через inline_css() (см. templates_env.py) —
# отдельно как URL не запрашиваются, поэтому кэш-бастинг им не нужен.
set -euo pipefail
cd "$(dirname "$0")"

npx tailwindcss -c tailwind.config.js -i input.css -o ../static/css/tailwind.css --minify
node build-fa-subset.js

echo "OK: website/static/css/{tailwind,fa-subset}.css пересобраны"
