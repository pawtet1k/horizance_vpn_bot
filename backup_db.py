"""Резервная копия баз раз в 6 часов (crontab пользователя pashavpsadm, 14.10.26).

sqlite3 .backup — согласованная копия на ходу, даже если бот и сайт в этот момент пишут (простой cp может
скопировать базу посреди записи). Копия проверяется (integrity_check), сжимается и кладётся в BACKUP_DIR вне
папки проекта — агент, чистящий проект, её не заденет. Храним 28 последних (неделя). Каждая копия ещё и
уходит на MX (решение владельца 14.10.26) в ту же папку ~/backups/horizance_vpn_bot — на случай потери NL.
"""
import asyncio
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


async def upload(files: list[Path]) -> None:
    """Копирует files на MX по SFTP и оставляет там те же KEEP последних копий каждой базы."""
    import asyncssh
    import credentials
    mx = next(s for s in credentials.SERVERS if s["name"] == "MX")
    remote = "backups/horizance_vpn_bot"
    async with asyncssh.connect(mx["host"], port=mx["port"], username=mx["username"], password=mx["password"],
                                known_hosts=None) as conn:
        await conn.run(f"mkdir -p -m 700 {remote}", check=True)
        async with conn.start_sftp_client() as sftp:
            await sftp.put([str(f) for f in files], remote)
        for db in DATABASES:
            await conn.run(f"ls -1 {remote}/{db.stem}_*.db.gz | head -n -{KEEP} | xargs -r rm --", check=True)


if __name__ == "__main__":
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    failed = False
    made = []
    for db in DATABASES:
        try:
            made.append(backup(db))
            print(datetime.now().isoformat(timespec="seconds"), made[-1])
        except Exception as e:
            failed = True
            print(datetime.now().isoformat(timespec="seconds"), db, "ОШИБКА:", e)
    if made:
        try:
            asyncio.run(upload(made))
            print(datetime.now().isoformat(timespec="seconds"), "на MX:", ", ".join(f.name for f in made))
        except Exception as e:
            failed = True
            print(datetime.now().isoformat(timespec="seconds"), "MX ОШИБКА:", repr(e))
    if failed:
        from utils import send_email
        import settings
        send_email(subject="Horizance VPN: бэкап базы не удался", body="См. my_logger/backup_db.log", to=settings.NOTIFY_EMAIL)
        sys.exit(1)
