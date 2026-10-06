from fastapi import APIRouter, Request, Depends
from fastapi.responses import RedirectResponse, PlainTextResponse
import asyncio
import base64
from datetime import date, datetime
import httpx
import json
from pathlib import Path
import subprocess
import sqlite3
import time
from my_logger import logger
from models import UserStatus
from request_helpers import get_client_ip
import credentials
import settings
from templates_env import templates
from vpn import VPNManager
import device_guard

_vpn = VPNManager()


BOT_DB_PATH = str(Path("~/horizance_vpn_bot/users.db").expanduser().resolve())
#DB_TABLES = {"users", "trial_sessions"}
ROUTING_TTL = 24 * 3600
routing_deeplink = None
routing_deeplink_fetch_time: float = 0.0
TITLE_MAIN = base64.b64encode("Horizance VPN".encode()).decode()
TITLE_TRIAL = base64.b64encode("Horizance VPN Trial".encode()).decode()
BOT_LINK = "https://t.me/Horizance_vpn_bot?start=183640993"

# Подписки-заглушки. Happ рисует #-фрагмент vless-ссылки как имя «сервера»
# в списке и обрезает его после STUB_NAME_MAX_LEN символов, поэтому текст
# разносится на несколько коротких «строк-серверов» (длину проверяет тест).
# Порты разные, иначе Happ схлопнет одинаковые записи в одну.
STUB_NAME_MAX_LEN = 28


def _stub_content(*names: str) -> str:
    # Happ показывает эти строки в обратном порядке (владелец, 01.10.26:
    # первая оказывалась последней), поэтому отдаём их перевёрнутыми.
    return "\n".join(
        f"vless://00000000-0000-0000-0000-000000000000@0.0.0.0:{port}#{name}"
        for port, name in enumerate(reversed(names), start=1)
    )


# uuid, которого больше нет в базе (обычно — устаревшая ссылка в приложении)
STALE_SUBSCRIPTION_CONTENT = _stub_content(
    "Эта подписка устарела!",
    "Удалите её, а новую",
    "получите в нашем ТГ-боте",
)
INACTIVE_SUBSCRIPTION_CONTENT = _stub_content(
    "Подписка закончилась",
    "Продлите её в нашем ТГ-боте",
)
TRIAL_OVER_CONTENT = _stub_content(
    "Пробный период закончился",
    "Оплата: horizancevpn.com/pay",
    "или в ТГ-боте:",
    "@horizance_vpn_bot",
)
# Строки заглушки в Happ не кликабельны, поэтому истёкшему триалу ещё и
# меняем заголовки: кнопка «веб-страница» профиля и баннер ведут на оплату.
TRIAL_OVER_PAY_URL = f"https://{credentials.SUBSCRIPTION_DOMAIN}/pay"
TRIAL_OVER_ANNOUNCE = (
    f"Пробный период закончился. Подписка от {settings.default_price} ₽ в месяц — "
    f"{credentials.SUBSCRIPTION_DOMAIN}/pay или в боте @Horizance_vpn_bot"
)

# Что отдаём устройству, которому device_guard отказал. Ключей в ответе нет —
# в этом и смысл: голый vless-ключ дальше не контролируется никак.
DEVICE_REFUSAL_CONTENT = {
    "device_limit": _stub_content(
        f"Лимит: {settings.DEVICE_LIMIT} устройства",
        "Нужно больше? Напишите",
        "в поддержку в нашем ТГ-боте",
    ),
    "trial_reuse": _stub_content(
        "Пробный период на этом",
        "устройстве уже был",
        "Подписка — в нашем ТГ-боте",
    ),
    "banned": _stub_content("Доступ заблокирован"),
}


class RoutingCache:
    def __init__(self):
        self._value: str = None
        self._timestamp: float = 0.0
        self._ttl = 24 * 3600
        self._lock = asyncio.Lock()

    async def get(self) -> str|None:
        async with self._lock:
            if self._is_outdated():
                new_value = await fetch_routing_deeplink()
                if new_value:
                    self._value = new_value
                    self._timestamp = time.time()
            return self._value

    def _is_outdated(self) -> bool:
        return (
            not self._value or
            time.time() - self._timestamp > self._ttl
        )

def get_user_by_uuid(table, client_uuid: str) -> dict | None:
    with sqlite3.connect(BOT_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f"SELECT * FROM {table} WHERE client_uuid = ?",
            (client_uuid,)
        ).fetchone()
    return dict(row) if row else None

