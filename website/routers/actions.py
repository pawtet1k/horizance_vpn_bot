import time
from datetime import datetime
from hashlib import md5
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

import bot
import settings
from db import UserManager
from messenger import CapturingMessenger
from models import UserStatus
from my_logger import logger
from subscription import SubscriptionManager
from telegram_markdown import to_html
from templates_env import templates
from request_helpers import get_client_ip, is_rate_limited
from utils import email_support, send_email
from vpn import VPNManager

from routers.faq import faq
from routers.trial import build_os_buttons


_db = UserManager(db_name=str(Path("~/horizance_vpn_bot/users.db").expanduser().resolve()))
_vpn = VPNManager()
_subscriptions = SubscriptionManager(_db, _vpn)

# Тот же бот, что и в routers/pages.py / routers/vpn.py (BOT_LINK).
BOT_USERNAME = "Horizance_vpn_bot"

router = APIRouter(tags=["dashboard-actions"])


def _current_user(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return None, None
    return user_id, _db.get_user(user_id)


def _render_messages(request: Request, title: str, messenger: CapturingMessenger,
                     retry_url: str | None = None):
    return templates.TemplateResponse(request, "action_result.html", {
        "title": title,
        "messages": [to_html(m.text) for m in messenger.messages],
        "retry_url": retry_url,
    })


@router.get("/api/subscription")
async def api_subscription(request: Request):
    user_id, user = _current_user(request)
    if not user_id or user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    is_active = user.status != UserStatus.INACTIVE
    expires_at = None
    if is_active:
        expires_at = datetime.strptime(
            user.next_payment_date, "%d.%m.%y"
        ).isoformat()
    return JSONResponse({
        "status": user.status.value,
        "status_label": user.status.label_ru(),
        "is_active": is_active,
        "expires_at": expires_at,
        "auto_renew": user.payment_token is not None,
        # оплата СБП асинхронная (подтверждение приходит вебхуком UDM Pay
        # уже после того, как страница /pay/waiting отрендерилась) -
        # фронтенд поллит это поле, чтобы понять, ждать ли ещё
        "payment_pending": user.pending_order_uid is not None,
        # /pay/waiting сравнивает это значение с baseline из returnLink,
        # чтобы понять, увеличилось ли число оплаченных периодов - только
        # так надёжно отличить успех от отказа, если пользователь уже был
        # активен (например, при смене тарифа)
        "paid_periods": user.paid_periods,
    })


PLAN_PRICES = {
    "month": settings.default_price,
    "year": settings.yearly_price,
    "router": settings.router_price,
}


@router.get("/pay")
async def pay_page(request: Request):
    user_id, user = _current_user(request)
    if not user_id:
        return RedirectResponse(url="/auth?next=/pay")

    if user.price == 0:
        # бесплатная подписка (промо/реферальная) - выбор тарифа не нужен
        messenger = CapturingMessenger()
        await _subscriptions.add_subscription(messenger, user)
        return _render_messages(request, "Оплата подписки", messenger)

    # у уже активного подписчика (payment_token задан) убираем из выбора
    # текущий тариф - "сменить" на тот же самый тариф не даём
    plans = {
        plan: price for plan, price in PLAN_PRICES.items()
        if not (user.payment_token and price == user.price)
    }
    return templates.TemplateResponse(request, "pay_select_plan.html", {"plans": plans})


@router.get("/pay/waiting")
async def pay_waiting_page(request: Request):
    """Страница, на которую UDM Pay возвращает пользователя после
    подтверждения в приложении банка (returnLink). Само подтверждение
    оплаты асинхронное - приходит вебхуком чуть позже, поэтому страница
    просто поллит /api/subscription до тех пор, пока payment_pending
    не станет false (см. extra_scripts в pay_waiting.html)."""
    user_id, user = _current_user(request)
    if not user_id:
        return RedirectResponse(url="/auth")
    try:
        baseline_paid_periods = int(request.query_params.get("baseline", user.paid_periods))
    except ValueError:
        baseline_paid_periods = user.paid_periods
    return templates.TemplateResponse(request, "pay_waiting.html", {
        "baseline_paid_periods": baseline_paid_periods
    })


@router.get("/pay/{plan}")
async def pay_plan(request: Request, plan: str):
    user_id, user = _current_user(request)
    if not user_id:
        return RedirectResponse(url="/auth?next=/pay")
    if plan not in PLAN_PRICES:
        return RedirectResponse(url="/pay")

    _db.update_user(user_id, price=PLAN_PRICES[plan])
    user = _db.get_user(user_id)

    messenger = CapturingMessenger()
    created = await _subscriptions.add_subscription(messenger, user)
    return _render_messages(
        request, "Оплата подписки", messenger,
        retry_url=None if created else f"/pay/{plan}",
    )


@router.get("/config")
async def config_page(request: Request):
    user_id, user = _current_user(request)
    if not user_id:
        return RedirectResponse(url="/auth")

    if user.status == UserStatus.INACTIVE:
        messenger = CapturingMessenger()
        await bot.send_vpn_setup_rejection_msg(messenger, user_id)
        return _render_messages(request, "Настройка VPN", messenger)

    return templates.TemplateResponse(request, "config_select_os.html", {
        "os_buttons": build_os_buttons()
    })


@router.get("/config/{device_os}")
async def config_finish(request: Request, device_os: str):
    user_id, user = _current_user(request)
    if not user_id:
        return RedirectResponse(url="/auth")
    if device_os not in settings.VPN_CLIENT_DOWNLOAD_URLS:
        return RedirectResponse(url="/config")

    messenger = CapturingMessenger()
    await bot.finish_vpn_setup(messenger, user_id, device_os, _db, _vpn)
    return _render_messages(request, "Настройка VPN", messenger)


@router.get("/invite_friend")
async def invite_friend_page(request: Request):
    user_id, user = _current_user(request)
    if not user_id:
        return RedirectResponse(url="/auth")

    messenger = CapturingMessenger()
    await bot.handle_invite_friend(messenger, user_id, BOT_USERNAME)
    return _render_messages(request, "Пригласить друга", messenger)


# Поддержка сознательно открыта без входа. Раньше /help уводил на /auth, а
# войти можно только через Telegram — то есть человек, у которого VPN не
# работает и Telegram поэтому недоступен, не мог пожаловаться именно на то,
# что у него не работает. У анонима нет user_id, поэтому вместо него просим
# контакт для ответа.
ANON_HELP_MAX_QUESTION_LEN = 4000

# Защита от повторной отправки одного и того же обращения.
#
# Первая версия отдавала на POST сразу отрендеренную страницу «спасибо».
# Браузер при этом остаётся на URL с методом POST, и любое восстановление
# вкладки (перезагрузка страницы, возврат к спящей вкладке, восстановление
# сессии) отправляет форму заново — письмо приходит второй раз спустя час.
# Ровно это и случилось 06.09: обращение записано дважды, в 09:23 и 10:43.
# Лечение — POST/Redirect/GET: после отправки уводим на GET /help?sent=1.
# Плюс короткое окно дедупликации на случай двойного клика по кнопке.
_recent_help_submissions: dict[str, float] = {}
HELP_DEDUP_WINDOW_SECONDS = 900


def _is_duplicate_help(fingerprint: str) -> bool:
    now = time.time()
    for key, seen_at in list(_recent_help_submissions.items()):
        if now - seen_at > HELP_DEDUP_WINDOW_SECONDS:
            del _recent_help_submissions[key]
    if fingerprint in _recent_help_submissions:
        return True
    _recent_help_submissions[fingerprint] = now
    return False


@router.get("/help")
async def help_page(request: Request):
    user_id, _ = _current_user(request)
    sent = request.query_params.get("sent") == "1"
    return templates.TemplateResponse(request, "help.html", {
        "is_authenticated": bool(user_id),
        "thanks_message": bot.SUPPORT_THANKS_MESSAGE if sent else None,
        "faq": faq,
    })


@router.post("/help")
async def help_submit(
    request: Request,
    question: str = Form(...),
    contact: str = Form(""),
):
    user_id, user = _current_user(request)
    question = question.strip()[:ANON_HELP_MAX_QUESTION_LEN]
    contact = contact.strip()[:200]

    if not question:
        return templates.TemplateResponse(request, "help.html", {
            "is_authenticated": bool(user_id),
            "error_message": "Опишите, пожалуйста, вопрос.",
            "faq": faq,
        }, status_code=400)

    client_ip = get_client_ip(request)
    fingerprint = md5(
        f"{user_id or client_ip}|{contact}|{question}".encode()
    ).hexdigest()
    if _is_duplicate_help(fingerprint):
        logger.info(f"/help: повтор того же обращения, письмо не шлём (ip={client_ip})")
        return RedirectResponse(url="/help?sent=1", status_code=303)

    if user_id and user is not None:
        logger.warning(f'{user_id}: ВОПРОС (сайт). {question}')
        await email_support(user, question, to=settings.SUPPORT_EMAIL)
        return RedirectResponse(url="/help?sent=1", status_code=303)

    if is_rate_limited(f"anon_help:{client_ip}", max_calls=3, window_seconds=3600):
        logger.warning(f"anon_help: превышен лимит обращений, ip={client_ip}")
        return templates.TemplateResponse(request, "help.html", {
            "is_authenticated": False,
            "error_message": (
                "Слишком много обращений с этого адреса. "
                "Попробуйте через час или напишите нам в Telegram."
            ),
            "faq": faq,
        }, status_code=429)

    logger.warning(f'аноним: ВОПРОС (сайт, без входа). контакт={contact!r}. {question}')
    send_email(
        subject="VPN. Вопрос с сайта (без входа)",
        body=(
            f"Вопрос от посетителя сайта, который не входил в личный кабинет.\n"
            f"Контакт для ответа: {contact or 'НЕ УКАЗАН'}\n"
            f"IP: {client_ip}\n"
            f"User-Agent: {request.headers.get('user-agent', '-')}\n\n"
            f"Вопрос:\n{question}"
        ),
        to=settings.SUPPORT_EMAIL,
    )
    return RedirectResponse(url="/help?sent=1", status_code=303)
