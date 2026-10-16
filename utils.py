import asyncio
from collections import namedtuple
from datetime import datetime
from email.mime.text import MIMEText
from email.utils import formataddr
import json
import smtplib
import uuid
import credentials
import httpx
from my_logger import logger

# message_en — тот же текст по-английски для иностранцев (i18n.is_english)
Campaign = namedtuple('Campaign', ['predicate', 'message', 'name', 'message_en'], defaults=(None,))


def generate_qrcode(link):
    import qrcode
    qr_file = f"user_qrcode(to_be_deleted)_{uuid.uuid4()}.png"
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(link)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    with open(qr_file, 'wb') as f:
        img.save(f)
    return qr_file

async def start_timer(
    vpn_bot, subscription_manager, user_manager, notification_campaigns, download_urls
):
    while True:
        try:
            await subscription_manager.process_payments(vpn_bot)
            await subscription_manager.delete_expired_trial_clients()
            await run_notification_campaigns(
                vpn_bot, user_manager, notification_campaigns
            )
            await check_download_links(download_urls)
            await refresh_geo_assets()
            logger.info('Цикл таймера завершён.')
        except Exception as e:
            logger.error(f'Ошибка в start_timer: {e}')
        await asyncio.sleep(12 * 3600)


async def refresh_geo_assets() -> None:
    """Обновляет geosite.dat/geoip.dat на нодах, где они старше недели.

    Сам по себе шаг дешёвый: на свежих нодах это один find по ssh. Реальная
    работа (скачивание + рестарт xray + возврат клиентов) случается примерно
    раз в неделю на ноду — см. geo_assets.py, там же объяснено, почему
    рестарт обязательно сопровождается restore_users.
    """
    import geo_assets  # локальный импорт: geo_assets тянет db/vpn через restore_users

    try:
        await geo_assets.refresh_all()
    except Exception as e:
        logger.error(f'Ошибка обновления geo-баз: {e}')


async def check_download_links(download_urls: dict[str, str]) -> None:
    """Раз в цикл таймера проверяет, что ссылки на скачивание VPN-клиента
    (settings.VPN_CLIENT_DOWNLOAD_URLS, показываются на /trial) ещё открываются.
    """
    import settings  # локальный импорт — settings.py импортирует utils.py, к этому моменту оба уже загружены

    broken = []
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        for name, url in download_urls.items():
            try:
                r = await client.head(url)
                if r.status_code >= 400:
                    r = await client.get(url)  # не все сервера отвечают на HEAD
                if r.status_code >= 400:
                    broken.append(f"{name}: {url} → {r.status_code}")
            except Exception as e:
                broken.append(f"{name}: {url} → {e}")

    if broken:
        send_email(
            "Horizance VPN: битые ссылки на скачивание клиента",
            "Не открываются следующие ссылки на /trial:\n\n" + "\n".join(broken),
            settings.NOTIFY_EMAIL,
        )
        logger.warning(f"Битые ссылки на скачивание клиента: {broken}")
    else:
        logger.info("Проверка ссылок на скачивание клиента: все рабочие.")

