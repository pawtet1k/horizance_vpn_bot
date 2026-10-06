import asyncio
import asyncssh
import json
import shlex
import subprocess
import uuid

import credentials
from my_logger import logger
import settings
from typing import Callable, Awaitable

def ssh_connect_kwargs(server: dict) -> dict:
    """Единая сборка параметров asyncssh для сервера из credentials.SERVERS.

    Держим в одном месте: раньше эта же четвёрка (host/port/username/пароль
    или ключ) собиралась заново в каждом скрипте, который ходит по нодам, и
    при добавлении, например, ssh_key_path пришлось бы править их все.
    """
    kwargs = {
        "host": server["host"],
        "port": server.get("port", 22),
        "username": server["username"],
        "known_hosts": None,
    }
    if server.get("password"):
        kwargs["password"] = server["password"]
    elif server.get("ssh_key_path"):
        kwargs["client_keys"] = [server["ssh_key_path"]]
    return kwargs


class VPNManager:
    def __init__(self) -> None:
        self.CONFIG_FILE = settings.XRAY_CONFIG_PATH
        self.inbound_tag, self.api_port = self._get_config_settings()

    def _load_vpn_config(self) -> dict:
        try:
            with open(self.CONFIG_FILE, 'r') as f:
                config = json.load(f)
            logger.debug('Файл конфигурации VPN был прочитан.')
            return config
        except FileNotFoundError:
            logger.error(f"Файл {self.CONFIG_FILE} не найден.")
            return {}

    def _get_config_settings(self):
        config = self._load_vpn_config()
        api_port = config['inbounds'][1]['port']
        inbound_tag = config["inbounds"][0]["tag"]
        logger.debug(
            f'В конфиге XRay: api_port={api_port}; inbound_tag={inbound_tag}'
        )
        return inbound_tag, api_port
    '''
    def generate_settings_link(self, client_uuid) -> str:
        settings_link = (
            f"vless://{client_uuid}@{credentials.server_ip}?"
            f"flow=xtls-rprx-vision&security=reality&encryption=none&"
            f"type=tcp&sid={credentials.XRAY_SHORTID}&fp=chrome&"
            f"pbk={credentials.SERVER_PUBLIC_KEY}&headerType=none&"
            f"sni={credentials.SNI}#horizance_vpn"
        )
        logger.info(
            f'Сформирована ссылка на настойки клиента VPN uuid={client_uuid}'
        )
        return settings_link
    '''

    async def get_vpn_clients(self) -> list:
        """Клиенты ТОЛЬКО локальной ноды.

        Годится для диагностики машины, на которой крутится бот, и больше ни
        для чего: клиентов регистрируем на всех серверах сразу, поэтому
        решения о том, кого удалять, по этому списку принимать нельзя —
        для этого есть get_all_vpn_clients ниже.
        """
        inbound_tag, api_port = self._get_config_settings()
        result = await asyncio.to_thread(
            subprocess.run, [
                "xray", "api", "inbounduser",
                f"--server=127.0.0.1:{api_port}", f"--tag={inbound_tag}"
            ], capture_output=True, text=True
        )
        if result. returncode != 0:
            raise RuntimeError (result.stderr.strip())
        data = json.loads(result.stdout)
        users = {
            u.get("email") for u in data.get("users", []) if u.get("email")
        }
        logger.info(f"В xray найдено {len(users)} клиентов: {users}")
        return list(users)

    async def _list_clients(self, server: dict) -> dict:
        cmd = [
            "xray", "api", "inbounduser",
            f"--server=127.0.0.1:{self.api_port}", f"--tag={self.inbound_tag}"
        ]
        return await self._run_ssh(cmd, server)

    async def get_all_vpn_clients(self) -> set:
        """Объединение клиентов по ВСЕМ серверам.

        Нужно потому, что списки на нодах расходятся: если удаление клиента
        не долетело до какой-то ноды (сеть, таймаут), там он остаётся жить.
        Список локальной ноды в этом случае показывает, что человека уже нет,
        а на пяти других он продолжает пользоваться VPN.
        """
        results = await self.run_on_all_servers(self._list_clients)
        emails: set[str] = set()
        for r in results:
            if not (isinstance(r, dict) and r.get("success")):
                continue
            try:
                data = json.loads(r["stdout"])
            except (ValueError, KeyError):
                continue
            emails |= {
                u.get("email") for u in data.get("users", []) if u.get("email")
            }
        logger.info(f"По всем серверам в xray найдено {len(emails)} клиентов")
        return emails

    async def _user_traffic(self, user_id, reset: bool, server: dict) -> dict:
        # server ПОСЛЕДНИМ: run_on_all_servers дописывает сервер в конец
        # аргументов (task(*args, s)), поэтому любой новый параметр должен
        # вставать перед ним, иначе он молча займёт место сервера.
        # Кавычки обязательны: _run_ssh склеивает список в строку и отдаёт её
        # шеллу, а `>>>` шелл понимает как перенаправление вывода и падает с
        # кодом 2. Локальный вызов через subprocess этой проблемы не имел -
        # там список передаётся без шелла.
        cmd = [
            "xray", "api", "statsquery",
            f"--server=127.0.0.1:{self.api_port}",
            shlex.quote(f"--pattern=user>>>{user_id}")
        ]
        if reset:
            cmd.append("-reset")
        return await self._run_ssh(cmd, server)

    async def get_user_traffic(self, user_id, reset: bool = False) -> dict:
        """Трафик пользователя, просуммированный по всем серверам.

        Раньше считалось только с локальной ноды, и у всех, кто подключается
        через US/DE/RU/KZ/MX, в базе навсегда оставались нули. На этих нулях
        построена рассылка «приложение установлено, но VPN выключен» — она
        уходила людям, которые VPN активно пользуются.

        reset=True снимает показания и тут же обнуляет счётчики в xray
        (`statsquery -reset`). Тогда вызывающий код может прибавить снятое
        к тому, что уже лежит в базе, и накопленный за всё время трафик
        перестаёт теряться при перезапуске ноды. Без reset счётчики отдают
        «с момента последнего старта xray» — это годится только для разовой
        диагностики.
        """
        results = await self.run_on_all_servers(
            self._user_traffic, user_id, reset
        )
        totals = {"uplink": 0, "downlink": 0}
        for r in results:
            if not (isinstance(r, dict) and r.get("success")):
                continue
            try:
                data = json.loads(r["stdout"])
            except (ValueError, KeyError):
                continue
            for stat in data.get("stat", []):
                key = stat["name"].split(">>>")[-1]
                if key in totals:
                    totals[key] += stat.get("value", 0)
        return totals

    async def remove_client_from_xray(self, user_id) -> None:
        await self.run_on_all_servers(self._unregister_client, user_id)
        logger.info(f'{user_id}: Удален uuid из xray')

    async def setup_xray(self, user) -> str:
        if not user.client_uuid:
            client_uuid = str(uuid.uuid4())
            logger.info(f'{user.user_id}: Создан новый uuid={client_uuid}')
        else:
            client_uuid = user.client_uuid
        await self.run_on_all_servers(
            self._register_clients,
            [{"uuid": client_uuid, "user_id": user.user_id}]
        )
        logger.info(f'{user.user_id}: В xray добавлен uuid={client_uuid}')
        return client_uuid

    async def setup_xray_for_trial(self, user_id: str) -> str:
        client_uuid = str(uuid.uuid4())
        await self.run_on_all_servers(
            self._register_clients, [{"uuid": client_uuid, "user_id": user_id}]
        )
        logger.info(f'{user_id}: Добавлен uuid={client_uuid}')
        return client_uuid

    def _build_payload(self, users: list[dict]) -> dict:
        clients = []
        for user in users:
            client = {
              "id": user['uuid'],
              "email": str(user['user_id']) #,
#              "flow": "xtls-rprx-vision"
            }
            clients.append(client)

        payload = {
          "inbounds": [
            {
              "tag": self.inbound_tag,
              "protocol": "vless",
              "port": 443,
              "settings": {
                "decryption": "none",
                "clients": clients
              }
            }
          ]
        }
        return payload

    async def _register_clients(self, users: list[dict], server: dict) -> None:
        # `xray api adu` НЕ перезаписывает уже существующий email: он пишет
        # "User <email> already exists" и выходит с кодом 0, оставляя в xray
        # старый uuid. Бот при этом считает регистрацию успешной и сохраняет
        # в БД новый uuid — ссылка у клиента расходится с тем, что примет
        # сервер, и туннель молча не поднимается. Поэтому сначала снимаем
        # старую регистрацию, потом добавляем заново.
        api = f"--server=127.0.0.1:{self.api_port}"
        emails = " ".join(str(user['user_id']) for user in users)
        cmd = [
            "xray", "api", "rmu", api, f"--tag={self.inbound_tag}", emails,
            ";",
            "xray", "api", "adu", api, "/dev/stdin"
        ]
        payload = self._build_payload(users)
        #subprocess.run(cmd, input=json.dumps(payload), text=True, check=True)
        return await self._run_ssh(cmd, server, json.dumps(payload))

    async def _unregister_client(self, user_id: int, server: dict) -> None:
        email = str(user_id)
        cmd = [
            "xray", "api", "rmu",
            f"--server=127.0.0.1:{self.api_port}",
            f"--tag={self.inbound_tag}",
            email
        ]
        #subprocess.run(cmd, check=True)
        return await self._run_ssh(cmd, server)
    '''
    async def run_locally(
        self, task: Callable[..., Awaitable[dict]], *args
    ) -> None:
        local_host = next(s for s in credentials.SERVERS if s.get('role') == 'localhost')
        return await task(*args, server=local_host)
    '''
    
    async def run_on_all_servers(
        self, task: Callable[..., Awaitable[dict]], *args
    ) -> list:
        """Возвращает результаты по каждому серверу.

        Раньше возвращала None, поэтому вызвать её ради данных (список
        клиентов, статистика) было нельзя — приходилось лезть к локальной
        ноде через subprocess, а это давало картину по одной машине из шести.
        Возврат результатов ничего не ломает: прежние вызывающие его просто
        игнорируют.
        """
        tasks = [task(*args, s) for s in credentials.SERVERS]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        error = False
        for r in results:
            if isinstance(r, dict) and r.get("success"):
                logger.debug(f"✓ {r['host']} - {task} - ok")
            else:
                logger.error(f"✗ {task} - Ошибка на сервере: {r}")
                error = True
        if error == False:
            # сбор трафика и списки клиентов идут сотни раз в сутки и забивали
            # треть info-лога; в info оставляем только изменения (adu/rmu)
            quiet = task.__name__ in ("_user_traffic", "_list_clients")
            (logger.debug if quiet else logger.info)(
                f"✓ all servers successfully completed action: {task.__name__}"
            )
        return results

    '''
    async def _run_on_server(self, server: Server, cmd: str) -> tuple[Server, str, Optional[str]]:
        """
        Возвращает (server, stdout, error_or_None)
        """
        try:
            if server.local:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode != 0:
                    return server, "", stderr.decode().strip()
                return server, stdout.decode().strip(), None
    '''

    async def _run_ssh(self, cmd, server: dict, input_data=None) -> dict:
        try:
            kwargs = ssh_connect_kwargs(server) | {"connect_timeout": 12}

            async with asyncssh.connect(**kwargs) as conn:
                result = await conn.run(
                    " ".join(cmd), input=input_data, timeout=25, check=True
                )
                return {
                    "host": server["host"], "success": True,
                    "stdout": result.stdout, "stderr": result.stderr
                }
        except Exception as e:
            # тип обязателен: у asyncio.TimeoutError пустой str(), и в логах
            # таймаут выглядел как error:'' — неотличимо от любой другой беды
            return {
                "host": server["host"], "success": False,
                "error": f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            }
