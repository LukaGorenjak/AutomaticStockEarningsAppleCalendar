"""Market abstraction for multi-market calendar generation."""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from earnings_cal.models import DateWindow, Event, parse_event_dict

if TYPE_CHECKING:
    from earnings_cal.fetch import EarningsProvider

logger = logging.getLogger(__name__)


class Market(ABC):
    """Abstract base class representing a financial market calendar feed.

    Subclasses define:
    - `id`: short market slug (e.g. `"us"`, `"ljse"`) used in `{id}.ics` and event UIDs
    - `display_name`: human-readable market name (e.g. `"United States"`)
    - `timezone`: IANA timezone identifier (e.g. `"America/New_York"`, `"Europe/Ljubljana"`)
    - `calendar_name`: `X-WR-CALNAME` header value (e.g. `"Earnings US"`, `"Events LJSE"`)
    - `fetch_events(window) -> list[Event]`: returns events within `[window.start, window.end]`
    """

    id: str
    display_name: str
    timezone: str
    calendar_name: str
    legacy_aliases: tuple[str, ...] = ()
    optional_when_unconfigured: bool = False

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        config_dir: Path | None = None,
        **options: Any,
    ) -> None:
        self.config: dict[str, Any] = dict(config or {})
        self.config_dir: Path = config_dir or Path(".")
        self.options: dict[str, Any] = options

    @property
    def feed_filename(self) -> str:
        """Output `.ics` filename for this market (e.g. `us.ics`)."""
        return f"{self.id}.ics"

    def effective_window(self, window: DateWindow) -> DateWindow:
        """Return the effective `[start, end]` date window used when fetching this market."""
        return window

    @abstractmethod
    def fetch_events(self, window: DateWindow) -> list[Event]:
        """Fetch events for this market within `[window.start, window.end]`."""


class EquitiesAPIMarket(Market):
    """Shared base for equity markets backed by an `EarningsProvider` + optional `manual_events`."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        config_dir: Path | None = None,
        provider: EarningsProvider | None = None,
        use_mock: bool = False,
        **options: Any,
    ) -> None:
        super().__init__(config=config, config_dir=config_dir, **options)
        self.provider = provider
        self.use_mock = use_mock

    def _resolve_provider(self) -> EarningsProvider:
        from earnings_cal.fetch import EarningsProvider, FinnhubProvider, MockEarningsProvider

        if self.provider is not None:
            return self.provider
        opt_provider = self.options.get("provider")
        if isinstance(opt_provider, EarningsProvider):
            return opt_provider

        env_key = os.environ.get("FINNHUB_API_KEY", "").strip()
        if self.use_mock or self.options.get("use_mock") or env_key.lower() == "mock":
            logger.info("Using MockEarningsProvider for market '%s'.", self.id)
            return MockEarningsProvider()

        return FinnhubProvider(api_key=env_key)

    def _get_tickers(self) -> list[str]:
        raw_tickers = self.config.get("tickers") or []
        return [str(t).strip().upper() for t in raw_tickers if t and str(t).strip()]

    def _get_manual_events(self) -> list[Event]:
        raw_manual = self.config.get("manual_events") or []
        events: list[Event] = []
        for item in raw_manual:
            if isinstance(item, Event):
                events.append(replace(item, market=self.id, market_timezone=item.market_timezone or self.timezone))
            elif isinstance(item, dict):
                events.append(parse_event_dict(item, default_market=self.id))
        return events
