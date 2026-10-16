from telethon import Button
#from telethon.tl.types import InlineKeyboardMarkup, InlineKeyboardButton

import asyncio
from datetime import datetime, timedelta
import json
import os
import time

import credentials
import device_guard
from my_logger import logger
from models import UserStatus
import settings
import utils
import whop
from utils import generate_qrcode
from i18n import is_english, tr


SUPPORT_THANKS_MESSAGE = "Спасибо за обращение! Разработчик ответит в течение 24 часов."
SUPPORT_THANKS_MESSAGE_EN = "Thank you for your message! The developer will reply within 24 hours."


# Быстрые ответы в /help (14.10.26): человек ждал ответа на «как добавить устройство» и уходил, не дождавшись.
# Кнопка — ответ сразу; свой вопрос по-прежнему можно написать следующим сообщением (main.waiting_for_question).
HELP_FAQ = {
    "devices": (
        "📱 Ещё одно устройство", "📱 Another device",
        "Отправьте боту /config и выберите систему нового устройства — бот пришлёт инструкцию и ссылку, "
        "откройте её на этом устройстве. Подписка остаётся одна, в неё входят 3 устройства.\n\n"
        "Места нет — команда /devices: там можно убрать старое устройство или подключить ещё 3 за +99 ₽ в месяц к подписке.",
        "Send /config to the bot and pick the new device's system — the bot will send instructions and a link, "
        "open it on that device. It is the same subscription; it includes 3 devices.\n\n"
        "No free slot? Send /devices: remove an old device there or add 3 more to your subscription.",
    ),
    "cabinet": (
        "🌐 Личный кабинет на сайте", "🌐 Account on the website",
        f"Адрес: {credentials.SUBSCRIPTION_DOMAIN}/auth\n\n"
        "Вход — через Telegram: на компьютере отсканируйте QR-код камерой телефона, на телефоне нажмите "
        "«Войти через Telegram». Затем подтвердите вход в этом боте.\n\n"
        "В кабинете — срок подписки, оплата, ссылка для подключения и ваши устройства.",
        f"Address: {credentials.SUBSCRIPTION_DOMAIN}/auth\n\n"
        "Sign in with Telegram: on a computer, scan the QR code with your phone camera; on a phone, tap "
        "\"Sign in with Telegram\". Then confirm the sign-in in this bot.\n\n"
        "Your account shows the subscription term, payment, the connection link and your devices.",
    ),
    "noconnect": (
        "🔌 VPN не подключается", "🔌 VPN won't connect",
        "1. В приложении обновите подписку и выберите другой сервер.\n"
        "2. Проверьте, что на устройстве включены автоматические дата и время.\n"
        "3. Обновите приложение до последней версии.\n\n"
        "Не помогло — напишите сюда, на каком устройстве и что видите на экране.",
        "1. Refresh the subscription in the app and pick another server.\n"
        "2. Make sure automatic date and time are on.\n"
        "3. Update the app to the latest version.\n\n"
        "Still not working? Write here which device you use and what you see on the screen.",
    ),
    "iphone": (
        "🍏 Нет Happ в App Store", "🍏 No Happ in the App Store",
        "Happ убрали из российского App Store. Подойдёт бесплатное приложение INCY: отправьте боту /config, "
        "выберите iOS — в инструкции будут ссылки на INCY и на добавление подписки.",
        "Happ was removed from the Russian App Store. Use the free INCY app instead: send /config to the bot, "
        "pick iOS — the instructions include links to INCY and to add your subscription.",
    ),
}
HELP_PROMPT = ("Частые вопросы — нажмите, ответ придёт сразу.\n\nИли напишите свой вопрос одним сообщением — "
               "его получит разработчик.",
               "Common questions — tap one for an instant answer.\n\nOr type your question in one message — "
               "it goes to the developer.")


async def handle_help(event, user_id):
    await event.reply(tr(user_id, *HELP_PROMPT), buttons=[
        [Button.inline(tr(user_id, ru, en), data=f"faq:{key}")] for key, (ru, en, *_) in HELP_FAQ.items()
    ])


async def callback_help_faq(vpn_bot, user_id, key):
    item = HELP_FAQ.get(key)
    if item is None:
        return
    logger.info(f"{user_id}: быстрый ответ /help: {key}.")
    await vpn_bot.send_message(user_id, tr(user_id, f"**{item[0]}**\n\n{item[2]}", f"**{item[1]}**\n\n{item[3]}"),
                               parse_mode="md", link_preview=False)


