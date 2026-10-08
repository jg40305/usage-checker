"""Run the usage bot (Claude + Codex in one Discord bot): python -m llm_usage_bot

For a window with connection status and live logs: pythonw -m llm_usage_bot.gui
"""

from __future__ import annotations

import asyncio
import logging

import discord

from .app import build_bot
from .config import load_config


async def main() -> None:
    cfg = load_config()
    await build_bot(cfg).start(cfg.bot_token)


if __name__ == "__main__":
    discord.utils.setup_logging(level=logging.INFO)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
