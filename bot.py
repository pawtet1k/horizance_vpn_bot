from telethon import Button
#from telethon.tl.types import InlineKeyboardMarkup, InlineKeyboardButton

import asyncio
from datetime import datetime, timedelta
import json
import os

import credentials
from my_logger import logger
from models import UserStatus
import settings
import utils
from utils import generate_qrcode


SUPPORT_THANKS_MESSAGE = "Спасибо за обращение! Разработчик ответит в течение 24 часов."


async def send_vpn_setup_rejection_msg(vpn_bot, user_id):
    keyboard = [
        [Button.inline("Подключить оплату", data="add_subscription")]
    ]
    await vpn_bot.send_message(
        user_id,
        "Для настройки приложения требуется подключить оплату",
        buttons=keyboard
    )


PLAN_OPTIONS = [
    (settings.default_price, "Месяц", "select_plan_month"),
    (settings.yearly_price, f"Год (−{settings.yearly_discount_percent}%)", "select_plan_year"),
]


def plan_choice_buttons(exclude_price=None):
    """exclude_price - убрать из списка уже действующий тариф (используется
    при смене тарифа, чтобы нельзя было "сменить" на тот же самый)."""
    return [
        [Button.inline(f"{label} — {price} ₽", data=data)]
        for price, label, data in PLAN_OPTIONS
        if price != exclude_price
    ]


async def callback_choose_plan(vpn_bot, user_id):
    await vpn_bot.send_message(
        user_id, "Выберите тариф подписки:", buttons=plan_choice_buttons()
    )


async def callback_change_plan(vpn_bot, user_id, user_manager):
    user = user_manager.get_user(user_id)
    buttons = plan_choice_buttons(exclude_price=user.price)
    if not buttons:
        await vpn_bot.send_message(
            user_id, "Для вашего текущего тарифа нет других вариантов."
        )
        return
    await vpn_bot.send_message(
        user_id, "Выберите новый тариф подписки:", buttons=buttons
    )


async def callback_select_plan(vpn_bot, user_id, price, user_manager, subscription_manager):
    user_manager.update_user(user_id, price=price)
    if settings.NOWPAYMENTS_ENABLED and price != 0:
        # выбор способа оплаты нужен только когда крипто реально доступна -
        # иначе (флаг выключен) ведём себя как раньше, без лишнего шага
        await callback_choose_payment_method(vpn_bot, user_id)
        return
    user = user_manager.get_user(user_id)
    await subscription_manager.add_subscription(vpn_bot, user)


def payment_method_buttons():
    return [
        [Button.inline("💳 СБП (Мир)", data="pay_method_sbp")],
        [Button.inline("🪙 Криптовалюта", data="pay_method_crypto")],
    ]


async def callback_choose_payment_method(vpn_bot, user_id):
    await vpn_bot.send_message(
        user_id, "Выберите способ оплаты:", buttons=payment_method_buttons()
    )


async def callback_pay_sbp(vpn_bot, user_id, user_manager, subscription_manager):
    user = user_manager.get_user(user_id)
    await subscription_manager.add_subscription(vpn_bot, user)


async def callback_pay_crypto(vpn_bot, user_id, user_manager, subscription_manager):
    user = user_manager.get_user(user_id)
    await subscription_manager.add_crypto_subscription(vpn_bot, user)


