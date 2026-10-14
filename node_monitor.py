"""Мониторинг VPN-нод: доступность, xray, клиенты, трафик, раздача ключей.

Зачем: 22–23.09.26 утёкшие триал-ключи раздали в Иран (~13 тыс. IP), xray на
US и DE убило OOM, systemd поднял его пустым, и сутки к этим нодам никто не мог
подключиться, а узнали мы об этом от пользователей. Здесь раз в
NODE_MONITOR_INTERVAL_SEC по каждой ноде, за ОДИН ssh-вход (каждый вход на
Ubuntu запускает motd, а он на US однажды съел 324 МБ и вызвал OOM):
  - ssh отвечает и xray active — иначе письмо (после двух неудач подряд) и
    письмо о восстановлении;
  - в xray есть все, кто должен быть (отбор как у restore_users) — недостающих
    регистрируем обратно; истёкшие триалы и неактивных снимаем;
  - сторож: сколько разных IP ходило по каждому ключу за WATCHDOG_WINDOW_MIN
    (по access.log всех нод). Больше WATCHDOG_WARN_IPS — warning в лог,
    больше WATCHDOG_CUT_IPS — ключ отключаем (триал и платный одинаково) и
    пишем письмо;
  - трафик: rx/tx интерфейса за интервал, одной строкой в info-лог; рекорд
    исходящего по каждой ноде хранится в NODE_TRAFFIC_RECORDS_PATH, письмо —
    при NODE_TRAFFIC_ALERT_SHARE от рекорда (1.0 — только новый рекорд).
Письма — на settings.NOTIFY_EMAIL, не чаще NODE_ALERT_COOLDOWN_SEC на ключ.
"""
import asyncio
import csv
import json
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path

import asyncssh
import httpx

import credentials
import device_guard
import payment_outage
import settings
from models import UserStatus, trial_expired
from i18n import is_english, tr
from my_logger import logger
from restore_users import clients_to_restore
from utils import send_email, send_telegram_api_message
from vpn import ssh_connect_kwargs

# интерфейс по умолчанию, его счётчики принятых/отправленных байт (rx/tx — то
# же, что показывает sar -n DEV) и состояние xray
_NET_CMD = (
    "IF=$(ip route show default | awk '{print $5; exit}'); "
    "cat /sys/class/net/$IF/statistics/rx_bytes "
    "/sys/class/net/$IF/statistics/tx_bytes; "
    "systemctl is-active xray"
)

# уникальные IP на ключ (email) за последние N минут по access.log. Время
# берём у самой ноды: логи пишутся в её часовом поясе
_WATCHDOG_SCRIPT = (
    "C=$(date -d '-{minutes} min' '+%Y/%m/%d %H:%M'); "
    "tail -n 300000 /var/log/xray/access.log | "
    "awk -v c=\"$C\" '($1\" \"substr($2,1,5))>=c && /accepted/ {{"
    "e=\"\"; for(i=1;i<=NF;i++) if($i==\"email:\") e=$(i+1); if(e==\"\") next; "
    "split($4,a,\":\"); k=e\" \"a[1]; if(!(k in s)){{s[k]=1; n[e]++}}}} "
    "END{{for(e in n) if(n[e]>=3) print e, n[e]}}'"
)


PROVIDER_LABELS = {"UDM Pay": "СБП", "NOWPayments": "криптовалюта", "Whop": "карта"}


