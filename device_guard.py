"""Лимиты на триал и число устройств на подписку, бан-лист.

Зачем: 22.09.26 одно устройство за 9 минут взяло три триала (два с одного
хостингового IP, третий — через нашу же DE-ноду), и ключи ушли в Иран на
~13 тыс. IP. Ноды US и DE упали по OOM, MX — по памяти TCP. Лимит «3 триала
в час с IP» жил в памяти процесса и ничего не остановил: атакующему хватило
трёх ключей.

Что здесь закрыто, а что нет: всё проверяется при выдаче триала и при запросе
подписки /sub. Кто уже получил голый vless-ключ, дальше ходит прямо на ноды,
минуя /sub, — такую раздачу отсюда не увидеть (это задача подсчёта IP на
ключ по access.log xray).

Таблицы живут в users.db рядом с users/trial_sessions, создаются при импорте.
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import credentials
import settings
from my_logger import logger

DB_PATH = str(Path(__file__).resolve().parent / "users.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables() -> None:
    with _connect() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS devices (
            user_id TEXT NOT NULL,
            hwid TEXT NOT NULL,
            model TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            PRIMARY KEY (user_id, hwid)
        )''')
        try:
            # устройство, которое владелец подписки убрал сам (/devices): слот свободен, а самому устройству
            # /sub отдаёт заглушку, пока его не вернут — иначе при первом же обновлении оно заняло бы слот снова
            conn.execute("ALTER TABLE devices ADD COLUMN removed_at TEXT")
        except sqlite3.OperationalError:
            pass  # столбец уже есть
        conn.execute('''CREATE TABLE IF NOT EXISTS banned (
            kind TEXT NOT NULL,          -- 'ip' | 'hwid'
            value TEXT NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (kind, value)
        )''')


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M+00:00")


def _cutoff_date(days: int) -> str:
    # created_at в trial_sessions исторически в двух форматах
    # ('2026-09-22 21:27+00:00' и ISO с 'T'), общее у них — дата в первых
    # 10 символах, поэтому сравниваем по дате, а не по полной строке
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def ban(kind: str, value: str, reason: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO banned (kind, value, reason, created_at) "
            "VALUES (?, ?, ?, ?)", (kind, value, reason, _now())
        )
    logger.warning(f"БАН: {kind}={value} ({reason})")


def is_banned(ip: str | None = None, hwid: str | None = None,
              user_id: str | None = None) -> bool:
    """kind='user' — ключ, отключённый сторожом раздачи (node_monitor)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM banned WHERE (kind='ip' AND value=?) "
            "OR (kind='hwid' AND value=?) OR (kind='user' AND value=?)",
            (ip, hwid, None if user_id is None else str(user_id))
        ).fetchone()
    return row is not None


def banned_users() -> set[str]:
    with _connect() as conn:
        return {r["value"] for r in conn.execute("SELECT value FROM banned WHERE kind='user'")}


def is_node_ip(ip: str) -> bool:
    """Запрос триала через нашу же ноду = человек уже сидит в нашем VPN."""
    return ip in {s["host"] for s in credentials.SERVERS}


def trial_refusal(ip: str) -> str | None:
    """Причина отказа в триале для этого IP или None, если выдавать можно."""
    if is_banned(ip=ip):
        return "banned"
    if is_node_ip(ip):
        return "node_ip"
    # считаем только триалы, которыми пользовались (приложение забрало
    # подписку или пошёл трафик): 22–23.09.26 выдача висела, люди жали кнопку
    # по 3 раза и получали по 3 пустых триала — иначе лимит отрезал бы их
    with _connect() as conn:
        issued = conn.execute(
            "SELECT COUNT(*) FROM trial_sessions "
            "WHERE (client_ip = ? OR client_ip LIKE ?) "
            "AND substr(created_at, 1, 10) >= ? "
            "AND (fingerprint IS NOT NULL OR COALESCE(traffic_out, 0) > 0)",
            (ip, f"{ip}|%", _cutoff_date(settings.TRIAL_WINDOW_DAYS))
        ).fetchone()[0]
    if issued >= settings.TRIAL_IP_LIMIT:
        return "ip_limit"
    return None


