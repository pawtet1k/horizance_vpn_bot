"""Download Xray geo assets, then restart the service."""
import subprocess
import tempfile
import urllib.request
from pathlib import Path

RELEASE = (
    "https://github.com/Loyalsoldier/v2ray-rules-dat"
    "/releases/latest/download"
)
ASSET_DIR = Path("/usr/local/share/xray")
FILES = ("geosite.dat", "geoip.dat")
READABLE = 0o644


def download(url: str, dest: Path, timeout: float = 30.0) -> None:
    """Fetch url to dest atomically via a temp file."""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        payload = resp.read()
    with tempfile.NamedTemporaryFile(
        dir=dest.parent, delete=False
    ) as tmp:
        tmp.write(payload)
        tmp_path = Path(tmp.name)
    tmp_path.replace(dest)
    dest.chmod(READABLE)


def update() -> None:
    """Refresh every asset, then restart Xray."""
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        download(f"{RELEASE}/{name}", ASSET_DIR / name)
    subprocess.run(["systemctl", "restart", "xray"], check=True)


if __name__ == "__main__":
    update()
