"""Резервная копия баз раз в 6 часов (crontab пользователя pashavpsadm, 14.10.26).

sqlite3 .backup — согласованная копия на ходу, даже если бот и сайт в этот момент пишут (простой cp может
скопировать базу посреди записи). Копия проверяется (integrity_check), сжимается и кладётся в BACKUP_DIR вне
папки проекта — агент, чистящий проект, её не заденет. Храним 28 последних (неделя). Копии вне сервера — нет:
от поломки сервера это не защищает, см. ответ владельцу 14.10.26.
"""
import gzip
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKUP_DIR = Path.home() / "backups" / "horizance_vpn_bot"
DATABASES = [ROOT / "users.db", ROOT / "my_logger" / "stats" / "telemetry.db"]
KEEP = 28


def backup(db: Path) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    tmp = BACKUP_DIR / f"{db.stem}_{stamp}.db"
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as src, sqlite3.connect(tmp) as dst:
        src.backup(dst)
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"{tmp}: integrity_check не ok")
    out = tmp.with_suffix(".db.gz")
    with tmp.open("rb") as f, gzip.open(out, "wb") as g:
        shutil.copyfileobj(f, g)
    tmp.unlink()
    for old in sorted(BACKUP_DIR.glob(f"{db.stem}_*.db.gz"))[:-KEEP]:
        old.unlink()
    return out


if __name__ == "__main__":
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    failed = False
    for db in DATABASES:
        try:
            print(datetime.now().isoformat(timespec="seconds"), backup(db))
        except Exception as e:
            failed = True
            print(datetime.now().isoformat(timespec="seconds"), db, "ОШИБКА:", e)
    if failed:
        from utils import send_email
        import settings
        send_email(subject="Horizance VPN: бэкап базы не удался", body="См. my_logger/backup_db.log", to=settings.NOTIFY_EMAIL)
        sys.exit(1)
