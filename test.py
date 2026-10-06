"""
Интеграционные тесты Horizance VPN bot.

Тестируется реальная цепочка main.py -> bot.py -> db.py / subscription.py /
vpn.py поверх временной SQLite базы (test_users.db) и тестового xray-конфига
(test_config.json). Границы, за которые реально выходить нельзя в тестах,
подменены моками:
  - реальное подключение к Telegram (TelegramClient.start/connect) никогда
    не вызывается — обработчики из main.py вызываются напрямую с "поддельным"
    event, который отдаёт только те атрибуты, которые код реально читает;
  - VPNManager: SSH/xray вызовы (setup_xray, remove_client_from_xray,
    get_vpn_clients) подменены на AsyncMock, чтобы не ходить по SSH на боевые
    сервера из credentials.SERVERS;
  - YooKassa (yookassa.Payment.create/find_one) подменена на моки;
  - email_support (SMTP) подменён на AsyncMock.

Каждый класс покрывает один пользовательский сценарий из списка команд
BotFather и, где это применимо, сравнивает поведение для нового и уже
знакомого системе пользователя.
"""
import os
import tempfile
# до импорта проекта: my_logger настраивает файлы при импорте. Без этого
# тесты писали в боевые логи my_logger/ (user 333001, нода «DE 10.0.0.1»…)
os.environ.setdefault("HORIZANCE_LOG_DIR", tempfile.mkdtemp(prefix="horizance_test_logs_"))
import asyncio
import unittest
from uuid import uuid4
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import credentials
import db
import main
import nowpayments
import settings
import subscription
import udmpay
from models import UserStatus

TEST_DB_NAME = 'test_users.db'
TEST_XRAY_CONFIG = 'test_config.json'


# --------------------------------------------------------------------------
# Поддельный telethon event: реализует только те атрибуты, которые реально
# читаются в main.py/bot.py (event.sender.id / event.sender_id,
# event.message.message / .text, event.data, event.reply, event.answer,
# event.client.get_entity). Полноценного диспетчера telethon мы не поднимаем,
# поэтому обработчики (main.start, main.pay, ...) вызываются напрямую.
# --------------------------------------------------------------------------

class FakeMessage:
    def __init__(self, text, media=None):
        self.message = text
        self.text = text
        self.media = media


class FakeSender:
    def __init__(self, user_id):
        self.id = user_id


class FakeTelegramUser:
    def __init__(self, user_id, username, first_name, last_name=None,
                 phone=None, lang_code='ru'):
        self.id = user_id
        self.username = username
        self.first_name = first_name
        self.last_name = last_name
        self.phone = phone
        self.lang_code = lang_code


class FakeEvent:
    def __init__(self, user_id, text=None, data=None, telegram_user=None):
        self.sender_id = user_id
        self.sender = FakeSender(user_id)
        self.message = FakeMessage(text) if text is not None else None
        self.data = data
        self.reply = AsyncMock()
        self.answer = AsyncMock()
        self.forward_to = AsyncMock()
        self.client = MagicMock()
        self.client.get_entity = AsyncMock(
            return_value=telegram_user or FakeTelegramUser(
                user_id, f'user{user_id}', 'Имя', 'Фамилия', '+70000000000'
            )
        )


def message_event(user_id, text, telegram_user=None):
    return FakeEvent(user_id, text=text, telegram_user=telegram_user)


def callback_event(user_id, data, telegram_user=None):
    if isinstance(data, str):
        data = data.encode()
    return FakeEvent(user_id, data=data, telegram_user=telegram_user)


# --------------------------------------------------------------------------
# udmpay.sign() - чистая функция, без сети и БД
# --------------------------------------------------------------------------

class TestUdmPaySigning(unittest.TestCase):

    def test_sign_sorts_keys_and_excludes_arrays_hash_and_none(self):
        import hashlib
        with patch.object(credentials, 'UDMPAY_PASSWORD', 'secret'):
            computed = udmpay.sign({
                'b': 'two', 'a': 'one', 'hash': 'ignored',
                'products': [1, 2], 'c': None,
            })
        expected = hashlib.sha256('onetwosecret'.encode()).hexdigest()
        self.assertEqual(computed, expected)

    def test_sign_formats_sum_with_two_decimals(self):
        import hashlib
        with patch.object(credentials, 'UDMPAY_PASSWORD', 'secret'):
            computed = udmpay.sign({'sum': 99})
        expected = hashlib.sha256('99.00secret'.encode()).hexdigest()
        self.assertEqual(computed, expected)


class TestUdmPayStatusUnwrapping(unittest.IsolatedAsyncioTestCase):
    """Регрессия на реальный баг 2026-08-14: /order/status и
    /sbp-subscription/status оборачивают ответ в {"data": {...}}, в отличие
    от create-with-payment/pay, где поля лежат на верхнем уровне. Вебхук
    читал status/paymentStatus с верхнего уровня, получал None и решал,
    что платёж "ещё не финализирован" - хотя он реально прошёл, вебхук
    дошёл, просто сообщение читалось не из того места."""

    async def test_get_order_status_unwraps_data_field(self):
        wrapped = {'data': {'orderId': 'x', 'status': 'success', 'paymentStatus': 'success'}}
        with patch.object(udmpay, '_post', AsyncMock(return_value=wrapped)):
            result = await udmpay.get_order_status('x')
        self.assertEqual(result.get('status'), 'success')

    async def test_get_subscription_status_unwraps_data_field(self):
        wrapped = {'data': {'subscriptionUid': 'x', 'status': 'active', 'isActive': True}}
        with patch.object(udmpay, '_post', AsyncMock(return_value=wrapped)):
            result = await udmpay.get_subscription_status('x')
        self.assertEqual(result.get('status'), 'active')
        self.assertTrue(result.get('isActive'))


# --------------------------------------------------------------------------
# nowpayments.verify_webhook_signature() - чистая функция, по официальному
# примеру из их доков (HMAC-SHA512 от json.dumps с sort_keys=True)
# --------------------------------------------------------------------------

class TestNowPaymentsSigning(unittest.TestCase):

    def test_verify_webhook_signature_matches_official_example(self):
        import hashlib
        import hmac
        import json
        payload = {"payment_id": 123, "payment_status": "finished", "fee": {"btc": 0.1}}
        with patch.object(credentials, 'NOWPAYMENTS_IPN_SECRET', 'secret'):
            sorted_msg = json.dumps(payload, separators=(',', ':'), sort_keys=True)
            expected_sig = hmac.new(
                b'secret', sorted_msg.encode(), hashlib.sha512
            ).hexdigest()
            self.assertTrue(nowpayments.verify_webhook_signature(payload, expected_sig))
            self.assertFalse(nowpayments.verify_webhook_signature(payload, 'wrong-signature'))


# --------------------------------------------------------------------------
# Базовый класс: реальные UserManager/VPNManager/SubscriptionManager поверх
# тестовой БД, с изоляцией всех внешних сетевых вызовов.
# --------------------------------------------------------------------------

class BotIntegrationTestCase(unittest.IsolatedAsyncioTestCase):

    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_NAME):
            os.remove(TEST_DB_NAME)

        cls._db_name_patcher = patch.object(db.UserManager, 'DB_NAME', TEST_DB_NAME)
        cls._xray_path_patcher = patch.object(settings, 'XRAY_CONFIG_PATH', TEST_XRAY_CONFIG)
        cls._db_name_patcher.start()
        cls._xray_path_patcher.start()

        main.initialize_bot_classes()

    @classmethod
    def tearDownClass(cls):
        cls._db_name_patcher.stop()
        cls._xray_path_patcher.stop()
        if os.path.exists(TEST_DB_NAME):
            os.remove(TEST_DB_NAME)

    def setUp(self):
        main.user_manager.execute_query('DELETE FROM users')
        main.user_manager.execute_query('DELETE FROM trial_sessions')
        main.waiting_for_question.clear()

        # SSH/xray никогда не должны реально вызываться в тестах
        # мок повторяет поведение настоящего setup_xray: переиспользует уже
        # выданный uuid, а если его нет — генерирует новый
        main.vpn_manager.setup_xray = AsyncMock(
            side_effect=lambda user: user.client_uuid or str(uuid4())
        )
        main.vpn_manager.remove_client_from_xray = AsyncMock()
        main.vpn_manager.get_vpn_clients = AsyncMock(return_value=[])

        # main.vpn_bot - настоящий telethon TelegramClient, но .start()/.connect()
        # тут никогда не вызывается, поэтому безопасно подменить методы отправки
        main.vpn_bot.send_message = AsyncMock()
        main.vpn_bot.edit_message = AsyncMock()
        main.vpn_bot.get_me = AsyncMock(return_value=MagicMock(username='horizance_vpn_bot'))

        payment_create_patcher = patch.object(subscription.Payment, 'create')
        payment_find_patcher = patch.object(subscription.Payment, 'find_one')
        self.mock_payment_create = payment_create_patcher.start()
        self.mock_payment_find = payment_find_patcher.start()
        self.addCleanup(payment_create_patcher.stop)
        self.addCleanup(payment_find_patcher.stop)

        email_patcher = patch.object(main.utils, 'email_support', new=AsyncMock())
        self.mock_email_support = email_patcher.start()
        self.addCleanup(email_patcher.stop)

        # admin-уведомление об оплате UDM Pay - реального SMTP в тестах быть не должно
        send_email_patcher = patch.object(subscription, 'send_email')
        self.mock_send_email = send_email_patcher.start()
        self.addCleanup(send_email_patcher.stop)

    def create_user(self, user_id, **overrides):
        defaults = dict(
            username=f'user{user_id}',
            first_name='Имя',
            last_name='Фамилия',
            phone='+70000000000',
            language='ru',
            referrer_id=None,
        )
        defaults.update(overrides)
        return main.user_manager.add_user(user_id=user_id, **defaults)

    def get_user(self, user_id):
        return main.user_manager.get_user(user_id)

    def future_date(self, days):
        return (datetime.now() + timedelta(days=days)).strftime("%d.%m.%y")

    def past_date(self, days):
        return (datetime.now() - timedelta(days=days)).strftime("%d.%m.%y")


