import asyncio
import asyncssh
import json
import shlex
import sys
from typing import Any

import credentials
from restore_users import restore_vpn
from vpn import ssh_connect_kwargs


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
        self.server = cfg
        self.host = cfg["host"]
        self.port = cfg.get("port", 22)
        self.username = cfg["username"]
        # у нод с ssh_key_path пароля нет, sudo там без пароля — sudo -S получит пустую строку
        self.password = cfg.get("password") or ""
        self.conn: asyncssh.SSHClientConnection | None = None

    async def connect(self) -> None:
        self.conn = await asyncssh.connect(**ssh_connect_kwargs(self.server))

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

def fail2ban_jail(server: dict) -> str:
    """Общая настройка fail2ban всех нод (с 18.10.26). Белый список — IP всех
    наших серверов: бот ходит на ноды по ssh с NL."""
    hosts = " ".join(s["host"] for s in credentials.SERVERS)
    return (
        "# Horizance (18.10.26): одна настройка на всех нодах, как на MX. Белый список — IP всех наших серверов\n"
        "# (бот ходит на ноды по ssh с NL). jail.d/*.local читается последним и перекрывает jail.local.\n"
        "# Пишется deploy_xray.ensure_fail2ban — правки вручную перезапишутся при следующем деплое.\n"
        "[DEFAULT]\n"
        f"ignoreip = 127.0.0.1/8 ::1 {hosts}\n"
        "maxretry = 5\nfindtime = 10m\nbantime = 1h\n"
        "bantime.increment = true\nbantime.maxtime = 1w\n\n"
        "[sshd]\nenabled = true\nmaxretry = 5\nfindtime = 10m\nbantime = 1h\n"
        "backend = systemd\n"
        "# порт(ы), где реально слушает sshd на этой ноде\n"
        f"port = {server.get('port', 22)}\n"
    )

async def ensure_fail2ban(client: SSHClient, server: dict) -> str:
    """Ставит fail2ban, если его нет, и приводит jail к общей настройке."""
    jail = shlex.quote(fail2ban_jail(server))
    await client.sudo_run(
        "command -v fail2ban-client >/dev/null || "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -q fail2ban >/dev/null 2>&1; "
        f"printf %s {jail} > /etc/fail2ban/jail.d/horizance-sshd.local; "
        "systemctl enable fail2ban >/dev/null 2>&1; systemctl restart fail2ban"
    )
    await asyncio.sleep(3)
    return await client.sudo_run("fail2ban-client status sshd | grep -E 'Currently|Total banned'")

async def ensure_log_access(client: SSHClient) -> str:
    """access.log xray читается пользователем ssh без sudo: файл <xray>:adm 640,
    пользователь в группе adm, ротация copytruncate (права файла сохраняются)."""
    logrotate = shlex.quote(
        "/var/log/xray/*.log {\n    daily\n    maxsize 100M\n"
        "    # старые не удаляем (решение владельца 25.09.26): 3650 сжатых архивов ~ 10 лет\n"
        "    rotate 3650\n    compress\n    delaycompress\n    missingok\n    notifempty\n    copytruncate\n}\n"
    )
    return await client.sudo_run(
        "U=$(systemctl show xray -p User --value); U=${U:-nobody}; "
        "mkdir -p /var/log/xray; chmod 755 /var/log/xray; "
        "touch /var/log/xray/access.log /var/log/xray/error.log; "
        "chown $U:adm /var/log/xray/*.log; chmod 640 /var/log/xray/*.log; "
        f"usermod -aG adm {shlex.quote(client.username)}; "
        f"[ -f /etc/logrotate.d/xray ] || printf %s {logrotate} > /etc/logrotate.d/xray; "
        "ls -l /var/log/xray/access.log"
    )

async def ensure_port_443(client: SSHClient) -> str:
    """Образы Oracle режут всё, кроме 22, правилом REJECT в iptables."""
    return await client.sudo_run(
        "if iptables -S INPUT | grep -q 'REJECT' && "
        "! iptables -C INPUT -p tcp -m state --state NEW -m tcp --dport 443 -j ACCEPT 2>/dev/null; then "
        "iptables -I INPUT $(iptables -S INPUT | grep -n REJECT | head -1 | cut -d: -f1 | awk '{print $1-1}') "
        "-p tcp -m state --state NEW -m tcp --dport 443 -j ACCEPT && "
        "(netfilter-persistent save >/dev/null 2>&1 || true) && echo '443 opened'; "
        "else echo '443 ok'; fi"
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

        if server.get("role") != "localhost":
            print(f"[{s_label}] firewall: {(await ensure_port_443(client)).strip()}")
        print(f"[{s_label}] fail2ban: {(await ensure_fail2ban(client, server)).strip()}")

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

        print(f"[{s_label}] access.log: {(await ensure_log_access(client)).strip()}")

        await truncate_logs(client)
        print(f"[{s_label}] Logs truncated")

        '''
        result = await function(client)
        print(f"[{s_label}] function result:\n{result}")
        '''
    finally:
        await client.close()

async def main():
    # Только явно названные ноды: deploy перезапускает xray, а раньше main() молча
    # проходил по SERVERS[1..3] и клал рабочие ноды.
    names = sys.argv[1:]
    known = {s["name"]: s for s in credentials.SERVERS}
    if not names or any(n not in known for n in names):
        print(f"usage: deploy_xray.py NAME [NAME ...]   ноды: {', '.join(known)}")
        sys.exit(1)
    for name in names:
        await deploy(known[name])

if __name__ == "__main__":
    asyncio.run(main())
