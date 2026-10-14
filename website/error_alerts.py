import time
import traceback

from fastapi import Request

import settings
from utils import send_email
from my_logger import logger

# Не чаще раза в 30 минут на одну и ту же ошибку+путь — иначе при падающем
# в цикле запросе приходили бы сотни писем подряд.
_ALERT_COOLDOWN = 30 * 60
_last_sent: dict[str, float] = {}


def notify(key: str, subject: str, body: str, cooldown: int = _ALERT_COOLDOWN) -> None:
    """Письмо с защитой от повторов: не чаще раза в cooldown секунд на key."""
    now = time.time()
    if now - _last_sent.get(key, 0) < cooldown:
        return
    _last_sent[key] = now
    send_email(subject=subject, body=body, to=settings.NOTIFY_EMAIL)
    logger.info(f"Уведомление '{key}' отправлено")


def notify_error(request: Request, exc: Exception) -> None:
    notify(
        key=f"{type(exc).__name__}:{request.url.path}",
        subject=f"Horizance VPN: ошибка на сайте — {type(exc).__name__}",
        body=(
            f"{request.method} {request.url.path}\n"
            f"{type(exc).__name__}: {exc}\n\n"
            f"{traceback.format_exc()}"
        ),
    )
