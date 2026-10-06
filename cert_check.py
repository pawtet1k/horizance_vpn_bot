"""Ежедневная проверка сроков TLS-сертификатов на серверах (не на Cloudflare).

Зачем: horizance.com за Cloudflare. 05.10.26 истёк сертификат Let's Encrypt на самом сервере MX
(certbot там не был установлен после переезда на новый инстанс 24.08), Cloudflare сутки отдавал 526,
а в браузере при этом был виден живой сертификат Cloudflare — просрочку никто не заметил.
Скрипт ходит на IP серверов напрямую, с нужным SNI, и пишет на почту, если до конца < ALERT_DAYS.

Запуск: /etc/cron.d/horizance-cert-check (ежедневно 09:00).
"""
import socket
import ssl
from datetime import datetime, timezone

import settings
from utils import send_email

ALERT_DAYS = 21
# (IP сервера, имя сайта): проверяем исходный сертификат, а не сертификат Cloudflare
ORIGINS = [
    ("193.233.127.32", "horizancevpn.com"),
    ("193.233.127.32", "horizance.ru"),
    ("163.192.140.167", "horizance.com"),
]


def days_left(ip: str, host: str) -> float:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # нужен срок даже у просроченного сертификата
    with socket.create_connection((ip, 443), timeout=15) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    from cryptography import x509
    not_after = x509.load_der_x509_certificate(der).not_valid_after_utc
    return (not_after - datetime.now(timezone.utc)).total_seconds() / 86400


def main():
    problems, report = [], []
    for ip, host in ORIGINS:
        try:
            left = days_left(ip, host)
            report.append(f"{host} ({ip}): осталось {left:.0f} дн.")
            if left < ALERT_DAYS:
                problems.append(f"{host} на {ip}: сертификат истекает через {left:.0f} дн.")
        except Exception as e:  # недоступность сервера тоже повод написать
            problems.append(f"{host} на {ip}: не удалось проверить сертификат: {e!r}")
    print("\n".join(report + problems))
    if problems:
        send_email("Horizance: сертификаты сайтов требуют внимания",
                   "\n".join(problems) + "\n\nВсе проверки:\n" + "\n".join(report),
                   settings.NOTIFY_EMAIL)


if __name__ == "__main__":
    main()