# --------------------------------------------------------------------------
# /start - "Посмотреть подписку"
# --------------------------------------------------------------------------

class TestStartCommand(BotIntegrationTestCase):

    async def test_new_user_without_referral_is_created_as_trial(self):
        event = message_event(111111, '/start')
        await main.start(event)

        user = self.get_user(111111)
        self.assertIsNotNone(user)
        self.assertEqual(user.status, UserStatus.TRIAL)
        self.assertIsNone(user.referrer_id)

        event.reply.assert_awaited_once()
        _, kwargs = event.reply.call_args
        self.assertIn('Рад, что вы ко мне обратились', event.reply.call_args.args[0])
        self.assertEqual(kwargs.get('file'), 'hero.jpg')

    async def test_new_user_via_referral_link_increments_referrer(self):
        referrer = self.create_user(222222, referrals=0)

        event = message_event(333333, f'/start {referrer.user_id}')
        await main.start(event)

        new_user = self.get_user(333333)
        self.assertEqual(new_user.referrer_id, referrer.user_id)

        updated_referrer = self.get_user(referrer.user_id)
        self.assertEqual(updated_referrer.referrals, 1)

    async def test_existing_user_sees_subscription_status_not_recreated(self):
        self.create_user(
            444444,
            status=UserStatus.ACTIVE,
            next_payment_date=self.future_date(10),
        )

        event = message_event(444444, '/start')
        await main.start(event)

        # пользователь не должен быть создан повторно
        rows = main.user_manager.execute_query(
            'SELECT COUNT(*) as cnt FROM users WHERE user_id = ?',
            (444444,), fetchone=True
        )
        self.assertEqual(rows['cnt'], 1)

        reply_text = event.reply.call_args.args[0]
        self.assertIn('С возвращением', reply_text)
        self.assertIn('Активен', reply_text)


# --------------------------------------------------------------------------
# /config и кнопка "🔗 Настроить VPN" - настройка VPN
# --------------------------------------------------------------------------

class TestConfigFlow(BotIntegrationTestCase):

    async def test_new_user_without_device_os_is_asked_to_choose_os(self):
        self.create_user(555555, device_os=None)

        event = message_event(555555, '/config')
        await main.config(event)

        main.vpn_bot.send_message.assert_awaited_once()
        args, kwargs = main.vpn_bot.send_message.call_args
        self.assertIn('операционную систему', args[1])
        buttons = kwargs['buttons']
        total_buttons = sum(len(row) for row in buttons)
        self.assertEqual(total_buttons, len(settings.VPN_CLIENT_DOWNLOAD_URLS))
        main.vpn_manager.setup_xray.assert_not_awaited()

    async def test_device_selection_provisions_vpn_and_sends_link(self):
        self.create_user(666666, device_os=None)
        await main.config(message_event(666666, '/config'))
        main.vpn_bot.send_message.reset_mock()

        cb_event = callback_event(666666, 'iOS')
        await main.cb_device_selected(cb_event)

        user = self.get_user(666666)
        self.assertEqual(user.device_os, 'iOS')
        self.assertIsNotNone(user.client_uuid)
        main.vpn_manager.setup_xray.assert_awaited_once()

        main.vpn_bot.send_message.assert_awaited_once()
        args, kwargs = main.vpn_bot.send_message.call_args
        sent_text = args[1]
        self.assertIn(user.client_uuid, sent_text)
        self.assertIn(settings.VPN_CLIENT_DOWNLOAD_URLS['iOS'], sent_text)

    async def test_tv_selection_sends_tv_instructions_with_sub_link(self):
        self.create_user(666667, device_os=None)
        await main.config(message_event(666667, '/config'))
        main.vpn_bot.send_message.reset_mock()

        await main.cb_device_selected(callback_event(666667, 'TV'))

        user = self.get_user(666667)
        self.assertEqual(user.device_os, 'TV')
        main.vpn_bot.send_message.assert_awaited_once()
        sent_text = main.vpn_bot.send_message.call_args.args[1]
        self.assertIn('tv.happ.su', sent_text)
        self.assertIn(f"/sub/{user.client_uuid}", sent_text)

    async def test_repeated_config_keeps_the_same_client_uuid(self):
        """Регрессия на баг двойной выдачи uuid (25.08.26).

        Раньше uuid генерировался внутри VPNManager.setup_xray по проверке
        `if not user.client_uuid`. Если пользователь быстро нажимал выдачу
        ссылки несколько раз, все вызовы успевали прочитать None и делали
        РАЗНЫЕ uuid: в xray оставался первый (`xray api adu` не перезаписывает
        существующий email и выходит с кодом 0), а в БД записывался последний.
        Ссылка у клиента и то, что примет сервер, расходились — VPN молча
        не работал. Пострадало 7 живых пользователей.
        """
        self.create_user(666777, device_os=None)

        seen = set()
        for _ in range(5):
            await main.config(message_event(666777, '/config'))
            await main.cb_device_selected(callback_event(666777, 'iOS'))
            seen.add(self.get_user(666777).client_uuid)

        self.assertEqual(len(seen), 1, f'uuid менялся между вызовами: {seen}')
        self.assertIsNotNone(seen.pop())

    async def test_concurrent_config_presses_create_only_one_uuid(self):
        """Регрессия на баг от 25.08.26: параллельные нажатия кнопки выдачи
        ссылки не должны порождать несколько uuid.

        Кнопку легко нажать несколько раз подряд, и Telethon запустит
        обработчик столько же раз параллельно. Раньше все запуски успевали
        прочитать client_uuid=None до первой записи и создавали РАЗНЫЕ uuid:
        `xray api adu` не перезаписывает существующий email, поэтому в xray
        оставался первый, а в БД записывался последний. Пострадало 7 живых
        пользователей. Последовательный вызов баг не ловит — нужен именно
        параллельный.
        """
        self.create_user(666778, device_os=None)

        issued = []

        async def slow_setup_xray(user):
            client_uuid = user.client_uuid or str(uuid4())
            issued.append(client_uuid)
            await asyncio.sleep(0.05)  # разъезд по серверам не мгновенный
            return client_uuid

        main.vpn_manager.setup_xray = AsyncMock(side_effect=slow_setup_xray)

        await main.config(message_event(666778, '/config'))
        await asyncio.gather(*[
            main.cb_device_selected(callback_event(666778, 'iOS'))
            for _ in range(5)
        ])

        self.assertEqual(
            len(set(issued)), 1,
            f'на 5 нажатий создано несколько uuid: {set(issued)}'
        )
        self.assertEqual(self.get_user(666778).client_uuid, issued[0])

    async def test_returning_user_with_saved_os_is_still_asked_to_choose_os(self):
        """Текущее поведение: /config всегда заново показывает выбор ОС,
        даже если device_os уже сохранён с прошлого раза — авто-переустановки
        по одному /config нет, нужен повторный выбор ОС через кнопки."""
        self.create_user(
            777777, device_os='Android', status=UserStatus.ACTIVE,
            next_payment_date=self.future_date(5),
        )

        event = message_event(777777, '/config')
        await main.config(event)

        main.vpn_manager.setup_xray.assert_not_awaited()
        main.vpn_bot.send_message.assert_awaited_once()
        args, kwargs = main.vpn_bot.send_message.call_args
        self.assertIn('операционную систему', args[1])

    async def test_inactive_user_is_rejected_and_not_provisioned(self):
        self.create_user(888888, status=UserStatus.INACTIVE, device_os=None)

        event = message_event(888888, '/config')
        await main.config(event)

        main.vpn_manager.setup_xray.assert_not_awaited()
        main.vpn_bot.send_message.assert_awaited_once()
        sent_text = main.vpn_bot.send_message.call_args.args[1]
        self.assertIn('требуется подключить оплату', sent_text)

    async def test_generate_settings_link_button_behaves_like_config(self):
        """Кнопка "generate_settings_link" вызывает тот же start_vpn_setup,
        что и /config: переустановки VPN сразу не происходит, пользователю
        снова показывается выбор ОС."""
        self.create_user(
            999901, device_os='Windows', status=UserStatus.ACTIVE,
            next_payment_date=self.future_date(5),
        )

        cb_event = callback_event(999901, 'generate_settings_link')
        await main.generate_settings_link(cb_event)

        main.vpn_manager.setup_xray.assert_not_awaited()
        main.vpn_bot.send_message.assert_awaited_once()
        args, kwargs = main.vpn_bot.send_message.call_args
        self.assertIn('операционную систему', args[1])


