#!/usr/bin/env python3
"""Разовый скрипт: чинит расхождение client_uuid между users.db и Xray.

`xray api adu` не перезаписывает уже существующий email — он пишет
"User <email> already exists" и выходит с кодом 0. Если обработчик настройки
VPN срабатывал дважды подряд, оба вызова видели client_uuid=None, генерировали
РАЗНЫЕ uuid, в Xray оставался первый, а в БД записывался второй. Ссылка у
клиента в итоге содержала uuid, которого сервер не знает: TCP до сервера есть
(пинг идёт), а VLESS-хендшейк не проходит и трафик не идёт.

Скрипт берёт БД как источник истины и перерегистрирует таких пользователей на
всех серверах через исправленный VPNManager._register_clients (rmu + adu).

Запуск:  .venv/bin/python3 resync_broken_uuids.py [--apply]
Без --apply только показывает, что будет сделано.
"""
import asyncio
import json
import subprocess
import sys

from db import UserManager

from my_logger import logger
from vpn import VPNManager

def xray_registered(vpn_manager) -> dict[str, str]:
    """email -> uuid, как их видит локальный Xray."""
    result = subprocess.run([
        "xray", "api", "inbounduser",
        f"--server=127.0.0.1:{vpn_manager.api_port}",
        f"--tag={vpn_manager.inbound_tag}",
    ], capture_output=True, text=True, check=True)
    return {
        u["email"]: u["account"]["id"]
        for u in json.loads(result.stdout).get("users", [])
        if u.get("email") and u.get("account", {}).get("id")
    }


def find_broken(user_manager, registered) -> list[dict]:
    broken = []
    # в trial_sessions нет колонки status, поэтому там берём всех с uuid
    sources = [
        user_manager.get_not_inactive_users_with_uuid("users"),
        user_manager.get_users_with_uuid("trial_sessions"),
    ]
    for rows in sources:
        for row in rows:
            in_xray = registered.get(str(row["user_id"]))
            if in_xray and in_xray != row["client_uuid"]:
                broken.append({
                    "user_id": row["user_id"], "uuid": row["client_uuid"],
                    "stale": in_xray, "status": row.get("status", "-"),
                })
    return broken


async def main() -> None:
    apply = "--apply" in sys.argv
    user_manager = UserManager()
    vpn_manager = VPNManager()

    broken = find_broken(user_manager, xray_registered(vpn_manager))
    if not broken:
        print("Расхождений нет.")
        return

    print(f"Найдено расхождений: {len(broken)}")
    for row in broken:
        print(f"  {row['user_id']:<14} status={row['status']:<8} "
              f"в БД={row['uuid']}  в Xray={row['stale']}")

    if not apply:
        print("\nЭто пробный прогон. Для перерегистрации: --apply")
        return

    print("\nПеререгистрирую на всех серверах...")
    await vpn_manager.run_on_all_servers(
        vpn_manager._register_clients,
        [{"uuid": r["uuid"], "user_id": r["user_id"]} for r in broken],
    )

    still = find_broken(user_manager, xray_registered(vpn_manager))
    if still:
        print(f"ОСТАЛИСЬ расхождения: {[r['user_id'] for r in still]}")
    else:
        print("Готово: локальный Xray синхронизирован с БД.")
    logger.info(f"resync_broken_uuids: обработано {len(broken)}, "
                f"осталось {len(still)}")


if __name__ == "__main__":
    asyncio.run(main())