def _get_user(table, user_id) -> dict | None:
    with sqlite3.connect(BOT_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f"SELECT * FROM {table} WHERE user_id = ?",
            (user_id,)
        ).fetchone()
    return dict(row) if row else None

def find_user_by_hwid(hwid: str) -> dict | None:
    """Ищет владельца по железному идентификатору устройства.

    fingerprint хранится как "<модель>|<hwid>", и hwid у устройства не
    меняется при перевыпуске client_uuid. Это единственная зацепка, по которой
    можно опознать хозяина протухшей ссылки-подписки.
    """
    if not hwid:
        return None
    with sqlite3.connect(BOT_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        for table in ("users", "trial_sessions"):
            row = conn.execute(
                f"SELECT * FROM {table} WHERE fingerprint LIKE ?",
                (f"%|{hwid}",)
            ).fetchone()
            if row:
                return dict(row)
    return None


def log_unknown_uuid(client_uuid: str, request: Request) -> None:
    from error_alerts import notify  # локальный импорт: error_alerts тянет utils

    headers = request.headers
    hwid = headers.get("x-hwid")
    owner = find_user_by_hwid(hwid)
    if owner:
        who = (
            f"ЭТО user_id={owner['user_id']} (@{owner.get('username')}), "
            f"актуальный uuid={owner.get('client_uuid')}"
        )
    else:
        who = "владельца опознать не удалось"
    details = (
        f"uuid не найден ни в users, ни в trial_sessions: {client_uuid}; "
        f"hwid={hwid} ip={get_client_ip(request)} "
        f"os={headers.get('x-device-os')} "
        f"model={headers.get('x-device-model')} "
        f"ua={headers.get('user-agent')} — {who}"
    )
    logger.error(details)
    # раньше об этом узнавали по письму про UnboundLocalError; ошибки больше
    # нет, поэтому шлём письмо явно — не чаще раза в сутки на один uuid,
    # иначе брошенный клиент с почасовым опросом завалит почту
    notify(
        key=f"unknown_uuid:{client_uuid}:{date.today()}",
        subject="Horizance VPN: обращение по устаревшей ссылке-подписке",
        body=details,
    )


def is_user_inactive(user: dict) -> bool:
    #user = _get_user('users', user_id)
    if user.get("status", None):
        return user["status"] == UserStatus.INACTIVE
    return time.time()>get_expire_date(user).timestamp()

def save_traffic(user_id: int, traffic: dict) -> dict:
    """Прибавляет снятые показания к накопленному в базе и возвращает итог.

    Раньше здесь была перезапись: в базу клалось текущее значение счётчика
    xray. Счётчик живёт в памяти процесса и обнуляется при каждом рестарте
    ноды, поэтому накопленный трафик регулярно обнулялся вместе с ним.

    Теперь снимаем показания с обнулением (`statsquery -reset`) и
    прибавляем к тому, что уже записано. Точка накопления — обращение
    приложения за подпиской, оно происходит примерно раз в час у каждого
    устройства, так что между снятиями счётчик не успевает ни переполниться,
    ни потеряться.

    Остаток меньше мегабайта при делении отбрасывается: колонка хранит
    мегабайты, а не байты.
    """
    delta_in = traffic["uplink"] // 1000000
    delta_out = traffic["downlink"] // 1000000
    table = "trial_sessions" if str(user_id).startswith("t_") else "users"
    with sqlite3.connect(BOT_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            f"UPDATE {table} SET "
            f"traffic_in = COALESCE(traffic_in, 0) + ?, "
            f"traffic_out = COALESCE(traffic_out, 0) + ? "
            f"WHERE user_id = ?",
            (delta_in, delta_out, user_id)
        )
        row = conn.execute(
            f"SELECT traffic_in, traffic_out FROM {table} WHERE user_id = ?",
            (user_id,)
        ).fetchone()
    total = {
        "uplink": (row["traffic_in"] or 0) if row else 0,
        "downlink": (row["traffic_out"] or 0) if row else 0,
    }
    logger.debug(
        f"{user_id}: трафик +{delta_in}/{delta_out} МБ, "
        f"накоплено {total['uplink']}/{total['downlink']} МБ"
    )
    return total

def save_device_os(table_name, user_id: int, device_os: str):
    with sqlite3.connect(BOT_DB_PATH) as conn:
        conn.execute(
            f"UPDATE {table_name} SET device_os = ? WHERE user_id = ?",
            (device_os, user_id)
        )
    logger.info(f"{user_id}: device_os сохранён - {device_os}")

async def save_client_location(table_name, user_id, request) -> None:
    """Сохраняет IP и регион клиента в том же формате, что и trial_sessions:
    "<ip>|('<регион>', '<город>')". Вызывается один раз — вместе с первой
    привязкой устройства, а не на каждый опрос подписки: ip-api.com отвечает
    по http и лимитирован по частоте.
    """
    from routers.trial import get_ip_location  # локальный импорт: trial тянет vpn

    client_ip = get_client_ip(request)
    try:
        location = await get_ip_location(client_ip)
        value = (
            f"{client_ip}|{(location['region'], location['city'])}"
            if location else client_ip
        )
    except Exception as e:
        logger.warning(f"{user_id}: регион по ip не определён: {e}")
        value = client_ip
    with sqlite3.connect(BOT_DB_PATH) as conn:
        conn.execute(
            f"UPDATE {table_name} SET client_ip = ? WHERE user_id = ?",
            (value, user_id)
        )
    logger.info(f"{user_id}: client_ip сохранён - {value}")


def save_fingerprint(table_name, user_id: int, device_model: str, hwid: str):
    fingerprint = f"{device_model}|{hwid}"
    with sqlite3.connect(BOT_DB_PATH) as conn:
        conn.execute(
            f"UPDATE {table_name} SET fingerprint = ? WHERE user_id = ?",
            (fingerprint, user_id)
        )
    logger.info(f"{user_id}: fingerprint сохранён - {fingerprint}")

ROUTING_DEEPLINK_PREFIX = "happ://routing/onadd/"

# Второй уровень защиты от рекламы — на устройстве, поверх серверного правила
# geosite:category-ads-all в /usr/local/etc/xray/config.json.
#
# Зачем он нужен, если на сервере уже блокируется: профиль маршрутизации
# отправляет geosite:category-ru НАПРЯМУЮ, мимо нашего сервера. То есть на
# российских сайтах серверное правило не работает вообще — а именно там
# крутится an.yandex.ru и mc.yandex.ru. Закрыть эту дыру можно только на
# стороне клиента, и самый дешёвый способ — фильтрующий DNS.
#
# Замеры с нашей же российской ноды 06.09.2026 (DoH, wireformat):
#   77.88.8.8 (Яндекс, что стояло раньше) — doubleclick.net, an.yandex.ru,
#       mc.yandex.ru отдаются нормальными адресами, не блокирует НИЧЕГО;
#   dns.adguard-dns.com  — все три отдаёт как 0.0.0.0, google.com живой;
#   dns.comss.one        — все три отдаёт как 0.0.0.0, google.com живой.
# Оба резолвера с российского IP доступны (443/tcp открыт).
#
# Comss.one стоит доменным (российским) резолвером: он хостится в России,
# поэтому на прямых запросах не добавляет задержки, и ведётся с оглядкой на
# российские рекламные сети. AdGuard DNS — удалённым.
#
# Побочный эффект, о котором надо помнить: mc.yandex.ru блокируется и для
# нашего собственного сайта, так что Яндекс.Метрика у пользователей с нашим
# VPN считать не будет. Логи nginx это видят, счётчики — нет.
ROUTING_OVERRIDES = {
    "Name": "Horizance",
    "RemoteDns": "94.140.14.14",
    "RemoteDNSType": "DoH",
    "RemoteDNSDomain": "https://dns.adguard-dns.com/dns-query",
    "RemoteDNSIP": "94.140.14.14",
    "DomesticDns": "195.133.25.16",
    "DomesticDNSType": "DoH",
    "DomesticDNSDomain": "https://dns.comss.one/dns-query",
    "DomesticDNSIP": "195.133.25.16",
}


def _apply_routing_overrides(deeplink: str) -> str:
    """Подмешивает наши настройки в чужой профиль маршрутизации.

    Профиль тянется из репозитория roscomvpn — там поддерживаются в
    актуальном состоянии geosite/geoip и списки «что напрямую, что через
    прокси» под российские реалии, и переписывать это самим смысла нет.
    А вот DNS оттуда нам не подходит (см. ROUTING_OVERRIDES), поэтому
    правим ровно те поля, которые перечислены, и оставляем всё остальное
    как есть — включая ссылки на geosite/geoip, которые они обновляют.

    Их geosite, кстати, крошечный (67 КБ, 23 группы) и в BlockSites у них
    стоит geosite:category-ads РОВНО НА ДВА домена. Подменять Geositeurl на
    Loyalsoldier (170 624 домена в category-ads-all) нельзя: у них нет групп
    whitelist / torrent / twitch-ads, на которые ссылается остальной профиль,
    и маршрутизация развалится. Поэтому реклама режется через DNS, а
    полноценный geosite:category-ads-all остаётся на серверной стороне.
    """
    payload = deeplink[len(ROUTING_DEEPLINK_PREFIX):]
    profile = json.loads(base64.b64decode(payload + "=" * (-len(payload) % 4)))
    profile.update(ROUTING_OVERRIDES)
    encoded = base64.b64encode(
        json.dumps(profile, ensure_ascii=False).encode()
    ).decode()
    return ROUTING_DEEPLINK_PREFIX + encoded


async def fetch_routing_deeplink() -> str | None:
    url = (
        "https://raw.githubusercontent.com/"
        "hydraponique/roscomvpn-routing/main/HAPP/DEFAULT.DEEPLINK"
    )
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url)
            r.raise_for_status()
            routing_deeplink = r.text.strip()
        if not routing_deeplink.startswith(ROUTING_DEEPLINK_PREFIX):
            logger.error(
                "routing deeplink пришёл в неожиданном формате, "
                "отдаю как есть без наших правок"
            )
            return routing_deeplink
        routing_deeplink = _apply_routing_overrides(routing_deeplink)
        logger.info("routing deeplink обновлён (с фильтрующим DNS)")
        return routing_deeplink
    except Exception as e:
        logger.error(f"Ошибка обновления routing deeplink: {e}")
        return None

