"""Run the Claude and Codex usage bots in one process: python -m llm_usage_bot"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from functools import partial

import discord

from . import billing
from .bot import ProviderSpec, UsageBot
from .claude_source import read_claude_usage
from .codex_source import fetch_codex_usage
from .config import load_config

log = logging.getLogger("llm_usage_bot")


async def main() -> None:
    cfg = load_config()
    bots: list[tuple[UsageBot, str]] = []

    if cfg.claude_token:

        async def claude_usage():
            return await asyncio.to_thread(read_claude_usage, cfg.claude_cache_path)

        spec = ProviderSpec(
            name="Claude",
            color=0xD97757,
            fetch_subscription=claude_usage,
            fetch_spend=partial(billing.anthropic_spend, cfg.anthropic_admin_key)
            if cfg.anthropic_admin_key
            else None,
            poll_seconds=60,  # cheap local file read
            stale_after=timedelta(hours=cfg.claude_stale_hours),
            notify_channel_id=cfg.claude_notify_channel_id,
            alert_on_error=False,  # a missing cache just means Claude Code hasn't run yet
        )
        bots.append((UsageBot(spec, cfg.guild_id, cfg.allowed_user_ids), cfg.claude_token))

    if cfg.codex_token:
        spec = ProviderSpec(
            name="Codex",
            color=0x10A37F,
            fetch_subscription=partial(fetch_codex_usage, cfg.codex_bin),
            fetch_spend=partial(billing.openai_spend, cfg.openai_admin_key)
            if cfg.openai_admin_key
            else None,
            poll_seconds=cfg.codex_poll_minutes * 60,
            stale_after=None,
            notify_channel_id=cfg.codex_notify_channel_id,
            alert_on_error=True,  # e.g. Codex login expired
        )
        bots.append((UsageBot(spec, cfg.guild_id, cfg.allowed_user_ids), cfg.codex_token))

    if not bots:
        raise SystemExit("請在 .env 至少設定 CLAUDE_BOT_TOKEN 或 CODEX_BOT_TOKEN 其中一個")

    log.info("starting %d bot(s)", len(bots))
    await asyncio.gather(*(bot.start(token) for bot, token in bots))


if __name__ == "__main__":
    discord.utils.setup_logging(level=logging.INFO)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
