#!/usr/bin/env python3
"""Обновление geosite.dat/geoip.dat на всех нодах — из таймера бота.

Зачем это здесь, а не в кроне: крон пришлось бы настраивать руками на каждой
новой машине отдельно от кода, он не попадёт в репозиторий вместе с
приложением и о нём легко забыть. Таймер бота (``utils.start_timer``) и так
ходит по всем нодам раз в 12 часов — обновление баз просто становится ещё
одним его шагом.

Что обновляем. В конфиге xray стоит правило ``geosite:category-ads-all`` —
это и есть блокировка рекламы на стороне сервера. Список берётся из
geosite.dat, а не из конфига, поэтому сам по себе конфиг «свежим» не бывает:
без обновления файла правило работает по данным того дня, когда файл
скачали. У Loyalsoldier в category-ads-all около 170 тысяч доменов (собрано
из AdGuard, EasyList, EasyPrivacy, Peter Lowe) против ~600 в исходной базе
v2fly, поэтому берём именно его.

Почему после обновления обязателен ``restore_users``: xray читает .dat только
при старте, значит нужен рестарт, а рестарт стирает список клиентов — он
живёт лишь в памяти процесса (``inbounds[].settings.clients`` в config.json
пустой). Ни xray.service, ни его drop-in клиентов не возвращают, это
проверено. Забыть здесь ``restore_vpn`` — значит отключить всех подписчиков
на этой ноде до их следующего обращения к боту.
"""
import asyncio

import asyncssh

import credentials
from my_logger import logger
from vpn import ssh_connect_kwargs

RELEASE_URL = (
    "https://github.com/Loyalsoldier/v2ray-rules-dat"
    "/releases/latest/download"
)
ASSET_DIR = "/usr/local/share/xray"
ASSETS = ("geosite.dat", "geoip.dat")
MAX_AGE_DAYS = 7


async def _sudo(conn, command: str, server: dict):
    """sudo без tty. На части нод NOPASSWD не настроен, пароль подаём на
    stdin через ``sudo -S -p ''`` — тот же, что для ssh."""
    password = server.get("password")
    if password:
        return await conn.run(
            f"sudo -S -p '' {command}", input=password + "\n", check=False
        )
    return await conn.run(f"sudo {command}", check=False)


async def _is_stale(conn) -> bool:
    """Свежесть определяем по mtime самого файла на ноде, а не по отметке у
    себя: так состояние не разъезжается, если бот перезапускали или ноду
    добавили позже остальных."""
    result = await conn.run(
        f"find {ASSET_DIR}/geosite.dat -mtime +{MAX_AGE_DAYS} -print 2>/dev/null; "
        f"test -f {ASSET_DIR}/geosite.dat || echo MISSING",
        check=False,
    )
    return bool(result.stdout.strip())


async def refresh_node(server: dict, force: bool = False) -> bool:
    """Возвращает True, если базы на ноде обновили."""
    from restore_users import restore_vpn  # локальный импорт: restore тянет vpn/db

    name = server.get("name", server["host"])
    kwargs = ssh_connect_kwargs(server) | {"connect_timeout": 15}

    async with asyncssh.connect(**kwargs) as conn:
        if not force and not await _is_stale(conn):
            logger.debug(f"geo: {name} — базы свежие, пропускаю")
            return False

        # Качаем во временный файл и подменяем одним mv: если сеть отвалится
        # на середине, xray не останется с обрезанным .dat.
        commands = " && ".join(
            f"curl -fsSL -o /tmp/{a} {RELEASE_URL}/{a} && "
            f"mv /tmp/{a} {ASSET_DIR}/{a} && chmod 644 {ASSET_DIR}/{a}"
            for a in ASSETS
        )
        result = await _sudo(conn, f"bash -c '{commands}'", server)
        if result.exit_status != 0:
            logger.error(
                f"geo: {name} — не удалось обновить базы: "
                f"{(result.stderr or '')[-300:]}"
            )
            return False

        await _sudo(conn, "systemctl restart xray", server)
        await asyncio.sleep(3)

    # restore_vpn открывает своё соединение, поэтому вне блока выше
    await restore_vpn(server)
    logger.info(f"geo: {name} — базы обновлены, клиенты возвращены в xray")
    return True


async def refresh_all(force: bool = False) -> None:
    """Проходит по нодам последовательно: рестарт xray на ноде — это
    несколько секунд простоя, и делать их одновременно на всех незачем."""
    for server in credentials.SERVERS:
        try:
            await refresh_node(server, force=force)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                f"geo: {server.get('name')} — ошибка обновления: "
                f"{type(exc).__name__}: {exc}"
            )


if __name__ == "__main__":
    import sys
    asyncio.run(refresh_all(force="--force" in sys.argv))