async def get_happ_encrypted_link(sub_url: str) -> str:
    async with httpx.AsyncClient() as client:
        logger.debug(f"sub_url = {sub_url}")
        resp = await client.post(
            url="https://crypto.happ.su/api-v2.php",
            json={"url": sub_url},
        )
        data = resp.json()
        return data["encrypted_link"]

def get_settings_link(client_uuid: str) -> str:
    vless_links = []
    for server in credentials.SERVERS:
        vless_link = (
            f"vless://{client_uuid}@{server['host']}:443?"
           # f"flow=xtls-rprx-vision&"
            f"security=reality&encryption=none&"
           # f"type=tcp&"
           # f"type=xhttp&path=/v1/messages&mode=stream-one&"
            f"type=grpc&serviceName=grpc_service&"
            f"sid={credentials.XRAY_SHORTID}&fp=chrome&"
            f"pbk={credentials.SERVER_PUBLIC_KEY}&headerType=none&"
            f"sni={credentials.SNI}"
            f"#horizance_vpn_{server.get('name','')}{server.get('flag','')}"
        )
        vless_links.append(vless_link)
    return "\n".join(vless_links)

async def get_user_traffic(user_id, reset: bool = False) -> dict:
    """Трафик пользователя по всем серверам.

    Здесь был свой вызов `xray api statsquery` к 127.0.0.1 с захардкоженным
    портом 62789 — то есть считалась одна нода из шести, и у всех, кто ходит
    через US/DE/RU/KZ/MX, в базе навсегда оставались нули. Логика жила в двух
    местах сразу (тут и в VPNManager), поэтому расчёт переехал в VPNManager,
    который и так умеет ходить по всем серверам и знает порт из конфига.
    """
    return await _vpn.get_user_traffic(user_id, reset=reset)

