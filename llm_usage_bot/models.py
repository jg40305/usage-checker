"""Provider-neutral data shapes shared by the sources and the Discord layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Window:
    """One rate-limit window, e.g. the 5-hour session or the weekly cap."""

    key: str  # stable id used for reset notifications, e.g. "five_hour"
    label: str  # human label shown in Discord
    used_percent: float
    resets_at: datetime | None


@dataclass
class SubscriptionUsage:
    windows: list[Window]
    fetched_at: datetime  # when the numbers were produced by the provider
    plan: str | None = None
    extra: dict[str, str] = field(default_factory=dict)  # extra key/value lines


@dataclass
class SpendSummary:
    month_to_date_usd: float
    today_usd: float
    fetched_at: datetime


class SourceError(Exception):
    """A source failed in a way the user should see instead of stale data."""
