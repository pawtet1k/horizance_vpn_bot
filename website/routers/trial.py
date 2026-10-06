import asyncio
import httpx
from pathlib import Path
import uuid
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, JSONResponse, PlainTextResponse

import settings
from counters import send_goal
from db import UserManager
from vpn import VPNManager
from request_helpers import get_client_ip
import device_guard
from routers.vpn import get_happ_encrypted_link
from templates_env import templates
from my_logger import logger

_db = UserManager(db_name=str(Path("~/horizance_vpn_bot/users.db").expanduser().resolve()))
_vpn = VPNManager()

FA_ICONS: dict[str, str] = {
    "iOS":     "fa-brands fa-apple",
    "Android": "fa-brands fa-android",
    "Windows": "fa-brands fa-windows",
    "macOS":   "fa-brands fa-apple",
    "Linux":   "fa-brands fa-linux",
    "TV":      "fa-solid fa-tv",
}

def build_os_buttons(src: str = "-") -> list[tuple[str, str, str, bool]]:
    """Кнопки ОС; ссылка «скачать» — через счётчик /go/app (воронка)."""
    from routers.go import app_link
    items = [(name, app_link(name, src)) for name in settings.VPN_CLIENT_DOWNLOAD_URLS]
    result = []
    for i, (name, url) in enumerate(items):
        icon_class = FA_ICONS.get(name, "fa-solid fa-desktop")
        is_full = (len(items) % 2 == 1) and (i == len(items) - 1)
        result.append((name, icon_class, url, is_full))
    return result

async def get_ip_location(ip: str) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.get(f"http://ip-api.com/json/{ip}?fields=status,country,regionName,city,isp,hosting,query")
        data = r.json()

    if data.get("status") != "success":
        return {}

    return {
        "ip": data["query"],
        "country": data["country"],
        "region": data["regionName"],
        "city": data["city"],
        "isp": data["isp"],
        "hosting": data.get("hosting", False),
    }

router = APIRouter(tags=["trial"])

# Ответы под запросы, по которым /trial получает клики из Google (GSC, сентябрь
# 2026: «впн пробный период happ», «пробная подписка хапп», «пробный впн на
# 24 часа» — позиция ~34). Факты сверены с settings на 01.10.2026.
TRIAL_FAQ = {
    "Есть ли у Horizance VPN пробный период для Happ?":
        "Да. На этой странице — 24 часа бесплатно, без регистрации и без карты: вы получаете "
        "ссылку-подписку и добавляете её в Happ (на iPhone — в INCY). В Telegram-боте новым пользователям "
        f"бесплатный период дольше — {settings.trial_days} дней.",
    "Как получить пробную подписку для Happ?":
        "Установите Happ (на iPhone — INCY: Happ сейчас нет в российском App Store), затем "
        "нажмите «Подключиться» в шаге 2. Откроется страница с вашей ссылкой и кнопкой, которая "
        "добавляет подписку в приложение. Серверы появятся в списке сразу.",
    "Пробный VPN чем-то ограничен?":
        "Нет: это те же серверы и та же скорость, что и по платной подписке. Ограничение одно — "
        "срок 24 часа.",
    "Что будет, когда 24 часа закончатся?":
        "Вместо серверов в приложении появится сообщение, что пробный период закончился. "
        f"Дальше можно продолжить в Telegram-боте ({settings.trial_days} дней бесплатно для новых "
        f"пользователей) или оформить подписку — {settings.default_price} ₽ в месяц, оплата по СБП "
        "или криптовалютой.",
    "Можно ли взять пробный период ещё раз?":
        "Пробный период даётся один раз на устройство. Чтобы пользоваться VPN дальше, "
        "подключите Telegram-бота.",
}

@router.get("/trial")
async def trial_page(request: Request, src: str | None = None):
    if src:
        # откуда пришли на /trial (например, src=login_modal — из окна «вход
        # в разработке»); в логах nginx это тоже видно, здесь — с IP для воронки
        logger.info(f"trial page: src={src[:40]}, ip={get_client_ip(request)}")
    return templates.TemplateResponse(
        request,
        "trial.html",
        {
            "os_buttons": build_os_buttons("trial"),
            # sub_url тут нет — ключ ещё не выдан, поэтому в блоке покажется
            # только список приложений без поля с адресом подписки
            "ios_clients": settings.IOS_ALTERNATIVE_CLIENTS,
            # блок «Happ нет в App Store» — только владельцам iPhone/iPad
            "is_ios": any(x in request.headers.get("user-agent", "") for x in ("iPhone", "iPad")),
            "faq": TRIAL_FAQ,
        }
    )

@router.post("/trial/connect")
async def trial_connect(request: Request):
    client_ip = get_client_ip(request)
    recent = device_guard.recent_trial_uuid(client_ip)
    if recent:
        logger.info(f"trial_connect: повторный клик, отдаём тот же триал, ip={client_ip}")
        return RedirectResponse(url=f"/vpn/{recent}", status_code=303)
    refusal = device_guard.trial_refusal(client_ip)
    if refusal:
        logger.warning(f"trial_connect: отказ ({refusal}), ip={client_ip}")
        return PlainTextResponse(
            "Пробный доступ с этого адреса уже выдавался. "
            "Оформить подписку можно в нашем Telegram-боте: https://t.me/horizance_vpn_bot",
            status_code=429,
        )

    form = await request.form()

    client_location_data = await get_ip_location(client_ip)
    if client_location_data.get("hosting"):
        # не баним: через хостинг ходят и обычные люди со своим VPN, —
        # только оставляем след, чтобы видеть такие выдачи в логах
        logger.warning(
            f"trial_connect: IP хостинга {client_ip} ({client_location_data.get('isp')})"
        )

    user_id=f"t_{str(uuid.uuid4())[:8]}" # "t_" отличает trial от обычных юзеров
    client_uuid = await _vpn.setup_xray_for_trial(user_id)

    client_location = (
        client_location_data["region"],
        client_location_data["city"]
    )
    client_ip_and_location = f"{client_ip}|{client_location}"
    _db.add_trial_session(
        user_id=user_id,
        fingerprint=None,
        device_os=None,
        client_uuid=client_uuid,
        client_ip=client_ip_and_location
    )
    logger.info(f"{user_id}: выдан триал, client_uuid={client_uuid}")

    asyncio.create_task(send_goal(request, "trial_get_link"))

    return RedirectResponse(url=f"/vpn/{client_uuid}", status_code=303)
