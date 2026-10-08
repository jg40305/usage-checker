"""Pay-as-you-go API spend via each provider's official admin cost endpoint.

Both need an organization admin key. They are optional: without a key the
bot simply leaves the spend section out.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiohttp

from .models import SourceError, SpendSummary

TIMEOUT = aiohttp.ClientTimeout(total=20)
USER_AGENT = "llm-usage-bot/1.0.0"


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def _get_pages(
    session: aiohttp.ClientSession, url: str, params: dict[str, Any], headers: dict[str, str]
) -> list[dict[str, Any]]:
    buckets: list[dict[str, Any]] = []
    page = None
    for _ in range(10):  # a month is at most 31 daily buckets; guard against loops
        query = dict(params, **({"page": page} if page else {}))
        async with session.get(url, params=query, headers=headers) as resp:
            if resp.status in (401, 403):
                raise SourceError(f"admin 金鑰無效或權限不足（HTTP {resp.status}）")
            if resp.status >= 400:
                raise SourceError(f"查詢花費失敗（HTTP {resp.status}）")
            body = await resp.json()
        buckets.extend(body.get("data") or [])
        page = body.get("next_page")
        if not body.get("has_more") or not page:
            break
    return buckets


async def anthropic_spend(admin_key: str) -> SpendSummary:
    """Anthropic cost_report: amounts are decimal strings in cents."""
    now = datetime.now(timezone.utc)
    start = _month_start(now)
    headers = {"x-api-key": admin_key, "anthropic-version": "2023-06-01", "User-Agent": USER_AGENT}
    params = {
        "starting_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bucket_width": "1d",
        "limit": "31",
    }
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        buckets = await _get_pages(
            session, "https://api.anthropic.com/v1/organizations/cost_report", params, headers
        )

    total = today = 0.0
    today_key = now.strftime("%Y-%m-%d")
    for b in buckets:
        cents = sum(float(r.get("amount") or 0) for r in b.get("results") or [])
        total += cents
        if str(b.get("starting_at", "")).startswith(today_key):
            today += cents
    return SpendSummary(month_to_date_usd=total / 100, today_usd=today / 100, fetched_at=now)


async def openai_spend(admin_key: str) -> SpendSummary:
    """OpenAI organization costs: amount.value is in dollars."""
    now = datetime.now(timezone.utc)
    start = _month_start(now)
    headers = {"Authorization": f"Bearer {admin_key}", "User-Agent": USER_AGENT}
    params = {"start_time": str(int(start.timestamp())), "bucket_width": "1d", "limit": "31"}
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        buckets = await _get_pages(
            session, "https://api.openai.com/v1/organization/costs", params, headers
        )

    total = today = 0.0
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    for b in buckets:
        dollars = sum(float((r.get("amount") or {}).get("value") or 0) for r in b.get("results") or [])
        total += dollars
        if float(b.get("start_time") or 0) >= today_start:
            today += dollars
    return SpendSummary(month_to_date_usd=total, today_usd=today, fetched_at=now)
