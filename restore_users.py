#!/usr/bin/env python3
import asyncio
import json
import credentials
from db import UserManager
from vpn import VPNManager
from my_logger import logger
from models import trial_expired
from datetime import datetime, timezone


def clients_to_restore(table_name, user_manager) -> list[dict]:
    """Кто должен быть зарегистрирован в xray: {'uuid', 'user_id'}.

    Общая точка для restore_vpn и node_monitor — мониторинг сверяет с этим
    списком то, что реально есть на ноде.
    """
    users_data = []
    users: list[dict] = user_manager.get_users_with_uuid(table_name)
    for user in users:
        if not str(user['user_id']).startswith("t_"):
            if user.get('status') == 'inactive':
                continue
        if str(user['user_id']).startswith("t_"):
            if trial_expired(user['expires_at']):
                continue
        users_data.append(
            {'uuid': user['client_uuid'], 'user_id': user['user_id']}
        )
    return users_data


async def restore_from_table(
    table_name, user_manager, vpn_manager, server
) -> None:
    try:
        users_data = clients_to_restore(table_name, user_manager)
        count = len(users_data)
        # используется приватная функция
        await vpn_manager._register_clients(users_data, server)
        logger.info(
            f"[{table_name}] Восстановление завершено: {count} пользователей"
        )
    except Exception as e:
        logger.error(f"[{table_name}] Ошибка восстановления юзеров {str(e)}")

async def restore_vpn(server) -> None:
    vpn_manager = VPNManager()
    user_manager = UserManager()
    await restore_from_table('users', user_manager, vpn_manager, server)
    await restore_from_table(
        'trial_sessions', user_manager, vpn_manager, server
    )

if __name__ == "__main__":
    local_host = next(s for s in credentials.SERVERS if s.get("role") == "localhost")
    asyncio.run(restore_vpn(local_host))