def is_happ_client(request: Request) -> bool:
    """Happ представляется как "Happ/<версия>/<ОС>/<hwid>" — по этому префиксу
    и отличаем его от всех остальных клиентов.

    Нужно потому, что подписку теперь тянет не только Happ: после удаления
    приложения из российского App Store часть людей на iPhone сидит на INCY/Karing
    или Shadowrocket (см. settings.IOS_ALTERNATIVE_CLIENTS). Строки
    "happ://routing/..." и "#providerid: ..." понимает только Happ, у чужого
    клиента они в лучшем случае просто мусор в списке серверов, в худшем —
    вся подписка считается битой ("The subscription URL is not valid").
    """
    return request.headers.get("user-agent", "").startswith("Happ/")


async def get_content(client_uuid, is_happ: bool = True) -> str:
    settings_link = get_settings_link(client_uuid)
    if not is_happ:
        return settings_link

    global routing_deeplink, routing_deeplink_fetch_time
    is_routing_deeplink_outdated = (
        time.time() - routing_deeplink_fetch_time > ROUTING_TTL
    )
    if not routing_deeplink or is_routing_deeplink_outdated:
        routing_deeplink = await fetch_routing_deeplink()
        if routing_deeplink:
            routing_deeplink_fetch_time = time.time()
    #routing_deeplink = await routing_cache.get()
    return f"{routing_deeplink}\n{settings_link}"

