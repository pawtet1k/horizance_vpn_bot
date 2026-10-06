import asyncio
import inspect
from telethon import TelegramClient, events
from telethon.tl.types import KeyboardButton, ReplyKeyboardMarkup
import pymorphy3

import bot
import credentials
from my_logger import logger
import settings
import utils
from node_monitor import NodeMonitor
from db import UserManager
from subscription import SubscriptionManager
from vpn import VPNManager


api_id = credentials.API_ID
api_hash = credentials.API_HASH
bot_token = credentials.BOT_TOKEN

vpn_bot = TelegramClient('bot', api_id=api_id, api_hash=api_hash)
#.start(bot_token=bot_token)

waiting_for_question = set()

user_manager = None
vpn_manager = None
subscription_manager = None
morth = None

def initialize_bot_classes():
    global user_manager, vpn_manager, subscription_manager, morth
    user_manager = UserManager()
    vpn_manager = VPNManager()
    subscription_manager = SubscriptionManager(user_manager, vpn_manager)
    morth = pymorphy3.MorphAnalyzer()

def logging_command(user_id):
    caller_func = inspect.stack()[1].function
    logger.info(f'{user_id}: Нажал кнопку {caller_func}.')

def safe_handler(func):
    async def wrapper(event):
        try:
            await func(event)
        except Exception as e:
            logger.error(
                f'Ошибка в {func.__name__} '
                f'(user {event.sender_id}): {e}',
                exc_info=True  # <-- полный traceback в лог
            )
            try:
                await event.answer("Что-то пошло не так. Попробуйте позже или сообщите об этой ошибке разработчику, написав: '/help'.")
            except Exception:
                pass  # если даже ответить не можем — молча глотаем
    return wrapper

# Обработчики команд:
@vpn_bot.on(events.NewMessage(pattern='/start', incoming=True))
@safe_handler
async def start(event):
    logging_command(event.sender.id)
    await bot.handle_start(
        event, vpn_bot, event.sender.id, user_manager, subscription_manager, morth
    )

@vpn_bot.on(events.NewMessage(pattern='/config', incoming=True))
@safe_handler
async def config(event):
    logging_command(event.sender.id)
    await bot.start_vpn_setup(
        vpn_bot, event.sender.id, user_manager, vpn_manager
    )

@vpn_bot.on(events.NewMessage(pattern='/invite_friend', incoming=True))
@safe_handler
async def invite_friend(event):
    logging_command(event.sender.id)
    bot_username = (await vpn_bot.get_me()).username
    await bot.handle_invite_friend(vpn_bot, event.sender.id, bot_username)

@vpn_bot.on(events.NewMessage(pattern='/qr_code', incoming=True))
@safe_handler
async def bot_qrcode(event):
    logging_command(event.sender.id)
    bot_username = (await vpn_bot.get_me()).username
    await bot.handle_generate_referral_qrcode(event, bot_username)

@vpn_bot.on(events.NewMessage(pattern='/help', incoming=True))
@safe_handler
async def help(event):
    logging_command(event.sender.id)
    waiting_for_question.add(event.sender.id)
    await event.reply("Задайте ваш вопрос:")

# Обработчик сообщений:
# incoming=True обязателен: без него Telethon отдаёт обработчику и
# СОБСТВЕННЫЕ исходящие сообщения бота (в NewMessage.__init__ при
# incoming=outgoing=None включается _no_check и фильтрация не делается
# вовсе). Из-за этого ответы бота повторно попадали в handle_message и
# в логах появлялись чужие user_id на тексте, который отправлял бот.
async def forward_media_to_owner(event, user_id) -> bool:
    """Скриншоты и файлы из обращений: на почту уходит только текст, а чат
    бота владельцу не виден — поэтому вложение пересылаем ему в Telegram."""
    if not event.message.media or user_id == settings.OWNER_TELEGRAM_ID:
        return False
    try:
        username = getattr(event.sender, 'username', None) or '—'
        await vpn_bot.send_message(
            settings.OWNER_TELEGRAM_ID, f"📎 Вложение от {user_id} (@{username}):"
        )
        await event.forward_to(settings.OWNER_TELEGRAM_ID)
        logger.info(f'{user_id}: вложение переслано владельцу в Telegram.')
        return True
    except Exception as e:
        logger.error(f'{user_id}: не удалось переслать вложение владельцу: {e}')
        return False


@vpn_bot.on(events.NewMessage(incoming=True))
@safe_handler
async def handle_message(event):
    if event.message.message.startswith('/'):
        return
    logging_command(event.sender.id)
    user_id = event.sender.id

    if event.message.message.strip().lower() == settings.PROMO_WORD:
        await bot.claim_promo(event, vpn_bot, user_manager, vpn_manager)
        return

    if user_id in waiting_for_question:
        waiting_for_question.discard(user_id)
        question = event.message.message
        logger.warning(f'{user_id}: ВОПРОС. {question}')
        if await forward_media_to_owner(event, user_id):
            question += "\n[вложение переслано в Telegram]"
        user = user_manager.get_user(user_id)
        await utils.email_support(user, question)
        await event.reply(bot.SUPPORT_THANKS_MESSAGE, parse_mode="html")
        return

    # Обычное сообщение. Раньше оно только писалось в лог: человек писал боту
    # (часто — вопрос или ответ на рассылку) и не получал ничего, а владелец
    # об этом не знал. Теперь — на почту, как /help, и благодарность в ответ.
    text = event.message.message
    logger.info(f'{user_id}: Текст сообщения: "{text}".')
    if await forward_media_to_owner(event, user_id):
        text += "\n[вложение переслано в Telegram]"
    if text.strip() and user_id not in settings.REFERRAL_EXCLUDED_IDS:
        user = user_manager.get_user(user_id)
        if user is not None:
            await utils.email_support(user, f"[сообщение без /help]\n{text}")
            await event.reply(bot.SUPPORT_THANKS_MESSAGE, parse_mode="html")
    await bot.handle_not_command_message(event, user_manager, vpn_manager)

