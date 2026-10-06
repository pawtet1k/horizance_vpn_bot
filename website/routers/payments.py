"""Вебхуки платёжных провайдеров — UDM Pay (СБП) и NOWPayments (крипто).

UDM Pay: спека не документирует подпись/аутентификацию входящих вебхуков
(в отличие от подписи исходящих запросов через hash), поэтому телу запроса
не доверяем: любое входящее уведомление лишь триггерит перепроверку
реального статуса через сам API UDM Pay (order/status, sbp-subscription/status)
и решение принимается по её результату, а не по полям тела вебхука. Это
защищает от поддельного POST с "успешной" оплатой на публичный URL.

NOWPayments: вебхуки подписаны (заголовок x-nowpayments-sig, HMAC-SHA512
на IPN-секрете) — здесь подпись и есть механизм доверия, отдельная
перепроверка через API не нужна.
"""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import nowpayments
import udmpay
from db import UserManager
from messenger import TelegramApiMessenger
from my_logger import logger
from request_helpers import get_client_ip, is_rate_limited
from subscription import SubscriptionManager
from vpn import VPNManager

_db = UserManager(db_name=str(Path("~/horizance_vpn_bot/users.db").expanduser().resolve()))
_vpn = VPNManager()
_subscriptions = SubscriptionManager(_db, _vpn)

router = APIRouter(tags=["payments"])


async def _read_json(request: Request) -> dict | None:
    try:
        return await request.json()
    except Exception:
        return None


@router.post("/api/payments/udmpay/webhook")
async def udmpay_payment_webhook(request: Request):
    client_ip = get_client_ip(request)
    if is_rate_limited(f"udmpay_webhook:{client_ip}", max_calls=30, window_seconds=60):
        logger.warning(f"udmpay webhook: превышен лимит запросов, ip={client_ip}")
        return JSONResponse({"ok": False}, status_code=429)

    payload = await _read_json(request)
    if payload is None:
        return JSONResponse({"ok": False}, status_code=400)

    order_id = payload.get("orderId")
    if not order_id:
        return JSONResponse({"ok": False}, status_code=400)

    user = _db.get_user_by_order(order_id)
    if user is None:
        logger.warning(f"udmpay webhook: неизвестный orderId={order_id}")
        return JSONResponse({"ok": True})

    try:
        order_status = await udmpay.get_order_status(order_id)
    except udmpay.UdmPayError as e:
        logger.error(f"udmpay webhook: не удалось проверить статус заказа {order_id}: {e}")
        return JSONResponse({"ok": False}, status_code=502)

    messenger = TelegramApiMessenger()
    status = order_status.get("status")
    if status == "success":
        await _subscriptions.activate_paid_subscription(messenger, user.user_id, order_id)
    elif status in ("failed", "cancel", "refunded"):
        message = f"платёж отклонён (статус: {order_status.get('paymentStatus', status)})"
        await _subscriptions.fail_pending_payment(messenger, user.user_id, order_id, message)
    else:
        logger.info(f"udmpay webhook: заказ {order_id} ещё не финализирован (status={status})")

    return JSONResponse({"ok": True})


@router.post("/api/payments/udmpay/subscription-webhook")
async def udmpay_subscription_webhook(request: Request):
    client_ip = get_client_ip(request)
    if is_rate_limited(f"udmpay_sub_webhook:{client_ip}", max_calls=30, window_seconds=60):
        logger.warning(f"udmpay subscription-webhook: превышен лимит запросов, ip={client_ip}")
        return JSONResponse({"ok": False}, status_code=429)

    payload = await _read_json(request)
    if payload is None:
        return JSONResponse({"ok": False}, status_code=400)

    subscription_uid = payload.get("subscriptionUid")
    order_uid = payload.get("orderUid")
    if not subscription_uid:
        return JSONResponse({"ok": False}, status_code=400)

    # Ищем по subscriptionUid, а не по orderUid: вебхук об оплате приходит
    # почти одновременно и обнуляет pending_order_uid, после чего поиск по
    # заказу перестаёт находить кого бы то ни было. По orderUid ходим только
    # запасным путём — на случай заказов, созданных до появления колонки
    # pending_subscription_uid.
    user = _db.get_user_by_pending_subscription_uid(subscription_uid)
    if user is None and order_uid:
        user = _db.get_user_by_pending_order_uid(order_uid)
    if user is None:
        logger.warning(
            f"udmpay subscription-webhook: не найден пользователь для "
            f"subscriptionUid={subscription_uid} (orderUid={order_uid})"
        )
        return JSONResponse({"ok": True})

    try:
        sub_status = await udmpay.get_subscription_status(subscription_uid)
    except udmpay.UdmPayError as e:
        logger.error(
            f"udmpay subscription-webhook: не удалось проверить статус "
            f"подписки {subscription_uid}: {e}"
        )
        return JSONResponse({"ok": False}, status_code=502)

    if sub_status.get("status") == "active" and sub_status.get("isActive"):
        await _subscriptions.bind_subscription(user.user_id, subscription_uid, order_uid)
    elif sub_status.get("status") == "rejected":
        await _subscriptions.fail_subscription_bind(
            user.user_id, subscription_uid, "привязка счёта отклонена банком"
        )

    return JSONResponse({"ok": True})


@router.post("/api/payments/nowpayments/webhook")
async def nowpayments_webhook(request: Request):
    client_ip = get_client_ip(request)
    if is_rate_limited(f"nowpayments_webhook:{client_ip}", max_calls=30, window_seconds=60):
        logger.warning(f"nowpayments webhook: превышен лимит запросов, ip={client_ip}")
        return JSONResponse({"ok": False}, status_code=429)

    payload = await _read_json(request)
    if payload is None:
        return JSONResponse({"ok": False}, status_code=400)

    signature = request.headers.get("x-nowpayments-sig")
    if not signature or not nowpayments.verify_webhook_signature(payload, signature):
        logger.warning(f"nowpayments webhook: неверная или отсутствующая подпись, ip={client_ip}")
        return JSONResponse({"ok": False}, status_code=401)

    order_id = payload.get("invoice_id") or payload.get("order_id")
    if not order_id:
        return JSONResponse({"ok": False}, status_code=400)
    order_id = str(order_id)

    user = _db.get_user_by_order(order_id)
    if user is None:
        logger.warning(f"nowpayments webhook: неизвестный/устаревший orderId={order_id}")
        return JSONResponse({"ok": True})

    messenger = TelegramApiMessenger()
    status = payload.get("payment_status")
    if status == "finished":
        await _subscriptions.activate_paid_subscription(
            messenger, user.user_id, order_id, provider="NOWPayments"
        )
    elif status in ("failed", "expired", "refunded"):
        await _subscriptions.fail_pending_payment(
            messenger, user.user_id, order_id, f"статус платежа: {status}",
            provider="NOWPayments"
        )
    else:
        logger.info(f"nowpayments webhook: платёж {order_id} ещё не финализирован (status={status})")

    return JSONResponse({"ok": True})
