import html
import re

_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_SAFE_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _render_link(match: re.Match) -> str:
    label, url = match.group(1), match.group(2)
    if not _SAFE_URL_RE.match(url):
        # Схема отличная от http(s) (javascript:, data: и т.п.) — не превращаем
        # в кликабельную ссылку, чтобы не пустить XSS через имя/юзернейм Telegram.
        return match.group(0)
    return f'<a href="{url}" target="_blank" rel="noopener">{label}</a>'


def to_html(text: str | None) -> str:
    """Renders the small subset of Telegram markdown used in bot.py/subscription.py
    message copy (**bold** and [label](url) links) as safe HTML for the website."""
    if not text:
        return ""
    escaped = html.escape(text)
    escaped = _LINK_RE.sub(_render_link, escaped)
    escaped = _BOLD_RE.sub(r"<strong>\1</strong>", escaped)
    return escaped.replace("\n", "<br>")
