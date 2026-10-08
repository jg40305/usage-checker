from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def default_claude_cache() -> Path:
    # Must match statusline/claude_statusline.py
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    return Path(base) / "llm-usage-bot" / "claude_rate_limits.json"


def _int(name: str) -> int | None:
    value = os.environ.get(name, "").strip()
    return int(value) if value else None


def _str(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


@dataclass(frozen=True)
class Config:
    guild_id: int | None
    allowed_user_ids: frozenset[int]

    claude_token: str | None
    claude_notify_channel_id: int | None
    claude_cache_path: Path
    claude_stale_hours: float
    anthropic_admin_key: str | None

    codex_token: str | None
    codex_notify_channel_id: int | None
    codex_bin: str | None
    codex_poll_minutes: float
    openai_admin_key: str | None


def load_config() -> Config:
    load_dotenv(PROJECT_ROOT / ".env")
    allowed = frozenset(
        int(x) for x in os.environ.get("DISCORD_ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
    )
    if not allowed:
        raise SystemExit("請在 .env 設定 DISCORD_ALLOWED_USER_IDS（你的 Discord 使用者 ID）")
    return Config(
        guild_id=_int("DISCORD_GUILD_ID"),
        allowed_user_ids=allowed,
        claude_token=_str("CLAUDE_BOT_TOKEN"),
        claude_notify_channel_id=_int("CLAUDE_NOTIFY_CHANNEL_ID"),
        claude_cache_path=Path(_str("CLAUDE_CACHE_PATH") or default_claude_cache()),
        claude_stale_hours=float(_str("CLAUDE_STALE_HOURS") or 6),
        anthropic_admin_key=_str("ANTHROPIC_ADMIN_KEY"),
        codex_token=_str("CODEX_BOT_TOKEN"),
        codex_notify_channel_id=_int("CODEX_NOTIFY_CHANNEL_ID"),
        codex_bin=_str("CODEX_BIN"),
        codex_poll_minutes=float(_str("CODEX_POLL_MINUTES") or 30),
        openai_admin_key=_str("OPENAI_ADMIN_KEY"),
    )