# --------------------------------------------------------------------------
# /pay и кнопки "💳 Подключить оплату" / "Отключить оплату"
# --------------------------------------------------------------------------

class TestSubscriptionFlow(BotIntegrationTestCase):

    async def test_add_subscription_with_price_shows_plan_choice(self):
        """У платного пользователя кнопка "Подключить оплату" сначала
        предлагает выбрать тариф (месяц/год), а не сразу выставляет счёт."""
        self.create_user(111001, price=99, status=UserStatus.TRIAL)

        event = callback_event(111001, 'add_subscription')
        await main.add_subscription(event)

        main.vpn_bot.send_message.assert_awaited_once()
        args, kwargs = main.vpn_bot.send_message.call_args
        self.assertIn('Выберите тариф', args[1])
        buttons = kwargs['buttons']
        self.assertEqual(sum(len(row) for row in buttons), 2)
        self.mock_payment_create.assert_not_called()

        user = self.get_user(111001)
        self.assertEqual(user.status, UserStatus.TRIAL)
        self.assertIsNone(user.payment_token)

    async def test_selecting_monthly_plan_sends_manual_bank_transfer(self):
        """Ручной перевод - fallback-путь на случай settings.UDMPAY_ENABLED=False
        (сейчас включено по умолчанию, поэтому патчим явно): после выбора
        тарифа create_payment отправляет реквизиты для ручного перевода и
        не трогает YooKassa/БД."""
        udmpay_disabled_patcher = patch.object(settings, 'UDMPAY_ENABLED', False)
        udmpay_disabled_patcher.start()
        self.addCleanup(udmpay_disabled_patcher.stop)
        nowpayments_disabled_patcher = patch.object(settings, 'NOWPAYMENTS_ENABLED', False)
        nowpayments_disabled_patcher.start()
        self.addCleanup(nowpayments_disabled_patcher.stop)

        self.create_user(111011, price=0, status=UserStatus.TRIAL)

        event = callback_event(111011, 'select_plan_month')
        await main.select_plan_month(event)

        self.assertEqual(self.get_user(111011).price, settings.default_price)
        main.vpn_bot.send_message.assert_awaited_once()
        sent_text = main.vpn_bot.send_message.call_args.args[1]
        self.assertIn('2200 7019 2437 9230', sent_text)
        self.mock_payment_create.assert_not_called()

        user = self.get_user(111011)
        self.assertEqual(user.status, UserStatus.TRIAL)
        self.assertIsNone(user.payment_token)

    async def test_selecting_yearly_plan_sets_yearly_price(self):
        self.create_user(111012, price=0, status=UserStatus.TRIAL)

        event = callback_event(111012, 'select_plan_year')
        await main.select_plan_year(event)

        self.assertEqual(self.get_user(111012).price, settings.yearly_price)

    async def test_change_plan_excludes_current_tariff(self):
        """Кнопка "Сменить тариф" не должна предлагать уже действующий
        тариф — только альтернативный."""
        self.create_user(
            111013, price=settings.default_price, payment_token='sub-existing',
            status=UserStatus.ACTIVE,
        )

        event = callback_event(111013, 'change_plan')
        await main.change_plan(event)

        main.vpn_bot.send_message.assert_awaited_once()
        args, kwargs = main.vpn_bot.send_message.call_args
        self.assertIn('Выберите новый тариф', args[1])
        buttons = kwargs['buttons']
        self.assertEqual(sum(len(row) for row in buttons), 1)

    async def test_add_subscription_with_zero_price_auto_activates(self):
        """Регрессионный тест на баг: `if not payment: return None` перед
        `if payment == 0:` делал бесплатную ветку недостижимой, т.к.
        `not 0` тоже True."""
        self.create_user(111002, price=0, status=UserStatus.TRIAL)

        event = callback_event(111002, 'add_subscription')
        await main.add_subscription(event)

        main.vpn_manager.setup_xray.assert_awaited_once()
        provisioned_user = main.vpn_manager.setup_xray.call_args.args[0]
        self.assertEqual(provisioned_user.user_id, 111002)
        self.assertEqual(self.get_user(111002).status, UserStatus.FREE)

        main.vpn_bot.send_message.assert_awaited_once()
        self.assertIn('бесплатно', main.vpn_bot.send_message.call_args.args[1])

    async def test_successful_online_payment_provisions_vpn(self):
        """Регрессионный тест на баг: SubscriptionManager.add_subscription
        вызывал vpn_manager.setup_xray(vpn_bot, user, user_manager), хотя
        реальная сигнатура - setup_xray(user). Обходим текущий ручной
        сценарий оплаты через create_payment/track_payment_status, чтобы
        проверить именно ветку "оплата прошла успешно". Ручной
        сценарий - fallback-путь на случай settings.UDMPAY_ENABLED=False
        (сейчас включено по умолчанию, поэтому патчим явно)."""
        udmpay_disabled_patcher = patch.object(settings, 'UDMPAY_ENABLED', False)
        udmpay_disabled_patcher.start()
        self.addCleanup(udmpay_disabled_patcher.stop)
        nowpayments_disabled_patcher = patch.object(settings, 'NOWPAYMENTS_ENABLED', False)
        nowpayments_disabled_patcher.start()
        self.addCleanup(nowpayments_disabled_patcher.stop)

        self.create_user(111003, price=0, status=UserStatus.TRIAL)

        fake_payment = MagicMock()
        main.subscription_manager.create_payment = AsyncMock(return_value=fake_payment)
        main.subscription_manager.track_payment_status = AsyncMock(return_value=True)

        event = callback_event(111003, 'select_plan_month')
        await main.select_plan_month(event)

        main.vpn_manager.setup_xray.assert_awaited_once()
        provisioned_user = main.vpn_manager.setup_xray.call_args.args[0]
        self.assertEqual(provisioned_user.user_id, 111003)
        main.vpn_bot.edit_message.assert_awaited_once()
        self.assertIn('успешно', main.vpn_bot.edit_message.call_args.args[2])

    async def test_cancel_subscription_deactivates_payment(self):
        self.create_user(
            111004, status=UserStatus.ACTIVE, payment_token='saved-token',
            next_payment_date=self.future_date(5),
        )

        event = callback_event(111004, 'cancel_subscription')
        await main.cancel_subscription(event)

        user = self.get_user(111004)
        self.assertEqual(user.status, UserStatus.CANCELED)
        self.assertIsNone(user.payment_token)
        main.vpn_bot.send_message.assert_awaited_once()
        self.assertIn('отменена', main.vpn_bot.send_message.call_args.args[1])

    async def test_pay_command_routes_to_same_flow_as_button(self):
        # /pay и кнопка «Подключить оплату» — это один обработчик
        # main.add_subscription с двумя подписками на события (см. main.py),
        # поэтому проверяем его обоими типами событий.
        self.create_user(111005, price=0, status=UserStatus.TRIAL)
        await main.add_subscription(message_event(111005, '/pay'))
        self.assertEqual(self.get_user(111005).status, UserStatus.FREE)

        self.create_user(111006, price=0, status=UserStatus.TRIAL)
        await main.add_subscription(callback_event(111006, 'add_subscription'))
        self.assertEqual(self.get_user(111006).status, UserStatus.FREE)


# --------------------------------------------------------------------------
# UDM Pay: settings.UDMPAY_ENABLED=True (по умолчанию выключено, см.
# TestSubscriptionFlow выше для веток с ручным переводом). Сеть замокана
# на уровне udmpay.create_subscription_with_payment/pay_by_subscription -
# сам udmpay.sign()/_post() покрыт TestUdmPaySigning отдельно.
# --------------------------------------------------------------------------

