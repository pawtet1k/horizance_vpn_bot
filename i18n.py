"""Язык сообщений бота: русский или английский.

Решение владельца (10.10.26): если человек не с российского IP и язык у него не русский — бот и все наши
сообщения на английском. Бот IP пользователя не видит; адрес появляется только когда приложение впервые забрало
подписку (users.client_ip), и часто это выход чужого VPN. Поэтому английский — когда выполнено всё сразу:
  * язык Telegram задан и это не язык, на котором у нас обычно читают по-русски (RU_LANGS);
  * в имени нет кириллицы — у многих русских Telegram на английском («en»), но имя «Иван»;
  * адрес, если известен, не российский.
Иностранцы до этого получали всё по-русски: 18.09 Shailesh четырежды пытался оплатить СБП и ушёл.
"""
import re

import requests

from my_logger import logger

RU_LANGS = {"ru", "uk", "be", "kk"}
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_ip_country: dict[str, str | None] = {}


def _country(ip: str) -> str | None:
    if ip not in _ip_country:
        try:
            r = requests.get(f"http://ip-api.com/json/{ip}?fields=countryCode", timeout=2)
            _ip_country[ip] = r.json().get("countryCode")
        except Exception as e:
            logger.warning(f"страна по ip {ip} не определена: {e}")
            return None  # не кешируем: в следующий раз спросим снова
    return _ip_country[ip]


def is_english(user) -> bool:
    if user is None:
        return False
    forced = getattr(user, "force_lang", None)  # сайт: язык страницы, а не профиля (actions.pay_plan)
    if forced in ("en", "ru"):
        return forced == "en"
    lang = (getattr(user, "language", None) or "").lower().split("-")[0]
    if not lang or lang in RU_LANGS:
        return False
    name = f"{getattr(user, 'first_name', '') or ''} {getattr(user, 'last_name', '') or ''}"
    if _CYRILLIC.search(name):
        return False
    ip = (getattr(user, "client_ip", None) or "").split("|")[0].strip()
    if ip and _country(ip) == "RU":
        return False
    return True


def _user(user_or_id):
    if user_or_id is None or hasattr(user_or_id, "language"):
        return user_or_id
    from db import UserManager  # локально: db тянет models, а bot/subscription импортируют i18n рано
    return UserManager().get_user(user_or_id)


def tr(user_or_id, ru: str, en: str) -> str:
    """Текст на языке пользователя. Принимает объект User или user_id."""
    return en if is_english(_user(user_or_id)) else ru
