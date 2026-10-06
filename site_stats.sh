#!/usr/bin/env bash
# Посещаемость сайтов по логам nginx через GoAccess.
#
# Зачем: счётчики (Метрика, GA4, Clarity) грузятся только после согласия на
# cookie, поэтому видят меньшую часть аудитории. Логи nginx видят всех, ничего
# никуда не передают и согласия не требуют — это единственный полный источник.
#
# Использование:
#   ./site_stats.sh              краткая сводка за сегодня (её читает Claude Code)
#   ./site_stats.sh --days 30    за последние N суток (нужно почти всегда:
#                                logrotate режет лог каждую ночь, поэтому без
#                                этого флага видно только текущие сутки)
#   ./site_stats.sh --html       плюс HTML-отчёт в my_logger/stats/
#   ./site_stats.sh --old        считать по архиву в старом формате (до 26.08.2026)
#
# Логи nginx лежат под root:adm, поэтому нужен sudo.
set -euo pipefail

cd "$(dirname "$0")"
CONF="$PWD/goaccess.conf"
OUT_DIR="$PWD/my_logger/stats"
# Только текущий лог — он один в новом формате (с $host первым полем).
LOGS="/var/log/nginx/access.log"
FORMAT_ARGS=()
DAYS=""
NEW_FORMAT_ONLY=0

prev=""
for arg in "$@"; do
  if [ "$arg" = "--old" ]; then
    # весь архив до перехода на vhost_combined 26.08.2026: поля $host там нет
    LOGS="/var/log/nginx/access.log.oldformat /var/log/nginx/access.log.[0-9] /var/log/nginx/access.log.*.gz"
    FORMAT_ARGS=(--log-format='%h %^[%d:%t %^] "%r" %s %b "%R" "%u"')
  fi
  if [ "$prev" = "--days" ]; then
    DAYS="$arg"
  fi
  prev="$arg"
done

if [ -n "$DAYS" ]; then
  # Ротация ежедневная, поэтому «за месяц» — это текущий лог плюс архивы.
  # Архив смешанный: файлы старше 26.08.2026 написаны прежним форматом без
  # $host. Отбрасываем их построчно (в новом формате 3-е и 4-е поля — "-",
  # в старом — 2-е и 3-е), иначе GoAccess спотыкается на каждой строке.
  LOGS="$(find /var/log/nginx -maxdepth 1 -name 'access.log*' \
            ! -name '*.oldformat' -mtime "-$DAYS" -print | sort | tr '\n' ' ')"
  NEW_FORMAT_ONLY=1
fi

# Отсев обращений к сайту по IP-адресу вместо доменного имени. Это не живые
# посетители: на 443 порт нод стучится Reality-заглушка (xray отдаёт такой
# трафик на локальный веб-сервер) плюс сканеры, которые ходят по голым IP.
# В отчёте за 26.08-06.09 таких «гостей» было 3253 против 2206 настоящих на
# horizancevpn.com — то есть без фильтра цифра завышалась больше чем вдвое.
#
# ВАЖНО: это отсекает только обращения по IP. Зонды, пришедшие с настоящим
# SNI (www.horizance.ru — он же используется для маскировки), в логах
# неотличимы от обычных посетителей horizance.ru и остаются в выборке.
drop_ip_hosts() {
  awk '$1 !~ /^([0-9]{1,3}\.){3}[0-9]{1,3}$/ && $1 !~ /:/'
}

read_logs() {
  if [ "$NEW_FORMAT_ONLY" = "1" ]; then
    sudo zcat -f $LOGS 2>/dev/null | awk '$3=="-" && $4=="-"' | drop_ip_hosts
  else
    sudo zcat -f $LOGS 2>/dev/null
  fi
}

# set -o pipefail ломает проверку через head (zcat получает SIGPIPE),
# поэтому считаем строки без обрыва пайпа
if [ "$(read_logs | wc -l)" -eq 0 ]; then
  echo "Логи не читаются. Нужен sudo и наличие /var/log/nginx/access.log*" >&2
  exit 1
fi

if [[ " $* " == *" --html "* ]]; then
  mkdir -p "$OUT_DIR"
  REPORT="$OUT_DIR/nginx-$(date +%F).html"
  read_logs | goaccess - --config-file="$CONF" "${FORMAT_ARGS[@]}" \
      -o "$REPORT" >/dev/null 2>&1
  echo "HTML-отчёт: $REPORT"
fi

# Сводка в JSON и короткая выжимка из неё — то, что удобно читать глазами
# и агенту, без сотен килобайт HTML.
read_logs | goaccess - --config-file="$CONF" "${FORMAT_ARGS[@]}" \
    -o json 2>/dev/null | python3 "$PWD/site_stats_summary.py"