class TestUdmPayPaymentFlow(BotIntegrationTestCase):

    def setUp(self):
        super().setUp()
        udmpay_enabled_patcher = patch.object(settings, 'UDMPAY_ENABLED', True)
        udmpay_enabled_patcher.start()
        self.addCleanup(udmpay_enabled_patcher.stop)

    async def test_purchase_creates_order_and_sends_payment_link(self):
        self.create_user(777001, price=99, status=UserStatus.TRIAL)
        fake_result = {
            'orderUid': 'order-abc',
            'subscriptionUid': 'sub-abc',
            'paymentLink': 'https://pay.udmpay.ru/p/order-abc',
        }
        with patch.object(
            udmpay, 'create_subscription_with_payment',
            AsyncMock(return_value=fake_result)
        ):
            user = self.get_user(777001)
            await main.subscription_manager.add_subscription(main.vpn_bot, user)

        main.vpn_bot.send_message.assert_awaited_once()
        sent_text = main.vpn_bot.send_message.call_args.args[1]
        self.assertIn('https://pay.udmpay.ru/p/order-abc', sent_text)

        updated_user = self.get_user(777001)
        self.assertEqual(updated_user.pending_order_uid, 'order-abc')
        # подписка ещё не активна - активация приходит отдельным вебхуком
        self.assertEqual(updated_user.status, UserStatus.TRIAL)

    async def test_repeat_press_reuses_unpaid_link_within_window(self):
        # 05.10.26: 4 ссылки за 8 минут, оплачены две
        self.create_user(777010, price=99, status=UserStatus.TRIAL)
        fake = AsyncMock(side_effect=[
            {'orderUid': 'o-1', 'subscriptionUid': 's-1', 'paymentLink': 'https://pay/o-1'},
            {'orderUid': 'o-2', 'subscriptionUid': 's-2', 'paymentLink': 'https://pay/o-2'},
        ])
        with patch.object(udmpay, 'create_subscription_with_payment', fake):
            await main.subscription_manager.add_subscription(main.vpn_bot, self.get_user(777010))
            await main.subscription_manager.add_subscription(main.vpn_bot, self.get_user(777010))
            self.assertEqual(fake.await_count, 1)
            self.assertIn('https://pay/o-1', main.vpn_bot.send_message.call_args.args[1])

            # ссылка старше окна — уже новый заказ
            main.user_manager.execute_query(
                "UPDATE payment_orders SET created_at = datetime('now', '-16 minutes') WHERE order_id = 'o-1'"
            )
            await main.subscription_manager.add_subscription(main.vpn_bot, self.get_user(777010))
            self.assertEqual(fake.await_count, 2)
            self.assertIn('https://pay/o-2', main.vpn_bot.send_message.call_args.args[1])

    async def test_udmpay_server_error_says_system_is_down(self):
        self.create_user(777011, price=99, status=UserStatus.TRIAL)
        with patch.object(
            udmpay, 'create_subscription_with_payment',
            AsyncMock(side_effect=udmpay.UdmPayError('Server Error', 500))
        ):
            await main.subscription_manager.add_subscription(main.vpn_bot, self.get_user(777011))
        self.assertIn('временно не работает', main.vpn_bot.send_message.call_args.args[1])

    async def test_udmpay_timeout_tells_user_to_retry(self):
        # 04.10.26 UDM Pay не ответил за 20 с, и /pay/month падал с 500
        import httpx
        self.create_user(777009, price=99, status=UserStatus.TRIAL)
        with patch.object(
            udmpay, 'create_subscription_with_payment',
            AsyncMock(side_effect=httpx.ReadTimeout('timeout'))
        ):
            result = await main.subscription_manager.add_subscription(
                main.vpn_bot, self.get_user(777009)
            )

        self.assertIsNone(result)
        sent_text = main.vpn_bot.send_message.call_args.args[1]
        self.assertIn('не отвечает', sent_text)
        self.assertIsNone(self.get_user(777009).pending_order_uid)

    async def test_activate_paid_subscription_extends_and_provisions(self):
        self.create_user(
            777002, price=settings.yearly_price, status=UserStatus.TRIAL,
            pending_order_uid='order-xyz', next_payment_date=self.future_date(0),
        )
        await main.subscription_manager.activate_paid_subscription(
            main.vpn_bot, 777002, 'order-xyz'
        )

        user = self.get_user(777002)
        self.assertEqual(user.status, UserStatus.ACTIVE)
        self.assertIsNone(user.pending_order_uid)
        self.assertEqual(
            user.next_payment_date,
            self.future_date(settings.get_billing_period_days(settings.yearly_price))
        )
        # годовая оплата - paid_periods растёт на 12, а не на 1
        self.assertEqual(user.paid_periods, 12)
        main.vpn_manager.setup_xray.assert_awaited_once()
        main.vpn_bot.send_message.assert_awaited_once()
        self.assertIn('активирована', main.vpn_bot.send_message.call_args.args[1])

        # админ получает письмо о поступлении оплаты - UDM Pay сам не уведомляет
        self.mock_send_email.assert_called_once()
        subject, body, to = self.mock_send_email.call_args.args
        self.assertIn(str(settings.yearly_price), subject)
        self.assertIn('User ID: 777002', body)
        self.assertIn('год', body)
        self.assertEqual(to, settings.NOTIFY_EMAIL)

    async def test_first_payment_of_friend_rewards_referrer_once(self):
        self.create_user(777101, price=99, status=UserStatus.ACTIVE,
                         next_payment_date=self.future_date(10))
        self.create_user(777102, price=99, status=UserStatus.TRIAL, referrer_id=777101,
                         pending_order_uid='order-f1', next_payment_date=self.future_date(0))
        await main.subscription_manager.activate_paid_subscription(main.vpn_bot, 777102, 'order-f1')
        self.assertEqual(self.get_user(777101).next_payment_date,
                         self.future_date(10 + settings.referral_free_days))

        # вторая оплата того же друга бонуса уже не даёт
        main.user_manager.update_user(777102, pending_order_uid='order-f2')
        await main.subscription_manager.activate_paid_subscription(main.vpn_bot, 777102, 'order-f2')
        self.assertEqual(self.get_user(777101).next_payment_date,
                         self.future_date(10 + settings.referral_free_days))

    async def test_owner_gets_no_referral_bonus(self):
        owner = next(iter(settings.REFERRAL_EXCLUDED_IDS))
        self.create_user(owner, price=99, status=UserStatus.ACTIVE, next_payment_date=self.future_date(5))
        self.create_user(777103, price=99, status=UserStatus.TRIAL, referrer_id=owner,
                         pending_order_uid='order-o1', next_payment_date=self.future_date(0))
        await main.subscription_manager.activate_paid_subscription(main.vpn_bot, 777103, 'order-o1')
        self.assertEqual(self.get_user(owner).next_payment_date, self.future_date(5))

    async def test_activate_extends_from_remaining_days_not_from_today(self):
        """Регрессия на реальный баг 2026-08-14: у пользователя оставалось
        47 дней подписки, он оплатил ещё 30 - должно было стать 77 дней
        (47 + 30), а не 30 (старый код всегда считал от "сегодня", теряя
        уже оплаченные дни)."""
        self.create_user(
            777009, price=settings.default_price, status=UserStatus.ACTIVE,
            pending_order_uid='order-early-renew', next_payment_date=self.future_date(47),
        )
        await main.subscription_manager.activate_paid_subscription(
            main.vpn_bot, 777009, 'order-early-renew'
        )
        self.assertEqual(self.get_user(777009).next_payment_date, self.future_date(77))

    async def test_activate_monthly_increments_paid_periods_by_one(self):
        self.create_user(
            777008, price=settings.default_price, status=UserStatus.TRIAL,
            pending_order_uid='order-month',
        )
        await main.subscription_manager.activate_paid_subscription(
            main.vpn_bot, 777008, 'order-month'
        )
        self.assertEqual(self.get_user(777008).paid_periods, 1)

    async def test_payment_of_earlier_order_is_credited_once(self):
        """30.09.26: «СБП» нажали 4 раза, оплатили ПЕРВУЮ ссылку — вебхук не
        находил владельца, т.к. pending_order_uid хранил только последний заказ."""
        self.create_user(777201, price=99, status=UserStatus.TRIAL, next_payment_date=self.future_date(0))
        for n in (1, 2, 3):
            fake = {'orderUid': f'multi-{n}', 'subscriptionUid': f's{n}', 'paymentLink': 'https://x'}
            with patch.object(udmpay, 'create_subscription_with_payment', AsyncMock(return_value=fake)):
                await main.subscription_manager.add_subscription(main.vpn_bot, self.get_user(777201))
            # нажатия с интервалом больше окна повторной выдачи ссылки
            main.user_manager.execute_query(
                "UPDATE payment_orders SET created_at = datetime('now', '-20 minutes') WHERE user_id = 777201"
            )
        self.assertEqual(self.get_user(777201).pending_order_uid, 'multi-3')
        self.assertEqual(main.user_manager.get_user_by_order('multi-1').user_id, 777201)

        await main.subscription_manager.activate_paid_subscription(main.vpn_bot, 777201, 'multi-1')
        user = self.get_user(777201)
        self.assertEqual(user.status, UserStatus.ACTIVE)
        self.assertEqual(user.paid_periods, 1)
        self.assertEqual(user.next_payment_date, self.future_date(30))

        # повторный вебхук того же заказа — второго зачисления нет
        await main.subscription_manager.activate_paid_subscription(main.vpn_bot, 777201, 'multi-1')
        self.assertEqual(self.get_user(777201).paid_periods, 1)

    async def test_owner_is_emailed_even_if_user_blocked_bot(self):
        self.create_user(777301, price=99, status=UserStatus.TRIAL,
                         pending_order_uid='order-blocked', next_payment_date=self.future_date(0))
        main.vpn_bot.send_message.side_effect = Exception("User is blocked")
        try:
            await main.subscription_manager.activate_paid_subscription(main.vpn_bot, 777301, 'order-blocked')
        finally:
            main.vpn_bot.send_message.side_effect = None
        self.assertEqual(self.get_user(777301).status, UserStatus.ACTIVE)
        self.mock_send_email.assert_called_once()

    async def test_activate_ignores_stale_or_unknown_order_uid(self):
        self.create_user(777003, status=UserStatus.TRIAL, pending_order_uid='real-order')

        await main.subscription_manager.activate_paid_subscription(
            main.vpn_bot, 777003, 'different-order'
        )

        user = self.get_user(777003)
        self.assertEqual(user.status, UserStatus.TRIAL)
        main.vpn_manager.setup_xray.assert_not_awaited()

    async def test_fail_pending_payment_clears_pending_but_keeps_token(self):
        """payment_token намеренно НЕ трогаем при отказе: этот путь общий
        для первой покупки/смены тарифа/продления, и в случае "уже активная
        подписка, неудачная смена тарифа" отвязывать рабочий payment_token
        было бы неправильно (см. комментарий в fail_pending_payment)."""
        self.create_user(
            777004, status=UserStatus.CANCELED, payment_token='sub-token',
            pending_order_uid='order-fail',
        )

        await main.subscription_manager.fail_pending_payment(
            main.vpn_bot, 777004, 'order-fail', 'insufficient_funds'
        )

        user = self.get_user(777004)
        self.assertIsNone(user.pending_order_uid)
        self.assertEqual(user.payment_token, 'sub-token')
        main.vpn_bot.send_message.assert_awaited_once()
        self.assertIn('Оплата не прошла', main.vpn_bot.send_message.call_args.args[1])

    async def test_recurring_charge_skips_when_already_pending(self):
        """Защита от повторного списания: если предыдущий запрос на
        продление ещё не подтверждён вебхуком (pending_order_uid уже
        занят), новый запрос к UDM Pay не отправляется."""
        user = self.create_user(
            777005, status=UserStatus.ACTIVE, payment_token='sub-token',
            pending_order_uid='already-in-flight',
        )

        with patch.object(udmpay, 'pay_by_subscription', AsyncMock()) as mock_pay:
            result = await main.subscription_manager._charge_udmpay_subscription(user)

        mock_pay.assert_not_called()
        self.assertTrue(result)

    async def test_bind_subscription_saves_token(self):
        self.create_user(777006, status=UserStatus.TRIAL)

        await main.subscription_manager.bind_subscription(
            777006, 'sub-new', 'order-new'
        )

        self.assertEqual(self.get_user(777006).payment_token, 'sub-new')