def get_expire_date(user):
    try:
        if str(user["user_id"]).startswith("t_"):
            expire_date = datetime.fromisoformat(user["expires_at"])
        else:
            expire_date = datetime.strptime(
                user["next_payment_date"], "%d.%m.%y"
            )
    except:
        expire_date = datetime(1970, 1, 1)
    return expire_date

# Сбор трафика идёт в фоне: ждать его в ответе /sub нельзя. Он ходит по SSH на
# все ноды, и одна зависшая нода (23.09.26 — MX под атакой) держала ответ до
# ~37 с: Happ не дожидался (917 ответов 499 за день) и подписка не обновлялась
# ни у кого. Теперь в приложение уходит итог из базы, а свежие показания
# доедут к следующему опросу. _traffic_in_flight — чтобы не запускать второй
# сбор по тому же человеку, пока не закончился первый; _traffic_tasks держит
# ссылки на задачи, иначе сборщик мусора может убить их на полпути.
# _traffic_slots ограничивает число одновременных сборов: каждый сбор — это
# SSH на все ноды, и пачка из 11 запросов /sub за секунду (27.09.26 08:18)
# открывала ~55 соединений разом — sshd на нодах рвал их (ConnectionLost
# сразу по всем нодам), а снятые с -reset показания при этом терялись.
_traffic_in_flight: set[str] = set()
_traffic_tasks: set[asyncio.Task] = set()
_traffic_slots = asyncio.Semaphore(2)


async def _collect_traffic(user_id: str) -> None:
    try:
        # reset=True: снимаем и обнуляем, save_traffic прибавляет к базе
        async with _traffic_slots:
            traffic = await get_user_traffic(user_id, reset=True)
        save_traffic(user_id, traffic)
    except Exception as e:
        logger.error(f"{user_id}: фоновый сбор трафика не удался: {e}")
    finally:
        _traffic_in_flight.discard(user_id)


def _schedule_traffic_collection(user_id: str) -> None:
    if user_id in _traffic_in_flight:
        return
    _traffic_in_flight.add(user_id)
    task = asyncio.create_task(_collect_traffic(user_id))
    _traffic_tasks.add(task)
    task.add_done_callback(_traffic_tasks.discard)


async def get_headers(user, bot_link=BOT_LINK) -> dict:
    user_id = str(user["user_id"])
    totals_mb = {
        "uplink": user.get("traffic_in") or 0,
        "downlink": user.get("traffic_out") or 0,
    }
    _schedule_traffic_collection(user_id)
    expire_date = get_expire_date(user)
    title = TITLE_TRIAL if user_id.startswith("t_") else TITLE_MAIN
    # Баннер над списком серверов в приложении. Его видят ТОЛЬКО те, у кого
    # приложение уже стоит и подписка уже добавлена, — то есть новости про
    # App Store здесь бессмысленны по определению: кому они нужны, тот этот
    # текст никогда не увидит. Поэтому тут то, что полезно любому и не
    # протухает от смены сезона.
    announce = "Продление и помощь — в боте @Horizance_vpn_bot"
    if user_id.startswith("t_"):
        # 01.10.26: 24 из 33 платящих пришли через триал на сайте — баннер
        # триала зовёт в бота, где ещё 14 дней бесплатно
        announce = f"Понравилось? Ещё {settings.trial_days} дней бесплатно в боте @Horizance_vpn_bot"

    return {
        "profile-title": f"base64:{title}",
        "subscription-userinfo":
             f"upload={totals_mb['uplink'] * 1000000};"
             f"download={totals_mb['downlink'] * 1000000};"
             f"total=0;"
             f"expire={int(expire_date.timestamp())}",
        "profile-update-interval": "1",
        "support-url": bot_link,
        "profile-web-page-url": f"{credentials.TRIAL_LINK}",
        "announce": f"base64:{base64.b64encode(announce.encode()).decode()}",
        "providerid": "73VflCto",
        "subscription-ping-onopen-enabled": "1",
        #"ping-type": "proxy",
        #"check-url-via-proxy": "https://cp.cloudflare.com/generate_204",
        "subscriptions-sort-type": "ping"
    }

