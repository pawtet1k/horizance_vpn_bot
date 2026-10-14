"""Whop — оплата картой для иностранцев (11.10.26, решение владельца; цены $1.8 / $11.9 — settings.CRYPTO_PRICE_USD).

Whop — продавец-посредник (Merchant of Record): сам принимает карты, платит налоги, разбирает возвраты. Аккаунт и
компания — Horizance LLC, те же, что у horizance.com (факс). Чтобы VPN не выдавал себя за horizance.com, товар и тариф
называются «Horizance VPN». Покупателям из России Whop продавать запрещает (санкции) — поэтому карта предлагается
только иностранцам (i18n.is_english).

Как и на horizance.com (проверено там на живом API 24.07.26):
  * один POST /checkout_configurations с разовым тарифом прямо в запросе; company_id — только внутри plan;
  * уникальный external_identifier товара на каждый заказ — иначе Whop переиспользует тариф и не вернёт metadata;
  * вебхуки по стандарту Standard Webhooks (HMAC-SHA256 от «id.timestamp.body»), секрет — свой вебхук VPN
    (создан через POST /webhooks, credentials.WHOP_WEBHOOK_SECRET).
Вебхуки компании приходят и на horizance.com: его обработчик заказ без job_id просто пропускает с 200.
"""
import base64
import hashlib
import hmac
import json
import time

import httpx

import credentials
from my_logger import logger

API_BASE = "https://api.whop.com/api/v1"
WEBHOOK_TOLERANCE_S = 5 * 60


def enabled() -> bool:
    return bool(getattr(credentials, "WHOP_API_KEY", "") and getattr(credentials, "WHOP_COMPANY_ID", ""))


async def create_checkout(order_id: str, user_id: int, price_usd: float, days: int, redirect_url: str) -> str | None:
    """Ссылка на оплату картой или None при ошибке. metadata возвращается в вебхуке payment.succeeded."""
    period = "1 year" if days >= 365 else "1 month"
    body = {
        "mode": "payment",
        "redirect_url": redirect_url,
        "metadata": {"service": "vpn", "order": order_id, "user_id": str(user_id)},
        "plan": {
            "company_id": credentials.WHOP_COMPANY_ID,
            "currency": "usd",
            "initial_price": round(float(price_usd), 2),
            "plan_type": "one_time",
            "title": f"Horizance VPN — {period}"[:30],
            "product": {"external_identifier": f"horizance-vpn-{order_id}", "title": "Horizance VPN"},
        },
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(f"{API_BASE}/checkout_configurations", json=body,
                                  headers={"Authorization": f"Bearer {credentials.WHOP_API_KEY}"})
        if r.status_code >= 300:
            logger.error(f"Whop: создание оплаты {order_id} не удалось, HTTP {r.status_code}: {r.text[:300]}")
            return None
        url = r.json().get("purchase_url") or r.json().get("url")
    except Exception as e:
        logger.error(f"Whop: создание оплаты {order_id} — ошибка: {e!r}")
        return None
    if url and url.startswith("/"):
        url = f"https://whop.com{url}"
    return url


def verify_webhook(payload: bytes, headers) -> dict | None:
    """Событие, если подпись верна и свежая, иначе None."""
    secret = getattr(credentials, "WHOP_WEBHOOK_SECRET", "")
    msg_id, ts, sig_header = (headers.get("webhook-id", ""), headers.get("webhook-timestamp", ""),
                              headers.get("webhook-signature", ""))
    if not (secret and msg_id and ts and sig_header):
        return None
    try:
        if abs(time.time() - int(ts)) > WEBHOOK_TOLERANCE_S:
            return None
    except ValueError:
        return None
    # ключ: у секретов Whop вида «ws_…» — сами байты строки, у «whsec_…» — base64 после префикса (как на horizance.com)
    key = base64.b64decode(secret[len("whsec_"):]) if secret.startswith("whsec_") else secret.encode()
    expected = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + payload, hashlib.sha256).digest()).decode()
    for part in sig_header.split():
        if hmac.compare_digest(part.split(",", 1)[-1], expected):
            try:
                return json.loads(payload)
            except ValueError:
                return None
    return None
