"""Клиент UDM Pay (СБП) — https://pay.udmpay.ru/docs/api/v1

Покрывает только то, что реально используется ботом: привязка подписки СБП
с одновременной оплатой (первый платёж), списание по уже привязанной
подписке (продление) и проверку статуса заказа/подписки. Остальные ручки
(order/create, order/refund, order/cancel, order/status/batch,
sbp-subscription/create без оплаты) не используются и не реализованы —
YAGNI, добавить когда появится конкретный вызывающий код.

Подпись запроса (hash) в спеке описана как:
    sha256(конкатенация значений параметров, отсортированных по ключу,
           без вложенных массивов и без самого поля hash) + account_password)
Ни рабочего примера запроса/ответа, ни подтверждённых login/account_password
на момент написания нет (см. credentials.UDMPAY_LOGIN/UDMPAY_PASSWORD) —
алгоритм реализован строго по тексту спеки и требует проверки на реальном
аккаунте, как только он заработает.
"""
import hashlib

import httpx

import credentials
import settings
from my_logger import logger


PROD_BASE_URL = "https://pay.udmpay.ru/api/v1"
SANDBOX_BASE_URL = "https://dev.pay.udmpay.ru/api/v1"

PAYMENT_WEBHOOK_PATH = "/api/payments/udmpay/webhook"
SUBSCRIPTION_WEBHOOK_PATH = "/api/payments/udmpay/subscription-webhook"
RETURN_PATH = "/pay/waiting"


class UdmPayError(Exception):
    def __init__(self, message, status_code=None, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


def _base_url() -> str:
    return SANDBOX_BASE_URL if settings.UDMPAY_SANDBOX else PROD_BASE_URL


def _format_value(key: str, value) -> str:
    # "Итоговая сумма нормализуется к виду с двумя десятичными знаками
    # перед сравнением с эталоном подписи" — только у sum, у остальных
    # полей формула хеша просто str(value).
    if key == "sum":
        return f"{float(value):.2f}"
    return str(value)


def sign(params: dict) -> str:
    scalar_items = [
        (key, value) for key, value in params.items()
        if key != "hash" and value is not None
        and not isinstance(value, (list, dict))
    ]
    raw = "".join(
        _format_value(key, value) for key, value in sorted(scalar_items)
    )
    raw += credentials.UDMPAY_PASSWORD
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _webhook_link() -> str:
    return settings.UDMPAY_WEBHOOK_BASE_URL + PAYMENT_WEBHOOK_PATH


def _subscription_webhook_link() -> str:
    return settings.UDMPAY_WEBHOOK_BASE_URL + SUBSCRIPTION_WEBHOOK_PATH


def _return_link(baseline_paid_periods: int) -> str:
    # Актуально только для оплаты с сайта (redirect после подтверждения в
    # приложении банка) - страница там сама дожидается вебхука поллингом
    # /api/subscription. В боте некуда возвращаться, поле просто игнорится.
    #
    # baseline передаётся в URL, а не берётся клиентом при загрузке страницы:
    # вебхук может прийти быстрее, чем откроется страница ожидания, и тогда
    # "текущее" значение paid_periods на клиенте уже было бы обновлённым -
    # сравнивать было бы не с чем.
    return f"{settings.WEBSITE_BASE_URL}{RETURN_PATH}?baseline={baseline_paid_periods}"


async def _post(path: str, body: dict) -> dict:
    body = {k: v for k, v in body.items() if v is not None}
    body["hash"] = sign(body)
    url = _base_url() + path

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(url, json=body)

    try:
        payload = response.json()
    except ValueError:
        payload = {"message": response.text}

    if response.status_code >= 400 or payload.get("status") == 0:
        message = payload.get("message", "Ошибка UDM Pay")
        logger.error(f"UDM Pay {path}: HTTP {response.status_code}, ответ={payload}")
        raise UdmPayError(message, response.status_code, payload)

    return payload


def _order_products(description: str, price: int) -> list[dict]:
    return [{
        "id": "subscription",
        "name": description,
        "quantity": 1,
        "price": price,
    }]


async def create_subscription_with_payment(
    *, ext_order_id: str, price: int, description: str, baseline_paid_periods: int
) -> dict:
    """Первый платёж: привязка подписки СБП + оплата одним заказом.
    Возвращает SbpSubscriptionCreateWithPaymentResource
    (subscriptionUid, orderUid, paymentLink, nspkPaymentLink, ...)."""
    body = {
        "login": credentials.UDMPAY_LOGIN,
        "extOrderId": ext_order_id,
        "paymentPurpose": description,
        "products": _order_products(description, price),
        "sum": price,
        "returnLink": _return_link(baseline_paid_periods),
        "webhookLink": _webhook_link(),
        "subscriptionPurpose": description,
        "subscriptionWebhookLink": _subscription_webhook_link(),
    }
    return await _post("/sbp-subscription/create-with-payment", body)


async def pay_by_subscription(
    *, subscription_uid: str, ext_order_id: str, price: int, description: str
) -> dict:
    """Списание по уже привязанной подписке (продление).
    Возвращает SbpSubscriptionPayResource (uid заказа, message, ...)."""
    body = {
        "login": credentials.UDMPAY_LOGIN,
        "extOrderId": ext_order_id,
        "paymentPurpose": description,
        "products": _order_products(description, price),
        "sum": price,
        "webhookLink": _webhook_link(),
        "subscriptionUid": subscription_uid,
    }
    return await _post("/sbp-subscription/pay", body)


async def get_order_status(order_id: str) -> dict:
    """Реальный ответ API оборачивает поля в {"data": {...}} - в отличие
    от create-with-payment/pay, где поля лежат на верхнем уровне (сам так
    получил и не заметил при первом заходе - воспроизведено дважды: в
    песочнице и на боевом вебхуке 2026-08-14). Разворачиваем здесь, чтобы
    вызывающий код (webhook-хендлеры) не знал про эту особенность."""
    body = {"login": credentials.UDMPAY_LOGIN, "orderId": order_id}
    payload = await _post("/order/status", body)
    return payload.get("data", payload)


async def get_subscription_status(subscription_uid: str) -> dict:
    body = {
        "login": credentials.UDMPAY_LOGIN,
        "subscriptionUid": subscription_uid,
    }
    payload = await _post("/sbp-subscription/status", body)
    return payload.get("data", payload)