async def send_telegram_api_message(user_id: int, text: str, buttons: list | None = None,
                                    parse_mode: str | None = "Markdown") -> bool:
    """Отправка сообщения пользователю напрямую через Telegram Bot API
    (a не через живую сессию Telethon). Нужна процессам без подключённого
    TelegramClient — например, вебхуку оплаты UDM Pay на сайте.

    Каждое отправленное сообщение пишется в лог строкой «Ответ техподдержки для <id>: <текст>» — переписка
    с клиентом должна быть видна будущим агентам (решение владельца 11.10.26). buttons — список рядов
    [{"text": ..., "callback_data": ...} | {"text": ..., "url": ...}]."""
    url = f"https://api.telegram.org/bot{credentials.BOT_TOKEN}/sendMessage"
    payload = {"chat_id": user_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(url, json=payload)
        if response.status_code != 200:
            logger.error(f"{user_id}: Telegram Bot API sendMessage HTTP {response.status_code}: {response.text}")
            logger.info(f"Ответ техподдержки для {user_id} НЕ ДОСТАВЛЕН (HTTP {response.status_code}): {text}")
            return False
        labels = " | ".join(b["text"] for row in (buttons or []) for b in row)
        logger.info(f"Ответ техподдержки для {user_id}: {text}" + (f" [кнопки: {labels}]" if labels else ""))
        return True
    except Exception as e:
        logger.error(f"{user_id}: ошибка Telegram Bot API sendMessage: {e}")
        return False


def days_since(date_str: str) -> int:
    start = datetime.strptime(date_str, "%d.%m.%y")
    return (datetime.now() - start).days

def _should_run_campaign_today(user, campaign_name: str) -> bool:
    if not user.notifications:
        return True
    today = datetime.now().strftime('%d.%m.%y')
    return not any(
        n['campaign'] == campaign_name and n['sent_at'].startswith(today)
        for n in json.loads(user.notifications)
    )

def _record_notification(user_manager, user, campaign_name: str) -> None:
    notifications = json.loads(user.notifications) if user.notifications else []
    notifications.append({
        'campaign': campaign_name,
        'sent_at': datetime.now().strftime('%d.%m.%y %H:%M:%S')
    })
    user_manager.update_user(
        user.user_id, notifications=json.dumps(notifications, ensure_ascii=False)
    )

async def broadcast_message(
    vpn_bot, user_manager, user_ids, message, campaign_name, buttons=None,
    message_en=None, buttons_en=None
):
    from i18n import is_english  # локально: i18n тянет db
    sent, skipped, failed = 0, 0, 0
    for user_id in user_ids:
        user = user_manager.get_user(user_id)
        if not _should_run_campaign_today(user, campaign_name):
            skipped += 1
            continue
        english = message_en is not None and is_english(user)
        try:
            await vpn_bot.send_message(
                user_id, message_en if english else message,
                buttons=(buttons_en or buttons) if english else buttons, parse_mode='md'
            )
            _record_notification(user_manager, user, campaign_name)
            sent += 1
            logger.info(f'{user_id}: сообщение рассылки "{campaign_name}" отправлено.')
        except Exception as e:
            failed += 1
            logger.error(f'{user_id}: ошибка отправки рассылки "{campaign_name}": {e}')
    logger.warning(
        f'Рассылка "{campaign_name}" завершена: '
        f'отправлено {sent}, пропущено {skipped}, ошибок {failed}.'
    )
    return sent, failed

async def run_notification_campaigns(vpn_bot, user_manager, campaigns):
    users = user_manager.load_users()
    for campaign in campaigns:
        target_ids = [
            row['user_id'] for row in users if campaign.predicate(row)
        ]
        if target_ids:
            await broadcast_message(
                vpn_bot, user_manager, target_ids, campaign.message, campaign.name,
                message_en=campaign.message_en
            )

async def ping(vpn_bot):
    from telethon.tl.functions import PingRequest
    i=0
    while True:
        i+=1
        try:
            await vpn_bot(PingRequest(ping_id=0))
        except Exception as e:
            logger.error(f"Ping #{i} failed: {e}")
        logger.debug(f"Ping #{i} sent")
        await asyncio.sleep(3600)

def send_email(subject: str, body: str, to: str) -> bool:
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = subject
    msg['From'] = formataddr((getattr(credentials, "SMTP_FROM_NAME", "Horizance VPN"), credentials.SMTP_FROM))
    msg['To'] = to

    try:
        with smtplib.SMTP_SSL(credentials.SMTP_HOST, 465) as smtp:
            smtp.login(credentials.SMTP_USER, credentials.SMTP_PASSWORD)
            smtp.send_message(msg)
        return True
    except Exception as e:
        logger.error(f"Ошибка отправки email '{subject}' на {to}: {e}")
        return False


async def notify_owner(subject: str, text: str) -> None:
    """Сразу сообщить владельцу: Telegram (Bot API — работает и из процесса сайта) + письмо на NOTIFY_EMAIL.
    Не через send_telegram_api_message: это не переписка с клиентом, в журнал «Ответ техподдержки» не пишем.
    16.10.26: о неудачных попытках оплаты владелец узнавал только на следующий день, когда спрашивал сам."""
    import settings
    logger.warning(f"Владельцу: {subject} | {text}")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(f"https://api.telegram.org/bot{credentials.BOT_TOKEN}/sendMessage",
                                         json={"chat_id": settings.OWNER_TELEGRAM_ID, "text": text})
        if response.status_code != 200:
            logger.error(f"Владельцу в Telegram не ушло: HTTP {response.status_code}: {response.text}")
    except Exception as e:
        logger.error(f"Владельцу в Telegram не ушло: {e}")
    await asyncio.to_thread(send_email, subject, text, settings.NOTIFY_EMAIL)


async def email_support(user, question: str, to: str | None = None):
    """to=None — исторический адрес из credentials.SMTP_TO (так шлёт бот).
    Форма на сайте передаёт settings.SUPPORT_EMAIL."""
    user_id = user.user_id
    username = getattr(user, 'username', 'неизвестен')

    body = (
        f"Вопрос от пользователя:\n"
        f"Telegram ID: {user_id}\n"
        f"Username: @{username}\n\n"
        f"Вопрос:\n{question}"
    )

    sent = send_email(
        f"VPN. Вопрос от @{username} ({user_id})", body, to or credentials.SMTP_TO
    )

    if sent:
        logger.info(f"{user_id}: вопрос отправлен на почту")
