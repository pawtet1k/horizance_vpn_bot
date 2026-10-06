#!/usr/bin/env python3
import asyncio

from telethon import TelegramClient
from telethon.sessions import MemorySession

import credentials
from db import UserManager
from models import UserStatus
from utils import Campaign, run_notification_campaigns
from my_logger import logger

# Сессия в памяти, а не файл 'bot.session' из main.py. Файл сессии — это
# состояние ОДНОГО экземпляра клиента (ключ авторизации, DC, кэш сущностей),
# и делить его между двумя процессами нельзя: постоянно работающий main.py
# держит sqlite открытым, а Telethon на connect() пишет в него
# (`delete from sessions`) — отсюда "sqlite3.OperationalError: database is
# locked". Боту хранить сессию вообще не нужно: bot_token авторизует с нуля
# при каждом запуске, поэтому MemorySession здесь и правильнее, и проще —
# ни файла, ни блокировок, ни лишнего мусора в репозитории.
vpn_bot = TelegramClient(
    MemorySession(), credentials.API_ID, credentials.API_HASH
)
bot_token = credentials.BOT_TOKEN


async def main():
    user_id = 6023219501
    condition = lambda row: row['user_id'] == user_id
    condition1 = lambda row: row['status'] == UserStatus.INACTIVE.value
    message = "Добрый день! Я - разработчик сервиса, получил вашу жалобу насчет неработающего Youtube. Было ли это разовой проблемой или постоянной? Какой сервер вы использовали? Пробовали ли переключиться на другие сервера? Youtube - это единственный из заблокированных РКН сайтов, который у вас не работал? Для удобства и более оперативной коммуникации можете написать мне ответ напрямую в ТГ @ppawtet1k. С нетерпением жду вашу обратную связь, чтобы в кратчайшие сроки разрешить возникшую проблему."
    #Да, конечно, устанавливайте на здоровье! Надеюсь, вам нравится сервис и вы останетесь нашим клиентом. Также, если порекомендуете сервис, то за каждого приглашенного активного пользователя один месяц подписки бесплатно. Сделав рассылку своей реферальной ссылки друзьям, так можно и сразу больше года бесплатного пользования получить. Также если у вас или ваших знакомых не будет стабильно работать ВПН, дайте знать, решим максимально оперативно."
    #Антон! Для настройки, наберите в боте '/config', далее следуйте появившейся инструкции."
    #У вас установлено приложение Happ? Пару недель назад его удалили из Appstore. Можно попробовать найти его в магазине бетаверсий Testflight. Или поменять свой регион в Appstore"
    logger.info(f"Ответ техподдержки для {user_id}: {message}")
    message1 = (
        "👋 Соскучились по свободному интернету?\n\n"
        "Ваша подписка на Horizance VPN сейчас неактивна, но вернуться "
        "очень просто — оформите новую подписку через /pay, и VPN снова "
        "заработает за пару минут! 🚀"
    )
    campaign_name = "разовое сообщение"
    campaign_name1 = "разовая рассылка (inactive)"

    await vpn_bot.start(bot_token=bot_token)
    user_manager = UserManager()    
    try:
        await run_notification_campaigns(
            vpn_bot,
            user_manager,
            [Campaign(condition, message, campaign_name)]
        )
    finally:
        await vpn_bot.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
