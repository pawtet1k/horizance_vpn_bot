from pathlib import Path
import credentials
from utils import Campaign, days_since

XRAY_CONFIG_PATH = '/usr/local/etc/xray/config.json'

LEGAL_ENTITY_NAME = "Horizance LLC"
LEGAL_ENTITY_ADDRESS = "1209 Mountain Road Pl NE, Ste R, Albuquerque, NM 87110, USA"
PRIVACY_CONTACT_EMAIL = credentials.SMTP_TO

YANDEX_METRIKA_COUNTER_ID = 110419855
GA4_MEASUREMENT_ID = "G-0ENH1QZ2S1"  # client-side tracking snippet, not usable for API reads
GA4_PROPERTY_ID = 544282884  # numeric Admin/Data API ID for horizancevpn.com, resolved via accountSummaries
CLARITY_PROJECT_ID = "xj79u3eid4"
# Живой чат tawk.to (05.10.26). Пустой TAWK_PROPERTY_ID — кнопки чата на сайте нет.
# ID берутся из кода виджета: https://embed.tawk.to/<PROPERTY_ID>/<WIDGET_ID>
TAWK_PROPERTY_ID = "6ac4cbadc86d2a34c444ac1d"
TAWK_WIDGET_ID = "default"

NOTIFY_EMAIL = "james.smith.web@gmail.com"
# Обращения из формы на сайте (/help) идут отдельным адресом, а не в общий
# ящик уведомлений: их читают как переписку с человеком, а не как алерты.
SUPPORT_EMAIL = "support@horizance.ru"

# iOS: Happ убирают из российского App Store по требованию РКН уже второй раз.
# id6788279553 (Happ - Proxy Utility Plus) в сентябре 2026 снят вообще со всех
# витрин - ссылка отдавала 404 и мониторинг check_download_links ругался на неё
# с 03.09. Живая сборка - исходное приложение id6504287215 (Flyfrog LLC), оно
# обновляется (5.7.0 от 03.09.2026) и лежит в глобальном App Store, но не в
# российском. Локаль в URL (/ru/, /us/) на доступность не влияет: её определяет
# страна Apple ID, поэтому локаль не указываем.
VPN_CLIENT_DOWNLOAD_URLS: dict[str, str] = {
    "iOS": "https://apps.apple.com/app/happ-proxy-utility/id6504287215",
    "Android": "https://play.google.com/store/apps/details?id=com.happproxy",
    "Windows": "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe",
    "macOS": "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/Happ.macOS.universal.dmg",
    "Linux": "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/Happ.linux.x64.deb",
    # Телевизор: Happ для Android TV (Google Play) и Apple TV (App Store РФ),
    # подписка переносится с телефона — инструкция на странице /tv.
    "TV": "https://horizancevpn.com/tv",
}

# Запасной клиент для iPhone, когда Happ поставить неоткуда.
#
# Приложение здесь СОЗНАТЕЛЬНО одно, а не список: если каждый ставит что
# захочет, разбирать жалобы придётся про пять разных интерфейсов сразу.
# Один клиент — одна инструкция, один набор известных ошибок.
#
# Почему INCY (заменил Karing 26.09.2026; сверено через itunes.apple.com/lookup&country=ru
# и по нашим логам):
#   * ПЕРЕДАЁТ hwid по умолчанию (x-hwid, модель, ОС) — без этого не работает
#     лимит 3 устройств (device_guard). Karing и Clash Mi умеют, но выключено
#     по умолчанию, и в наших логах Karing приходит без hwid;
#   * бесплатный на всех платформах, есть в российском App Store (iPhone, iPad,
#     Mac, Apple TV), в Google Play (Android, Android TV), для Windows/macOS/Linux
#     — сборки на github.com/INCY-DEV/incy-platforms;
#   * самый популярный из бесплатных в российской витрине: 1514 оценок против
#     332 у Karing (26.09.2026); минус — средняя оценка 3,4;
#   * xray-ядро, VLESS+Reality поверх gRPC — наш случай; подписка добавляется
#     обычным https-адресом, а по ссылке incy://add/<адрес> — в одно касание.
#
# ВНИМАНИЕ: в российской витрине лежат «Happ VPN», «Happ VPN ++», «Happ VPN
# Official» от посторонних издателей (не Flyfrog LLC) — это не Happ, и сюда
# они попасть не должны.
IOS_ALTERNATIVE_CLIENTS: list[dict[str, str]] = [
    {
        "name": "INCY",
        "url": "https://apps.apple.com/app/incy/id6756943388",
        # настоящая иконка из App Store (artworkUrl512), уменьшена до 96px
        "icon": "/static/img/apps/incy.webp",
        "note": "бесплатно, есть в российском App Store",
        # «добавить подписку в одно касание»: incy://crypt1/<зашифрованный адрес>
        # (incy.gitbook.io → Deep Links; библиотека incy-crypto-link от INCY).
        # Прячет адрес подписки от случайного копирования — не защита.
        "add_link": "incy_crypt",
    },
]

