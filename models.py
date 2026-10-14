from datetime import datetime, timedelta, timezone
from enum import Enum
import settings
import credentials

def trial_expired(expires_at: str, now: datetime | None = None) -> bool:
    """Истёк ли триал.

    expires_at хранится в двух форматах: старый ISO с 'T' и нынешний с
    пробелом ('2026-09-23 21:27+00:00', см. TrialUser). Раньше его сравнивали
    строкой с datetime.isoformat() (с 'T'), а ' ' < 'T', поэтому в день
    окончания триал считался истёкшим уже с 00:00 UTC — до 21 часа раньше
    срока. Сравниваем как даты.
    """
    now = now or datetime.now(timezone.utc)
    return datetime.fromisoformat(expires_at) <= now


class UserStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    TRIAL = "trial"
    CANCELED = "canceled"
    FREE = "free"

    def label_ru(self) -> str:
        return {
            UserStatus.ACTIVE: "🟢 Активен",
            UserStatus.INACTIVE: "🔴 Неактивен",
            UserStatus.TRIAL: "🟡 Пробный",
            UserStatus.CANCELED: "🟡 Отменён",
            UserStatus.FREE: "🎁 Бесплатный",
        }[self]

    def label_en(self) -> str:
        return {
            UserStatus.ACTIVE: "🟢 Active",
            UserStatus.INACTIVE: "🔴 Inactive",
            UserStatus.TRIAL: "🟡 Trial",
            UserStatus.CANCELED: "🟡 Cancelled",
            UserStatus.FREE: "🎁 Free",
        }[self]

def _subscription_defaults() -> dict:
    """Общие поля VPN-подписки, одинаковые для User и WebUser."""
    return {
        'referrals': 0,
        'paid_periods': 0,
        'price': settings.default_price,
        'client_ip': None,
        'device_os': None,
        'client_uuid': None,
        'start_date': datetime.now().strftime("%d.%m.%y"),
        'next_payment_date': (
            datetime.now() + timedelta(days=settings.trial_days)
        ).strftime("%d.%m.%y"),
        'status': UserStatus.TRIAL,
        'payment_token': None,
        'pending_order_uid': None,
        # subscriptionUid, который UDM Pay возвращает уже в ответе на
        # sbp-subscription/create-with-payment. По нему вебхук о привязке
        # находит владельца — см. db.get_user_by_pending_subscription_uid.
        'pending_subscription_uid': None,
        'vpn_protocol': 'VLESS+Reality',
        'server_public_key': credentials.SERVER_PUBLIC_KEY,
        'server_ip': None,
        'fingerprint': None,
        'traffic_in': None,
        'traffic_out': None,
        # роутер: трафик (МБ, in+out) и дата (ISO) начала текущего окна лимита — см. settings.ROUTER_DEVICE
        'traffic_period_start': None,
        'traffic_period_date': None,
        'notifications': None,
        # докупленные устройства действуют по эту дату включительно (dd.mm.yy) — см. settings.EXTRA_DEVICES_*
        'extra_devices_until': None,
        # 1 — опция «доп. устройства» подключена и продлевается вместе с подпиской (settings.EXTRA_DEVICES_*)
        'extra_devices': 0,
    }


class User:
    def __init__(
        self,
        user_id,
        username,
        first_name,
        last_name,
        phone,
        language,
        referrer_id,
        **kwargs
    ):
        self.user_id = user_id
        self.username = username
        self.first_name = first_name
        self.last_name = last_name
        self.phone = phone
        self.language = language
        self.referrer_id = referrer_id

        for key, value in _subscription_defaults().items():
            setattr(self, key, kwargs.get(key, value))

        if not isinstance(self.status, UserStatus):
            self.status = UserStatus(self.status)

    # ---- роутер (settings.ROUTER_DEVICE): лимит трафика окнами по ROUTER_PERIOD_DAYS ----

    def traffic_total_mb(self) -> int:
        return (self.traffic_in or 0) + (self.traffic_out or 0)

    def router_window_end(self):
        """Дата, с которой начинается следующее окно лимита (None — окно ещё не открыто)."""
        if not self.traffic_period_date:
            return None
        return (datetime.fromisoformat(self.traffic_period_date)
                + timedelta(days=settings.ROUTER_PERIOD_DAYS)).date()

    def router_quota_exhausted(self) -> bool:
        end = self.router_window_end()
        if end is None or datetime.now().date() >= end:
            return False  # окна нет или оно кончилось — node_monitor откроет новое
        used = self.traffic_total_mb() - (self.traffic_period_start or 0)
        return used > settings.ROUTER_TRAFFIC_LIMIT_GB * 1024


class WebUser:
    """Аккаунт личного кабинета на сайте (вход через Яндекс ID).

    user_id — настоящий numeric id аккаунта Яндекс ID (как user_id у User —
    настоящий Telegram id), а не синтетический, как у анонимных TrialUser.
    """
    def __init__(
        self,
        user_id,
        login,
        first_name,
        last_name,
        email,
        referrer_id,
        **kwargs
    ):
        self.user_id = user_id
        self.login = login
        self.first_name = first_name
        self.last_name = last_name
        self.email = email
        self.referrer_id = referrer_id

        for key, value in _subscription_defaults().items():
            setattr(self, key, kwargs.get(key, value))

        if not isinstance(self.status, UserStatus):
            self.status = UserStatus(self.status)


class TrialUser:
    def __init__(
        self,
        user_id,
        fingerprint,
        device_os,
        client_uuid,
        client_ip
    ):
        now = datetime.now(timezone.utc)
        self.user_id = user_id
        self.fingerprint = fingerprint
        self.device_os = device_os
        self.client_uuid = client_uuid
        self.client_ip = client_ip
        self.created_at = now.isoformat(sep=" ", timespec="minutes")
        self.expires_at = (now + timedelta(days=1)).isoformat(
            sep=" ", timespec="minutes"
        )
        self.traffic_in = None
        self.traffic_out = None