# Обработчики нажатия кнопок:
@vpn_bot.on(events.CallbackQuery(data="generate_settings_link"))
@safe_handler
async def generate_settings_link(event):
    logging_command(event.sender.id)
    await bot.start_vpn_setup(
        vpn_bot, event.sender.id, user_manager, vpn_manager
    )

'''
@vpn_bot.on(events.CallbackQuery(data="generate_settings_qrcode"))
@safe_handler
async def generate_settings_qrcode(event):
    logging_command(event.sender.id)
    await bot.callback_generate_settings_qrcode(vpn_bot, event.sender.id)
'''

# Команда /pay и кнопка «Подключить оплату» делают ровно одно и то же,
# поэтому это один обработчик с двумя подписками на события, а не две копии.
@vpn_bot.on(events.NewMessage(pattern='/pay', incoming=True))
@vpn_bot.on(events.CallbackQuery(data="add_subscription"))
@safe_handler
async def add_subscription(event):
    logging_command(event.sender.id)
    user = user_manager.get_user(event.sender.id)
    await bot.callback_add_subscription(vpn_bot, user, subscription_manager)

@vpn_bot.on(events.CallbackQuery(data="select_plan_month"))
@safe_handler
async def select_plan_month(event):
    logging_command(event.sender.id)
    await bot.callback_select_plan(
        vpn_bot, event.sender.id, settings.default_price,
        user_manager, subscription_manager
    )

@vpn_bot.on(events.CallbackQuery(data="select_plan_year"))
@safe_handler
async def select_plan_year(event):
    logging_command(event.sender.id)
    await bot.callback_select_plan(
        vpn_bot, event.sender.id, settings.yearly_price,
        user_manager, subscription_manager
    )

@vpn_bot.on(events.CallbackQuery(data="select_plan_router"))
@safe_handler
async def select_plan_router(event):
    logging_command(event.sender.id)
    await bot.callback_select_plan(
        vpn_bot, event.sender.id, settings.router_price,
        user_manager, subscription_manager
    )

@vpn_bot.on(events.CallbackQuery(data="change_plan"))
@safe_handler
async def change_plan(event):
    logging_command(event.sender.id)
    await bot.callback_change_plan(vpn_bot, event.sender.id, user_manager)

@vpn_bot.on(events.CallbackQuery(data="pay_method_sbp"))
@safe_handler
async def pay_method_sbp(event):
    logging_command(event.sender.id)
    await bot.callback_pay_sbp(vpn_bot, event.sender.id, user_manager, subscription_manager)

@vpn_bot.on(events.CallbackQuery(data="pay_method_crypto"))
@safe_handler
async def pay_method_crypto(event):
    logging_command(event.sender.id)
    await bot.callback_pay_crypto(vpn_bot, event.sender.id, user_manager, subscription_manager)

@vpn_bot.on(events.CallbackQuery(data="cancel_subscription"))
@safe_handler
async def cancel_subscription(event):
    logging_command(event.sender.id)
    await bot.callback_cancel_subscription(
        vpn_bot, event.sender.id, subscription_manager
    )

@vpn_bot.on(events.CallbackQuery)
@safe_handler
async def cb_device_selected(event):
    device_os = event.data.decode()
    if device_os not in settings.VPN_CLIENT_DOWNLOAD_URLS:
        return
    await bot.finish_vpn_setup(
        vpn_bot, event.sender.id, device_os, user_manager, vpn_manager
    )

def handle_exception(loop, context):
    exception = context.get("exception")
    message = context.get("message")
    logger.error(f'Необработанное исключение: {message} | {exception}')


async def main():
    try:
        await vpn_bot.start(bot_token=bot_token)
        asyncio.create_task(utils.start_timer(
            vpn_bot, subscription_manager, user_manager, settings.NOTIFICATION_CAMPAIGNS,
            settings.VPN_CLIENT_DOWNLOAD_URLS,
        ))
        asyncio.create_task(utils.ping(vpn_bot))
        asyncio.create_task(NodeMonitor(vpn_manager, user_manager, vpn_bot).run())
        logger.info('--------------------------------------------------------')
        logger.info('Клиент telethon запущен.')
        await vpn_bot.run_until_disconnected()
    except Exception as e:
        logger.error(f'Общепрограммная ошибка: {e}')
    finally:
        await vpn_bot.disconnect()

if __name__ == "__main__":
    initialize_bot_classes()
    asyncio.run(main())
#    loop = asyncio.get_event_loop()
#    loop.set_exception_handler(handle_exception)
#    loop.set_debug(True)
    #with vpn_bot:
#    loop.run_until_complete(main())
    #asyncio.run(main())
    #vpn_bot.loop.run_until_complete(main())

#loop.run_until_complete(client.start())
#asyncio.run(main())
#loop.run_forever()
#loop = asyncio.get_event_loop()
#loop.run_until_complete(main())
#loop.close()
