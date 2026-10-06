from telethon import Button
from yookassa import Configuration, Payment

import asyncio
import uuid

import httpx
from datetime import datetime, timedelta, timezone
from time import sleep

import settings
import credentials
import udmpay
import nowpayments
from models import UserStatus
from my_logger import logger
from utils import broadcast_message, days_since, send_email


class SubscriptionManager:
    def __init__(self, user_manager, vpn_manager):
        self.user_manager = user_manager
        self.vpn_manager = vpn_manager
        logger.debug(f'Экземпляр SubscriptionManager создан.')

        # Настройка YooKassa
        Configuration.account_id = credentials.yookassa_id
        Configuration.secret_key = credentials.yookassa_key

    def _extend_next_payment_date(self, user, period_days: int) -> str:
        """Продлеваем от максимума (текущая дата окончания, сегодня) - а не
        всегда от "сегодня". Баг 2026-08-14: если у пользователя ещё
        оставались неиспользованные дни (оплатил заранее/до истечения),
        они просто сгорали - 47 оставшихся дней + оплаченные 30 превращались
        в 30, а должны были в 77."""
        try:
            current = datetime.strptime(user.next_payment_date, "%d.%m.%y")
        except (TypeError, ValueError):
            current = datetime.now()
        base = max(current, datetime.now())
        return (base + timedelta(days=period_days)).strftime("%d.%m.%y")

    def update_subscription_in_db(self, user_id, payment):
        if payment.payment_method.saved:
            payment_token = payment.payment_method.id
        else:
            payment_token = None

        user = self.user_manager.get_user(user_id)
        next_payment_date = self._extend_next_payment_date(
            user, settings.get_billing_period_days(user.price)
        )

        self.user_manager.update_user(
            user_id,
            paid_periods=user.paid_periods + settings.get_periods_increment(user.price),
            next_payment_date=next_payment_date,
            status=UserStatus.ACTIVE,
            payment_token=payment_token
        )

    async def track_payment_status(self, user_id, payment):
        max_attempts = 5
        attempt = 0
        while attempt < max_attempts:
            try:
                payment = Payment.find_one(payment.id)
                if payment.status == 'succeeded':
                    self.update_subscription_in_db(user_id, payment)
                    logger.warning(f'{user_id}: Платеж прошел успешно.')
                    return True
                elif payment.status == 'canceled':
                    logger.warning(f'{user_id}: Платеж был отменен.')
                    return False
                else:
                    await asyncio.sleep(15)
                    attempt += 1
                    logger.info(f"{user_id}: Статус оплаты - {payment.status}")
            except Exception as e:
                logger.error(
                    f"{user_id}: Ошибка при проверке статуса платежа: {e}"
                )
                await asyncio.sleep(15)
                attempt += 1
        return False

    def _order_description(self, price: int) -> str:
        days = settings.get_billing_period_days(price)
        return f"Подписка Horizance на {days} дней"

    async def _create_udmpay_payment(self, vpn_bot, user):
        """Первый платёж через UDM Pay: заказ + привязка подписки СБП одним
        запросом. Оплата асинхронная (СБП QR через банк) — активация
        подписки происходит не здесь, а в activate_paid_subscription(),
        вызываемой вебхуком UDM Pay (см. website/routers/payments.py)."""
        recent = self.user_manager.get_recent_unpaid_order(
            user.user_id, "UDM Pay", user.price, settings.PAYMENT_LINK_REUSE_MINUTES
        )
        if recent:
            # повторное нажатие — та же ссылка, а не новый заказ: иначе
            # человек оплачивает две ссылки (05.10.26, @Samij_Sok)
            await self._send_udmpay_link(vpn_bot, user.user_id, recent["payment_link"])
            logger.info(
                f"{user.user_id}: повторно отдана ссылка неоплаченного заказа "
                f"UDM Pay, orderUid={recent['order_id']}."
            )
            return True

        ext_order_id = f"user-{user.user_id}-{uuid.uuid4().hex[:8]}"
        description = self._order_description(user.price)
        try:
            result = await udmpay.create_subscription_with_payment(
                ext_order_id=ext_order_id, price=user.price, description=description,
                baseline_paid_periods=user.paid_periods,
            )
        except udmpay.UdmPayError as e:
            logger.error(f"{user.user_id}: ошибка создания заказа UDM Pay: {e}")
            if (e.status_code or 0) >= 500:
                # сбой на стороне UDM Pay: 05.10.26 он длился ~10 минут, и
                # «попробуйте еще разок» человек нажимал 10 раз подряд
                text = ("⏳ Платёжная система временно не работает. Деньги не списаны — "
                        "попробуйте через 10–15 минут.")
            else:
                text = "Во время оплаты произошла ошибка. Попробуйте еще разок."
            await vpn_bot.send_message(user.user_id, text)
            return None
        except httpx.TransportError as e:
            # UDM Pay не ответил за 20 с или связь оборвалась (04.10.26 так
            # дважды упала страница /pay/month). Ссылку человек не получил,
            # значит и платить ему было нечем.
            logger.warning(f"{user.user_id}: UDM Pay не ответил при создании заказа: {e!r}")
            await vpn_bot.send_message(
                user.user_id,
                "⏳ Платёжная система сейчас не отвечает. Деньги не списаны — "
                "попробуйте ещё раз через минуту."
            )
            return None

        order_uid = result.get("orderUid")
        payment_link = result.get("paymentLink")
        # subscriptionUid запоминаем сразу: вебхук о привязке счёта приходит
        # почти одновременно с вебхуком об оплате, а тот обнуляет
        # pending_order_uid. Раньше привязку теряли именно на этом - 5 раз
        # из 6 за 22.08-02.09, см. docs/udmpay_support_request.md.
        subscription_uid = result.get("subscriptionUid")
        self.user_manager.update_user(
            user.user_id,
            pending_order_uid=order_uid,
            pending_subscription_uid=subscription_uid,
        )
        self.user_manager.record_payment_order(
            order_uid, user.user_id, "UDM Pay", user.price, payment_link
        )

        await self._send_udmpay_link(vpn_bot, user.user_id, payment_link)
        logger.info(
            f"{user.user_id}: создан заказ UDM Pay, orderUid={order_uid}, "
            f"subscriptionUid={subscription_uid}."
        )
        return True

    async def _send_udmpay_link(self, vpn_bot, user_id, payment_link):
        await vpn_bot.send_message(
            user_id,
            f"Для оплаты подписки перейдите по ссылке: {payment_link}\n\n"
            "После оплаты подписка активируется автоматически "
            "(обычно в течение минуты).\n\n"
            "💡 Банк может предложить разрешить автоплатёж по этой подписке. "
            "Если согласитесь — продлевать вручную больше не нужно, "
            "отменить можно в любой момент в приложении банка или кнопкой "
            "«Отключить оплату» в /start."
        )

    async def _create_crypto_payment(self, vpn_bot, user):
        """Оплата криптовалютой через NOWPayments. Настоящего автосписания
        в крипте нет — это разовый счёт на каждый период, как и раньше с
        ручным переводом, просто с автоматическим подтверждением через
        вебхук вместо ручной проверки. Активация — activate_paid_subscription(),
        вызываемая вебхуком NOWPayments (см. website/routers/payments.py)."""
        ext_order_id = f"user-{user.user_id}-{uuid.uuid4().hex[:8]}"
        description = self._order_description(user.price)
        pay_currency = settings.get_crypto_pay_currency(user.price)
        try:
            result = await nowpayments.create_invoice(
                price_amount=settings.get_crypto_price_usd(user.price),
                order_id=ext_order_id,
                order_description=description,
                ipn_callback_url=settings.WEBSITE_BASE_URL + "/api/payments/nowpayments/webhook",
                pay_currency=pay_currency,
            )
        except nowpayments.NowPaymentsError as e:
            logger.error(f"{user.user_id}: ошибка создания счёта NOWPayments: {e}")
            await vpn_bot.send_message(
                user.user_id,
                "Во время оплаты произошла ошибка. Попробуйте еще разок."
            )
            return None

        invoice_id = str(result.get("id"))
        invoice_url = result.get("invoice_url")
        self.user_manager.update_user(user.user_id, pending_order_uid=invoice_id)
        self.user_manager.record_payment_order(invoice_id, user.user_id, "NOWPayments", user.price)

        if pay_currency:
            payment_note = "Оплата принимается в USDT (сеть BSC / BNB Smart Chain)."
        else:
            # выбор открыт - но не все валюты пройдут по минимуму на такую
            # сумму, явно предупреждаем, чтобы ошибку NOWPayments не приняли
            # за нашу
            payment_note = (
                "На странице можно выбрать любую валюту, **кроме USDT в сети "
                "TRC20/Tron** — у неё минимум выше суммы счёта, платёж не пройдёт."
            )
        await vpn_bot.send_message(
            user.user_id,
            f"Для оплаты перейдите по ссылке: {invoice_url}\n\n"
            f"{payment_note} После оплаты подписка активируется автоматически."
        )
        logger.info(f"{user.user_id}: создан счёт NOWPayments, invoiceId={invoice_id}.")
        return True

    async def add_crypto_subscription(self, vpn_bot, user):
        if user.price == 0:
            # бесплатная подписка (промо/реферальная) - крипта не нужна
            return await self.add_subscription(vpn_bot, user)
        return await self._create_crypto_payment(vpn_bot, user)

    async def _charge_udmpay_subscription(self, user) -> bool:
        """Продление через уже привязанную подписку СБП (payment_token
        хранит subscriptionUid). Возвращает True, если запрос на списание
        принят банком на обработку (или уже был отправлен ранее и мы просто
        ждём вебхук) — False, если запрос отклонён/не удался."""
        if user.pending_order_uid:
            logger.info(
                f"{user.user_id}: продление уже отправлено "
                f"(orderUid={user.pending_order_uid}), ждём вебхук."
            )
            return True

        ext_order_id = f"renew-{user.user_id}-{uuid.uuid4().hex[:8]}"
        description = self._order_description(user.price)
        try:
            result = await udmpay.pay_by_subscription(
                subscription_uid=user.payment_token,
                ext_order_id=ext_order_id,
                price=user.price,
                description=description,
            )
        except udmpay.UdmPayError as e:
            logger.error(f"{user.user_id}: ошибка продления через UDM Pay: {e}")
            return False

        order_uid = result.get("uid")
        self.user_manager.update_user(user.user_id, pending_order_uid=order_uid)
        self.user_manager.record_payment_order(order_uid, user.user_id, "UDM Pay", user.price)
        logger.info(
            f"{user.user_id}: запрос на продление UDM Pay отправлен, "
            f"orderUid={order_uid}."
        )
        return True

    def _notify_admin_of_payment(self, user, order_uid, provider):
        """Ни UDM Pay, ни NOWPayments не присылают уведомлений о получении
        денег продавцу напрямую (деньги оседают у них) - без этого письма
        о поступлении оплаты просто неоткуда узнать."""
        days = settings.get_billing_period_days(user.price)
        plan_label = "год" if user.price == settings.yearly_price else "месяц"
        full_name = " ".join(filter(None, [user.first_name, user.last_name])) or "—"
        if provider == "NOWPayments":
            amount_label = f"{settings.get_crypto_price_usd(user.price)} $"
        else:
            amount_label = f"{user.price} ₽"
        body = (
            f"User ID: {user.user_id}\n"
            f"Username: @{user.username or '—'}\n"
            f"Имя: {full_name}\n"
            f"Сумма: {amount_label}\n"
            f"Тариф: {plan_label} ({days} дней)\n"
            f"Провайдер: {provider}\n"
            f"orderId: {order_uid}\n"
            f"\n{self._signup_to_payment_line(user)}\n"
            f"Всего активных подписок: {self.user_manager.count_active_users()}"
        )
        send_email(
            f"Оплата {provider}: {amount_label} от {user.user_id}",
            body, settings.NOTIFY_EMAIL
        )

    @staticmethod
    def _signup_to_payment_line(user) -> str:
        """Сколько дней прошло от регистрации до оплаты.

        Отвечает на вопрос «зашёл и сразу купил или досидел до конца триала»:
        0-1 день — пришёл готовым, ближе к длине триала — дозрел.
        """
        try:
            days = days_since(user.start_date)
        except (TypeError, ValueError):
            return f"Зарегистрирован: {user.start_date or '—'} (дату не разобрать)"
        if days == 0:
            suffix = "в день регистрации"
        else:
            suffix = f"на {days}-й день после регистрации"
        return f"Зарегистрирован: {user.start_date}, оплата {suffix}"

    async def activate_paid_subscription(self, vpn_bot, user_id, order_uid, provider="UDM Pay"):
        """PaymentSuccessWebhookPayload (UDM Pay) / invoice finished (NOWPayments):
        оплата подтверждена — продлеваем подписку и (пере)выпускаем VPN-доступ."""
        user = self.user_manager.get_user(user_id)
        # Заказ засчитываем, если он последний (pending_order_uid) ИЛИ есть в
        # журнале заказов этого пользователя: оплатить могут и более раннюю
        # ссылку, если «СБП» нажали несколько раз.
        order = self.user_manager.get_payment_order(order_uid)
        known = user is not None and (
            user.pending_order_uid == order_uid
            or (order is not None and order["user_id"] == user.user_id)
        )
        if not known:
            logger.warning(
                f"{user_id}: вебхук оплаты {provider} для устаревшего/"
                f"неизвестного orderId={order_uid}, игнорирую."
            )
            return
        if order is not None and not self.user_manager.mark_payment_order_paid(order_uid):
            logger.warning(f"{user_id}: заказ {order_uid} уже зачтён, повторный вебхук игнорирую.")
            return

        # цена — та, по которой создан заказ: между заказами могли сменить тариф
        price = (order or {}).get("price") or user.price
        next_payment_date = self._extend_next_payment_date(
            user, settings.get_billing_period_days(price)
        )
        # первая оплата: paid_periods только растёт, поэтому бонус пригласившему
        # начисляется ровно один раз без отдельного флага в базе
        first_payment = not user.paid_periods
        updates = dict(
            price=price,
            paid_periods=user.paid_periods + settings.get_periods_increment(price),
            next_payment_date=next_payment_date,
            status=UserStatus.ACTIVE,
            pending_order_uid=None,
        )
        if price == settings.router_price:
            # новый месяц роутерного тарифа: лимит трафика считается заново от этой точки
            updates["traffic_period_start"] = (user.traffic_in or 0) + (user.traffic_out or 0)
        if provider == "NOWPayments":
            # payment_token у крипты - не настоящий токен на автосписание
            # (в крипте его не бывает без предоплаченного баланса у
            # процессора), а метка "чем в прошлый раз платили" - по ней
            # charge_subscription поймёт, что для продления нужно сгенерить
            # новый счёт и прислать ссылку в чат, а не пытаться списать через UDM Pay.
            updates["payment_token"] = "crypto"
        self.user_manager.update_user(user_id, **updates)

        user = self.user_manager.get_user(user_id)
        await self.vpn_manager.setup_xray(user)
        try:
            text = "Ваш платёж успешно завершён, подписка активирована! 🎉"
            if price == settings.router_price:
                text += (f"\n\nСсылка-подписка для роутера (VLESS):\n"
                         f"https://{credentials.SUBSCRIPTION_DOMAIN}/sub/{user.client_uuid}\n"
                         f"Лимит — {settings.ROUTER_TRAFFIC_LIMIT_GB} ГБ в месяц.")
            await vpn_bot.send_message(user_id, text)
        except Exception as e:
            # заблокировал бота: оплата уже зачтена, а письмо владельцу нужно
            # всё равно (30.09.26 без этого письмо о cheet_nickers не ушло)
            logger.error(f"{user_id}: не удалось сообщить об активации: {e}")
        self._notify_admin_of_payment(user, order_uid, provider)
        logger.info(f"{user_id}: подписка активирована через {provider} (orderId={order_uid}).")
        if first_payment and user.referrer_id:
            await self.reward_referrer(vpn_bot, user)

    async def reward_referrer(self, vpn_bot, friend):
        """Реферальная программа: +settings.referral_free_days пригласившему,
        когда приглашённый впервые оплатил подписку. Владелец — пригласивший
        по умолчанию у всех, кто пришёл с сайта (BOT_LINK со start=его id),
        себе бонус не начисляет."""
        referrer = self.user_manager.get_user(friend.referrer_id)
        if referrer is None or referrer.user_id in settings.REFERRAL_EXCLUDED_IDS:
            return
        updates = dict(next_payment_date=self._extend_next_payment_date(
            referrer, settings.referral_free_days))
        if referrer.status == UserStatus.INACTIVE:
            updates["status"] = UserStatus.ACTIVE
        self.user_manager.update_user(referrer.user_id, **updates)
        if referrer.status == UserStatus.INACTIVE:
            await self.vpn_manager.setup_xray(self.user_manager.get_user(referrer.user_id))
        try:
            await vpn_bot.send_message(
                referrer.user_id,
                "🎁 Ваш друг оплатил подписку по вашей рекомендации. Спасибо! "
                f"Дарим вам {settings.referral_free_days} дней бесплатно — "
                f"подписка продлена до {updates['next_payment_date']}."
            )
        except Exception as e:
            logger.warning(f"{referrer.user_id}: не удалось сообщить о реферальном бонусе: {e}")
        logger.info(
            f"{referrer.user_id}: реферальный бонус +{settings.referral_free_days} дн. "
            f"за оплату друга {friend.user_id}, до {updates['next_payment_date']}"
        )

    async def fail_pending_payment(self, vpn_bot, user_id, order_uid, message, provider="UDM Pay"):
        """Оплата отклонена/не прошла.

        payment_token (если был) намеренно не трогаем: этот путь общий для
        первой покупки, смены тарифа и продления, и в случае "уже активная
        подписка, неудачная попытка смены тарифа" отвязывать рабочий
        payment_token из-за отказа НОВОГО заказа было бы неправильно.
        Для продления это означает автоматический повтор на следующем тике
        process_payments (раз в 12 часов) тем же payment_token - пока он
        либо не сработает, либо пользователь не отменит подписку сам."""
        user = self.user_manager.get_user(user_id)
        if user is None or user.pending_order_uid != order_uid:
            logger.warning(
                f"{user_id}: вебхук отказа оплаты {provider} для устаревшего/"
                f"неизвестного orderId={order_uid}, игнорирую."
            )
            return

        self.user_manager.update_user(user_id, pending_order_uid=None)

        await vpn_bot.send_message(
            user_id,
            f"Оплата не прошла: {message}\n\nПопробуйте ещё раз через /pay."
        )
        logger.warning(f"{user_id}: оплата {provider} отклонена (orderId={order_uid}): {message}")

    async def bind_subscription(self, user_id, subscription_uid, order_uid):
        """SubscriptionSuccessWebhookPayload: банк подтвердил привязку счёта
        к подписке СБП — сохраняем subscriptionUid для будущих продлений."""
        user = self.user_manager.get_user(user_id)
        if user is None:
            logger.warning(
                f"{user_id}: вебхук привязки подписки UDM Pay "
                f"(subscriptionUid={subscription_uid}) для неизвестного пользователя."
            )
            return
        self.user_manager.update_user(
            user_id,
            payment_token=subscription_uid,
            pending_subscription_uid=None,
        )
        logger.info(f"{user_id}: подписка СБП привязана, subscriptionUid={subscription_uid}.")

    async def fail_subscription_bind(self, user_id, subscription_uid, message):
        """SubscriptionFailWebhookPayload: банк отклонил привязку счёта.
        payment_token заранее не проставлялся, откатывать нечего — сам
        разовый платёж (если прошёл) обрабатывается отдельным
        payment-вебхуком. Снимаем только метку ожидания, чтобы этот
        subscriptionUid не висел в базе навсегда."""
        self.user_manager.update_user(user_id, pending_subscription_uid=None)
        logger.warning(
            f"{user_id}: привязка подписки СБП отклонена "
            f"(subscriptionUid={subscription_uid}): {message}"
        )

    async def create_payment(self, vpn_bot, user_id):
        user = self.user_manager.get_user(user_id)
        if user.price == 0:
            return 0
        try:
            await vpn_bot.send_message(
                user_id,
                "Наш онлайн эквайринг временно не работает!\n"
                "Для оплаты Вашей подписки просим сделать перевод на карту:\n"
                "**2200 7019 2437 9230** (Тинькофф).\n\n"
                "⚠️ В назначении платежа необходимо указать имя Вашего аккаунта в Телеграмм.\n"
                "Благодарим за понимание!",
                file='hero.jpg',
                parse_mode='md'
            )
        except Exception as e:
            logger.error(f'Ошибка отправки уведомления {user_id} реквизитов: {e}')
        return None
        generic_payment_data = {
            "amount": {
                "value": str(user.price),
                "currency": "RUB"
            },
            "capture": True,
            "description": f"Ежемесячная подписка для пользователя id={user_id}"
        }
        if user.payment_token is None:
            specific_payment_data = {
                "confirmation": {
                    "type": "redirect",
                    "return_url": "https://"
                },
                "save_payment_method": True
            }
        else:
            specific_payment_data = {"payment_method_id": user.payment_token}
            payment_method = user.payment_token
        total_payment_data = {**generic_payment_data, **specific_payment_data}

        try:
            payment = Payment.create(total_payment_data)
            logger.info(f'{user_id}: Счет был выставлен.')
            return payment
        except Exception as e:
            vpn_bot.send_message(
                user.user_id,
                "Во время оплаты произошла ошибка. Попробуйте еще разок."
            )
            logger.error(f'{user_id}: Во время оплаты произошла ошибка: {e}')
            return None

    async def add_subscription(self, vpn_bot, user):
        if settings.UDMPAY_ENABLED and user.price != 0:
            return await self._create_udmpay_payment(vpn_bot, user)

        payment = await self.create_payment(vpn_bot, user.user_id)
        if payment is None:
            return None
        if payment == 0:
            await self.vpn_manager.setup_xray(user)
            self.user_manager.update_user(
                user.user_id, status=UserStatus.FREE
            )
            await vpn_bot.send_message(
                user.user_id,
                "Лично для вас подключение подписки бесплатно. "
                "Спасибо, что пользуетесь нашим сервисом!"
            )
            logger.info(f'{user.user_id}: Бесплатная подписка активирована!')
            return True

        payment_url = payment.confirmation.confirmation_url
        message = await vpn_bot.send_message(
            user.user_id,
            f"Для оплаты подписки перейдите по ссылке: {payment_url}"
        )
        logger.info(
            f'{user.user_id}: Отправлена ссылка на подтверждение оплаты.'
        )
        successful_payment = await self.track_payment_status(
            user.user_id, payment
        )
        if successful_payment == True:
            #if user.paid_periods == 1 and user.referrer_id:
             #   await self.increase_referrals(vpn_bot, user)
            client_uuid = await self.vpn_manager.setup_xray(user)
            await vpn_bot.edit_message(
                user.user_id, message.id,
                "Ваш платеж успешно завершен, подписка активирована!"
            )
            logger.info(
                f'{user.user_id}: Платеж завершен, подписка активирована.'
            )
        elif successful_payment == False:
            await vpn_bot.edit_message(
                user.user_id, message.id,
                "Платеж был отменен."
            )
            logger.warning(f'{user.user_id}: При оплате платеж был отменен.')
        elif successful_payment is None:
            await vpn_bot.edit_message(
                user.user_id, message.id,
                "Во время настройки оплаты произошла ошибка. "
                "Попробуйте еще разок."
            )
            logger.warning(f'{user.user_id}: Не удалось подключить подписку.')

        return successful_payment

    async def cancel_subscription(self, vpn_bot, user_id):
        self.user_manager.update_user(
            user_id,
            status=UserStatus.CANCELED,
            payment_token=None
        )
        await vpn_bot.send_message(user_id, "Ваша подписка была отменена.")
        logger.warning(f'{user_id}: Подписка была отменена пользователем.')

    def get_days_left(self, user):
        next_payment_date = datetime.strptime(
            user.next_payment_date, "%d.%m.%y"
        )
        days_left = (next_payment_date - datetime.now()).days + 1
        # удалить
        logger.debug(next_payment_date - datetime.now())
        logger.debug(
            f'{user.user_id}: Статус:{user.status}, '
            f'рассчет дней до оплаты:{max(0, days_left)}.'
        )
        return max(0, days_left)

    async def _charge_crypto_subscription(self, vpn_bot, user) -> bool:
        """"Автопродление" по крипте - в крипте не бывает тихого списания
        без предоплаченного баланса у процессора (см. память агента), так
        что это не списание, а автоматическая генерация нового счёта
        NOWPayments и отправка ссылки в чат - тот же самый разовый платёж,
        что и при ручной покупке, просто без клика пользователя на этом шаге."""
        if user.pending_order_uid:
            logger.info(
                f"{user.user_id}: крипто-счёт на продление уже отправлен "
                f"(orderUid={user.pending_order_uid}), ждём вебхук."
            )
            return True
        return await self._create_crypto_payment(vpn_bot, user) is not None

    async def charge_subscription(self, vpn_bot, user, deactivate_on_failure=True):
        """deactivate_on_failure=False — только попытка списания.

        Нужно для ранней попытки за 2 дня: там неудача не должна ничего
        отключать, иначе человек с оплаченной подпиской остался бы без VPN
        на два дня раньше срока. Отключаем только в последний день, когда
        запаса времени уже нет.
        """
        if user.payment_token == "crypto":
            if await self._charge_crypto_subscription(vpn_bot, user):
                return None  # новый счёт отправлен, ждём вебхук с результатом
            failed_charge_msg = (
                "⚠️ Не удалось создать счёт на продление подписки.\n\n"
                "Подключите оплату заново через /pay."
            )
        elif user.payment_token:
            if settings.UDMPAY_ENABLED:
                if await self._charge_udmpay_subscription(user):
                    return None  # запрос принят, ждём вебхук с результатом
                failed_charge_msg = (
                    "⚠️ При попытке продления подписки оплата не прошла.\n\n"
                    "Подключите оплату заново через /pay."
                )
            else:
                payment = await self.create_payment(vpn_bot, user.user_id)
                successful_payment = await self.track_payment_status(
                    user.user_id, payment
                )
                if successful_payment:
                    return True
                logger.warning(f"{user.user_id}: Не удалось списать средства.")
                failed_charge_msg = (
                    "⚠️ При попытке продления подписки оплата не прошла.\n\n"
                    "Подключите оплату заново через /pay."
                )