# Обработчики событий и коллбэков
async def handle_start(event, vpn_bot, user_id, user_manager, subscription_manager, morth):
    def decline_word(number, word, morth=morth):
        parsed_word = morth.parse(word)[0]
        declined_word = parsed_word.make_agree_with_number(number).word
        return declined_word

    async def get_referrer(event):
        referrer_id = None
        if event.message.text.strip() != '/start':
            referrer_id = int(event.message.text.split()[-1])
            logger.info(f'Определена реферальная ссылка: {referrer_id}.')
        return referrer_id

    async def increase_referrals(vpn_bot, referrer, user_manager):
        referrer.referrals += 1
        user_manager.update_user(
            referrer.user_id,
            referrals=referrer.referrals
        )
        logger.info(f'{referrer.user_id} привлек друга!')
        '''
        await vpn_bot.send_message(
            referrer.user_id,
            "Благодаря вашей рекомендации, один из приглашенных вами друзей "
            "стал нашим пользователем.\n"
            "В знак благодарности за поддержку нашего проекта дарим вам "
            f"бесплатную подписку на {settings.referral_free_days}."
        )
        '''

    async def create_user(event, vpn_bot, user_id, user_manager=user_manager):
        telegram_user = await event.client.get_entity(user_id)
        username = telegram_user.username
        first_name = telegram_user.first_name
        last_name = telegram_user.last_name
        phone = telegram_user.phone
        language = telegram_user.lang_code
        referrer_id = await get_referrer(event)
        user = user_manager.add_user(
            user_id=user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            language=language,
            referrer_id=referrer_id
        )
        referrer_info = ''
        if referrer_id:
            referrer = user_manager.get_user(referrer_id)
            await increase_referrals(vpn_bot, referrer, user_manager)
            referrer_info = (
                f'\nСправка по {referrer_id}: {referrer.username}, '
                f'{referrer.first_name}, {referrer.last_name}, '
                f'{referrer.phone}.'
            )

        logger.warning(
            f'{user_id}: НОВЫЙ ПОЛЬЗОВАТЕЛЬ! Пригласил: {referrer_id}.\n'
            f'Справка по {user_id}: {username}, {first_name}, '
            f'{last_name}, {phone}.{referrer_info}.'
        )
        return user

    if user_manager.is_user_new(user_id):
        is_user_new = True
        user = await create_user(event, vpn_bot, user_id)
    else:
        is_user_new = False
        user = user_manager.get_user(user_id)

    billing_period_days = settings.get_billing_period_days(user.price)
    if user.payment_token == None:
        subscription_rows = [
            [Button.inline("💳 Подключить оплату", data="add_subscription")]
        ]
        subscription_status = "⚠**не настроено** (подключите оплату)"
    else:
        subscription_rows = [
            [Button.inline("🔄 Сменить тариф", data="change_plan")],
            [Button.inline("Отключить оплату", data="cancel_subscription")],
        ]
        # тариф показываем только тут - в общем виде (независимо от того,
        # настроено продление или нет) это поле не нужно
        subscription_status = f"🌐 **настроено** ({user.price} руб / {billing_period_days} дней)"
    keyboard = [
        [Button.inline("🔗 Настроить VPN", data="generate_settings_link")],
 #       [Button.inline("По QR коду", data="generate_settings_qrcode")],
        *subscription_rows
    ]
    referral_free_days_text = (
        f"{settings.referral_free_days} "
        f"{decline_word(settings.referral_free_days, 'день')}"
    )
    subscription_status_and_price_and_referral_program_msg = (
        f"Продление подписки:\n**{subscription_status}**\n\n"
        f"Если у вас возникнут вопросы или проблемы с подключением, напишите /help.\n"
        f"Также для восстановления доступа к боту вы можете воспользоваться [пробным доступом на 1 день]({credentials.TRIAL_LINK})."
    )
    if is_user_new:
        trial_days_text = (
            f"{settings.trial_days}"
            f"{decline_word(settings.trial_days, 'день')}"
        )
        await event.reply(
            "Рад, что вы ко мне обратились!\n"
            f"Оцените, как здорово пользоваться интернетом вместе со мной!\n"
            f"В течение ближайших **{trial_days_text}** "
            "мои услуги для вас будут бесплатными.\n"
            f"{subscription_status_and_price_and_referral_program_msg}"
            "Пожалуйста, выберите 'Настроить VPN':",
            file='hero.jpg', buttons=keyboard, parse_mode='md'
        )

    else:
        #user = user_manager.get_user(user_id)
        days_left = subscription_manager.get_days_left(user)
        days_left_text = (
            f"{decline_word(days_left, 'осталось')} "
            f"**{days_left} {decline_word(days_left, 'день')}**"
        )
        await event.reply(
            "С возвращением, друг!\n\n"
            f"Статус вашей подписки:\n**{user.status.label_ru()}**\n"
            f"Подписка действует до:\n"
            f"📆 **{user.next_payment_date}**\n"
            f"⏰ {days_left_text}\n"
            f"{subscription_status_and_price_and_referral_program_msg}",
            file='hero.jpg', buttons=keyboard, parse_mode='md'
        )

# Настройку одного пользователя выполняем строго по одной за раз. Кнопку можно
# нажать несколько раз подряд, и Telethon запустит обработчик столько же раз
# параллельно. Без этого все запуски успевали прочитать client_uuid=None до
# того, как первый из них его сохранит, и каждый создавал СВОЙ uuid: в xray
# оставался один, в БД — другой, ссылка у клиента не работала. С блокировкой
# второе нажатие дожидается первого и видит уже сохранённый uuid.
_vpn_setup_locks: dict[int, asyncio.Lock] = {}


