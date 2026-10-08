"""One Discord client for all providers: `/usage`, auto reports and reset notifications."""

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


def _error_text(e: Exception) -> str:
    return str(e) if isinstance(e, SourceError) else f"未預期的錯誤：{type(e).__name__}"


def _build_embed(
    spec: ProviderSpec,
    usage: SubscriptionUsage | None,
    sub_error: str | None,
    spend: SpendSummary | None,
    spend_error: str | None,
) -> discord.Embed:
    now = datetime.now(timezone.utc)
    title = f"{spec.name} 用量"
    if usage and usage.plan:
        title += f"（{usage.plan}）"
    embed = discord.Embed(title=title, color=spec.color)

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
        if spec.stale_after and now - usage.fetched_at > spec.stale_after:
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


class UsageBot(discord.Client):
    def __init__(
        self,
        providers: list[ProviderSpec],
        guild_id: int | None,
        allowed_user_ids: frozenset[int],
        notify_channel_id: int | None,
        dm_user_ids: frozenset[int],
        auto_report_interval: timedelta | None,
        reminder_before: timedelta | None = None,
    ):
        # guilds (not privileged) lets get_channel() hit the cache; nothing else is needed
        super().__init__(intents=discord.Intents(guilds=True))
        self.providers = providers
        self.guild_id = guild_id
        self.allowed_user_ids = allowed_user_ids
        self.notify_channel_id = notify_channel_id
        self.dm_user_ids = dm_user_ids
        self.auto_report_interval = auto_report_interval
        self.reminder_before = reminder_before
        self.tree = app_commands.CommandTree(self)
        self._reset_tasks: dict[str, tuple[datetime, asyncio.Task]] = {}
        self._last_error: dict[str, str | None] = {}
        self._last_report_at = datetime.now(timezone.utc)
        self._register_commands()

    # ---- setup -------------------------------------------------------------

    def _register_commands(self) -> None:
        names = "、".join(p.name for p in self.providers)

        @app_commands.command(name="usage", description=f"查看 {names} 的用量與重置時間")
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
        for spec in self.providers:
            asyncio.create_task(self._poll_loop(spec))
        if self.auto_report_interval and self._has_targets:
            asyncio.create_task(self._auto_report_loop())

    async def on_ready(self) -> None:
        log.info("bot ready as %s (%s)", self.user, ", ".join(p.name for p in self.providers))

    # ---- report ------------------------------------------------------------

    async def _provider_embed(self, spec: ProviderSpec) -> discord.Embed:
        sub_task = asyncio.create_task(spec.fetch_subscription())
        spend_task = asyncio.create_task(spec.fetch_spend()) if spec.fetch_spend else None

        try:
            usage = await sub_task
        except Exception as e:  # show the error rather than stale data
            if not isinstance(e, SourceError):
                log.exception("%s subscription fetch failed", spec.name)
            usage, sub_error = None, _error_text(e)
        else:
            sub_error = None
            self._schedule_resets(spec, usage)

        spend: SpendSummary | None = None
        spend_error: str | None = None
        if spend_task:
            try:
                spend = await spend_task
            except Exception as e:
                if not isinstance(e, SourceError):
                    log.exception("%s spend fetch failed", spec.name)
                spend_error = _error_text(e)

        return _build_embed(spec, usage, sub_error, spend, spend_error)

    async def _collect_embeds(self) -> list[discord.Embed]:
        return list(await asyncio.gather(*(self._provider_embed(p) for p in self.providers)))

    def _is_allowed(self, interaction: discord.Interaction) -> bool:
        if self.allowed_user_ids:
            return interaction.user.id in self.allowed_user_ids
        # No allow-list: accept anyone, but only in your own server when one is configured
        return self.guild_id is None or interaction.guild_id == self.guild_id

    async def _handle_usage(self, interaction: discord.Interaction) -> None:
        if not self._is_allowed(interaction):
            await interaction.response.send_message("你沒有使用這個 bot 的權限。", ephemeral=True)
            return
        log.info("/usage from %s", interaction.user)
        await interaction.response.defer(thinking=True)
        embeds = await self._collect_embeds()
        await interaction.followup.send(embeds=embeds)
        self._last_report_at = datetime.now(timezone.utc)  # restart the auto-report countdown

    async def _auto_report_loop(self) -> None:
        assert self.auto_report_interval
        await self.wait_until_ready()
        while not self.is_closed():
            due = self._last_report_at + self.auto_report_interval
            # +1s so an early wake-up (timer granularity) doesn't land just before the deadline
            await asyncio.sleep(max(0, (due - datetime.now(timezone.utc)).total_seconds()) + 1)
            if datetime.now(timezone.utc) < self._last_report_at + self.auto_report_interval:
                continue  # a manual /usage pushed the deadline back
            await self._send(content="⏰ 定時用量回報", embeds=await self._collect_embeds())
            self._last_report_at = datetime.now(timezone.utc)

    # ---- reset notifications ----------------------------------------------

    async def _poll_loop(self, spec: ProviderSpec) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            last_error = self._last_error.get(spec.name)
            try:
                usage = await spec.fetch_subscription()
            except Exception as e:
                message = _error_text(e)
                log.warning("%s poll failed: %s", spec.name, message)
                if spec.alert_on_error and message != last_error:
                    await self._send(f"⚠️ {spec.name} 額度讀取失敗：{message}")
                self._last_error[spec.name] = message
            else:
                if spec.alert_on_error and last_error is not None:
                    await self._send(f"✅ {spec.name} 額度讀取已恢復")
                self._last_error[spec.name] = None
                self._schedule_resets(spec, usage)
            await asyncio.sleep(spec.poll_seconds)

    def _schedule_resets(self, spec: ProviderSpec, usage: SubscriptionUsage) -> None:
        if not self._has_targets:
            return
        now = datetime.now(timezone.utc)
        for w in usage.windows:
            if not w.resets_at or w.resets_at <= now:
                continue
            key = f"{spec.name}:{w.key}"
            current = self._reset_tasks.get(key)
            if current and not current[1].done():
                # Same reset (allow small jitter between reads): keep it.
                if abs((current[0] - w.resets_at).total_seconds()) < 90:
                    continue
                current[1].cancel()  # reset time moved, e.g. a manual reset
            task = asyncio.create_task(self._fire_reset(spec, w.key, w.label, w.resets_at))
            self._reset_tasks[key] = (w.resets_at, task)
            log.info("%s %s reset scheduled at %s", spec.name, w.label, w.resets_at.isoformat())

    async def _fire_reset(self, spec: ProviderSpec, key: str, label: str, resets_at: datetime) -> None:
        """Remind `reminder_before` ahead of the reset (if still ahead), then announce the reset."""
        if self.reminder_before:
            delay = (resets_at - self.reminder_before - datetime.now(timezone.utc)).total_seconds()
            if delay > 0:  # already inside the window (e.g. bot just started): skip the reminder
                await asyncio.sleep(delay)
                await self._send(await self._reminder_text(spec, key, label, resets_at))
        delay = (resets_at - datetime.now(timezone.utc)).total_seconds() + 5
        await asyncio.sleep(max(0, delay))
        await self._send(f"✅ {spec.name} {label}額度已重置（{_ts(resets_at, 't')}）")

    async def _reminder_text(self, spec: ProviderSpec, key: str, label: str, resets_at: datetime) -> str:
        text = f"⏰ {spec.name} {label}額度 {_ts(resets_at, 'R')} 重置（{_ts(resets_at, 't')}）"
        try:
            usage = await spec.fetch_subscription()
        except Exception as e:
            log.warning("%s reminder could not refresh usage: %s", spec.name, _error_text(e))
            return text
        w = next((w for w in usage.windows if w.key == key), None)
        if w:
            text += f"，目前已用 **{w.used_percent:.0f}%**"
            if spec.stale_after and datetime.now(timezone.utc) - usage.fetched_at > spec.stale_after:
                text += f"（資料時間 {_ts(usage.fetched_at, 'R')}）"
        return text

    @property
    def _has_targets(self) -> bool:
        return bool(self.notify_channel_id or self.dm_user_ids)

    async def _send(self, content: str | None = None, embeds: list[discord.Embed] | None = None) -> None:
        """Post a notification to the channel and/or DM, whichever are configured."""
        if self.notify_channel_id:
            try:
                channel = self.get_channel(self.notify_channel_id) or await self.fetch_channel(
                    self.notify_channel_id
                )
                await channel.send(content=content, embeds=embeds or [])
                log.info("posted to channel: %s", content or "(embeds)")
            except discord.DiscordException:
                log.exception("failed to post to channel %s", self.notify_channel_id)
        for user_id in self.dm_user_ids:
            try:
                user = self.get_user(user_id) or await self.fetch_user(user_id)
                await user.send(content=content, embeds=embeds or [])
                log.info("sent DM to %s: %s", user, content or "(embeds)")
            except discord.Forbidden:
                log.error(
                    "無法私訊 %s：請確認你和 bot 在同一個伺服器，且該伺服器的隱私設定允許成員私訊", user_id
                )
            except discord.DiscordException:
                log.exception("failed to DM %s", user_id)
