"""One Discord client per provider: `/usage` plus reset notifications."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import discord
from discord import app_commands

from .models import SourceError, SpendSummary, SubscriptionUsage, Window

log = logging.getLogger(__name__)

SubscriptionFetcher = Callable[[], Awaitable[SubscriptionUsage]]
SpendFetcher = Callable[[], Awaitable[SpendSummary]]


@dataclass
class ProviderSpec:
    name: str  # "Claude" / "Codex"
    color: int
    fetch_subscription: SubscriptionFetcher
    fetch_spend: SpendFetcher | None  # None when no admin key is configured
    poll_seconds: float
    stale_after: timedelta | None  # None for live sources
    notify_channel_id: int | None
    alert_on_error: bool  # post when the source starts/stops failing


def _ts(dt: datetime, style: str) -> str:
    return f"<t:{int(dt.timestamp())}:{style}>"


def _bar(percent: float, width: int = 10) -> str:
    filled = max(0, min(width, round(percent / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def _window_value(w: Window, now: datetime) -> str:
    text = f"`{_bar(w.used_percent)}` **{w.used_percent:.0f}%**"
    if w.resets_at is None:
        return text
    if w.resets_at <= now:
        return text + "\n已過重置時間（實際應已歸零），等待新資料"
    return text + f"\n{_ts(w.resets_at, 'R')} 重置（{_ts(w.resets_at, 'f')}）"


def _status_color(usage: SubscriptionUsage, now: datetime) -> int:
    live = [w.used_percent for w in usage.windows if not w.resets_at or w.resets_at > now]
    peak = max(live, default=0)
    if peak >= 80:
        return 0xE5484D
    if peak >= 50:
        return 0xF5A524
    return 0x30A46C


class UsageBot(discord.Client):
    def __init__(self, spec: ProviderSpec, guild_id: int | None, allowed_user_ids: frozenset[int]):
        super().__init__(intents=discord.Intents.none())
        self.spec = spec
        self.guild_id = guild_id
        self.allowed_user_ids = allowed_user_ids
        self.tree = app_commands.CommandTree(self)
        self._reset_tasks: dict[str, tuple[datetime, asyncio.Task]] = {}
        self._last_error: str | None = None
        self._register_commands()

    # ---- setup -------------------------------------------------------------

    def _register_commands(self) -> None:
        @app_commands.command(name="usage", description=f"查看 {self.spec.name} 的用量與重置時間")
        async def usage(interaction: discord.Interaction) -> None:
            await self._handle_usage(interaction)

        self.tree.add_command(usage)

    async def setup_hook(self) -> None:
        if self.guild_id:
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()  # global commands can take a while to appear
        asyncio.create_task(self._poll_loop())

    async def on_ready(self) -> None:
        log.info("%s bot ready as %s", self.spec.name, self.user)

    # ---- /usage ------------------------------------------------------------

    async def _handle_usage(self, interaction: discord.Interaction) -> None:
        if interaction.user.id not in self.allowed_user_ids:
            await interaction.response.send_message("你沒有使用這個 bot 的權限。", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)

        sub_task = asyncio.create_task(self.spec.fetch_subscription())
        spend_task = asyncio.create_task(self.spec.fetch_spend()) if self.spec.fetch_spend else None

        try:
            usage = await sub_task
        except SourceError as e:
            usage, sub_error = None, str(e)
        except Exception as e:  # unexpected: still show it rather than stale data
            log.exception("%s subscription fetch failed", self.spec.name)
            usage, sub_error = None, f"未預期的錯誤：{type(e).__name__}"
        else:
            sub_error = None
            self._schedule_resets(usage)

        spend: SpendSummary | None = None
        spend_error: str | None = None
        if spend_task:
            try:
                spend = await spend_task
            except SourceError as e:
                spend_error = str(e)
            except Exception as e:
                log.exception("%s spend fetch failed", self.spec.name)
                spend_error = f"未預期的錯誤：{type(e).__name__}"

        await interaction.followup.send(embed=self._build_embed(usage, sub_error, spend, spend_error))

    def _build_embed(
        self,
        usage: SubscriptionUsage | None,
        sub_error: str | None,
        spend: SpendSummary | None,
        spend_error: str | None,
    ) -> discord.Embed:
        now = datetime.now(timezone.utc)
        title = f"{self.spec.name} 用量"
        if usage and usage.plan:
            title += f"（{usage.plan}）"
        embed = discord.Embed(title=title, color=self.spec.color)

        if usage is None:
            embed.color = 0xE5484D
            embed.description = f"⚠️ 無法取得訂閱額度\n{sub_error}"
        else:
            embed.color = _status_color(usage, now)
            for w in usage.windows:
                embed.add_field(name=w.label, value=_window_value(w, now), inline=False)
            for k, v in usage.extra.items():
                embed.add_field(name=k, value=v, inline=True)
            freshness = f"資料時間：{_ts(usage.fetched_at, 'R')}"
            if self.spec.stale_after and now - usage.fetched_at > self.spec.stale_after:
                embed.color = 0x8B8D98
                freshness = "⚠️ 資料可能已過期，" + freshness
            embed.description = freshness

        if spend:
            embed.add_field(
                name="API 花費（UTC 本月）",
                value=f"本月 **${spend.month_to_date_usd:,.2f}**｜今日 ${spend.today_usd:,.2f}",
                inline=False,
            )
        elif spend_error:
            embed.add_field(name="API 花費", value=f"⚠️ {spend_error}", inline=False)
        return embed

    # ---- reset notifications ----------------------------------------------

    async def _poll_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                usage = await self.spec.fetch_subscription()
            except Exception as e:
                message = str(e) if isinstance(e, SourceError) else f"未預期的錯誤：{type(e).__name__}"
                log.warning("%s poll failed: %s", self.spec.name, message)
                if self.spec.alert_on_error and message != self._last_error:
                    await self._notify(f"⚠️ {self.spec.name} 額度讀取失敗：{message}")
                self._last_error = message
            else:
                if self.spec.alert_on_error and self._last_error is not None:
                    await self._notify(f"✅ {self.spec.name} 額度讀取已恢復")
                self._last_error = None
                self._schedule_resets(usage)
            await asyncio.sleep(self.spec.poll_seconds)

    def _schedule_resets(self, usage: SubscriptionUsage) -> None:
        if not self.spec.notify_channel_id:
            return
        now = datetime.now(timezone.utc)
        for w in usage.windows:
            if not w.resets_at or w.resets_at <= now:
                continue
            current = self._reset_tasks.get(w.key)
            if current and not current[1].done():
                # Same reset (allow small jitter between reads): keep it.
                if abs((current[0] - w.resets_at).total_seconds()) < 90:
                    continue
                current[1].cancel()  # reset time moved, e.g. a manual reset
            task = asyncio.create_task(self._fire_reset(w.label, w.resets_at))
            self._reset_tasks[w.key] = (w.resets_at, task)
            log.info("%s %s reset scheduled at %s", self.spec.name, w.label, w.resets_at.isoformat())

    async def _fire_reset(self, label: str, resets_at: datetime) -> None:
        delay = (resets_at - datetime.now(timezone.utc)).total_seconds() + 5
        await asyncio.sleep(max(0, delay))
        await self._notify(f"✅ {self.spec.name} {label}額度已重置（{_ts(resets_at, 't')}）")

    async def _notify(self, content: str) -> None:
        channel_id = self.spec.notify_channel_id
        if not channel_id:
            return
        try:
            channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
            await channel.send(content)
        except discord.DiscordException:
            log.exception("%s failed to post notification", self.spec.name)