async def finish_vpn_setup(
    vpn_bot, user_id, device_os, user_manager, vpn_manager
):
    app_url = await set_and_send_app_url(vpn_bot, user_id, device_os)
    to_router = device_os == settings.ROUTER_DEVICE

    lock = _vpn_setup_locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        user = user_manager.get_user(user_id)
        if to_router and user.router_quota_exhausted():
            await vpn_bot.send_message(
                user_id,
                f"Лимит роутера — {settings.ROUTER_TRAFFIC_LIMIT_GB} ГБ за {settings.ROUTER_PERIOD_DAYS} дней — "
                f"исчерпан. Роутер снова заработает {user.router_window_end():%d.%m}.\n\n"
                "Телефон или компьютер можно подключить и сейчас: /config → ваше устройство."
            )
            return
        # Ссылка у человека одна: или обычная (устройства по hwid, не больше трёх), или роутерная (без
        # счёта устройств, с лимитом трафика). Смена режима — новый uuid; setup_xray снимает старый из
        # xray (rmu по email = user_id), и прежняя ссылка перестаёт работать.
        switched = bool(user.client_uuid) and (user.device_os == settings.ROUTER_DEVICE) != to_router
        if switched:
            logger.warning(f"{user_id}: смена режима {user.device_os} → {device_os}, выдаём новый uuid")
            user.client_uuid = None
        updates = dict(device_os=device_os)
        if to_router and not user.traffic_period_date:
            # первое окно лимита; дальше окна двигает node_monitor, повторный выбор роутера их не сбрасывает
            updates.update(traffic_period_start=user.traffic_total_mb(),
                           traffic_period_date=datetime.now().date().isoformat())
        client_uuid = await vpn_manager.setup_xray(user)
        user_manager.update_user(user_id, client_uuid=client_uuid, **updates)

    note = ""
    if switched:
        note = ("\n\n⚠️ Ссылка у вас одна: прежняя ссылка для телефонов и компьютеров больше не работает. "
                "Вернуться — /config → ваше устройство." if to_router else
                "\n\n⚠️ Ссылка у вас одна: роутерная ссылка больше не работает. "
                "Вернуться — /config → «Роутер».")
    await set_and_send_client_link(
        vpn_bot, user_id, client_uuid, app_url, device_os, note
    )

async def start_vpn_setup(vpn_bot, user_id, user_manager, vpn_manager):
    user = user_manager.get_user(user_id)
    if user.status == UserStatus.INACTIVE:
        await send_vpn_setup_rejection_msg(vpn_bot, user_id)
        return

    await resolve_device_os(vpn_bot, user_id)

async def set_and_send_app_url(vpn_bot, user_id, device_os):
    app_url = settings.VPN_CLIENT_DOWNLOAD_URLS.get(
        device_os, "Дистрибутив для ващей операционной системы не найден."
    )
    return app_url