# --------------------------------------------------------------------------
# NOWPayments (крипто): settings.NOWPAYMENTS_ENABLED=True (по умолчанию
# выключено - без этого флага выбор способа оплаты вообще не показывается,
# см. bot.callback_select_plan). Сеть замокана на уровне
# nowpayments.create_invoice - сам nowpayments.verify_webhook_signature
# покрыт TestNowPaymentsSigning отдельно.
# --------------------------------------------------------------------------

class TestNowPaymentsFlow(BotIntegrationTestCase):

    def setUp(self):
        super().setUp()
        nowpayments_enabled_patcher = patch.object(settings, 'NOWPAYMENTS_ENABLED', True)
        nowpayments_enabled_patcher.start()
        self.addCleanup(nowpayments_enabled_patcher.stop)

    async def test_selecting_plan_shows_payment_method_choice(self):
        self.create_user(888001, price=0, status=UserStatus.TRIAL)

        event = callback_event(888001, 'select_plan_month')
        await main.select_plan_month(event)

        self.assertEqual(self.get_user(888001).price, settings.default_price)
        main.vpn_bot.send_message.assert_awaited_once()
        args, kwargs = main.vpn_bot.send_message.call_args
        self.assertIn('способ оплаты', args[1])
        buttons = kwargs['buttons']
        self.assertEqual(sum(len(row) for row in buttons), 2)

    async def test_pay_sbp_routes_to_udmpay_flow(self):
        self.create_user(888002, price=settings.default_price, status=UserStatus.TRIAL)

        fake_result = {'orderUid': 'order-sbp', 'subscriptionUid': 'sub-sbp', 'paymentLink': 'https://pay.example/sbp'}
        with patch.object(udmpay, 'create_subscription_with_payment', AsyncMock(return_value=fake_result)):
            event = callback_event(888002, 'pay_method_sbp')
            await main.pay_method_sbp(event)

        self.assertEqual(self.get_user(888002).pending_order_uid, 'order-sbp')

    async def test_pay_crypto_creates_invoice_and_sends_link(self):
        self.create_user(888003, price=settings.yearly_price, status=UserStatus.TRIAL)

        fake_result = {'id': '4522625843', 'invoice_url': 'https://nowpayments.io/payment/?iid=4522625843'}
        with patch.object(nowpayments, 'create_invoice', AsyncMock(return_value=fake_result)):
            event = callback_event(888003, 'pay_method_crypto')
            await main.pay_method_crypto(event)

        self.assertEqual(self.get_user(888003).pending_order_uid, '4522625843')
        main.vpn_bot.send_message.assert_awaited_once()
        sent_text = main.vpn_bot.send_message.call_args.args[1]
        self.assertIn('https://nowpayments.io/payment/?iid=4522625843', sent_text)

    async def test_crypto_monthly_forces_currency_yearly_leaves_open(self):
        """Месячная крипто-оплата форсит одну сеть (иначе клиент может
        наткнуться на "less than minimal" на ровном месте - см. историю
        реального теста 2026-08-14); годовая сумма выше минимумов всех
        проверенных валют кроме USDT-TRC20, поэтому выбор оставлен открытым."""
        self.create_user(888009, price=settings.default_price, status=UserStatus.TRIAL)
        self.create_user(888010, price=settings.yearly_price, status=UserStatus.TRIAL)
        fake_result = {'id': 'x', 'invoice_url': 'https://nowpayments.io/payment/?iid=x'}

        with patch.object(nowpayments, 'create_invoice', AsyncMock(return_value=fake_result)) as mock_create:
            await main.subscription_manager.add_crypto_subscription(
                main.vpn_bot, main.user_manager.get_user(888009)
            )
            monthly_kwargs = mock_create.call_args.kwargs

            await main.subscription_manager.add_crypto_subscription(
                main.vpn_bot, main.user_manager.get_user(888010)
            )
            yearly_kwargs = mock_create.call_args.kwargs

        self.assertEqual(monthly_kwargs['pay_currency'], settings.get_crypto_pay_currency(settings.default_price))
        self.assertIsNone(yearly_kwargs['pay_currency'])

    async def test_activate_crypto_subscription_sends_usd_email(self):
        self.create_user(
            888004, price=settings.default_price, status=UserStatus.TRIAL,
            pending_order_uid='4522625843',
        )

        await main.subscription_manager.activate_paid_subscription(
            main.vpn_bot, 888004, '4522625843', provider='NOWPayments'
        )

        user = self.get_user(888004)
        self.assertEqual(user.status, UserStatus.ACTIVE)
        self.mock_send_email.assert_called_once()
        subject, body, to = self.mock_send_email.call_args.args
        self.assertIn('NOWPayments', subject)
        self.assertIn(f'{settings.get_crypto_price_usd(settings.default_price)} $', body)
        self.assertNotIn('₽', body)

    async def test_activate_crypto_sets_payment_token_sentinel(self):
        """payment_token="crypto" - не настоящий токен на автосписание,
        а метка "чем в прошлый раз платили", по которой charge_subscription
        решает, генерить ли новый крипто-счёт при продлении."""
        self.create_user(
            888005, price=settings.default_price, status=UserStatus.TRIAL,
            pending_order_uid='order-crypto-token',
        )
        await main.subscription_manager.activate_paid_subscription(
            main.vpn_bot, 888005, 'order-crypto-token', provider='NOWPayments'
        )
        self.assertEqual(self.get_user(888005).payment_token, 'crypto')

    async def test_charge_subscription_crypto_creates_new_invoice(self):
        user = self.create_user(
            888006, price=settings.default_price, status=UserStatus.ACTIVE,
            payment_token='crypto', next_payment_date=self.future_date(0),
        )
        fake_result = {'id': '999888777', 'invoice_url': 'https://nowpayments.io/payment/?iid=999888777'}
        with patch.object(nowpayments, 'create_invoice', AsyncMock(return_value=fake_result)):
            result = await main.subscription_manager.charge_subscription(main.vpn_bot, user)

        self.assertIsNone(result)  # ждём вебхук, не финальный исход
        updated = self.get_user(888006)
        self.assertEqual(updated.pending_order_uid, '999888777')
        self.assertEqual(updated.payment_token, 'crypto')  # не сброшен

    async def test_charge_subscription_crypto_skips_when_already_pending(self):
        user = self.create_user(
            888007, price=settings.default_price, status=UserStatus.ACTIVE,
            payment_token='crypto', pending_order_uid='already-sent',
        )
        with patch.object(nowpayments, 'create_invoice', AsyncMock()) as mock_create:
            await main.subscription_manager.charge_subscription(main.vpn_bot, user)
        mock_create.assert_not_called()


