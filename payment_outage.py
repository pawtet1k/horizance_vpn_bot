"""Сбой платёжного провайдера: кто пытался оплатить, пока он лежал, и в каком состоянии сбой.

Файл (settings.PAYMENT_OUTAGE_PATH) переживает рестарт бота: иначе «снова онлайн» и рассылка людям потерялись бы.
  users      — user_id, у которых UDM Pay не создал заказ (subscription.py пишет сюда сам); вне сбоя
               (случайный таймаут) node_monitor их стирает — писать «оплата заработала» им незачем;
  since      — с какой даты искать неоплаченные заказы (крипта и др.) в payment_orders;
  down_since — когда node_monitor.run_udmpay_watch объявил UDM Pay недоступным (None, если всё хорошо);
  attempts   — {user_id: unix-время последней неудачной попытки СБП}: письмо о брошенной оплате пишет «перед этим СБП
               не сработала», только если она правда не сработала у этого человека незадолго до заказа (14.10.26);
  queued     — [{user_id, text}]: сообщения, одобренные владельцем «отправить, когда заработает оплата» (14.10.26);
               уходят один раз сразу после рассылки «оплата заработала».
Рассылка при восстановлении — node_monitor.run_udmpay_watch → notify_recovered.
Пишут два процесса (бот и сайт) без блокировки: при одновременной записи одна попытка может потеряться — редкость,
цена — одно сообщение «оплата заработала».
"""
import json
import time
from pathlib import Path

import settings


def load() -> dict:
    try:
        data = json.loads(Path(settings.PAYMENT_OUTAGE_PATH).read_text())
    except (OSError, ValueError):
        data = {}
    return {"users": sorted(set(data.get("users", []))), "since": data.get("since"), "down_since": data.get("down_since"),
            "attempts": data.get("attempts", {}), "queued": data.get("queued", [])}


def save(data: dict) -> None:
    Path(settings.PAYMENT_OUTAGE_PATH).write_text(json.dumps(data, ensure_ascii=False))


def is_down() -> bool:
    return load()["down_since"] is not None


def note_attempt(user_id: int) -> None:
    data = load()
    if user_id not in data["users"]:
        data["users"].append(user_id)
    data["attempts"][str(user_id)] = time.time()
    save(data)


def failed_between(user_id: int, start: float, end: float) -> bool:
    """Была ли у человека неудачная попытка СБП в этом промежутке (unix-время)."""
    t = load()["attempts"].get(str(user_id))
    return t is not None and start <= t <= end