TELEGRAM_APP_URLS: dict[str, str] = {
    "iOS": "https://apps.apple.com/app/telegram-messenger/id686449807",
    "Android": "https://play.google.com/store/apps/details?id=org.telegram.messenger",
    # desktop.telegram.org сам определяет ОС и предлагает нужный установщик -
    # не хардкодим версии/архитектуры Windows/macOS/Linux по отдельности
    "Windows": "https://desktop.telegram.org/",
    "macOS": "https://desktop.telegram.org/",
    "Linux": "https://desktop.telegram.org/",
}

default_price = 99
yearly_price = 999
# Подписка для роутера (06.10.26): та же подписка, но без счёта устройств (роутер не шлёт hwid) и с лимитом
# трафика в месяц — им и ограничивается «кафе на 1000 человек»: лимит кончится за часы, доступ закроется
# до следующей оплаты. Безлимитного роутерного тарифа нет намеренно.
router_price = 199
ROUTER_TRAFFIC_LIMIT_GB = 300
# Выгода годовой подписки против 12 месячных — показывается при выборе тарифа (06.10.26: годовую почти
# не выбирали, потому что в момент выбора выгоды не было видно).
yearly_saving_rub = default_price * 12 - yearly_price
yearly_discount_percent = round(100 * yearly_saving_rub / (default_price * 12))
subscription_period = 30
yearly_subscription_period = 365
trial_days = 14
referral_free_days = 30
# Кто не получает реферальный бонус: владелец стоит пригласившим у всех,
# кто пришёл с сайта (BOT_LINK со start=183640993).
OWNER_TELEGRAM_ID = 183640993
REFERRAL_EXCLUDED_IDS = {OWNER_TELEGRAM_ID}
# Промо для вернувшихся неактивных: слово в чат боту (main.handle_message) —
# доступ на PROMO_DAYS от сегодняшнего дня, один раз на человека.
PROMO_WORD = "живой"
PROMO_DAYS = 14
PROMO_CAMPAIGN_NAME = "промо: живой"

# Защита от раздачи триалов (device_guard.py), введена после аварии 22.09.26.
# Триалов с одного IP за окно; окно же — для «второй триал с того же телефона».
TRIAL_IP_LIMIT = 3
TRIAL_WINDOW_DAYS = 30
# Сколько разных устройств (hwid из Happ) может брать одну подписку через /sub.
DEVICE_LIMIT = 3
# Устройства без лимитов — телефон владельца, на нём тестируются триалы и
# чужие аккаунты.
DEVICE_LIMIT_EXEMPT_HWIDS: set[str] = {"layw7phlg0yd2ip2"}

# Мониторинг нод (node_monitor.py), цикл раз в 10 мин.
NODE_MONITOR_INTERVAL_SEC = 600
# Трафик: рекорд исходящего по каждой ноде (Мбит/с, среднее за цикл) хранится в
# файле и обновляется сам; письмо — когда нагрузка дошла до этой доли рекорда.
# Пол — чтобы не писать про «80% от 3 Мбит/с» на тихих нодах.
NODE_TRAFFIC_RECORDS_PATH = str(Path(__file__).resolve().parent / "node_traffic_records.json")
# 1.0 = письмо только при новом рекорде ноды: при 0.8 приходили письма на
# обычные вечерние пики DE (27.09 и 01.10.26)
NODE_TRAFFIC_ALERT_SHARE = 1.0
NODE_TRAFFIC_ALERT_FLOOR_MBPS = 20
# Сторож раздачи ключей: разных IP на один ключ за окно, по всем нодам.
# Норма (NL, 23–25.09.26) — до 5; при раздаче 22.09 были тысячи.
WATCHDOG_WINDOW_MIN = 15
# Одинаково для триала и платных (решение владельца 26.09.26):
WATCHDOG_WARN_IPS = 10        # больше — warning в лог
WATCHDOG_CUT_IPS = 20         # больше — ключ отключается + письмо
NODE_ALERT_COOLDOWN_SEC = 3600

WEBSITE_BASE_URL = "https://horizancevpn.com"

# UDM Pay зарегистрирован на horizance.ru, а не horizancevpn.com - вебхуки
# (серверные, невидимые пользователю) идут туда и прозрачно проксируются
# nginx-ом horizance.ru на 127.0.0.1:8001 (тот же бэкенд, что и WEBSITE_BASE_URL).
# returnLink (виден пользователю в браузере после оплаты) остаётся на
# WEBSITE_BASE_URL - иначе выглядело бы подозрительно и выдавало бы связь
# между двумя доменами.
UDMPAY_WEBHOOK_BASE_URL = "https://horizance.ru"

# UDM Pay (СБП): в проде с 2026-08-13 (боевые credentials.UDMPAY_LOGIN/
# UDMPAY_PASSWORD подтверждены). UDMPAY_SANDBOX переключает хост API на
# dev.pay.udmpay.ru вместо боевого pay.udmpay.ru.
UDMPAY_ENABLED = True
# Сколько минут повторное «Оплатить» отдаёт уже выданную неоплаченную ссылку
# вместо новой: 05.10.26 человек получил 4 ссылки за 8 минут и оплатил две.
PAYMENT_LINK_REUSE_MINUTES = 15
UDMPAY_SANDBOX = False