# --------------------------------------------------------------------------
# /invite_friend и /qr_code
# --------------------------------------------------------------------------

class TestReferralAndQrCode(BotIntegrationTestCase):

    async def test_invite_friend_sends_referral_and_trial_links(self):
        import credentials

        self.create_user(222001)
        event = message_event(222001, '/invite_friend')
        await main.invite_friend(event)

        main.vpn_bot.get_me.assert_awaited_once()
        main.vpn_bot.send_message.assert_awaited_once()
        reply_text = main.vpn_bot.send_message.call_args.args[1]
        self.assertIn('t.me/horizance_vpn_bot?start=222001', reply_text)
        self.assertIn(credentials.TRIAL_LINK, reply_text)

    async def test_qr_code_command_sends_and_cleans_up_file(self):
        self.create_user(222002)
        event = message_event(222002, '/qr_code')
        await main.bot_qrcode(event)

        event.reply.assert_awaited_once()
        qr_path = event.reply.call_args.kwargs['file']
        self.assertFalse(os.path.exists(qr_path))


# --------------------------------------------------------------------------
# /help + свободный текст (пересылка вопроса на почту) и прочие сообщения
# --------------------------------------------------------------------------

class TestHelpAndMessages(BotIntegrationTestCase):

    async def test_help_then_free_text_is_emailed_to_support(self):
        self.create_user(333001)

        help_event = message_event(333001, '/help')
        await main.help(help_event)
        self.assertIn(333001, main.waiting_for_question)
        self.assertIn('вопрос', help_event.reply.call_args.args[0])

        question_event = message_event(333001, 'У меня не подключается VPN')
        await main.handle_message(question_event)

        self.mock_email_support.assert_awaited_once()
        sent_user, sent_question = self.mock_email_support.call_args.args
        self.assertEqual(sent_user.user_id, 333001)
        self.assertEqual(sent_question, 'У меня не подключается VPN')

        self.assertNotIn(333001, main.waiting_for_question)
        self.assertIn('Спасибо за обращение', question_event.reply.call_args.args[0])

    async def test_plain_message_is_emailed_and_acknowledged(self):
        # раньше обычное сообщение только логировалось: человек не получал
        # ответа, а владелец не знал о сообщении
        self.create_user(333002)

        event = message_event(333002, 'Привет, как дела?')
        await main.handle_message(event)

        main.vpn_manager.get_vpn_clients.assert_awaited()
        self.mock_email_support.assert_awaited_once()
        self.assertIn('Привет, как дела?', self.mock_email_support.call_args.args[1])
        self.assertIn('Спасибо за обращение', event.reply.call_args.args[0])

    async def test_screenshot_is_forwarded_to_owner(self):
        # почта уносит только текст — скриншот раньше просто терялся
        self.create_user(333005)
        event = message_event(333005, '')
        event.message.media = object()
        await main.handle_message(event)
        event.forward_to.assert_awaited_once_with(settings.OWNER_TELEGRAM_ID)
        self.mock_email_support.assert_awaited_once()
        self.assertIn('вложение', self.mock_email_support.call_args.args[1])
        self.assertIn('Спасибо за обращение', event.reply.call_args.args[0])

    async def test_promo_keyword_extends_trial_and_reprovisions_vpn(self):
        self.create_user(
            333003, status=UserStatus.INACTIVE,
            next_payment_date=self.past_date(1),
        )

        event = message_event(333003, 'Живой')
        await main.handle_message(event)

        user = self.get_user(333003)
        self.assertEqual(user.status, UserStatus.TRIAL)
        self.assertIsNotNone(user.client_uuid)
        # от сегодняшнего дня, а не от старой даты окончания
        self.assertEqual(user.next_payment_date, self.future_date(settings.PROMO_DAYS))

        main.vpn_manager.setup_xray.assert_awaited_once()
        event.reply.assert_awaited_once()
        self.assertIn('промо-доступ', event.reply.call_args.args[0])
        self.mock_email_support.assert_not_awaited()

    async def test_promo_only_once_per_user(self):
        self.create_user(333004, status=UserStatus.INACTIVE, next_payment_date=self.past_date(400))
        await main.handle_message(message_event(333004, 'живой'))
        self.assertEqual(self.get_user(333004).status, UserStatus.TRIAL)
        # истёк снова — второй раз промо не срабатывает
        main.user_manager.update_user(333004, status=UserStatus.INACTIVE)
        event = message_event(333004, 'Живой')
        await main.handle_message(event)
        self.assertEqual(self.get_user(333004).status, UserStatus.INACTIVE)
        self.assertIn('не можете', event.reply.call_args.args[0])

    async def test_command_like_messages_are_ignored_by_generic_handler(self):
        self.create_user(333004)

        event = message_event(333004, '/unknown_command')
        await main.handle_message(event)

        main.vpn_manager.get_vpn_clients.assert_not_awaited()
        self.mock_email_support.assert_not_awaited()


# --------------------------------------------------------------------------
# Таймер (utils.start_timer): рассылки на протяжении жизни пользователя -
# один пользователь последовательно проходит через все 4 сценария рассылок:
# не установил Happ (день 1) -> установил, но не включил VPN (день 3) ->
# подписка истекает завтра -> подписка закончилась.
# --------------------------------------------------------------------------

class TestNotificationLifecycle(BotIntegrationTestCase):

    async def test_user_lifecycle_notification_pipeline(self):
        user_id = 555001
        self.create_user(
            user_id,
            status=UserStatus.TRIAL,
            start_date=self.past_date(1),
            next_payment_date=self.future_date(10),
            traffic_in=None,
            traffic_out=None,
        )

        # День 1: трафика ещё нет вовсе -> "не установлен Happ"
        await main.utils.run_notification_campaigns(
            main.vpn_bot, main.user_manager, settings.NOTIFICATION_CAMPAIGNS
        )
        main.vpn_bot.send_message.assert_awaited_once()
        self.assertIn('/help', main.vpn_bot.send_message.call_args.args[1])

        # Повторный обход в тот же день не должен дублировать сообщение
        main.vpn_bot.send_message.reset_mock()
        await main.utils.run_notification_campaigns(
            main.vpn_bot, main.user_manager, settings.NOTIFICATION_CAMPAIGNS
        )
        main.vpn_bot.send_message.assert_not_awaited()

        # День 3: приложение установлено, но сам VPN не включен (трафик 0)
        main.user_manager.update_user(
            user_id, start_date=self.past_date(3), traffic_in=0, traffic_out=0
        )
        main.vpn_bot.send_message.reset_mock()
        await main.utils.run_notification_campaigns(
            main.vpn_bot, main.user_manager, settings.NOTIFICATION_CAMPAIGNS
        )
        main.vpn_bot.send_message.assert_awaited_once()
        self.assertIn('включ', main.vpn_bot.send_message.call_args.args[1].lower())

        # Подписка истекает завтра
        main.user_manager.update_user(
            user_id, status=UserStatus.ACTIVE, next_payment_date=self.future_date(1)
        )
        main.vpn_bot.send_message.reset_mock()
        await main.subscription_manager.process_payments(main.vpn_bot)
        main.vpn_bot.send_message.assert_awaited_once()
        self.assertIn(
            'истекает завтра', main.vpn_bot.send_message.call_args.args[1]
        )
        self.assertEqual(self.get_user(user_id).status, UserStatus.ACTIVE)

        # Подписка закончилась: платёжного токена нет -> деактивация и снятие VPN
        main.user_manager.update_user(user_id, next_payment_date=self.future_date(0))
        main.vpn_bot.send_message.reset_mock()
        with patch('subscription.asyncio.sleep', new=AsyncMock()):
            await main.subscription_manager.process_payments(main.vpn_bot)

        main.vpn_bot.send_message.assert_awaited_once()
        self.assertIn(
            'период закончился', main.vpn_bot.send_message.call_args.args[1]
        )
        user = self.get_user(user_id)
        self.assertEqual(user.status, UserStatus.INACTIVE)
        main.vpn_manager.remove_client_from_xray.assert_awaited_once_with(user_id)