async def set_and_send_client_link(
    vpn_bot, user_id, client_uuid, app_url, device_os=None, note=""
):
    client_link = f"https://{credentials.SUBSCRIPTION_DOMAIN}/vpn/{client_uuid}" #vpn_manager.generate_settings_link(client_uuid)
    # Адрес подписки в чистом виде. /vpn/<uuid> открывает Happ по
    # happ://-ссылке, и на iPhone это ломается, если Happ не установлен, —
    # приложения нет в российском App Store, а система на такую ссылку
    # отвечает «неизвестное действие deepLink». Обычный https-адрес
    # понимают все клиенты, поэтому на iOS даём его сразу, а не после
    # обращения в поддержку.
    sub_link = f"https://{credentials.SUBSCRIPTION_DOMAIN}/sub/{client_uuid}"

    if device_os == settings.ROUTER_DEVICE:
        await vpn_bot.send_message(
            user_id,
            "🛜 **VPN на роутере**\n\n"
            "Адрес подписки для роутера:\n"
            f"`{sub_link}`\n\n"
            "Добавьте его в VPN-клиент роутера как подписку — нужна прошивка с поддержкой VLESS Reality.\n\n"
            "Через VPN работают все устройства в сети роутера, их число не ограничено. "
            f"Лимит трафика — {settings.ROUTER_TRAFFIC_LIMIT_GB} ГБ за {settings.ROUTER_PERIOD_DAYS} дней."
            f"{note}",
            link_preview=False,
        )
        return

    if device_os == "TV":
        # На телевизоре Telegram не нужен: ссылку получают здесь, на телефоне,
        # и переносят в Happ на ТВ (QR с экрана ТВ или Web Import через
        # tv.happ.su — см. happ.su/main/ru/faq/android-tv)
        await vpn_bot.send_message(
            user_id,
            "📺 **VPN на телевизоре**\n\n"
            "1. Установите Happ на телевизор: на Android TV — из Google Play, "
            "на Apple TV — «Happ – Proxy Utility for TV» из App Store.\n"
            "2. Откройте Happ на телевизоре и выберите **Web Import** — на экране появится код.\n"
            "3. На телефоне откройте [tv.happ.su](https://tv.happ.su), введите код и вставьте адрес подписки:\n"
            f"`{sub_link}`\n\n"
            "Если Happ уже стоит на телефоне с нашей подпиской, можно проще: "
            "отсканируйте телефоном QR-код, который Happ показывает на телевизоре при первом запуске.\n\n"
            "Подробная инструкция: https://horizancevpn.com/tv"
            f"{note}",
            link_preview=False,
        )
        return

    ios_hint = ""
    if device_os == "iOS":
        clients = ", ".join(
            f"[{c['name']}]({c['url']})" for c in settings.IOS_ALTERNATIVE_CLIENTS
        )
        ios_hint = (
            f"\n\n📱 Если Happ не ставится (Apple убрала его из российского "
            f"App Store) — подойдёт бесплатное {clients}. Поставьте его и откройте "
            f"«Ваша личная конфигурация» — там кнопка «Добавить подписку в INCY». "
            f"Или в INCY нажмите «+» и вставьте адрес (как подписку):\n"
            f"`{sub_link}`"
        )

    await vpn_bot.send_message(
        user_id,
        f"1. Установите приложение Happ:\n[🔗 Скачать приложение]({app_url})\n\n"
        f"2. Кликните по ссылке:\n[🔗 Ваша личная конфигурация]({client_link})\n\n"
        f"⚠️ При возникновении ошибки попробуйте вставить эту ссылку в поисковую строку браузера вашего устройства\n\n"
        "3. Выберите в Happ любой из доступных серверов и нажмите на кнопку подключения 🔘"
        f"{ios_hint}{note}",
        file='hero.jpg'
    )
    #await event.respond(f"[Нажмите на меня]({client_link})", link_preview=False, parse_mode='md')
    #await event.respond(f"<a href='{client_link}'>Нажмите на меня</a>", link_preview=False, parse_mode='html')

'''
async def callback_generate_settings_qrcode(vpn_bot, user_id):
    user = user_manager.get_user(user_id)
    if user.status == UserStatus.INACTIVE:
        await send_vpn_setup_rejection_msg(vpn_bot, user_id)
        return

    client_uuid = await vpn_manager.setup_xray(vpn_bot, user, user_manager)
    settings_link = vpn_manager.generate_settings_link(client_uuid)
    settings_qr_file = generate_qrcode(settings_link)
    await vpn_bot.send_message(user_id, file=settings_qr_file)
    os.remove(settings_qr_file)
'''

async def callback_add_subscription(vpn_bot, user, subscription_manager):
    if user.price == 0:
        # бесплатная подписка (промо/реферальная) - выбор тарифа не нужен
        await subscription_manager.add_subscription(vpn_bot, user)
        return
    await callback_choose_plan(vpn_bot, user.user_id)

async def callback_cancel_subscription(vpn_bot, user_id, subscription_manager):
    await subscription_manager.cancel_subscription(vpn_bot, user_id)

async def generate_referral_link(user_id, bot_username):
    referral_url = f'https://t.me/{bot_username}?start={user_id}'
    return referral_url

async def handle_generate_referral_qrcode(event, bot_username):
    referral_link = await generate_referral_link(event.sender_id, bot_username)
    referral_qr_file = generate_qrcode(referral_link)
    await event.reply(file=referral_qr_file)
    logger.warning(f"Путь к qr коду: {referral_qr_file}")
    os.remove(referral_qr_file)

async def handle_invite_friend(vpn_bot, user_id, bot_username):
    referral_url = await generate_referral_link(user_id, bot_username)
    await vpn_bot.send_message(
        user_id,
        "Перешлите ссылку другу:\n"
        f"🤖 {referral_url}\n"
        f"🌐 {credentials.TRIAL_LINK}",
        file='hero.jpg'
        )