PLAN_PERIOD_DAYS = {
    default_price: subscription_period,
    yearly_price: yearly_subscription_period,
    router_price: subscription_period,
}

# На сколько "периодов" (месяцев) продвигать paid_periods за один платёж -
# для годовой подписки это 12, а не 1, иначе счётчик перестаёт отражать
# реальное количество оплаченных месяцев.
PLAN_PERIODS_INCREMENT = {
    default_price: 1,
    yearly_price: 12,
    router_price: 1,
}


def get_billing_period_days(price: int) -> int:
    return PLAN_PERIOD_DAYS.get(price, subscription_period)


def get_periods_increment(price: int) -> int:
    return PLAN_PERIODS_INCREMENT.get(price, 1)


# NOWPayments (крипто, USDT и другое - выбор монеты остаётся за клиентом на
# странице оплаты, вывод в единую валюту настраивается в личном кабинете
# NOWPayments как Outcome Wallet, в коде не участвует). Выключено, пока не
# пройдёт первый настоящий тестовый платёж - см. project_nowpayments_integration
# в памяти агента. Настоящего автосписания в крипте нет технически (см. историю
# решения) - каждый период это новый разовый счёт, как и раньше с ручным переводом.
NOWPAYMENTS_ENABLED = True

# Цены для крипто-оплаты - отдельные от рублёвых default_price/yearly_price
# (курс не конвертируется на лету, эти цифры зафиксированы явно, ~100 руб/$).
# ВРЕМЕННЫЕ значения (2026-08-14, обсуждаем окончательные с пользователем):
# $1 для месячного тарифа не работал вообще ни на одной сети - у NOWPayments
# у каждой валюты/сети есть свой минимум, и дешевле ~$1.72 (XRP, самый дешёвый
# из проверенных 18 валют) не бывает нигде; TRC20 (Tron) требует вообще от
# $13.5+ - при такой цене подписки не сработает никогда. Но даже $1.7-1.9,
# которые проходят у NOWPayments, физически не вывести с Binance (минимум
# вывода $5) или Bybit (минимум ~$10) - это отдельный, более высокий порог
# самой биржи, не зависящий от выбора сети. Проверено через API 2026-08-14.
CRYPTO_PRICE_USD = {
    default_price: 1.8,
    yearly_price: 11.9,
    router_price: 3.6,
}


def get_crypto_price_usd(price: int) -> int:
    return CRYPTO_PRICE_USD.get(price, 1)


# NOWPayments не даёт ограничить выбор внутри ОДНОГО счёта несколькими
# валютами - либо конкретная одна (pay_currency), либо выбор из всех ~300.
# ВАЖНО: минимум у NOWPayments (проверено API, 18 валют, $1.72-13.7) - это
# не то же самое, что минимум ВЫВОДА с биржи, у которой клиент реально держит
# деньги. У Binance минимум вывода USDT - $5 вне зависимости от сети, у Bybit -
# ~$10 - то есть при месячной цене $1.8 ни с одной из этих двух бирж платёж
# физически не отправить, каким бы дешёвым ни был сам минимум NOWPayments.
# Пока (обсуждается с пользователем) для месяца форсим одну сеть без выбора -
# не потому что она "работает с Binance/Bybit" (не работает при такой цене),
# а чтобы клиент не наткнулся на пугающую ошибку "less than minimal" от
# NOWPayments, выбрав валюту наугад. Для года (сумма выше бирж-минимумов)
# выбор оставлен открытым - см. get_crypto_pay_currency().
CRYPTO_PAY_CURRENCY = {
    default_price: "usdtbsc",
    router_price: "usdtbsc",
    # yearly_price: не задано намеренно - выбор открыт (см. docstring выше)
}


def get_crypto_pay_currency(price: int) -> str | None:
    return CRYPTO_PAY_CURRENCY.get(price)


def get_periods_increment(price: int) -> int:
    return PLAN_PERIODS_INCREMENT.get(price, 1)


def _happ_setup_issue(row: dict) -> bool:
    return (
        row['traffic_in'] is None and row['traffic_out'] is None
        and days_since(row['start_date']) == 1
    )

def _vpn_not_enabled(row: dict) -> bool:
    return (
        row['traffic_in'] == 0 and row['traffic_out'] == 0
        and days_since(row['start_date']) == 3
    )

NOTIFICATION_CAMPAIGNS = [
    Campaign(
        _happ_setup_issue,
        "👋 Не получается настроить VPN? Напишите /help — поможем за пару минут! 🚀",
        "не установлен Happ"
    ),
    Campaign(
        _vpn_not_enabled,
        "🔌 Приложение установлено, но VPN выключен. Включите его в Happ 🔘. "
        "Не получается — напишите /help!",
        "VPN не включен"
    ),
]
