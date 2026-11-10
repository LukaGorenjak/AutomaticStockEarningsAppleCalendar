"""Earnings Calendar (.ics) generator package."""

from earnings_cal.build import build_calendar, merge_events, write_calendar
from earnings_cal.fetch import EarningsProvider, FinnhubProvider, ProviderError
from earnings_cal.models import EarningsEvent, WatchlistConfig, load_config

__all__ = [
    "EarningsEvent",
    "EarningsProvider",
    "FinnhubProvider",
    "ProviderError",
    "WatchlistConfig",
    "build_calendar",
    "load_config",
    "merge_events",
    "write_calendar",
]
