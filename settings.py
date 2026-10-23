import re
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
# Роутер (06.10.26): не отдельный тариф, а вариант при настройке — та же подписка за
# default_price. Выбирается вместо ОС (device_os = ROUTER_DEVICE), ссылка у человека одна: смена режима
# обычный ↔ роутер выдаёт новый uuid, старый из xray удаляется. Лимит ROUTER_TRAFFIC_LIMIT_GB считается
# окнами по ROUTER_PERIOD_DAYS от traffic_period_date (и заново с каждой оплатой); исчерпал — роутер
# отключается от xray до начала следующего окна (node_monitor._router_quota). Лимитом и ограничивается
# «кафе на 1000 человек»: роутер за NAT серверу виден как одно подключение, устройства за ним не посчитать.
# Безлимитного роутера нет намеренно.
ROUTER_TRAFFIC_LIMIT_GB = 300
ROUTER_DEVICE = "Router"
ROUTER_LABEL = "Роутер"
ROUTER_LABEL_EN = "Router"
ROUTER_PERIOD_DAYS = 30
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
# Ссылки сайта на бота: t.me/<бот>?start=site_<страница> (14.10.26; раньше у всех был start=183640993 и источник не был
# виден). Бот пишет страницу в лог «Пришёл с сайта: <страница>» и ставит пригласившим владельца — как раньше, на этом
# держится «ссылка с сайта» в stats_growth.py. Старые ссылки с start=183640993 (кэш, заголовки подписки в Happ) работают.
BOT_URL = "https://t.me/Horizance_vpn_bot"
SITE_START_PREFIX = "site_"


def site_bot_link(src: str) -> str:
    """Ссылка на бота с меткой страницы сайта; payload в Telegram — до 64 символов [A-Za-z0-9_-]."""
    return f"{BOT_URL}?start={SITE_START_PREFIX}{re.sub(r'[^A-Za-z0-9_-]', '_', src)[:64 - len(SITE_START_PREFIX)]}"
# Вход на сайт через бота (routers/auth.py → bot.handle_web_login): ссылка t.me/…?start=login_<token>.
# Токен живёт WEB_LOGIN_TTL секунд (сайт сам показывает новый QR), подтверждённый вход сайт забирает
# за WEB_LOGIN_APPROVED_TTL секунд — иначе он сгорает.
WEB_LOGIN_PREFIX = "login_"
WEB_LOGIN_TTL = 180
WEB_LOGIN_APPROVED_TTL = 300


def web_login_link(token: str) -> str:
    return f"{BOT_URL}?start={WEB_LOGIN_PREFIX}{token}"


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
# Доп. устройства (владелец, 13.10.26) — опция подписки, как add-on в SaaS (co-terming): +EXTRA_DEVICES_COUNT к лимиту,
# продлевается вместе с подпиской одним платежом (месяц 99 + 99 ₽, год 999 + 999 ₽ — опция стоит как ещё один такой же
# тариф). Подключил посреди оплаченного срока — сразу доплата только за остаток срока (пропорционально дням), и
# дальше одна дата продления. На триале — опция добавляется к первой оплате. Отключить можно в /devices: устройства
# останутся до конца оплаченного срока, со следующего продления — без опции.
EXTRA_DEVICES_COUNT = 3
EXTRA_DEVICES_PRICE = 99          # ₽ за месяц; за год — в той же пропорции, что тариф (get_extra_devices_price)
# Минимальная доплата за остаток срока: меньше этих сумм провайдер счёт не создаст (у NOWPayments минимум
# $1.72–13.7 в зависимости от монеты, см. CRYPTO_PAY_CURRENCY ниже) — берём минимум, а не отказываем.
EXTRA_DEVICES_MIN_CHARGE_RUB = 10
EXTRA_DEVICES_MIN_CHARGE_USD = 1.8


def get_extra_devices_price(plan_price: int) -> int:
    return round(plan_price * EXTRA_DEVICES_PRICE / default_price) if plan_price else 0
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
# Пинг UDM Pay (node_monitor.run_udmpay_watch)
UDMPAY_WATCH_INTERVAL_SEC = 60
# Замер доступности нод для клиентов (node_monitor.run_uptime_probe → my_logger/stats/node_uptime.csv, отчёт analytics/uptime.py)
NODE_UPTIME_INTERVAL_SEC = 60
NODE_UPTIME_PORT = 443
NODE_UPTIME_TIMEOUT_SEC = 5
# INCY (iPhone) понимает тот же профиль маршрутизации, что Happ (поля DirectSites/ProxySites…), только со схемой
# incy://routing/onadd/ (документация INCY-DEV/incy-docs, ru/dev-docs/routing.md). 13.10.26 на устройстве не проверено —
# пока выключено для всех; проверка: /sub/<uuid>?incy_routing=1 из INCY. Заработает — поставить True.
INCY_ROUTING_ENABLED = True  # включено 14.10.26: владелец проверил на iPhone — Яндекс видит реальный IP, Google — IP VPN
# Что видит человек из блок-листа владельца (device_guard.is_blocked) в боте и на сайте
BLOCKED_TEXT = ("Ваш аккаунт заблокирован: доступ к Horizance VPN для вас закрыт навсегда.",
                "Your account is blocked: access to Horizance VPN is permanently closed for you.")

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
}

