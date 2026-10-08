"""Read Codex subscription limits through the official `codex app-server`.

The app-server speaks newline-delimited JSON-RPC over stdio and answers
`account/rateLimits/read` without consuming quota. Codex handles its own
login; this module never touches Codex credentials.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import SourceError, SubscriptionUsage, Window

CLIENT_INFO = {"name": "llm_usage_bot", "title": "LLM Usage Bot", "version": "0.1.0"}


def resolve_codex_command(codex_bin: str | None) -> list[str]:
    """Return argv for codex, preferring node + codex.js over the npm .cmd shim.

    Going through the .cmd shim puts cmd.exe between us and node, so killing
    the shim can orphan the real process. Calling node directly avoids that.
    """
    if codex_bin:
        return [codex_bin]
    found = shutil.which("codex")
    if not found:
        raise SourceError("找不到 codex 指令，請確認已安裝 Codex CLI 或在 .env 設定 CODEX_BIN")
    shim = Path(found)
    script = shim.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    node = shutil.which("node")
    if shim.suffix.lower() in {".cmd", ".ps1", ""} and script.exists() and node:
        return [node, str(script)]
    return [found]


async def read_rate_limits(codex_bin: str | None = None, timeout: float = 20.0) -> dict[str, Any]:
    """Return the raw `account/rateLimits/read` result."""
    argv = resolve_codex_command(codex_bin) + ["app-server"]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert proc.stdin and proc.stdout

    async def send(msg: dict[str, Any]) -> None:
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        await proc.stdin.drain()

    async def wait_for(msg_id: int) -> dict[str, Any]:
        while True:
            line = await proc.stdout.readline()
            if not line:
                raise SourceError("codex app-server 意外結束")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") != msg_id:
                continue  # notifications or unrelated responses
            if "error" in msg:
                err = msg["error"]
                raise SourceError(f"codex app-server 回報錯誤：{err.get('message', err)}")
            return msg.get("result") or {}

    async def exchange() -> dict[str, Any]:
        await send({"method": "initialize", "id": 0, "params": {"clientInfo": CLIENT_INFO}})
        await wait_for(0)
        await send({"method": "initialized"})
        await send({"method": "account/rateLimits/read", "id": 1})
        return await wait_for(1)

    try:
        return await asyncio.wait_for(exchange(), timeout)
    except asyncio.TimeoutError as e:
        raise SourceError(f"codex app-server 在 {timeout:.0f} 秒內沒有回應") from e
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), 3)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


def _ts(value: Any) -> datetime | None:
    if value in (None, 0):
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def _window_label(minutes: Any, fallback: str) -> str:
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return fallback
    if m % (60 * 24) == 0:
        days = m // (60 * 24)
        return "每週" if days == 7 else f"{days} 天"
    if m % 60 == 0:
        return f"{m // 60} 小時"
    return f"{m} 分鐘"


def parse_rate_limits(result: dict[str, Any]) -> SubscriptionUsage:
    limits = result.get("rateLimits") or result
    windows: list[Window] = []
    for key, fallback in (("primary", "5 小時"), ("secondary", "每週")):
        w = limits.get(key)
        if not w:
            continue
        windows.append(
            Window(
                key=key,
                label=_window_label(w.get("windowDurationMins"), fallback),
                used_percent=float(w.get("usedPercent") or 0),
                resets_at=_ts(w.get("resetsAt")),
            )
        )
    if not windows:
        raise SourceError("Codex 沒有回傳額度資料（可能尚未用 ChatGPT 帳號登入 codex）")

    extra: dict[str, str] = {}
    credits = limits.get("credits") or result.get("credits")
    if isinstance(credits, dict):
        if credits.get("unlimited"):
            extra["Credits"] = "無限制"
        elif credits.get("hasCredits") or str(credits.get("balance") or "0") not in {"0", "0.0"}:
            extra["Credits 餘額"] = str(credits.get("balance"))
    resets = result.get("rateLimitResetCredits")
    if isinstance(resets, dict) and resets.get("availableCount"):
        line = f"{resets['availableCount']} 次"
        expiries = [
            c["expiresAt"]
            for c in resets.get("credits") or []
            if c.get("status") == "available" and c.get("expiresAt")
        ]
        if expiries:
            line += f"（最早 <t:{int(min(expiries))}:R> 到期）"
        extra["可用的手動重置"] = line
    if limits.get("spendControlReached"):
        extra["花費上限"] = "已達到"

    return SubscriptionUsage(
        windows=windows,
        fetched_at=datetime.now(timezone.utc),
        plan=limits.get("planType") or result.get("planType"),
        extra=extra,
    )


async def fetch_codex_usage(codex_bin: str | None = None) -> SubscriptionUsage:
    return parse_rate_limits(await read_rate_limits(codex_bin))