# --------------------------------------------------------------------------
# safe_handler: обработчики не должны падать наружу при ошибках
# --------------------------------------------------------------------------

class TestErrorResilience(BotIntegrationTestCase):

    async def test_pay_for_unknown_user_reports_generic_error(self):
        event = message_event(444001, '/pay')  # пользователь не создан

        await main.add_subscription(event)  # не должно бросить исключение наружу

        event.answer.assert_awaited_once()
        self.assertIn('пошло не так', event.answer.call_args.args[0])



# --------------------------------------------------------------------------
# device_guard: лимиты на триал и устройства (после аварии 22.09.26)
# --------------------------------------------------------------------------

import device_guard

GUARD_DB = 'test_guard.db'


class TestDeviceGuard(unittest.TestCase):

    def setUp(self):
        if os.path.exists(GUARD_DB):
            os.remove(GUARD_DB)
        patcher = patch.object(device_guard, 'DB_PATH', GUARD_DB)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: os.path.exists(GUARD_DB) and os.remove(GUARD_DB))
        db.UserManager(db_name=GUARD_DB).create_trial_table()
        device_guard.init_tables()

    def add_trial(self, user_id, ip, days_ago=0, hwid=None, used=True, minutes_ago=0):
        created = (datetime.utcnow() - timedelta(days=days_ago, minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M+00:00")
        with device_guard._connect() as conn:
            conn.execute(
                "INSERT INTO trial_sessions (user_id, fingerprint, client_uuid, client_ip, "
                "created_at, expires_at, traffic_out) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, f"SM-A266B|{hwid}" if hwid else None, str(uuid4()),
                 f"{ip}|('Bavaria', 'Nuremberg')", created, created, 5 if used else None)
            )

    def test_ip_limit_counts_last_30_days_from_db(self):
        for i in range(settings.TRIAL_IP_LIMIT):
            self.add_trial(f"t_{i}", "5.75.195.183")
        self.assertEqual(device_guard.trial_refusal("5.75.195.183"), "ip_limit")
        self.assertIsNone(device_guard.trial_refusal("8.8.4.4"))

    def test_unused_trials_do_not_count(self):
        # 23.09.26: выдача висела, люди жали кнопку трижды и получали 3 пустых триала
        for i in range(settings.TRIAL_IP_LIMIT):
            self.add_trial(f"t_{i}", "176.15.175.0", used=False)
        self.assertIsNone(device_guard.trial_refusal("176.15.175.0"))

    def test_repeat_click_reuses_fresh_trial(self):
        self.add_trial("t_fresh", "46.19.85.148", used=False, minutes_ago=2)
        self.assertIsNotNone(device_guard.recent_trial_uuid("46.19.85.148"))
        self.assertIsNone(device_guard.recent_trial_uuid("8.8.8.8"))

    def test_old_trial_is_not_reused(self):
        self.add_trial("t_old", "46.19.85.148", used=False, minutes_ago=30)
        self.assertIsNone(device_guard.recent_trial_uuid("46.19.85.148"))

    def test_old_trials_do_not_count(self):
        for i in range(settings.TRIAL_IP_LIMIT):
            self.add_trial(f"t_{i}", "5.75.195.183", days_ago=settings.TRIAL_WINDOW_DAYS + 2)
        self.assertIsNone(device_guard.trial_refusal("5.75.195.183"))

    def test_our_node_ip_is_refused(self):
        node_ip = credentials.SERVERS[0]["host"]
        self.assertEqual(device_guard.trial_refusal(node_ip), "node_ip")

    def test_banned_ip_is_refused(self):
        device_guard.ban("ip", "1.2.3.4", "test")
        self.assertEqual(device_guard.trial_refusal("1.2.3.4"), "banned")

    def test_fourth_device_gets_refused_known_ones_pass(self):
        user = {"user_id": 555}
        for i in range(settings.DEVICE_LIMIT):
            self.assertIsNone(device_guard.device_refusal(user, f"hw{i}", "Phone"))
        self.assertEqual(device_guard.device_refusal(user, "hw_new", "Phone"), "device_limit")
        # уже привязанное устройство не выбивается
        self.assertIsNone(device_guard.device_refusal(user, "hw0", "Phone"))

    def test_second_trial_from_same_phone_is_refused(self):
        # сценарий атаки 22.09: три триала с одного hwid за 9 минут
        self.add_trial("t_first", "5.75.195.183", hwid="3b5338f1")
        self.add_trial("t_second", "5.75.195.183")
        self.assertEqual(
            device_guard.device_refusal({"user_id": "t_second"}, "3b5338f1", "SM-A266B"),
            "trial_reuse",
        )

    def test_owner_phone_is_exempt(self):
        owner_hwid = next(iter(settings.DEVICE_LIMIT_EXEMPT_HWIDS))
        self.add_trial("t_first", "1.1.1.1", hwid=owner_hwid)
        self.add_trial("t_second", "1.1.1.1")
        self.assertIsNone(device_guard.device_refusal({"user_id": "t_second"}, owner_hwid, "iPhone"))

    def test_banned_hwid_is_refused(self):
        device_guard.ban("hwid", "badhw", "test")
        self.assertEqual(device_guard.device_refusal({"user_id": 1}, "badhw", "X"), "banned")



# --------------------------------------------------------------------------
# node_monitor: письма о падении нод и возврат потерянных клиентов
# --------------------------------------------------------------------------

import node_monitor


class TestNodeMonitor(unittest.IsolatedAsyncioTestCase):

    SERVER = {"name": "DE", "host": "10.0.0.1", "username": "u"}

    def setUp(self):
        self.vpn = MagicMock()
        self.vpn._register_clients = AsyncMock(return_value={"success": True})
        self.vpn._run_ssh = AsyncMock(return_value={"success": True})
        self.vpn.remove_client_from_xray = AsyncMock()
        self.monitor = node_monitor.NodeMonitor(self.vpn, MagicMock())
        rec = patch.object(self.monitor, '_records_path', MagicMock())
        self.records = rec.start()
        self.records.read_text.return_value = '{}'
        self.addCleanup(rec.stop)
        mail = patch.object(node_monitor, 'send_email')
        self.mock_mail = mail.start()
        self.addCleanup(mail.stop)
        self.expected = {"1": {"uuid": "u1", "user_id": 1}, "2": {"uuid": "u2", "user_id": 2}}
        self.state = {"expected": self.expected, "unwanted": {"t_old"}}

    def xray_users(self, *emails, ips=None):
        stdout = '{"users": [%s]}' % ", ".join('{"email": "%s"}' % e for e in emails)
        self.monitor._probe = AsyncMock(return_value={
            "rx": 0, "tx": 0, "xray": "active", "clients": stdout, "ips": ips or {}})

    async def test_missing_clients_are_registered_back_and_reported(self):
        self.xray_users("1")  # после OOM-рестарта клиента "2" нет

        await self.monitor.check_node(self.SERVER, self.state)

        self.vpn._register_clients.assert_awaited_once_with([self.expected["2"]], self.SERVER)
        self.mock_mail.assert_called_once()

    async def test_all_clients_present_no_mail(self):
        self.xray_users("1", "2")

        await self.monitor.check_node(self.SERVER, self.state)

        self.vpn._register_clients.assert_not_awaited()
        self.mock_mail.assert_not_called()

    async def test_expired_trial_is_removed_from_node(self):
        self.xray_users("1", "2", "t_old")

        await self.monitor.check_node(self.SERVER, self.state)

        cmd = self.vpn._run_ssh.call_args.args[0]
        self.assertIn("rmu", cmd)
        self.assertIn("t_old", cmd)

    async def test_down_alert_after_two_failures_then_recovery(self):
        self.monitor._probe = AsyncMock(side_effect=TimeoutError())
        await self.monitor.check_node(self.SERVER, self.state)
        self.mock_mail.assert_not_called()  # один сбой — не повод для письма
        await self.monitor.check_node(self.SERVER, self.state)
        self.assertIn("не отвечает", self.mock_mail.call_args.kwargs["subject"])

        self.xray_users("1", "2")
        await self.monitor.check_node(self.SERVER, self.state)
        self.assertIn("снова работает", self.mock_mail.call_args.kwargs["subject"])

    async def test_watchdog_warns_at_10_cuts_at_20_for_trial_and_paid(self):
        self.monitor.users = MagicMock()
        with patch.object(node_monitor.device_guard, 'ban') as ban:
            await self.monitor._watchdog([
                {"t_shared": settings.WATCHDOG_CUT_IPS + 1, "t_warn": settings.WATCHDOG_WARN_IPS + 1},
                {"555": settings.WATCHDOG_CUT_IPS + 1, "556": settings.WATCHDOG_WARN_IPS + 1},
            ])
        cut = {c.args[0] for c in self.vpn.remove_client_from_xray.await_args_list}
        self.assertEqual(cut, {"t_shared", "555"})
        self.monitor.users.execute_query.assert_called_once()   # expires_at триала
        ban.assert_called_once()
        self.assertEqual(ban.call_args.args[:2], ("user", "555"))
        self.assertEqual(self.mock_mail.call_count, 2)

    def test_traffic_near_record_is_quiet_new_record_alerts(self):
        # с 01.10.26 письмо только при новом рекорде: 85% — обычный вечерний пик
        name = "DE 10.0.0.1"
        self.records.read_text.return_value = '{"DE": {"tx_mbps": 100}}'
        self.monitor._net[name] = (0, 0, node_monitor.time.time() - 600)
        self.monitor._check_traffic(name, 0, int(85 * 1_000_000 / 8 * 600))
        self.mock_mail.assert_not_called()
        self.records.write_text.assert_not_called()

        self.monitor._net[name] = (0, 0, node_monitor.time.time() - 600)
        self.monitor._check_traffic(name, 0, int(120 * 1_000_000 / 8 * 600))
        self.assertIn("нагрузка", self.mock_mail.call_args.kwargs["subject"])
        self.records.write_text.assert_called_once()

    def test_traffic_below_share_of_record_is_quiet(self):
        name = "DE 10.0.0.1"
        self.records.read_text.return_value = '{"DE": {"tx_mbps": 100}}'
        self.monitor._net[name] = (0, 0, node_monitor.time.time() - 600)
        self.monitor._check_traffic(name, 0, int(50 * 1_000_000 / 8 * 600))
        self.mock_mail.assert_not_called()



# --------------------------------------------------------------------------
# Заглушки подписки: Happ обрезает имя «сервера» после 27 символов
# --------------------------------------------------------------------------

class TestStubSubscriptionNames(unittest.TestCase):

    def test_every_stub_line_fits_happ_limit(self):
        import sys
        from urllib.parse import unquote
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'website'))
        from routers import vpn as web_vpn

        stubs = {}
        for attr, value in vars(web_vpn).items():
            if attr.endswith('_CONTENT') and isinstance(value, str):
                stubs[attr] = value
            elif attr.endswith('_CONTENT') and isinstance(value, dict):
                stubs.update({f"{attr}[{k}]": v for k, v in value.items()})
        self.assertGreaterEqual(len(stubs), 6)
        for where, content in stubs.items():
            for line in content.splitlines():
                name = unquote(line.split('#', 1)[1])
                with self.subTest(where=where, name=name):
                    self.assertLessEqual(len(name), 28)



    def test_stub_lines_are_reversed_for_happ(self):
        # Happ рисует строки заглушки снизу вверх — отдаём в обратном порядке
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'website'))
        from routers import vpn as web_vpn
        lines = web_vpn._stub_content("первая", "вторая").splitlines()
        self.assertTrue(lines[0].endswith("#вторая"))
        self.assertTrue(lines[1].endswith("#первая"))


