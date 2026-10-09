"""Build the usage bot from config; shared by the CLI (__main__) and the GUI."""

from __future__ import annotations

from datetime import timedelta
from functools import partial

from . import billing
from .bot import ProviderSpec, UsageBot
from .claude_source import fetch_claude_usage
from .codex_source import fetch_codex_usage
from .config import Config, claude_cache_path


def build_bot(cfg: Config) -> UsageBot:
    providers = [
        ProviderSpec(
            name="Claude",
            color=0xD97757,
            fetch_subscription=partial(fetch_claude_usage, claude_cache_path()),
            fetch_spend=partial(billing.anthropic_spend, cfg.anthropic_admin_key)
            if cfg.anthropic_admin_key
            else None,
            poll_seconds=cfg.claude_poll_minutes * 60,
            stale_after=timedelta(hours=cfg.claude_stale_hours),  # only the cache fallback ages
            alert_on_error=True,  # e.g. Claude Code missing or logged out, and no cache
        ),
        ProviderSpec(
            name="Codex",
            color=0x10A37F,
            fetch_subscription=fetch_codex_usage,
            fetch_spend=partial(billing.openai_spend, cfg.openai_admin_key)
            if cfg.openai_admin_key
            else None,
            poll_seconds=cfg.codex_poll_minutes * 60,
            stale_after=None,
            alert_on_error=True,  # e.g. Codex login expired
        ),
    ]

    return UsageBot(
        providers,
        cfg.guild_id,
        cfg.allowed_user_ids,
        cfg.notify_channel_id if cfg.notify_mode in ("channel", "both") else None,
        cfg.allowed_user_ids if cfg.notify_mode in ("dm", "both") else frozenset(),
        timedelta(minutes=cfg.auto_report_minutes) if cfg.auto_report_minutes > 0 else None,
        timedelta(minutes=cfg.reminder_minutes) if cfg.reminder_minutes > 0 else None,
    )
