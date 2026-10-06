import time
from collections import defaultdict

from fastapi import Request

# nginx (единственный доверенный обратный прокси перед uvicorn) всегда
# подключается с localhost — см. `proxy_pass http://127.0.0.1:8001` в конфиге.
# Доверять X-Forwarded-For/X-Real-IP можно только если TCP-соединение
# пришло оттуда, иначе любой, кто достучится до аппа напрямую, подделает IP.
TRUSTED_PROXY_IPS = {"127.0.0.1", "::1"}


def get_client_ip(request: Request) -> str:
    direct_ip = request.client.host if request.client else None
    if direct_ip in TRUSTED_PROXY_IPS:
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()  # может быть цепочка прокси
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip
    return direct_ip or "unknown"


_rate_limit_buckets: dict[str, list[float]] = defaultdict(list)


def is_rate_limited(key: str, max_calls: int, window_seconds: int) -> bool:
    """Простой sliding-window лимитер в памяти процесса (без внешних зависимостей).
    Не шарится между воркерами — рассчитан на однопроцессный uvicorn, как сейчас."""
    now = time.monotonic()
    bucket = _rate_limit_buckets[key]
    cutoff = now - window_seconds
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    if len(bucket) >= max_calls:
        return True
    bucket.append(now)
    return False