async def send_vpn_setup_rejection_msg(vpn_bot, user_id):
    keyboard = [
        [Button.inline(tr(user_id, "Подключить оплату", "Subscribe"), data="add_subscription")]
    ]
    await vpn_bot.send_message(
        user_id,
        tr(user_id, "Для настройки приложения требуется подключить оплату",
           "To set up the app, please subscribe first"),
        buttons=keyboard
    )


PLAN_OPTIONS = [
    (settings.default_price, "Месяц", "Month", "select_plan_month"),
    (settings.yearly_price, f"Год (−{settings.yearly_discount_percent}%)",
     f"Year (−{settings.yearly_discount_percent}%)", "select_plan_year"),
]


def plan_choice_buttons(exclude_price=None, english=False, extra=False):
    """exclude_price - убрать из списка уже действующий тариф (используется
    при смене тарифа, чтобы нельзя было "сменить" на тот же самый).
    Иностранцу цену показываем и в долларах — в той же сумме, что берёт криптовалюта.
    extra — подключены доп. устройства: показываем сумму вместе с опцией (subscription._order_amounts)."""
    def label(price, ru, en):
        total = price + (settings.get_extra_devices_price(price) if extra else 0)
        if not english:
            return f"{ru} — {total} ₽" + (f" (с {settings.EXTRA_DEVICES_COUNT} доп. устройствами)" if extra else "")
        usd = settings.CRYPTO_PRICE_USD.get(price)
        usd = round(usd * total / price, 2) if usd else None
        return f"{en} — {total} ₽" + (f" (≈ ${usd})" if usd else "") + (" incl. extra devices" if extra else "")
    return [
        [Button.inline(label(price, ru, en), data=data)]
        for price, ru, en, data in PLAN_OPTIONS
        if price != exclude_price
    ]


async def callback_choose_plan(vpn_bot, user_id):
    user = utils_user(user_id)
    english = is_english(user)
    text = "Choose a plan:" if english else "Выберите тариф подписки:"
    # 15.10.26: с 05.10 новые на триале смотрели тарифы и уходили — не знали, что бесплатные дни не сгорают:
    # оплата продлевает от даты окончания (SubscriptionManager._extend_next_payment_date)
    try:
        until = datetime.strptime(user.next_payment_date, "%d.%m.%y").date()
    except (AttributeError, TypeError, ValueError):
        until = None
    if until and until > datetime.now().date() and user.status in (UserStatus.TRIAL, UserStatus.ACTIVE):
        if english:
            kind = "free days" if user.status == UserStatus.TRIAL else "subscription days"
            text += f"\n\nYour remaining {kind} are kept: the paid period starts after {until:%d.%m}."
        else:
            kind = "бесплатные дни" if user.status == UserStatus.TRIAL else "дни подписки"
            text += f"\n\nОставшиеся {kind} сохранятся: оплаченный период начнётся после {until:%d.%m}."
    await vpn_bot.send_message(
        user_id, text,
        buttons=plan_choice_buttons(english=english, extra=bool(user and user.extra_devices))
    )


def utils_user(user_id):
    from db import UserManager
    return UserManager().get_user(user_id)