async def handle_not_command_message(event, user_manager, vpn_manager):
    async def check_user(user_id):
        #user = user_manager.get_user(user_id)
        user = await event.client.get_entity(user_id)
        logger.info(
            f'Информация о пользователе {user_id}:\n'
            f'{user.username},{user.first_name},{user.last_name},{user.phone}'
        )

    async def get_and_check_number_of_clients(users, user_manager, vpn_manager):
        not_inactive_users_with_uuid = len(user_manager.get_not_inactive_users_with_uuid('users'))
        clients = await vpn_manager.get_vpn_clients()
        not_trial_clients = {x for x in clients if not str(x).startswith('t_')}
        number_of_clients = len(clients)
        number_of_not_trial_clients = len(not_trial_clients)
        not_inactive_users = 0
        active_users = 0
        trial_users = 0
        free_users = 0
        for user in users:
            if user['status'] != UserStatus.INACTIVE:
                not_inactive_users += 1
                if user['status'] == UserStatus.ACTIVE:
                    active_users += 1
                if user['status'] == UserStatus.TRIAL:
                    trial_users += 1
                if user['status'] == UserStatus.FREE:
                    free_users += 1
        if number_of_not_trial_clients == not_inactive_users_with_uuid:
            logger.info(
                f'Все Ок! Кол-во VPN клиентов: {number_of_clients}, '
                f'из них кол-во основных клиентов: {number_of_not_trial_clients}. '
                f'Кол-во недеактивированных юзеров: {not_inactive_users}, '
                f'из них кол-во триал юзеров: {trial_users}, активных: {active_users}, '
                f'бесплатных: {free_users}'
            )
        else:
            logger.warning(
                'Что-то работает не так!\n'
                f'Кол-во недеактивированных юзеров с uuid: {not_inactive_users_with_uuid}, '
                f'а кол-во не триал клиентов {number_of_not_trial_clients}, '
                f'а всего VPN клиентов: {number_of_clients}.'
            )

    try:
        #await check_user(7528457226)
        users = user_manager.load_users()
        log_message = "\n" + "\n".join(str(user) for user in users)
        logger.debug(log_message)
        await get_and_check_number_of_clients(users, user_manager, vpn_manager)

        #for user in users:
         #   user_id = user['user_id']
          #  the_user = user_manager.get_user(user_id)
           # if the_user.language == '0':
            #    telegram_user = await event.client.get_entity(user_id)
             #   language = telegram_user.lang_code
              #  user_manager.update_user(user_id, language=language)
    except Exception as e:
        logger.error(f'Ошибка на сообщение "{event.message.message}": {e}')

async def resolve_device_os(vpn_bot, user_id):
    os_keys = list(settings.VPN_CLIENT_DOWNLOAD_URLS.keys())
    keyboard = [
        [Button.inline(os_keys[i], data=os_keys[i]) for i in range(2)],
        [Button.inline(os_keys[i], data=os_keys[i]) for i in range(2, len(os_keys))],
        [Button.inline(settings.ROUTER_LABEL, data=settings.ROUTER_DEVICE)],
    ]

    await vpn_bot.send_message(
        user_id, "Выберите вашу операционную систему:", buttons=keyboard
    )

async def claim_promo(event, vpn_bot, user_manager, vpn_manager) -> None:
    user_id = event.sender_id
    user = user_manager.get_user(user_id)
    # один раз на человека: отметка в notifications, как у рассылок
    already = user is not None and user.notifications and any(
        n.get("campaign") == settings.PROMO_CAMPAIGN_NAME
        for n in json.loads(user.notifications)
    )
    if user is not None and user.status == UserStatus.INACTIVE and not already:
        # от СЕГОДНЯ, а не от старой даты окончания: у тех, кто ушёл в 2025,
        # «старая дата + N дней» тоже в прошлом, и таймер сразу отключал бы снова
        new_next_payment_date = (
            datetime.now() + timedelta(days=settings.PROMO_DAYS)
        ).strftime("%d.%m.%y")
        client_uuid = await vpn_manager.setup_xray(user)
        user_manager.update_user(
            user_id,
            next_payment_date=new_next_payment_date,
            client_uuid=client_uuid,
            status=UserStatus.TRIAL
        )
        await event.reply(
            "🎉 Ваш промо-доступ активирован!\n\n"
            f"VPN продлён до **{new_next_payment_date}**.",
            parse_mode='md'
        )
        logger.warning(
            f'{user_id}: активирован промо-доступ до {new_next_payment_date}.'
        )
        utils._record_notification(
            user_manager, user_manager.get_user(user_id), settings.PROMO_CAMPAIGN_NAME
        )
        await start_vpn_setup(vpn_bot, user_id, user_manager, vpn_manager)
    else:
        await event.reply(
            "⛔️К сожалению, вы не можете воспользоваться данной акцией",
            parse_mode='md'
        )
