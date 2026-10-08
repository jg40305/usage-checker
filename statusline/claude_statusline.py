"""Claude Code statusLine command.

Claude Code pipes session JSON on stdin after each response. On Pro/Max it
includes `rate_limits` (five_hour / seven_day with used_percentage and
resets_at). We cache that for the Discord bot and print a short status line.

Standard library only, so any Python can run it.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path


def cache_path() -> Path:
    override = os.environ.get("CLAUDE_CACHE_PATH")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    return Path(base) / "llm-usage-bot" / "claude_rate_limits.json"


def write_cache(rate_limits: dict) -> None:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"written_at": time.time(), "rate_limits": rate_limits}
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".claude_rl_", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


def fmt_window(name: str, w: dict | None) -> str | None:
    if not w or w.get("used_percentage") is None:
        return None
    text = f"{name} {w['used_percentage']:.0f}%"
    resets_at = w.get("resets_at")
    if resets_at:
        left = int(resets_at - time.time())
        if left > 0:
            h, m = divmod(left // 60, 60)
            text += f" ({h}h{m:02d}m)" if h < 48 else f" ({h // 24}d)"
    return text


def main() -> None:
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
    except json.JSONDecodeError:
        data = {}

    rate_limits = data.get("rate_limits")
    if isinstance(rate_limits, dict) and rate_limits:
        try:
            write_cache(rate_limits)
        except OSError:
            pass  # never break the status line over a cache write

    parts = []
    model = (data.get("model") or {}).get("display_name")
    if model:
        parts.append(model)
    if isinstance(rate_limits, dict):
        for name, key in (("5h", "five_hour"), ("7d", "seven_day")):
            text = fmt_window(name, rate_limits.get(key))
            if text:
                parts.append(text)
    sys.stdout.write(" | ".join(parts))


if __name__ == "__main__":
    main()
