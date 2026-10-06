import asyncio
import asyncssh
import json
from typing import Any

import credentials
from restore_users import restore_vpn


INSTALL_CMD = (
    "curl -Ls https://github.com/XTLS/Xray-install/raw/main/install-release.sh "
    "-o /tmp/install-xray.sh && "
    "bash /tmp/install-xray.sh"
)

LOCAL_CONFIG_PATH = "/usr/local/etc/xray/config.json"
REMOTE_CONFIG_PATH = "/usr/local/etc/xray/config.json"
TMP_PATH = "/tmp/config.json"

def get_target_server(i) -> dict[str, Any]:
    return credentials.SERVERS[i]


class SSHClient:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.host = cfg["host"]
        self.port = cfg["port"]
        self.username = cfg["username"]
        self.password = cfg["password"]
        self.conn: asyncssh.SSHClientConnection | None = None

    async def connect(self) -> None:
        self.conn = await asyncssh.connect(
            self.host, port=self.port,
            username=self.username,
            password=self.password,
            known_hosts=None,
        )

    async def run(self, cmd: str) -> str:
        if not self.conn:
            raise RuntimeError("No SSH connection")
        res = await self.conn.run(cmd, check=False)
        return res.stdout + res.stderr

    async def sudo_run(self, cmd: str) -> str:
        if not self.conn:
            raise RuntimeError("No SSH connection")
        res = await self.conn.run(
            f"sudo -S bash", input=f"{self.password}\n{cmd}\n",
            check=False
        )
        return res.stdout + res.stderr

    async def upload(self, local: str, remote: str) -> None:
        if not self.conn:
            raise RuntimeError("No SSH connection")
        await asyncssh.scp(local, (self.conn, TMP_PATH))
        await self.sudo_run(f"mv {TMP_PATH} {remote}")

    async def close(self) -> None:
        if self.conn:
            self.conn.close()
            await self.conn.wait_closed()


async def get_last_reboot_date(client: SSHClient):
    return await client.run("uptime -s")


async def get_latest_xray_version(client: SSHClient) -> str:
    result = await client.run(
        "curl -s https://api.github.com/repos/XTLS/Xray-core/releases/latest"
    )
    return json.loads(result)["tag_name"].lstrip("v")

async def check_xray(client: SSHClient, latest_version: str) -> bool:
    out1 = await client.run("systemctl is-active xray")
    if out1.strip() != "active":
        return False
    out2 = await client.run("xray -version")
    if latest_version not in out2:
        print(f"⚠️ Версия Xray устарела! Версия: {out2}")
        return False
    return True

async def install_xray(client: SSHClient) -> str:
    return await client.sudo_run(INSTALL_CMD)

async def enable_xray(client: SSHClient) -> str:
    return await client.sudo_run("systemctl enable xray")

async def correct_config(client: SSHClient) -> bool:
    await client.sudo_run(
        """sed -i 's|"xver": 1|"xver": 0|g' /usr/local/etc/xray/config.json"""
    )
    await client.sudo_run(
        """sed -i 's|"dest": "127.0.0.1:8080"|"dest": "www.horizance.ru:443"|g' /usr/local/etc/xray/config.json"""
    )
    check = await client.run(
        '''grep -E '"xver"|"dest"' /usr/local/etc/xray/config.json'''
    )
    return "127.0.0.1:8080" not in check and "xver\": 1" not in check

async def get_vpn_clients(client: SSHClient) -> int:
    inbound_tag, api_port = "vless_tls", 62789
    result = await client.run(
        f"xray api inbounduser --server=127.0.0.1:{api_port} --tag={inbound_tag}"
    )
    dict = json.loads(result)
    users = {u.get("email") for u in dict.get("users", []) if u.get("email")}
    return list(users)

async def get_last_xray_restart_date(client: SSHClient):
    result = await client.run(
        "systemctl show xray -p ActiveEnterTimestamp --value"
    )
    return result

async def truncate_logs(client: SSHClient) -> str:
    sizes = await client.sudo_run(
        "stat -c%s /var/log/xray/access.log /var/log/xray/error.log"
    )
    access_size, error_size = (int(s) for s in sizes.split() if s.isdigit())
    print(
        f"Логи до очистки: access.log — {access_size / 1024**2:.1f} МБ, "
        f"error.log — {error_size / 1024**2:.1f} МБ"
    )
    return await client.sudo_run(
        "truncate -s 0 /var/log/xray/access.log /var/log/xray/error.log"
    )

async def function(client: SSHClient):
    result = await client.sudo_run(
        'grep -r "xray" /etc/cron* /var/spool/cron/ 2>/dev/null'
    )
    return result

async def deploy(server) -> None:
    client = SSHClient(server)
    s_label = f"{server['name']} {server['flag']}"
    try:
        await client.connect()

        last_reboot_date = await get_last_reboot_date(client)
        print(f"[{s_label}] Last reboot was on {last_reboot_date}")

        latest_version = await get_latest_xray_version(client)
        exists = await check_xray(client, latest_version)
        if not exists:
            print(f"[{s_label}] Xray not found, installing...")
            result = await install_xray(client)
            print(f"[{s_label}] {result}")
            result = await enable_xray(client)
            print(f"[{s_label}] {result}")

        # повторная проверка
        if not await check_xray(client, latest_version):
            raise RuntimeError(f"[{s_label}] Xray failed to start")
        print(f"[{s_label}] Xray is active")
        if server.get("role") != "localhost":
            await client.upload(LOCAL_CONFIG_PATH, REMOTE_CONFIG_PATH)
            print(f"[{s_label}] Xray config has been uploaded")
            if server.get("role") == "default":
                is_correct = await correct_config(client)
                if is_correct:
                    print(f"[{s_label}] Xray config is correct")
                else:
                    print(f"[{s_label}] ⚠️Xray config is INCORRECT!")
        result = await client.sudo_run("systemctl restart xray")

        if not await check_xray(client, latest_version):
            raise RuntimeError(f"[{s_label}] Xray failed after restart")
        print(f"[{s_label}] Xray has been restarted")

        await restore_vpn(server)
        print(f"[{s_label}] Users restored")

        users = await get_vpn_clients(client)
        print(f"[{s_label}] В xray найдено {len(users)} клиентов:\n{users}")

        last_xray_restart_date = await get_last_xray_restart_date(client)
        print(f"[{s_label}] Last xray restart was on {last_xray_restart_date}")

        await truncate_logs(client)
        print(f"[{s_label}] Logs truncated")

        '''
        result = await function(client)
        print(f"[{s_label}] function result:\n{result}")
        '''
    finally:
        await client.close()

async def main():
    for i in range(1,4):
        server = get_target_server(i)
        await deploy(server)

if __name__ == "__main__":
    asyncio.run(main())