async def callback_change_plan(vpn_bot, user_id, user_manager):
    user = user_manager.get_user(user_id)
    buttons = plan_choice_buttons(exclude_price=user.price, english=is_english(user), extra=bool(user.extra_devices))
    if not buttons:
        await vpn_bot.send_message(
            user_id, tr(user, "Для вашего текущего тарифа нет других вариантов.",
                        "There are no other options for your current plan.")
        )
        return
    await vpn_bot.send_message(
        user_id, tr(user, "Выберите новый тариф подписки:", "Choose a new plan:"), buttons=buttons
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


def payment_method_buttons(english=False):
    if english:
        # иностранцу: карта (Whop — России Whop не продаёт, поэтому только здесь), криптовалюта; СБП — только РФ-банки
        rows = [[Button.inline("💳 Card / Apple Pay", data="pay_method_card")]] if whop.enabled() else []
        return rows + [
            [Button.inline("🪙 Cryptocurrency (USDT and others)", data="pay_method_crypto")],
            [Button.inline("🏦 SBP — Russian bank cards only", data="pay_method_sbp")],
        ]
    return [
        [Button.inline("💳 СБП (Мир)", data="pay_method_sbp")],
        [Button.inline("🪙 Криптовалюта", data="pay_method_crypto")],
    ]


# ---------- /devices: свои устройства и докупка (владелец, 13.10.26) ----------
def devices_summary(user) -> tuple[list[dict], list[dict], int]:
    """(активные, убранные, лимит) — общее для бота и сайта (/devices)."""
    devices = device_guard.list_devices(user.user_id)
    active = [d for d in devices if not d["removed_at"]]
    removed = [d for d in devices if d["removed_at"]]
    return active, removed, device_guard.device_limit(vars(user))


def _seen(d: dict) -> str:
    return datetime.fromisoformat(d["last_seen"]).strftime("%d.%m")


def extra_devices_status(user) -> str | None:
    """Строка про опцию «доп. устройства» для /devices (бот и сайт) или None, если её нет."""
    n = settings.EXTRA_DEVICES_COUNT
    active = device_guard.extra_devices_active(user.extra_devices_until)
    if user.extra_devices and active:
        return tr(user, f"Доп. устройства (+{n}) подключены, продлеваются вместе с подпиской.",
                  f"Extra devices (+{n}) are on and renew together with your subscription.")
    if user.extra_devices:
        return tr(user, f"Доп. устройства (+{n}) войдут в оплату подписки.",
                  f"Extra devices (+{n}) will be included in your subscription payment.")
    if active:
        return tr(user, f"Доп. устройства (+{n}) отключены и действуют до {user.extra_devices_until}.",
                  f"Extra devices (+{n}) are turned off and stay active until {user.extra_devices_until}.")
    return None


# ─── Вход на сайт через бота (website/routers/auth.py, 14.10.26) ───
# Сайт показывает QR / кнопку со ссылкой t.me/<бот>?start=login_<token>. Кто её открыл, того Telegram и
# прислал нам в /start — его и спрашиваем. Подтверждение обязательно: иначе мошенник покажет свой QR, человек
# отсканирует, и браузер мошенника войдёт в чужой кабинет. Поэтому в вопросе — устройство и город входа.
WEB_LOGIN_EXPIRED_TEXT = ("Ссылка для входа устарела. Обновите страницу на сайте и отсканируйте новый QR-код.",
                          "This sign-in link has expired. Reload the page on the website and scan the new QR code.")


async def handle_web_login(event, vpn_bot, user_id, token, user_manager):
    login = user_manager.get_web_login(token)
    fresh = login and time.time() - login["created_at"] <= settings.WEB_LOGIN_TTL
    sender = event.sender
    if not fresh or not user_manager.update_web_login(
            token, "pending", status="opened", user_id=user_id, username=getattr(sender, "username", None),
            first_name=getattr(sender, "first_name", None), last_name=getattr(sender, "last_name", None)):
        logger.info(f"{user_id}: вход на сайт — ссылка устарела или уже использована.")
        await event.reply(tr(user_id, *WEB_LOGIN_EXPIRED_TEXT))
        return
    logger.info(f"{user_id}: вход на сайт — спрашиваем подтверждение ({login['device']}, {login['place']}).")
    where = "\n".join(x for x in (login["device"], login["place"]) if x)
    await event.reply(
        tr(user_id,
           f"Войти на сайт horizancevpn.com?\n\n{where}\n\nЕсли вход запрашивали не вы, нажмите «Отмена».",
           f"Sign in to horizancevpn.com?\n\n{where}\n\nIf you did not request this, tap \"Cancel\"."),
        buttons=[[Button.inline(tr(user_id, "✅ Войти", "✅ Sign in"), data=f"wl_ok:{token}"),
                  Button.inline(tr(user_id, "Отмена", "Cancel"), data=f"wl_no:{token}")]],
    )


async def callback_web_login(event, user_id, token, approve, user_manager):
    login = user_manager.get_web_login(token)
    in_time = login and time.time() - login["created_at"] <= settings.WEB_LOGIN_TTL + settings.WEB_LOGIN_APPROVED_TTL
    if not in_time or login["user_id"] != user_id or not user_manager.update_web_login(
            token, "opened", status="approved" if approve else "denied"):
        await event.edit(tr(user_id, *WEB_LOGIN_EXPIRED_TEXT))
        return
    logger.info(f"{user_id}: вход на сайт {'подтверждён' if approve else 'отклонён'}.")
    await event.edit(tr(user_id, "✅ Готово — вернитесь в браузер, вход выполнен." if approve
                        else "Вход отменён.",
                        "✅ Done — go back to your browser, you are signed in." if approve
                        else "Sign-in cancelled."))


async def show_devices(vpn_bot, user_id, user_manager):
    user = user_manager.get_user(user_id)
    active, removed, limit = devices_summary(user)
    lines = [tr(user, f"📱 Устройства в подписке: {len(active)} из {limit}.",
                f"📱 Devices on your subscription: {len(active)} of {limit}.")]
    status = extra_devices_status(user)
    if status:
        lines.append(status)
    lines.append("")
    if not active:
        lines.append(tr(user, "Пока ни одно устройство не подключено.", "No devices connected yet."))
    for i, d in enumerate(active, 1):
        lines.append(f"{i}. {d['model'] or '—'} — " + tr(user, f"последний раз {_seen(d)}", f"last seen {_seen(d)}"))
    if removed:
        lines.append("")
        lines.append(tr(user, "Отключены вами (подписка им не выдаётся):", "Removed by you (no subscription for them):"))
        lines += [f"• {d['model'] or '—'}" for d in removed]
    lines.append("")
    lines.append(tr(user, "Убранное устройство освобождает место для нового — новое подключается той же ссылкой.",
                    "Removing a device frees a slot for a new one — connect it with the same link."))
    buttons = [[Button.inline(tr(user, "✖️ Убрать: ", "✖️ Remove: ") + (d["model"] or "—")[:24],
                              data=f"dev_rm:{d['hwid']}")] for d in active]
    buttons += [[Button.inline(tr(user, "↩️ Вернуть: ", "↩️ Restore: ") + (d["model"] or "—")[:24],
                               data=f"dev_back:{d['hwid']}")] for d in removed]
    if user.price:
        n, addon = settings.EXTRA_DEVICES_COUNT, settings.get_extra_devices_price(user.price)
        if user.extra_devices:
            buttons.append([Button.inline(tr(user, "Отключить доп. устройства", "Turn off extra devices"),
                                          data="extra_off")])
        elif device_guard.extra_devices_active(user.extra_devices_until):
            buttons.append([Button.inline(tr(user, "Продлевать доп. устройства", "Keep renewing extra devices"),
                                          data="extra_on")])
        else:
            buttons.append([Button.inline(tr(user, f"➕ Ещё {n} устройства — +{addon} ₽ к подписке",
                                             f"➕ {n} more devices — added to your subscription"),
                                          data="extra_buy")])
    await vpn_bot.send_message(user_id, "\n".join(lines), buttons=buttons)


async def callback_device_remove(vpn_bot, user_id, hwid, user_manager):
    if not device_guard.remove_device(user_id, hwid):
        await vpn_bot.send_message(user_id, tr(user_id, "Это устройство уже убрано.", "This device is already removed."))
    await show_devices(vpn_bot, user_id, user_manager)


async def callback_device_restore(vpn_bot, user_id, hwid, user_manager):
    user = user_manager.get_user(user_id)
    refusal = device_guard.restore_device(vars(user), hwid)
    if refusal == "device_limit":
        await vpn_bot.send_message(user_id, tr(
            user, "Свободных мест нет: сначала уберите другое устройство или подключите доп. устройства.",
            "No free slots: remove another device first or add extra devices."))
    await show_devices(vpn_bot, user_id, user_manager)


def extra_payment_buttons(english=False):
    """Как payment_method_buttons, только для доплаты за доп. устройства."""
    if english:
        rows = [[Button.inline("💳 Card / Apple Pay", data="extra_pay_card")]] if whop.enabled() else []
        return rows + [[Button.inline("🪙 Cryptocurrency (USDT and others)", data="extra_pay_crypto")],
                       [Button.inline("🏦 SBP — Russian bank cards only", data="extra_pay_sbp")]]
    return [[Button.inline("💳 СБП (Мир)", data="extra_pay_sbp")],
            [Button.inline("🪙 Криптовалюта", data="extra_pay_crypto")]]


async def callback_extra_buy(vpn_bot, user_id, user_manager, subscription_manager):
    """«Ещё 3 устройства»: при оплаченном сроке — доплата за остаток (способ оплаты), иначе опция входит в оплату
    подписки (выбор тарифа уже с суммой опции)."""
    user = user_manager.get_user(user_id)
    english = is_english(user)
    quote = subscription_manager.extra_devices_quote(user)
    n = settings.EXTRA_DEVICES_COUNT
    if quote["mode"] == "prorate":
        await vpn_bot.send_message(user_id, tr(
            user,
            f"+{n} устройства подключатся сразу после доплаты за остаток оплаченного срока (до {quote['until']}): "
            f"{quote['rub']} ₽. Дальше — одно продление: {quote['renewal']} ₽ вместо {user.price} ₽. "
            "Отключить можно в любой момент в /devices.\n\nВыберите способ оплаты:",
            f"+{n} devices are added right after an extra charge for the rest of your paid period "
            f"(until {quote['until']}): ${quote['usd']}. Then one renewal for everything. "
            "You can turn this off anytime in /devices.\n\nChoose a payment method:"),
            buttons=extra_payment_buttons(english))
    elif quote["mode"] == "with_subscription":
        subscription_manager.set_extra_devices(user, True)
        await vpn_bot.send_message(user_id, tr(
            user,
            f"+{n} устройства добавлены к подписке и подключатся после её оплаты. Выберите тариф:",
            f"+{n} devices are added to your subscription and turn on after payment. Choose a plan:"),
            buttons=plan_choice_buttons(english=english, extra=True))


async def callback_extra_toggle(vpn_bot, user_id, on: bool, user_manager, subscription_manager):
    subscription_manager.set_extra_devices(user_manager.get_user(user_id), on)
    await show_devices(vpn_bot, user_id, user_manager)


async def callback_extra_pay(vpn_bot, user_id, method, user_manager, subscription_manager):
    await subscription_manager.buy_extra_devices(vpn_bot, user_manager.get_user(user_id), method)


async def callback_choose_payment_method(vpn_bot, user_id):
    english = is_english(utils_user(user_id))
    await vpn_bot.send_message(
        user_id, "Choose a payment method:" if english else "Выберите способ оплаты:",
        buttons=payment_method_buttons(english)
    )


async def callback_pay_sbp(vpn_bot, user_id, user_manager, subscription_manager):
    user = user_manager.get_user(user_id)
    await subscription_manager.add_subscription(vpn_bot, user)


async def callback_pay_card(vpn_bot, user_id, user_manager, subscription_manager):
    user = user_manager.get_user(user_id)
    await subscription_manager.add_card_subscription(vpn_bot, user)


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
        parts = event.message.text.split()
        if len(parts) < 2:
            return None
        payload = parts[-1]
        if payload.startswith(settings.SITE_START_PREFIX):
            logger.info(f'Пришёл с сайта: {payload[len(settings.SITE_START_PREFIX):]}.')
            return settings.OWNER_TELEGRAM_ID
        try:
            referrer_id = int(payload)
        except ValueError:
            logger.info(f'Непонятный параметр /start: {payload[:64]}.')
            return None
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
        if not user.language:
            # вход через виджет на сайте языка не даёт — берём его при первом /start в боте (i18n.is_english)
            lang = getattr(event.sender, "lang_code", None)
            if lang:
                user_manager.update_user(user_id, language=lang)
                user.language = lang

    billing_period_days = settings.get_billing_period_days(user.price)
    english = is_english(user)
    if user.payment_token == None:
        subscription_rows = [
            [Button.inline("💳 Subscribe" if english else "💳 Подключить оплату", data="add_subscription")]
        ]
        subscription_status = ("⚠**not set up** (subscribe)" if english
                               else "⚠**не настроено** (подключите оплату)")
    else:
        subscription_rows = [
            [Button.inline("🔄 Change plan" if english else "🔄 Сменить тариф", data="change_plan")],
            [Button.inline("Cancel auto-renewal" if english else "Отключить оплату", data="cancel_subscription")],
        ]
        # тариф показываем только тут - в общем виде (независимо от того,
        # настроено продление или нет) это поле не нужно
        subscription_status = (f"🌐 **active** ({user.price} RUB / {billing_period_days} days)" if english
                               else f"🌐 **настроено** ({user.price} руб / {billing_period_days} дней)")
    keyboard = [
        [Button.inline("🔗 Set up VPN" if english else "🔗 Настроить VPN", data="generate_settings_link")],
 #       [Button.inline("По QR коду", data="generate_settings_qrcode")],
        *subscription_rows
    ]
    if english:
        subscription_status_and_price_and_referral_program_msg = (
            f"Subscription renewal:\n**{subscription_status}**\n\n"
            "If you have any questions or connection problems, send /help."
        )
    else:
        subscription_status_and_price_and_referral_program_msg = (
            f"Продление подписки:\n**{subscription_status}**\n\n"
            f"Если у вас возникнут вопросы или проблемы с подключением, напишите /help.\n"
            f"Также для восстановления доступа к боту вы можете воспользоваться [пробным доступом на 1 день]({credentials.TRIAL_LINK})."
        )
    if is_user_new:
        if english:
            text = (
                "Glad you're here!\n"
                f"For the next **{settings.trial_days} days** the VPN is free for you.\n\n"
                f"{subscription_status_and_price_and_referral_program_msg}\n\n"
                "Please tap 'Set up VPN':"
            )
        else:
            trial_days_text = (
                f"{settings.trial_days}"
                f"{decline_word(settings.trial_days, 'день')}"
            )
            text = (
                "Рад, что вы ко мне обратились!\n"
                f"Оцените, как здорово пользоваться интернетом вместе со мной!\n"
                f"В течение ближайших **{trial_days_text}** "
                "мои услуги для вас будут бесплатными.\n"
                f"{subscription_status_and_price_and_referral_program_msg}"
                "Пожалуйста, выберите 'Настроить VPN':"
            )
        await event.reply(text, file='hero.jpg', buttons=keyboard, parse_mode='md')

    else:
        #user = user_manager.get_user(user_id)
        days_left = subscription_manager.get_days_left(user)
        if english:
            text = (
                "Welcome back!\n\n"
                f"Subscription status:\n**{user.status.label_en()}**\n"
                f"Valid until:\n"
                f"📆 **{user.next_payment_date}**\n"
                f"⏰ **{days_left}** day(s) left\n\n"
                f"{subscription_status_and_price_and_referral_program_msg}"
            )
        else:
            days_left_text = (
                f"{decline_word(days_left, 'осталось')} "
                f"**{days_left} {decline_word(days_left, 'день')}**"
            )
            text = (
                "С возвращением, друг!\n\n"
                f"Статус вашей подписки:\n**{user.status.label_ru()}**\n"
                f"Подписка действует до:\n"
                f"📆 **{user.next_payment_date}**\n"
                f"⏰ {days_left_text}\n"
                f"{subscription_status_and_price_and_referral_program_msg}"
            )
        await event.reply(text, file='hero.jpg', buttons=keyboard, parse_mode='md')

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
                tr(user,
                   f"Лимит роутера — {settings.ROUTER_TRAFFIC_LIMIT_GB} ГБ за {settings.ROUTER_PERIOD_DAYS} дней — "
                   f"исчерпан. Роутер снова заработает {user.router_window_end():%d.%m}.\n\n"
                   "Телефон или компьютер можно подключить и сейчас: /config → ваше устройство.",
                   f"The router limit — {settings.ROUTER_TRAFFIC_LIMIT_GB} GB per {settings.ROUTER_PERIOD_DAYS} days — "
                   f"is used up. The router will work again on {user.router_window_end():%d.%m}.\n\n"
                   "You can still connect a phone or computer now: /config → your device.")
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
        if is_english(user):
            note = ("\n\n⚠️ You have one link: your previous link for phones and computers no longer works. "
                    "To switch back — /config → your device." if to_router else
                    "\n\n⚠️ You have one link: the router link no longer works. "
                    "To switch back — /config → «Router».")
        else:
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
    english = is_english(utils_user(user_id))

    if device_os == settings.ROUTER_DEVICE and english:
        await vpn_bot.send_message(
            user_id,
            "🛜 **VPN on a router**\n\n"
            "Subscription address for the router:\n"
            f"`{sub_link}`\n\n"
            "Add it to the router's VPN client as a subscription — the firmware must support VLESS Reality.\n\n"
            "All devices on the router's network use the VPN, with no device limit. "
            f"Traffic limit — {settings.ROUTER_TRAFFIC_LIMIT_GB} GB per {settings.ROUTER_PERIOD_DAYS} days."
            f"{note}",
            link_preview=False,
        )
        return

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

    if device_os == "TV" and english:
        await vpn_bot.send_message(
            user_id,
            "📺 **VPN on a TV**\n\n"
            "1. Install Happ on the TV: on Android TV — from Google Play, "
            "on Apple TV — «Happ – Proxy Utility for TV» from the App Store.\n"
            "2. Open Happ on the TV and choose **Web Import** — a code will appear on the screen.\n"
            "3. On your phone open [tv.happ.su](https://tv.happ.su), enter the code and paste the subscription address:\n"
            f"`{sub_link}`\n\n"
            "If Happ with our subscription is already on your phone, it's simpler: "
            "scan the QR code that Happ shows on the TV at first launch with your phone."
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

    if device_os == "iOS" and english:
        # за пределами России Happ в App Store есть — он первый, INCY запасной
        incy = settings.IOS_ALTERNATIVE_CLIENTS[0]["name"]
        await vpn_bot.send_message(
            user_id,
            "📱 **Connecting on iPhone**\n\n"
            f"1. Install Happ: [🔗 Download Happ](https://{credentials.SUBSCRIPTION_DOMAIN}/go/app/iOS?src=bot_en)\n"
            f"2. Tap: [🔗 Add subscription to Happ]({client_link})\n"
            "3. In Happ, pick any server and tap the connect button, then allow adding the VPN.\n\n"
            f"Happ not available in your App Store? Use the free {incy}: "
            f"[download](https://{credentials.SUBSCRIPTION_DOMAIN}/go/app/{incy}?src=bot_en), "
            f"then [add subscription to {incy}]({client_link}?app=incy)."
            f"{note}",
            file='hero.jpg'
        )
        return

    if device_os == "iOS":
        # Happ убран из российского App Store: кто его уже поставил — добавляет подписку в Happ, остальным —
        # бесплатное INCY. Диплинки happ:// и incy:// Telegram в сообщение не пропускает, поэтому обе ссылки
        # ведут на нашу страницу /vpn, а она уже открывает нужное приложение (?app=incy — INCY).
        incy = settings.IOS_ALTERNATIVE_CLIENTS[0]["name"]
        await vpn_bot.send_message(
            user_id,
            "📱 **Подключение на iPhone**\n\n"
            f"**Happ уже установлен?** Нажмите:\n[🔗 Добавить подписку в Happ]({client_link})\n\n"
            f"**Happ нет?** Его убрали из российского App Store — подойдёт бесплатное {incy}:\n"
            f"1. [🔗 Скачать {incy}](https://{credentials.SUBSCRIPTION_DOMAIN}/go/app/{incy}?src=bot)\n"
            f"2. [🔗 Добавить подписку в {incy}]({client_link}?app=incy)\n\n"
            "Потом в приложении нажмите кнопку подключения и разрешите добавить VPN."
            f"{note}",
            file='hero.jpg'
        )
        return

    if english:
        text = (f"1. Install the Happ app:\n[🔗 Download Happ]({app_url})\n\n"
                f"2. Tap the link:\n[🔗 Your personal configuration]({client_link})\n\n"
                "3. In Happ, pick any server and tap the connect button 🔘")
    else:
        text = (f"1. Установите приложение Happ:\n[🔗 Скачать приложение]({app_url})\n\n"
                f"2. Кликните по ссылке:\n[🔗 Ваша личная конфигурация]({client_link})\n\n"
                "3. Выберите в Happ любой из доступных серверов и нажмите на кнопку подключения 🔘")
    await vpn_bot.send_message(user_id, f"{text}{note}", file='hero.jpg')
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
        # только бот: по реф-ссылке друг засчитывается пригласившему, сайт-триал этого не даёт
        tr(user_id, f"Перешлите ссылку другу:\n🤖 {referral_url}", f"Share this link with a friend:\n🤖 {referral_url}"),
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
        [Button.inline(tr(user_id, settings.ROUTER_LABEL, settings.ROUTER_LABEL_EN), data=settings.ROUTER_DEVICE)],
    ]

    await vpn_bot.send_message(
        user_id, tr(user_id, "Выберите вашу операционную систему:", "Choose your device:"), buttons=keyboard
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
