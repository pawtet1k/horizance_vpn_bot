"""Клиент NOWPayments (крипто-оплата) — https://documenter.getpostman.com/view/7907941/2s93JusNJt

Покрывает только то, что реально используется ботом: создание счёта
(invoice) и проверку статуса платежа. Настоящего автосписания NOWPayments
не даёт — их "Recurring Payments API" на деле просто рассылает клиенту
новую ссылку на оплату по email каждый период (клиент платит заново
вручную), а реальный автоплатёж (custodial sub-partner balance) требует
предоплаченного баланса у NOWPayments и отдельной, более тяжёлой
интеграции — не реализовано, см. память агента. Поэтому здесь только
разовые счета через /invoice, тот же паттерн "оплатил → ждём вебхук",
что и у UDM Pay, просто на каждый период заново.
"""
import hashlib
import hmac
import json

import httpx

import credentials
from my_logger import logger


BASE_URL = "https://api.nowpayments.io/v1"


class NowPaymentsError(Exception):
    def __init__(self, message, status_code=None, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


def _headers() -> dict:
    return {
        "x-api-key": credentials.NOWPAYMENTS_API_KEY,
        "Content-Type": "application/json",
    }


async def create_invoice(
    *, price_amount, order_id: str, order_description: str, ipn_callback_url: str,
    pay_currency: str | None = None,
    success_url: str | None = None, cancel_url: str | None = None,
) -> dict:
    """Создаёт счёт на оплату. Если pay_currency не передан, клиент сам
    выбирает монету на странице invoice_url из всех поддерживаемых NOWPayments -
    но у каждой монеты/сети свой минимум в USD (проверено через GET /v1/min-amount
    2026-08-14: от $1.73 у BSC/Celo/opBNB до $13.69 у TRC20), так что при
    низкой цене подписки лучше передавать конкретную дешёвую сеть явно
    (см. settings.CRYPTO_PAY_CURRENCY), а не оставлять выбор открытым."""
    body = {
        "price_amount": price_amount,
        "price_currency": "usd",
        "order_id": order_id,
        "order_description": order_description,
        "ipn_callback_url": ipn_callback_url,
    }
    if pay_currency:
        body["pay_currency"] = pay_currency
    if success_url:
        body["success_url"] = success_url
    if cancel_url:
        body["cancel_url"] = cancel_url

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(f"{BASE_URL}/invoice", json=body, headers=_headers())

    try:
        payload = response.json()
    except ValueError:
        payload = {"message": response.text}

    if response.status_code >= 400:
        message = payload.get("message", "Ошибка NOWPayments")
        logger.error(f"NOWPayments /invoice: HTTP {response.status_code}, ответ={payload}")
        raise NowPaymentsError(message, response.status_code, payload)

    return payload


async def get_payment_status(payment_id: str) -> dict:
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(f"{BASE_URL}/payment/{payment_id}", headers=_headers())

    try:
        payload = response.json()
    except ValueError:
        payload = {"message": response.text}

    if response.status_code >= 400:
        message = payload.get("message", "Ошибка NOWPayments")
        logger.error(f"NOWPayments /payment/{payment_id}: HTTP {response.status_code}, ответ={payload}")
        raise NowPaymentsError(message, response.status_code, payload)

    return payload


def verify_webhook_signature(payload: dict, signature: str) -> bool:
    """HMAC-SHA512 от JSON-тела с рекурсивно отсортированными по ключу
    полями, ровно как в официальном примере (sort_keys=True в json.dumps
    сортирует и вложенные словари, как php-шный tksort)."""
    sorted_msg = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    digest = hmac.new(
        credentials.NOWPAYMENTS_IPN_SECRET.encode(),
        sorted_msg.encode(),
        hashlib.sha512,
    )
    return hmac.compare_digest(digest.hexdigest(), signature)
