from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, load_dotenv, set_key

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
ENV_EXAMPLE_PATH = PROJECT_ROOT / ".env.example"
NOTIFY_MODES = ("channel", "dm", "both")


def read_env() -> dict[str, str]:
    """Current values in .env (empty dict if it doesn't exist yet)."""
    if not ENV_PATH.exists():
        return {}
    return {k: v or "" for k, v in dotenv_values(ENV_PATH).items()}


def save_env(values: dict[str, str]) -> None:
    """Write keys into .env, keeping its other lines and comments.

    A new .env starts from .env.example so the remaining options stay documented.
    """
    if not ENV_PATH.exists():
        template = ENV_EXAMPLE_PATH.read_text(encoding="utf-8") if ENV_EXAMPLE_PATH.exists() else ""
        ENV_PATH.write_text(template, encoding="utf-8")
    for key, value in values.items():
        set_key(ENV_PATH, key, value, quote_mode="never")


def claude_cache_path() -> Path:
    # Must match statusline/claude_statusline.py
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    return Path(base) / "llm-usage-bot" / "claude_rate_limits.json"


_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")  # zero-width / bidi marks
_MENTION = re.compile(r"<[@#][!&]?(\d+)>")


def parse_ids(text: str) -> list[int]:
    """Parse Discord IDs as people actually paste them.

    Tolerates full-width digits, invisible characters copied along from
    Discord, mentions like <@123>, and spaces / newlines / ， / 、 as separators.
    Raises ValueError naming the first part that isn't an ID.
    """
    text = _INVISIBLE.sub("", unicodedata.normalize("NFKC", text))
    text = _MENTION.sub(r"\1", text)
    ids = []
    for part in re.split(r"[\s,;、]+", text):
        if not part:
            continue
        if not part.isdigit():
            raise ValueError(part)
        ids.append(int(part))
    return ids


def _int(name: str) -> int | None:
    try:
        ids = parse_ids(os.environ.get(name, ""))
    except ValueError as e:
        raise SystemExit(f"{name} 的「{e}」不是 Discord ID") from e
    return ids[0] if ids else None


def _str(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


@dataclass(frozen=True)
class Config:
    guild_id: int | None
    allowed_user_ids: frozenset[int]  # empty: anyone in guild_id may use /usage

    bot_token: str
    notify_channel_id: int | None
    notify_mode: str  # "channel" / "dm" / "both"
    auto_report_minutes: float
    reminder_minutes: float

    claude_stale_hours: float
    anthropic_admin_key: str | None

    codex_poll_minutes: float
    openai_admin_key: str | None


def load_config() -> Config:
    load_dotenv(ENV_PATH, override=True)  # override: re-read after the GUI saves new values
    try:
        allowed = frozenset(parse_ids(os.environ.get("DISCORD_ALLOWED_USER_IDS", "")))
    except ValueError as e:
        raise SystemExit(f"DISCORD_ALLOWED_USER_IDS 裡的「{e}」不是 Discord ID") from e
    notify_mode = (_str("NOTIFY_MODE") or "channel").lower()
    if notify_mode not in NOTIFY_MODES:
        raise SystemExit(f"NOTIFY_MODE 只能是 {' / '.join(NOTIFY_MODES)}")
    if notify_mode != "channel" and not allowed:
        raise SystemExit("NOTIFY_MODE 要私訊時，請在 DISCORD_ALLOWED_USER_IDS 填你的使用者 ID（私訊會發給這些人）")
    token = _str("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("請在 .env 設定 DISCORD_BOT_TOKEN")
    return Config(
        guild_id=_int("DISCORD_GUILD_ID"),
        allowed_user_ids=allowed,
        bot_token=token,
        notify_channel_id=_int("NOTIFY_CHANNEL_ID"),
        notify_mode=notify_mode,
        auto_report_minutes=float(_str("AUTO_REPORT_MINUTES") or 60),
        reminder_minutes=float(_str("REMINDER_MINUTES") or 60),
        claude_stale_hours=float(_str("CLAUDE_STALE_HOURS") or 6),
        anthropic_admin_key=_str("ANTHROPIC_ADMIN_KEY"),
        codex_poll_minutes=float(_str("CODEX_POLL_MINUTES") or 30),
        openai_admin_key=_str("OPENAI_ADMIN_KEY"),
    )