# домены, с которых подписки переводим на SUBSCRIPTION_DOMAIN
LEGACY_SUBSCRIPTION_HOSTS = {"horizance.ru", "www.horizance.ru"}


def _is_legacy_host(request: Request) -> bool:
    host = (request.headers.get("host") or "").split(":")[0].lower()
    return host in LEGACY_SUBSCRIPTION_HOSTS


async def update_device_binding(table_name, user, request) -> str | None:
    """Привязывает устройство; возвращает причину отказа device_guard или None."""
    headers = request.headers
    hwid = headers.get("x-hwid")
    device_os = headers.get("x-device-os", "unknown")
    device_model = headers.get("x-device-model", "unknown")
    user_agent = headers.get("user-agent", "")

    logger.debug(
        f"sub request: uuid={user['user_id']} "
        f"hwid={hwid} os={device_os} "
        f"model={device_model} ua={user_agent}"
    )
    if device_guard.is_banned(ip=get_client_ip(request), user_id=user["user_id"]):
        logger.warning(f"sub: запрос с забаненного IP {get_client_ip(request)}")
        return "banned"
    if not hwid:
        # без hwid (Karing, Clash и т.п.; Happ, INCY, v2box его шлют) устройство не посчитать — пропускаем
        logger.debug('Не получилось определить hwid')
        return None
    refusal = device_guard.device_refusal(user, hwid, device_model)
    if refusal:
        return refusal
    # регион пишем всем, у кого его ещё нет, а не только новичкам: у давних
    # клиентов fingerprint давно заполнен, и привязка ниже к ним не заходит —
    # без отдельной проверки client_ip у них так и остался бы пустым
    if not user.get("client_ip"):
        await save_client_location(table_name, user["user_id"], request)

    if not user["fingerprint"]:
        save_device_os(table_name, user["user_id"], device_os)
        save_fingerprint(table_name, user["user_id"], device_model, hwid)
        logger.info(f'saved_hwid={hwid}')
    else:
        saved_hwid = user["fingerprint"].split("|")[1]
        if saved_hwid != hwid:
            logger.debug(
                f"второе устройство: user_id={user['user_id']} "
                f"saved={saved_hwid} got={hwid}"
            )
    return None


router = APIRouter(
#prefix="/support", # Все маршруты внутри будут начинаться с /auth
tags=["users"], # Для документации (Swagger UI)
)
routing_cache = RoutingCache()

@router.get("/vpn/{client_uuid}")
async def get_happ_link(request: Request, client_uuid: str):
    """Отдаёт страницу, которая сама пытается открыть Happ по зашифрованной
    ссылке, а если за пару секунд ничего не произошло (приложение не
    установлено) — показывает фолбэк со ссылкой на скачивание.

    Не путать с /sub/{client_uuid} ниже — этим эндпоинтом пользуется сам
    Happ для обновления списка серверов, его трогать нельзя.
    """
    # ссылку копируют из Telegram руками и иногда захватывают пробел:
    # 24.09.26 '/vpn/ be44e52f…' упал с KeyError вместо страницы
    client_uuid = client_uuid.strip()
    sub_url = f"https://{credentials.SUBSCRIPTION_DOMAIN}/sub/{client_uuid}"
    happ_url = await get_happ_encrypted_link(sub_url)
    logger.debug(f"Успешно зашифрована ссылка для uuid={client_uuid}")

    user = get_user_by_uuid('users', client_uuid) or get_user_by_uuid('trial_sessions', client_uuid)
    device_os = user.get('device_os') if user else None
    from routers.go import app_link
    store_url = (app_link(device_os, "vpn_page")
                 if device_os in settings.VPN_CLIENT_DOWNLOAD_URLS else None)

    # Запасной путь на случай, когда Happ поставить неоткуда (в первую очередь
    # iPhone: приложения нет в российском App Store). Тогда человеку нужна не
    # зашифрованная happ://-ссылка, а обычный адрес подписки — его понимает
    # любой клиент из settings.IOS_ALTERNATIVE_CLIENTS.
    ua = request.headers.get("user-agent", "")
    is_ios = (device_os == "iOS") or "iPhone" in ua or "iPad" in ua

    # одноразовые ссылки «добавить в приложение» для iOS-клиентов
    client_add_urls = {}
    for client in settings.IOS_ALTERNATIVE_CLIENTS:
        if client.get("add_link") == "incy_crypt":
            from incy_crypto_link import encrypt_link
            client_add_urls[client["name"]] = encrypt_link(sub_url, name="Horizance VPN")

    return templates.TemplateResponse(request, "open_happ.html", {
        "happ_url": happ_url,
        "store_url": store_url,
        "sub_url": sub_url,
        "is_ios": is_ios,
        "ios_clients": settings.IOS_ALTERNATIVE_CLIENTS,
        "client_add_urls": client_add_urls,
    })

