"""Read Claude subscription limits.

Primary source: `claude -p /usage`, a local slash command that asks Anthropic
for the live numbers without calling the model, so it costs no quota and sees
usage from every client (terminal, VS Code, claude.ai). Claude Code handles its
own login; this module never touches Claude credentials.

Fallback: the cache written by statusline/claude_statusline.py, which only
updates while terminal Claude Code is in use. Callers show `fetched_at` so
stale data is obvious.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import SourceError, SubscriptionUsage, Window

WINDOWS = (("five_hour", "5 小時"), ("seven_day", "每週"))

# `/usage` prints lines like
#   Current session: 12% used · resets Oct 9, 1:50pm (Asia/Taipei)
#   Current week (all models): 29% used · resets Oct 12, 10am (Asia/Taipei)
_USAGE_LINE = re.compile(
    r"^Current (?P<window>session|week(?: \(all models\))?):\s*(?P<pct>\d+(?:\.\d+)?)% used"
    r"(?:\s*·\s*resets (?P<resets>[^(\n]+?))?\s*(?:\([^)\n]*\))?\s*$",
    re.MULTILINE,
)
_WINDOW_KEYS = {"session": "five_hour", "week": "seven_day"}
_RESET_FORMATS = ("%b %d, %I:%M%p", "%b %d, %I%p", "%I:%M%p", "%I%p")

# Set once the CLI answered /usage with the model instead of locally; from then
# on only the cache is used, so polling can't keep burning quota.
_live_disabled: str | None = None


def _parse_reset(text: str, now: datetime) -> datetime | None:
    """Parse "Oct 9, 1:50pm" / "10am" as local time, picking the next occurrence.

    The CLI runs on this machine, so its timezone is ours.
    """
    text = text.strip()
    for fmt in _RESET_FORMATS:
        has_date = "%b" in fmt
        try:
            # Prepend the year so strptime doesn't default to 1900 (Feb 29 would fail)
            parsed = datetime.strptime(f"{now.year} {text}" if has_date else text, f"%Y {fmt}" if has_date else fmt)
        except ValueError:
            continue
        if not has_date:
            parsed = datetime.combine(now.astimezone().date(), parsed.time())
        resets_at = parsed.astimezone(timezone.utc)  # naive -> treated as local time
        # Allow a little slack for a reset that just passed; anything older is
        # the next occurrence (year rollover for "Jan 2", tomorrow for "1am").
        if has_date and resets_at < now - timedelta(days=1):
            resets_at = resets_at.replace(year=resets_at.year + 1)
        elif not has_date and resets_at < now - timedelta(hours=1):
            resets_at += timedelta(days=1)
        return resets_at
    return None


def parse_usage_text(text: str, now: datetime) -> list[Window]:
    windows = {}
    for m in _USAGE_LINE.finditer(text):
        key = _WINDOW_KEYS[m["window"].split()[0]]
        if key in windows:
            continue  # first match wins
        windows[key] = Window(
            key=key,
            label=dict(WINDOWS)[key],
            used_percent=float(m["pct"]),
            resets_at=_parse_reset(m["resets"], now) if m["resets"] else None,
        )
    return [windows[key] for key, _ in WINDOWS if key in windows]


async def fetch_claude_live(timeout: float = 60.0) -> SubscriptionUsage:
    global _live_disabled
    if _live_disabled:
        raise SourceError(_live_disabled)
    claude = shutil.which("claude")
    if not claude:
        raise SourceError("找不到 claude 指令，請確認已安裝 Claude Code 且在 PATH 裡")
    proc = await asyncio.create_subprocess_exec(
        claude,
        "-p",
        "/usage",
        "--output-format",
        "json",
        "--no-session-persistence",  # don't leave a transcript per poll
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # no console window flashing up when running under the GUI
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise SourceError(f"claude /usage 在 {timeout:.0f} 秒內沒有回應") from e

    try:
        payload = json.loads(stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError as e:
        detail = (stderr or stdout).decode("utf-8", "replace").strip().splitlines()
        raise SourceError(f"claude /usage 失敗：{detail[0] if detail else f'exit {proc.returncode}'}") from e
    result = str(payload.get("result") or "")
    if payload.get("local_command") != "usage":
        _live_disabled = "claude 沒有把 /usage 當成本地指令執行，已停用以免消耗額度（重新啟動 bot 會再試一次）"
        raise SourceError(_live_disabled)

    now = datetime.now(timezone.utc)
    windows = parse_usage_text(result, now)
    if not windows:
        first = next((line for line in result.splitlines() if line.strip()), "（沒有輸出）")
        raise SourceError(f"claude /usage 的輸出裡沒有 5 小時／每週的資料：{first}")
    return SubscriptionUsage(windows=windows, fetched_at=now)


async def fetch_claude_usage(cache_path: Path) -> SubscriptionUsage:
    """Live numbers from `claude /usage`, falling back to the status line cache."""
    try:
        return await fetch_claude_live()
    except SourceError as live_error:
        try:
            usage = await asyncio.to_thread(read_claude_usage, cache_path)
        except SourceError:
            raise live_error from None
        usage.extra["來源"] = f"狀態列快取（{live_error}）"
        return usage


def read_claude_usage(path: Path) -> SubscriptionUsage:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise SourceError(
            "還沒有 Claude 額度資料：請確認已設定狀態列，並在 Claude Code 裡至少完成一次回應"
        ) from e
    except (OSError, json.JSONDecodeError) as e:
        raise SourceError(f"讀取 Claude 額度快取失敗：{e}") from e

    rate_limits = payload.get("rate_limits") or {}
    windows = []
    for key, label in WINDOWS:
        w = rate_limits.get(key)
        if not w or w.get("used_percentage") is None:
            continue
        resets_at = w.get("resets_at")
        windows.append(
            Window(
                key=key,
                label=label,
                used_percent=float(w["used_percentage"]),
                resets_at=datetime.fromtimestamp(resets_at, tz=timezone.utc) if resets_at else None,
            )
        )
    if not windows:
        raise SourceError("Claude 額度快取裡沒有 5 小時／每週的資料")

    return SubscriptionUsage(
        windows=windows,
        fetched_at=datetime.fromtimestamp(payload.get("written_at", 0), tz=timezone.utc),
    )
