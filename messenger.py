class CapturedMessage:
    def __init__(self, message_id, text, buttons=None, parse_mode=None, file=None):
        self.id = message_id
        self.text = text
        self.buttons = buttons
        self.parse_mode = parse_mode
        self.file = file


class CapturingMessenger:
    """Duck-types the subset of the Telethon client's interface
    (send_message/edit_message) that bot.py and subscription.py call.

    Passed in place of vpn_bot from website routes, so the exact same
    handler functions used by the Telegram bot can run there too: instead
    of hitting Telegram, messages land here for the caller to render into
    an HTTP response.
    """

    def __init__(self):
        self.messages: list[CapturedMessage] = []
        self._next_id = 0

    async def send_message(
        self, user_id, text=None, *, buttons=None, parse_mode=None, file=None, **_
    ):
        self._next_id += 1
        message = CapturedMessage(self._next_id, text, buttons, parse_mode, file)
        self.messages.append(message)
        return message

    async def edit_message(
        self, user_id, message_id, text=None, *, buttons=None, parse_mode=None, **_
    ):
        for message in self.messages:
            if message.id == message_id:
                message.text = text
                message.buttons = buttons
                message.parse_mode = parse_mode
                return message
        return await self.send_message(
            user_id, text, buttons=buttons, parse_mode=parse_mode
        )

    @property
    def last_text(self) -> str | None:
        return self.messages[-1].text if self.messages else None


class TelegramApiMessenger:
    """Duck-types send_message так же, как CapturingMessenger, но реально
    отправляет сообщение в Telegram через Bot API (utils.send_telegram_api_message)
    вместо захвата. Нужен процессам без живой сессии Telethon — например,
    вебхуку оплаты UDM Pay на сайте (website/routers/payments.py), где нет
    доступа к TelegramClient из main.py."""

    async def send_message(self, user_id, text=None, **_):
        from utils import send_telegram_api_message
        await send_telegram_api_message(user_id, text)

    async def edit_message(self, user_id, message_id, text=None, **_):
        from utils import send_telegram_api_message
        await send_telegram_api_message(user_id, text)
