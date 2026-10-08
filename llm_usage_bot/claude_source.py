"""Read Claude subscription limits cached by statusline/claude_statusline.py.

The numbers come from Claude Code itself (statusLine `rate_limits`), so this
only updates while Claude Code is in use. Callers show `fetched_at` so stale
data is obvious.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .models import SourceError, SubscriptionUsage, Window

WINDOWS = (("five_hour", "5 小時"), ("seven_day", "每週"))


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