class NodeMonitor:
    def __init__(self, vpn_manager, user_manager, vpn_bot=None):
        self.vpn = vpn_manager
        self.users = user_manager
        self.bot = vpn_bot
        self._fails: dict[str, int] = {}
        self._down_alerted: set[str] = set()
        self._net: dict[str, tuple[int, int, float]] = {}
        self._load: dict[str, str] = {}
        self._last_alert: dict[str, float] = {}
        self._records_path = Path(settings.NODE_TRAFFIC_RECORDS_PATH)

    # ---------- общее ----------

    async def _router_quota(self) -> None:
        """Лимит трафика роутера (settings.ROUTER_DEVICE) окнами по ROUTER_PERIOD_DAYS. Роутер может вообще
        не ходить за /sub (там копится трафик остальных), поэтому у роутеров трафик снимаем здесь, раз в цикл.
        Исчерпал — снимаем uuid с нод до конца окна; новое окно возвращает его обратно. Статус не трогаем:
        подписка оплачена, закрыт только роутер."""
        limit_mb = settings.ROUTER_TRAFFIC_LIMIT_GB * 1024
        for user in self.users.get_router_users():
            try:
                t = await self.vpn.get_user_traffic(str(user.user_id), reset=True)
            except Exception as e:
                logger.error(f"node_monitor: трафик роутера {user.user_id} не снят: {e}")
                continue
            was_exhausted = user.router_quota_exhausted()
            total = self.users.add_traffic(user.user_id, t["uplink"] // 1000000, t["downlink"] // 1000000)
            used = total - (user.traffic_period_start or 0)
            end = user.router_window_end()
            today = datetime.now().date()
            if end is None or today >= end:
                # новое окно (или первое — у тех, кто выбрал роутер до появления окон)
                self.users.update_user(user.user_id, traffic_period_start=total,
                                       traffic_period_date=today.isoformat())
                if end and used > limit_mb:  # в прошлом окне роутер отключили — возвращаем
                    await self.vpn.setup_xray(user)
                    logger.warning(f"{user.user_id}: новое окно лимита роутера, доступ возвращён")
                    await self._tell(user.user_id, "Начался новый период: роутер снова работает через VPN.")
                continue
            if used <= limit_mb or was_exhausted:
                continue
            await self.vpn.remove_client_from_xray(user.user_id)
            logger.warning(f"{user.user_id}: лимит роутера {settings.ROUTER_TRAFFIC_LIMIT_GB} ГБ исчерпан, "
                           f"роутер отключён до {end:%d.%m}")
            await self._tell(user.user_id,
                             f"Лимит роутера — {settings.ROUTER_TRAFFIC_LIMIT_GB} ГБ за {settings.ROUTER_PERIOD_DAYS} "
                             f"дней — исчерпан. Роутер снова заработает {end:%d.%m}.")

    async def _abandoned_orders(self) -> None:
        """Брошенная оплата — см. settings.ABANDONED_ORDER_MINUTES. Окно сутки: дольше смысла звать уже нет."""
        rows = self.users.execute_query(
            "SELECT o.user_id, COUNT(*) AS n, MAX(o.order_id) AS last_order, MIN(o.created_at) AS first_at, "
            "MAX(o.created_at) AS last_at, GROUP_CONCAT(DISTINCT o.provider) AS providers "
            "FROM payment_orders o WHERE o.paid_at IS NULL AND o.abandoned_alerted_at IS NULL "
            "AND o.created_at >= datetime('now', '-1 day') AND o.created_at <= datetime('now', ?) "
            "AND NOT EXISTS (SELECT 1 FROM payment_orders p WHERE p.user_id = o.user_id "
            "AND p.paid_at IS NOT NULL AND p.created_at >= o.created_at) GROUP BY o.user_id",
            (f"-{settings.ABANDONED_ORDER_MINUTES} minutes",), fetchall=True,
        ) or []
        for r in rows:
            key = f"abandoned:{r['user_id']}:{r['last_order']}"
            # отметка в самих заказах — одно письмо на заказ, и после рестарта бота тоже (11.10.26 повторялись)
            self.users.execute_query(
                "UPDATE payment_orders SET abandoned_alerted_at = datetime('now') WHERE user_id = ? "
                "AND paid_at IS NULL AND abandoned_alerted_at IS NULL AND created_at <= datetime('now', ?)",
                (r["user_id"], f"-{settings.ABANDONED_ORDER_MINUTES} minutes"),
            )
            user = self.users.get_user(r["user_id"])
            who = (f"@{user.username or '—'} ({user.first_name or ''}), язык {user.language or '?'}, "
                   f"статус {getattr(user.status, 'value', user.status)}, оплачено месяцев {user.paid_periods}"
                   if user else "нет в users (сайт?)")
            methods = ", ".join(PROVIDER_LABELS.get(p, p) for p in (r["providers"] or "").split(","))
            text = (f"Брошенная оплата: {r['user_id']} {who}. Создал ссылку на оплату ({methods}) "
                    f"{r['first_at'][:16]} UTC и не оплатил за {settings.ABANDONED_ORDER_MINUTES} мин"
                    + (f", заказов: {r['n']}" if r['n'] > 1 else "") + ".")
            # подсказка — только если она про этого человека (14.10.26 «иностранцу — крипту» приходило про
            # русскую Марию, которая и так ушла в крипту, потому что СБП не работала)
            first_ts = datetime.strptime(r["first_at"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
            if payment_outage.failed_between(r["user_id"], first_ts - 2 * 3600, time.time()):
                text += " Перед этим СБП у него не сработала (сбой UDM Pay)."
            if user and is_english(user) and "NOWPayments" not in (r["providers"] or ""):
                text += " Иностранец: СБП только для российских банков — предложить криптовалюту или карту."
            self._alert(key, f"Horizance VPN: брошенная оплата {r['user_id']}", text)
            if self.bot:
                try:
                    await self.bot.send_message(settings.OWNER_TELEGRAM_ID, "💳 " + text)
                except Exception as e:
                    logger.error(f"node_monitor: не удалось сообщить владельцу о брошенной оплате: {e}")

    async def _tell(self, user_id, text: str) -> None:
        if not self.bot:
            return
        try:
            await self.bot.send_message(user_id, text)
        except Exception as e:
            logger.error(f"{user_id}: не удалось отправить сообщение о лимите роутера: {e}")

    def _alert(self, key: str, subject: str, body: str) -> None:
        now = time.time()
        if now - self._last_alert.get(key, 0) < settings.NODE_ALERT_COOLDOWN_SEC:
            return
        self._last_alert[key] = now
        logger.warning(f"node_monitor: {subject} | {body}")
        send_email(subject=subject, body=body, to=settings.NOTIFY_EMAIL)

    async def _sudo(self, conn, server: dict, script: str):
        """sh-скрипт от root: access.log на нодах читает только root/nobody."""
        cmd = f"sudo -S -p '' sh -c {shlex.quote(script)}"
        return await conn.run(
            cmd, input=(server.get("password") or "") + "\n",
            check=False, timeout=60,
        )

    async def _probe(self, server: dict) -> dict:
        kwargs = ssh_connect_kwargs(server) | {"connect_timeout": 20}
        async with asyncssh.connect(**kwargs) as conn:
            net = await conn.run(_NET_CMD, check=False, timeout=30)
            clients = await conn.run(
                f"xray api inbounduser --server=127.0.0.1:{self.vpn.api_port} "
                f"--tag={self.vpn.inbound_tag}", check=False, timeout=30,
            )
            ips = await self._sudo(conn, server, _WATCHDOG_SCRIPT.format(
                minutes=settings.WATCHDOG_WINDOW_MIN))
        lines = net.stdout.split()
        return {
            "rx": int(lines[0]), "tx": int(lines[1]), "xray": lines[-1],
            "clients": clients.stdout if clients.exit_status == 0 else None,
            "ips": {
                e: int(n) for e, n in
                (l.split() for l in ips.stdout.splitlines() if len(l.split()) == 2)
            },
        }

    def _db_state(self) -> dict:
        """Один раз на цикл: кто должен быть в xray и кого там быть не должно."""
        expected = {}
        for table in ("users", "trial_sessions"):
            for c in clients_to_restore(table, self.users):
                expected[str(c["user_id"])] = c
        # ключи, отключённые сторожом, не возвращаем «недостающими»
        banned = device_guard.banned_users()
        for uid in banned:
            expected.pop(uid, None)
        unwanted = set(banned)
        for row in self.users.get_users_with_uuid("users"):
            if row.get("status") == "inactive":
                unwanted.add(str(row["user_id"]))
        for row in self.users.get_users_with_uuid("trial_sessions"):
            if trial_expired(row["expires_at"]):
                unwanted.add(str(row["user_id"]))
        return {"expected": expected, "unwanted": unwanted}

    # ---------- по ноде ----------

    async def check_node(self, server: dict, state: dict) -> dict:
        """Возвращает счётчики IP по ключам этой ноды (для сторожа)."""
        name = f"{server.get('name', '')} {server['host']}"
        try:
            probe = await asyncio.wait_for(self._probe(server), 90)
            if probe["xray"] != "active":
                raise RuntimeError(f"xray: {probe['xray']}")
        except Exception as e:
            self._fails[name] = self._fails.get(name, 0) + 1
            logger.error(f"node_monitor: {name} недоступна ({self._fails[name]} подряд): {type(e).__name__}: {e}")
            if self._fails[name] >= 2:
                self._down_alerted.add(name)
                await asyncio.to_thread(
                    self._alert, f"down:{name}",
                    f"Horizance VPN: нода {name} не отвечает",
                    f"{self._fails[name]} проверки подряд без ответа.\n"
                    f"Последняя ошибка: {type(e).__name__}: {e}",
                )
            return {}

        self._fails[name] = 0
        if name in self._down_alerted:
            self._down_alerted.discard(name)
            await asyncio.to_thread(
                self._alert, f"up:{name}",
                f"Horizance VPN: нода {name} снова работает", "ssh и xray отвечают.",
            )
        if probe["clients"] is not None:
            await self._sync_clients(server, name, probe["clients"], state)
        await asyncio.to_thread(self._check_traffic, name, probe["rx"], probe["tx"])
        return probe["ips"]

    async def _sync_clients(self, server: dict, name: str, raw: str, state: dict) -> None:
        present = {u.get("email") for u in json.loads(raw).get("users", [])}
        expected = state["expected"]
        missing = [c for uid, c in expected.items() if uid not in present]
        extra = sorted(present & state["unwanted"])
        if extra:
            # истёкшие триалы и неактивные: раньше их снимал только 12-часовой
            # таймер, и истёкший триал работал лишние часы
            r = await self.vpn._run_ssh([
                "xray", "api", "rmu", f"--server=127.0.0.1:{self.vpn.api_port}",
                f"--tag={self.vpn.inbound_tag}", *extra,
            ], server)
            logger.info(f"node_monitor: {name} сняты {len(extra)} (истёкшие/неактивные): {', '.join(extra)}"
                        + ("" if r.get("success") else f" — ОШИБКА {r.get('error')}"))
        if not missing:
            return
        reg = await self.vpn._register_clients(missing, server)
        ok = isinstance(reg, dict) and reg.get("success")
        await asyncio.to_thread(
            self._alert, f"clients:{name}",
            f"Horizance VPN: на ноде {name} не хватало клиентов",
            f"Не хватало {len(missing)} из {len(expected)}: "
            f"{', '.join(str(c['user_id']) for c in missing[:20])}.\n"
            f"Возвращены автоматически: {'да' if ok else 'НЕТ — ' + str(reg)}.\n"
            f"Если не хватало всех — xray на ноде перезапускался (OOM, рестарт руками).",
        )

    # ---------- трафик ----------

    def _load_records(self) -> dict:
        try:
            return json.loads(self._records_path.read_text())
        except (OSError, ValueError):
            return {}

    def _check_traffic(self, name: str, rx_bytes: int, tx_bytes: int) -> None:
        now = time.time()
        prev = self._net.get(name)
        self._net[name] = (rx_bytes, tx_bytes, now)
        if not prev or tx_bytes < prev[1]:  # первый замер или перезагрузка ноды
            return
        elapsed = now - prev[2]
        rx_mbps = (rx_bytes - prev[0]) * 8 / elapsed / 1_000_000
        mbps = (tx_bytes - prev[1]) * 8 / elapsed / 1_000_000
        node = name.split()[0]
        self._load[name] = f"{node} {mbps:.1f}/{rx_mbps:.1f}"

        records = self._load_records()
        record = records.get(node, {}).get("tx_mbps", 0)
        if mbps > record:
            records[node] = {
                "tx_mbps": round(mbps, 1),
                "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            }
            self._records_path.write_text(json.dumps(records, ensure_ascii=False, indent=1))
            logger.info(f"node_monitor: {node} новый рекорд исходящего трафика {mbps:.1f} Мбит/с (был {record})")
        threshold = settings.NODE_TRAFFIC_ALERT_SHARE * max(record, mbps)
        if mbps >= threshold and mbps >= settings.NODE_TRAFFIC_ALERT_FLOOR_MBPS:
            self._alert(
                f"traffic:{name}",
                f"Horizance VPN: высокая нагрузка на ноде {name}",
                f"Исходящий трафик {mbps:.0f} Мбит/с в среднем за {elapsed / 60:.0f} мин — "
                f"это {mbps / max(record, mbps) * 100:.0f}% от рекорда ноды ({max(record, mbps):.0f} Мбит/с).\n"
                f"Если это не вечерний пик — проверьте, нет ли раздачи ключа: "
                f"уникальные IP на email в /var/log/xray/access.log.",
            )

    # ---------- сторож ----------

    async def _watchdog(self, per_node: list[dict]) -> None:
        totals: dict[str, int] = {}
        for ips in per_node:
            for email, n in (ips or {}).items():
                totals[email] = totals.get(email, 0) + n
        for email, n in totals.items():
            if n <= settings.WATCHDOG_WARN_IPS:
                continue
            if n <= settings.WATCHDOG_CUT_IPS:
                logger.warning(
                    f"node_monitor: по ключу {email} за {settings.WATCHDOG_WINDOW_MIN} мин "
                    f"{n} разных IP (warning > {settings.WATCHDOG_WARN_IPS})"
                )
                continue
            await self.vpn.remove_client_from_xray(email)
            if email.startswith("t_"):
                # expires_at = сейчас: иначе мониторинг вернул бы триал обратно
                self.users.execute_query(
                    "UPDATE trial_sessions SET expires_at = ? WHERE user_id = ?",
                    (datetime.now(timezone.utc).isoformat(sep=" ", timespec="minutes"), email),
                )
                how = "Триал закрыт."
            else:
                # пользователя бота не трогаем в базе (статус, оплату) — только бан
                # ключа: мониторинг его не вернёт, /sub отдаёт заглушку
                device_guard.ban("user", email, f"сторож: {n} IP за {settings.WATCHDOG_WINDOW_MIN} мин")
                user = self.users.get_user(int(email))
                who = f"статус {getattr(user.status, 'value', user.status)}, оплачено месяцев {user.paid_periods}" if user else "нет в users"
                how = (f"Пользователь бота ({who}): ключ в бан-листе (таблица banned, kind='user'). "
                       "Вернуть — удалить эту строку, мониторинг сам зарегистрирует его обратно.")
            await asyncio.to_thread(
                self._alert, f"watchdog:{email}",
                f"Horizance VPN: ключ {email} раздают ({n} IP) — отключён",
                f"За последние {settings.WATCHDOG_WINDOW_MIN} мин по ключу {email} "
                f"подключались {n} разных IP (порог отключения {settings.WATCHDOG_CUT_IPS}). "
                f"{how}\nОбычно у одного человека 1–5 IP.",
            )

    async def notify_recovered(self) -> list[int]:
        """Оплата заработала: пишем всем, кто пытался оплатить за время сбоя (СБП — payment_outage.users,
        крипта и прочее — неоплаченные заказы в payment_orders с даты since), если они с тех пор не оплатили."""
        from bot import plan_choice_buttons  # локально: bot тянет Telethon и много лишнего
        data = payment_outage.load()
        since = data["since"] or "2026-10-10"
        rows = self.users.execute_query(
            "SELECT DISTINCT user_id FROM payment_orders WHERE paid_at IS NULL AND created_at >= ?",
            (since,), fetchall=True,
        ) or []
        ids = (set(data["users"]) | {r["user_id"] for r in rows}) - {settings.OWNER_TELEGRAM_ID}
        sent = []
        for uid in sorted(ids):
            user = self.users.get_user(uid)
            if user is None or self._paid_since(uid, since):
                continue
            en = is_english(user)
            rows_btn = [[{"text": b.text, "callback_data": b.data.decode()}] for row in plan_choice_buttons(english=en, extra=bool(user.extra_devices))
                        for b in row]
            text = tr(user, "Оплата снова работает. Выберите тариф ниже — и вам придёт ссылка на оплату.",
                      "Payments are working again. Choose a plan below and you will get a payment link.")
            if await send_telegram_api_message(uid, text, buttons=rows_btn, parse_mode=None):
                sent.append(uid)
        for item in data["queued"]:
            if await send_telegram_api_message(item["user_id"], item["text"], parse_mode=None):
                sent.append(item["user_id"])
        data["queued"] = []
        data["users"] = []
        data["since"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        payment_outage.save(data)
        return sent

    def _paid_since(self, user_id: int, since: str) -> bool:
        return bool(self.users.execute_query(
            "SELECT 1 FROM payment_orders WHERE user_id = ? AND paid_at IS NOT NULL AND paid_at >= ?",
            (user_id, since), fetchone=True))

    async def run_udmpay_watch(self) -> None:
        """Пинг UDM Pay раз в UDMPAY_WATCH_INTERVAL_SEC: письмо владельцу, когда лёг и когда поднялся, и рассылка
        тем, кто пытался оплатить, когда он поднялся. Лежит = нет HTTP-ответа вообще (код ответа корня API
        в рабочем режиме не проверен — на 5xx не опираемся, чтобы не объявить вечный сбой); два промаха подряд (и два
        успеха подряд для «снова онлайн»), чтобы не реагировать на один сбой. Состояние хранится в payment_outage.json
        (переживает рестарт бота). Каждая проверка пишется в my_logger/stats/udmpay_uptime.csv.
        Проверка — запрос /order/status с нашим логином (udmpay.health_check), а не пинг хоста: 14.10.26 хост ожил,
        а магазина в нём не было (404 Accounts\\Account), и пинг разослал «оплата снова работает» зря."""
        import udmpay
        url = udmpay._base_url()
        log = Path(__file__).resolve().parent / "my_logger" / "stats" / "udmpay_uptime.csv"
        misses = oks = 0
        while True:
            t0, code = time.time(), ""
            last = self.users.execute_query(
                "SELECT order_id FROM payment_orders WHERE provider = 'UDM Pay' AND paid_at IS NOT NULL "
                "ORDER BY paid_at DESC LIMIT 1", fetchone=True)
            try:
                err = await udmpay.health_check(last["order_id"] if last else None)
            except Exception as e:  # пинг не должен ронять цикл
                err = type(e).__name__
            try:
                new = not log.exists()
                with log.open("a", newline="") as f:
                    w = csv.writer(f)
                    if new:
                        w.writerow(["utc", "ok", "error", "sec", "http"])
                    w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), int(not err), err,
                                round(time.time() - t0, 1), code])
            except OSError as e:
                logger.error(f"udmpay_watch: не записать {log}: {e}")
            misses, oks = (misses + 1, 0) if err else (0, oks + 1)
            state = payment_outage.load()
            if misses >= 2 and state["down_since"] is None:
                state["down_since"] = time.time() - settings.UDMPAY_WATCH_INTERVAL_SEC
                state["since"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                payment_outage.save(state)
                send_email(subject="Horizance VPN: UDM Pay недоступен",
                           body=(f"UDM Pay не принимает наш логин: магазин не найден (HTTP 404 Accounts\\Account) — "
                                 f"нужно писать в поддержку UDM Pay." if err == "account_missing" else
                                 f"{url} не отвечает ({err}).") + " Оплата по СБП на сайте и в боте не работает.",
                           to=settings.NOTIFY_EMAIL)
                logger.warning(f"udmpay_watch: UDM Pay недоступен ({err})")
            elif oks >= 2 and state["down_since"] is None and state["users"]:
                # сбоя нет, а попытки накопились — разовые таймауты; иначе следующая рассылка ушла бы и им
                state["users"] = []
                payment_outage.save(state)
            elif oks >= 2 and state["down_since"] is not None:
                mins = round((time.time() - state["down_since"]) / 60)
                state["down_since"] = None
                payment_outage.save(state)
                sent = await self.notify_recovered()
                send_email(subject="Horizance VPN: UDM Pay снова онлайн",
                           body=f"UDM Pay отвечает. Был недоступен примерно {mins} мин. "
                                f"Рассылка «оплата заработала» отправлена {len(sent)} пользователям: {sent}.",
                           to=settings.NOTIFY_EMAIL)
                logger.warning(f"udmpay_watch: UDM Pay снова онлайн, простой ~{mins} мин, рассылка: {sent}")
            await asyncio.sleep(settings.UDMPAY_WATCH_INTERVAL_SEC)

    async def run(self) -> None:
        while True:
            try:
                state = self._db_state()  # один раз на цикл, а не на каждую ноду
                self._load.clear()
                per_node = await asyncio.gather(
                    *(self.check_node(s, state) for s in credentials.SERVERS),
                    return_exceptions=True,
                )
                await self._watchdog([r for r in per_node if isinstance(r, dict)])
                await self._router_quota()
                await self._abandoned_orders()
                if self._load:
                    # одна строка на цикл: история нагрузки нод
                    logger.info(
                        "Нагрузка нод, Мбит/с исходящий/входящий: "
                        + ", ".join(self._load.values())
                    )
            except Exception as e:
                logger.error(f"node_monitor: ошибка цикла: {e}")
            await asyncio.sleep(settings.NODE_MONITOR_INTERVAL_SEC)