#       elif user.status == UserStatus.ACTIVE:
#           logger.warning(
#                f"У пользователя {user.user_id} статус active, "
#                "но платежный токен не сохранился.")
        elif user.status == UserStatus.TRIAL or user.status == UserStatus.FREE:
            failed_charge_msg = (
                "Ваш бесплатный ознакомительный период закончился. "
                "Надеемся, вам все понравилось 🤗\n\n"
                "Ваш VPN отключён, чтобы подключиться снова, "
                "оплатите подписку через /pay или воспользуйтесь "
                f"[пробным доступом на 1 день]({credentials.TRIAL_LINK})."
            )
        elif user.status == UserStatus.CANCELED or user.status == UserStatus.ACTIVE:
            failed_charge_msg = (
                "Оплаченный вами период закончился. "
                "Надеемся, вы к нам еще вернетесь 🤗\n\n"
                "Ваш VPN отключён, чтобы подключиться снова, "
                "оплатите подписку через /pay или воспользуйтесь "
                f"[пробным доступом на 1 день]({credentials.TRIAL_LINK})."
            )
        if not deactivate_on_failure:
            logger.warning(
                f"{user.user_id}: ранняя попытка продления не удалась, "
                "ничего не отключаем — повторим в последний день."
            )
            return False

        keyboard = [
            [Button.inline("Подключить оплату", data="add_subscription")]
        ]
        await broadcast_message(
            vpn_bot, self.user_manager, [user.user_id], failed_charge_msg,
            "подписка закончилась", buttons=keyboard
        )
        if user.payment_token:
            self.user_manager.update_user(user.user_id, payment_token=None)
            return False

        self.user_manager.update_user(user.user_id, status=UserStatus.INACTIVE)
        self._notify_admin_of_churn(user.user_id)
        await asyncio.sleep(10)
        await self.vpn_manager.remove_client_from_xray(user.user_id)
        return False

    def _notify_admin_of_churn(self, user_id) -> None:
        """Письмо о том, что подписчик отвалился.

        Про приход денег письмо есть, про уход - не было, и отвал замечался
        только вручную по счётчику активных. Шлём всю строку из базы, а не
        выжимку: заранее не угадать, какое поле понадобится при разборе
        (трафик, даты, устройство, откуда пришёл).
        """
        user = self.user_manager.get_user(user_id)
        if user is None:
            return
        fields = vars(user)
        traffic_in = fields.get('traffic_in') or 0
        traffic_out = fields.get('traffic_out') or 0
        try:
            lifetime = f"{days_since(user.start_date)} дн."
        except (TypeError, ValueError):
            lifetime = "—"
        summary = (
            f"Отвалился подписчик: {user_id} (@{user.username or '—'})\n"
            f"Прожил с нами: {lifetime} (с {user.start_date})\n"
            f"Оплачено месяцев: {user.paid_periods}\n"
            f"Трафик: {traffic_in} МБ вход / {traffic_out} МБ выход\n"
            f"Осталось активных подписок: {self.user_manager.count_active_users()}\n"
            f"\n--- вся запись из базы ---\n"
            + "\n".join(f"{k}: {v}" for k, v in fields.items())
        )
        send_email(
            f"Отвал: {user_id} (@{user.username or '—'})",
            summary, settings.NOTIFY_EMAIL
        )

    RENEWAL_ATTEMPT_DAYS_LEFT = 2

    async def process_payments(self, vpn_bot):
        """Цикл продлений, раз в 12 часов.

        Расписание по дням до конца подписки:
          2 — пробуем списать по сохранённой привязке. Раньше пытались в
              последний день, и если оплата не проходила, запаса на вторую
              попытку уже не оставалось;
          1 — напоминаем вручную продлить, но ТОЛЬКО тем, у кого списание не
              удалось или привязки нет. У кого продление прошло, дата уже
              уехала вперёд, и до этой ветки они не доходят; у кого платёж в
              процессе (pending_order_uid) - молчим, чтобы не дёргать
              человека, пока деньги идут;
          0 — отключаем.

        Ветка «напомнить» намеренно не знает, через какого провайдера идёт
        оплата: при переезде на другой эквайринг меняется только
        charge_subscription.
        """
        users = self.user_manager.load_users()
        remind_ids = []
        for item in users:
            user_id = item['user_id']
            user = self.user_manager.get_user(user_id)
            if user.status in (UserStatus.INACTIVE, UserStatus.FREE):
                continue
            days_left = self.get_days_left(user)

            if days_left == self.RENEWAL_ATTEMPT_DAYS_LEFT and user.payment_token:
                await self.charge_subscription(
                    vpn_bot, user, deactivate_on_failure=False
                )
            elif days_left == 1 and not user.pending_order_uid:
                remind_ids.append(user_id)
            elif days_left == 0:
                await self.charge_subscription(vpn_bot, user)

        if remind_ids:
            await broadcast_message(
                vpn_bot, self.user_manager, remind_ids,
                "⚠️ **Ваша подписка истекает завтра**\n\n"
                "Чтобы не потерять доступ, продлите её: отправьте команду "
                "**/pay** и выберите тариф. Занимает меньше минуты.",
                "подписка истекает завтра"
            )
        logger.info('process_payments успешно завершена')


    async def get_expired_trial_clients(self) -> list[dict]:
        expired = self.user_manager.get_expired_trial_sessions()
        # По ВСЕМ серверам, а не по локальному. Раньше здесь стоял
        # get_vpn_clients(), который спрашивает только 127.0.0.1, и просроченный
        # триал, уже снятый с локальной ноды, в этот список не попадал — то есть
        # на остальных пяти нодах он оставался жить и продолжал пользоваться VPN
        # бесплатно. Это же было причиной расхождения числа клиентов между
        # нодами (58 на локальной против 67-71 на прочих на 06.09.2026).
        client_ids = await self.vpn_manager.get_all_vpn_clients()
        expired_trial_clients = []
        for session in expired:
            if session["user_id"] in client_ids:
                expired_trial_clients.append(session)
        if expired_trial_clients:
            logger.info(
                "Просроченные триал клиенты: "
                + ", ".join(c["user_id"] for c in expired_trial_clients)
            )
        else:
            logger.info("Просроченных trial-сессий не найдено.")
        return expired_trial_clients

    async def delete_expired_trial_clients(self) -> None:
        expired_trial_clients = await self.get_expired_trial_clients()
        for client in expired_trial_clients:
            xray_email = client["user_id"]
            try:
                await self.vpn_manager.remove_client_from_xray(xray_email)
                #self.user_manager.delete_trial_session(xray_email)
                #logger.info(f"Trial удалён: {xray_email}")
            except Exception as e:
                logger.error(f"Ошибка удаления trial {xray_email}: {e}")


#    def _should_charge(self, user_id):
#        user = user_manager.get_user(user_id)
#        last_payment_date = datetime.strptime(user.last_payment_date, '%d.%m.%y')
#        next_payment_date = last_payment_date + timedelta(days=30)
#        return subscription.status == 'active' and next_payment_date <= datetime.now()