class TestSubDoesNotWaitForNodes(unittest.IsolatedAsyncioTestCase):

    async def test_headers_return_without_waiting_for_traffic(self):
        # 23.09.26 зависшая нода держала ответ /sub ~37 с, Happ бросал запрос
        import sys, time as _time
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'website'))
        from routers import vpn as web_vpn

        async def slow_traffic(user_id, reset=False):
            await asyncio.sleep(30)
            return {"uplink": 0, "downlink": 0}

        with patch.object(web_vpn, 'get_user_traffic', side_effect=slow_traffic), \
                patch.object(web_vpn, 'save_traffic'):
            user = {"user_id": 42, "traffic_in": 5, "traffic_out": 700,
                    "status": "active", "next_payment_date": "01.01.30"}
            started = _time.monotonic()
            headers = await web_vpn.get_headers(user)
            self.assertLess(_time.monotonic() - started, 1)
            self.assertIn("download=700000000", headers["subscription-userinfo"])
            for task in list(web_vpn._traffic_tasks):
                task.cancel()


    async def test_parallel_traffic_collections_are_capped(self):
        # 27.09.26: 11 запросов /sub за секунду -> ~55 SSH разом, sshd рвал их
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'website'))
        from routers import vpn as web_vpn
        running, peak = 0, 0

        async def counting_traffic(user_id, reset=False):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            running -= 1
            return {"uplink": 0, "downlink": 0}

        with patch.object(web_vpn, 'get_user_traffic', side_effect=counting_traffic), \
                patch.object(web_vpn, 'save_traffic'):
            await asyncio.gather(*(web_vpn._collect_traffic(str(i)) for i in range(11)))
        self.assertEqual(peak, 2)

    async def test_expired_trial_gets_pay_link(self):
        import sys, base64 as b64
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'website'))
        from routers import vpn as web_vpn
        trial = {"user_id": "t_test", "expires_at": "2026-01-01 00:00+00:00",
                 "traffic_in": 0, "traffic_out": 0}
        request = MagicMock(headers={"user-agent": "Happ/5.9.0/ios", "host": "horizancevpn.com"})

        def by_uuid(table, uuid):
            return trial if table == 'trial_sessions' else None

        with patch.object(web_vpn, 'get_user_by_uuid', side_effect=by_uuid), \
                patch.object(web_vpn, 'update_device_binding', AsyncMock(return_value=None)), \
                patch.object(web_vpn, '_schedule_traffic_collection'), \
                patch.object(web_vpn, 'is_user_inactive', return_value=True), \
                patch.object(web_vpn, 'fetch_routing_deeplink', AsyncMock(return_value="")):
            resp = await web_vpn.get_client_config("uuid", request)
        body = resp.body.decode()
        self.assertIn("horizancevpn.com/pay", body)
        self.assertEqual(resp.headers["profile-web-page-url"], "https://horizancevpn.com/pay")
        announce = b64.b64decode(resp.headers["announce"].split(":", 1)[1]).decode()
        self.assertIn("/pay", announce)



class TestLoginNextRedirect(unittest.TestCase):

    def test_only_local_paths_are_accepted(self):
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'website'))
        from routers.pages import safe_next
        self.assertEqual(safe_next("/pay"), "/pay")
        for bad in (None, "", "https://evil.com", "//evil.com", "/\\evil.com"):
            self.assertEqual(safe_next(bad), "/dashboard")


class TestTrialExpiry(unittest.TestCase):

    def test_both_date_formats_compare_as_dates(self):
        from datetime import timezone as tz
        from models import trial_expired
        now = datetime(2026, 9, 23, 16, 42, tzinfo=tz.utc)
        # раньше строковое сравнение считало этот триал истёкшим с 00:00 UTC
        self.assertFalse(trial_expired("2026-09-23 21:27+00:00", now))
        self.assertTrue(trial_expired("2026-09-23 16:00+00:00", now))
        self.assertFalse(trial_expired("2026-09-24T06:00:19.976771+00:00", now))



class TestLegacyHostDetection(unittest.TestCase):

    def test_only_horizance_ru_is_legacy(self):
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'website'))
        from routers import vpn as web_vpn
        req = lambda host: MagicMock(headers={"host": host})
        self.assertTrue(web_vpn._is_legacy_host(req("horizance.ru")))
        self.assertTrue(web_vpn._is_legacy_host(req("www.horizance.ru:443")))
        self.assertFalse(web_vpn._is_legacy_host(req("horizancevpn.com")))


if __name__ == '__main__':
    unittest.main()