@router.get("/sub/{client_uuid}")
async def get_client_config (
    client_uuid: str, request: Request
) -> PlainTextResponse:
    logger.debug("------------------------------------------------------------")
    is_happ = is_happ_client(request)
    # ?routing=off — подписка без раздельной маршрутизации (весь трафик через
    # VPN): профиль маршрутизации не отдаём и выключаем его в Happ. В Happ
    # routing-enable глобальный, поэтому это для личного устройства владельца.
    no_routing = request.query_params.get("routing") == "off"
    content = (get_settings_link(client_uuid) if no_routing
               else await get_content(client_uuid, is_happ))
    meta = (
        "#providerid: 73VflCto\n"
        "#subscription-ping-onopen-enabled: 1\n"
        "#ping-type: proxy\n"
        "#check-url-via-proxy: https://cp.cloudflare.com/generate_204\n"
        "#sort-by: ping\n\n"
    ) if is_happ else ""
    if is_happ and _is_legacy_host(request):
        # 26–30.09.26 заголовок new-domain не перевёл ни одной подписки (даже
        # Happ 5.9.0). Документация Happ допускает тот же параметр в теле
        # ответа — дублируем.
        meta = f"#new-domain: {credentials.SUBSCRIPTION_DOMAIN}\n" + meta
    if is_happ and no_routing:
        meta = "#routing-enable: 0\n" + meta

    # headers инициализируем до ветвлений: если uuid не нашёлся ни в одной
    # таблице, раньше до PlainTextResponse(headers=headers) долетал
    # UnboundLocalError и Happ получал 500 вместо внятного ответа.
    headers: dict = {}
    refusal = None
    user = get_user_by_uuid('users', client_uuid)
    if user:
        logger.debug(f"Найден uuid, user: {dict(user)}")
        refusal = await update_device_binding('users', user, request)
        headers = await get_headers(user)
        if is_user_inactive(user):
            content = INACTIVE_SUBSCRIPTION_CONTENT
    else:
        trial_user = get_user_by_uuid('trial_sessions', client_uuid)
        if trial_user:
            logger.debug(f"Trial uuid найден, trial user: {dict(trial_user)}")
            refusal = await update_device_binding('trial_sessions', trial_user, request)
            headers = await get_headers(trial_user)
            if is_user_inactive(trial_user):
                content = TRIAL_OVER_CONTENT
                headers["profile-web-page-url"] = TRIAL_OVER_PAY_URL
                headers["announce"] = "base64:" + base64.b64encode(
                    TRIAL_OVER_ANNOUNCE.encode()).decode()
        else:
            log_unknown_uuid(client_uuid, request)
            content = STALE_SUBSCRIPTION_CONTENT
    if refusal:
        content = DEVICE_REFUSAL_CONTENT[refusal]
    if headers and no_routing:
        headers["routing-enable"] = "0"
    if headers and is_happ and _is_legacy_host(request):
        # Старые подписки выданы на horizance.ru — это отдельный проект, VPN с
        # ним связывать не нужно. Happ по заголовку new-domain (работает при
        # указанном providerid, он есть в get_headers) сам меняет домен в
        # сохранённой ссылке, путь /sub/<uuid> остаётся прежним.
        headers["new-domain"] = credentials.SUBSCRIPTION_DOMAIN
        logger.info(f"sub: переводим подписку {client_uuid} с {request.headers.get('host')} на {credentials.SUBSCRIPTION_DOMAIN}")
    return PlainTextResponse(
        headers=headers,
        content=meta+content
    )