def recent_trial_uuid(ip: str, minutes: int = 10) -> str | None:
    """uuid триала, выданного этому IP только что, — повторный клик получает
    его же, а не ещё один триал."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT client_uuid, created_at FROM trial_sessions "
            "WHERE client_ip = ? OR client_ip LIKE ? "
            "ORDER BY created_at DESC LIMIT 1", (ip, f"{ip}|%")
        ).fetchone()
    if not row:
        return None
    created = datetime.fromisoformat(row["created_at"])
    if datetime.now(timezone.utc) - created <= timedelta(minutes=minutes):
        return row["client_uuid"]
    return None


def _other_trial_with_hwid(user_id: str, hwid: str) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT user_id FROM trial_sessions WHERE fingerprint LIKE ? "
            "AND user_id != ? AND substr(created_at, 1, 10) >= ?",
            (f"%|{hwid}", user_id, _cutoff_date(settings.TRIAL_WINDOW_DAYS))
        ).fetchone()
    return row["user_id"] if row else None


def extra_devices_active(until: str | None) -> bool:
    """Докупленные устройства (settings.EXTRA_DEVICES_*) действуют по дату until (dd.mm.yy) включительно."""
    if not until:
        return False
    try:
        return datetime.strptime(until, "%d.%m.%y").date() >= datetime.now().date()
    except ValueError:
        return False


def _active_hwids(conn, user_id: str) -> list[str]:
    """Активные устройства по порядку подключения; служебные (DEVICE_LIMIT_EXEMPT_HWIDS) в лимит не входят."""
    return [r["hwid"] for r in conn.execute(
        "SELECT hwid FROM devices WHERE user_id=? AND removed_at IS NULL ORDER BY first_seen, hwid", (user_id,))
        if r["hwid"] not in settings.DEVICE_LIMIT_EXEMPT_HWIDS]


def device_limit(user: dict) -> int:
    extra = settings.EXTRA_DEVICES_COUNT if extra_devices_active(user.get("extra_devices_until")) else 0
    return settings.DEVICE_LIMIT + extra


def list_devices(user_id) -> list[dict]:
    """Устройства подписки для /devices: сначала активные (по первому появлению), потом убранные."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT hwid, model, first_seen, last_seen, removed_at FROM devices WHERE user_id=? "
            "ORDER BY removed_at IS NOT NULL, first_seen", (str(user_id),)
        ).fetchall()
    return [dict(r) for r in rows if r["hwid"] not in settings.DEVICE_LIMIT_EXEMPT_HWIDS]


def remove_device(user_id, hwid: str) -> bool:
    with _connect() as conn:
        changed = conn.execute(
            "UPDATE devices SET removed_at=? WHERE user_id=? AND hwid=? AND removed_at IS NULL",
            (_now(), str(user_id), hwid)
        ).rowcount
    if changed:
        logger.info(f"{user_id}: устройство hwid={hwid} убрано владельцем подписки")
    return bool(changed)


def restore_device(user: dict, hwid: str) -> str | None:
    """Вернуть убранное устройство. None — вернули; 'device_limit' — нет свободного слота; 'unknown' — нет такого."""
    user_id = str(user["user_id"])
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM devices WHERE user_id=? AND hwid=? AND removed_at IS NOT NULL", (user_id, hwid)
        ).fetchone()
        if not row:
            return "unknown"
        if len(_active_hwids(conn, user_id)) >= device_limit(user):
            return "device_limit"
        conn.execute("UPDATE devices SET removed_at=NULL WHERE user_id=? AND hwid=?", (user_id, hwid))
    logger.info(f"{user_id}: устройство hwid={hwid} возвращено владельцем подписки")
    return None


def device_refusal(user: dict, hwid: str, model: str) -> str | None:
    """Проверяет устройство при запросе /sub и запоминает его.

    None — отдавать подписку как обычно; иначе причина отказа:
    'banned', 'trial_reuse' (второй триал с того же телефона за
    TRIAL_WINDOW_DAYS), 'device_limit' (больше device_limit(user) устройств),
    'device_removed' (владелец подписки убрал это устройство в /devices).
    Уже известное устройство пропускается всегда: заглушка должна доставаться
    только новому, а не выбивать того, кто пользовался первым.
    """
    user_id = str(user["user_id"])
    if hwid in settings.DEVICE_LIMIT_EXEMPT_HWIDS:
        return None
    if is_banned(hwid=hwid):
        return "banned"
    now = _now()
    with _connect() as conn:
        known = conn.execute(
            "SELECT removed_at FROM devices WHERE user_id=? AND hwid=?", (user_id, hwid)
        ).fetchone()
        if known:
            conn.execute(
                "UPDATE devices SET last_seen=? WHERE user_id=? AND hwid=?",
                (now, user_id, hwid)
            )
            if known["removed_at"]:
                return "device_removed"
            # доп. устройства закончились, а подключено больше лимита: работают первые по времени подключения,
            # остальным заглушка, пока владелец не уберёт лишние в /devices или не продлит опцию
            return "device_limit" if hwid in _active_hwids(conn, user_id)[device_limit(user):] else None
        count = len(_active_hwids(conn, user_id))
    if user_id.startswith("t_"):
        other = _other_trial_with_hwid(user_id, hwid)
        if other:
            logger.warning(
                f"ПОВТОРНЫЙ ТРИАЛ: {user_id} hwid={hwid} уже брал {other}"
            )
            return "trial_reuse"
    limit = device_limit(user)
    if count >= limit:
        logger.warning(
            f"УСТРОЙСТВО СВЕРХ ЛИМИТА {limit}: {user_id} уже {count}, "
            f"отказ hwid={hwid} ({model})"
        )
        return "device_limit"
    with _connect() as conn:
        conn.execute(
            "INSERT INTO devices (user_id, hwid, model, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?)", (user_id, hwid, model, now, now)
        )
    logger.info(f"{user_id}: новое устройство {count + 1}/{limit} hwid={hwid} ({model})")
    return None


init_tables()