# На сколько "периодов" (месяцев) продвигать paid_periods за один платёж -
# для годовой подписки это 12, а не 1, иначе счётчик перестаёт отражать
# реальное количество оплаченных месяцев.
PLAN_PERIODS_INCREMENT = {
    default_price: 1,
    yearly_price: 12,
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
    yearly_price: 12.9,   # владелец 18.10.26: год $12.9 (было $11.9) — покрывает комиссии Whop за не-долларовую карту
}
# Иностранцу цены — только в долларах (владелец 18.10.26), скидка года — от долларовых цен, не от рублёвых
yearly_saving_usd = round(CRYPTO_PRICE_USD[default_price] * 12 - CRYPTO_PRICE_USD[yearly_price], 2)
yearly_discount_percent_usd = round(100 * yearly_saving_usd / (CRYPTO_PRICE_USD[default_price] * 12))


def to_usd(rub_amount: int, plan_price: int) -> float:
    """Рублёвая сумма заказа (тариф, опция) → доллары по курсу тарифа plan_price (как берут крипта и Whop)."""
    return round(get_crypto_price_usd(plan_price) * rub_amount / plan_price, 2) if plan_price else 0


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

# Заплатил, а VPN почти не пользуется (владелец 18.10.26): из 8 не продливших первый месяц 5 за месяц набрали 0–125 МБ —
# потеряны на подключении, а не на цене. Через PAID_NOT_USING_DAY дней после оплаты — напоминание с кнопкой настройки,
# через PAID_NOT_USING_REPORT_DAY — список владельцу (utils.report_paid_not_using), дальше пишем лично.
PAID_NOT_USING_MB = 50
PAID_NOT_USING_DAY = 2
PAID_NOT_USING_REPORT_DAY = 4


def paid_not_using(row: dict, day: int) -> bool:
    return (
        row['status'] == 'active' and bool(row['paid_date']) and row['device_os'] != ROUTER_DEVICE
        and days_since(row['paid_date']) == day
        and (row['traffic_in'] or 0) + (row['traffic_out'] or 0) - (row['paid_traffic_mb'] or 0) < PAID_NOT_USING_MB
    )


NOTIFICATION_CAMPAIGNS = [
    Campaign(
        _happ_setup_issue,
        "👋 Не получается настроить VPN? Напишите /help — поможем за пару минут! 🚀",
        "не установлен Happ",
        "👋 Having trouble setting up the VPN? Send /help — we'll help in a couple of minutes! 🚀",
    ),
    Campaign(
        _vpn_not_enabled,
        "🔌 Приложение установлено, но VPN выключен. Включите его кнопкой подключения в приложении 🔘. "
        "Не получается — напишите /help!",
        "VPN не включен",
        "🔌 The app is installed, but the VPN is off. Turn it on with the connect button in the app 🔘. "
        "Doesn't work — send /help!",
    ),
    Campaign(
        lambda row: paid_not_using(row, PAID_NOT_USING_DAY),
        "👋 Спасибо за оплату! За эти два дня VPN почти не использовался. Нажмите кнопку — бот пришлёт настройку "
        "для вашего устройства. Не получается — напишите сюда, поможем.",
        "заплатил и не пользуется",
        "👋 Thanks for your payment! The VPN has barely been used in these two days. Tap the button and the bot will "
        "send the setup for your device. Doesn't work — write here and we'll help.",
        [("🔌 Подключить VPN", "generate_settings_link")],
        [("🔌 Connect VPN", "generate_settings_link")],
    ),
]

# Тест транспорта XHTTP (10.10.26): у этих подписок первой строкой идёт NL на порту 8443 через XHTTP
# (инбаунд vless_xhttp, stream-one, есть в config.json NL с 25.08). Kosta_Kibardin (225268791): мобильный МТС,
# Telegram-файлы по gRPC не уходят. uuid надо вручную прописать в инбаунд (xray api adu --tag=vless_xhttp,
# port 8443) и снять после теста — node_monitor этот инбаунд не обслуживает.
XHTTP_TEST_UUIDS: set[str] = {"b8853d1c-bf7d-470a-a267-b5138b64b507"}
XHTTP_TEST_HOST = "193.233.127.32"

# Брошенная оплата: заказ создан, а через столько минут ни он, ни следующие не оплачены → владельцу в Telegram и на
# почту. 10.10.26 выяснилось, что Shailesh 18.09 четыре раза пытался оплатить СБП (иностранец, СБП — только
# российские банки) и об этом никто не узнал: сообщали только об успешных оплатах.
ABANDONED_ORDER_MINUTES = 30

# Сколько письмо об оплате UDM Pay ждёт ответа банка на привязку автоплатежа (17.10.26: одно письмо вместо двух)
AUTOPAY_WAIT_SEC = 60
