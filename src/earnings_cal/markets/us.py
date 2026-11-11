"""United States (`us`) market implementation backed by Finnhub."""

from __future__ import annotations

import logging
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from earnings_cal.build import merge_events
from earnings_cal.fetch import (
    EarningsProvider,
    FinnhubProvider,
    MockEarningsProvider,
    ProviderError,
)
from earnings_cal.markets.base import Market
from earnings_cal.models import DateWindow, Event, EventKind, parse_event_dict

logger = logging.getLogger(__name__)


class USMarket(Market):
    """US equities earnings market (`docs/us.ics`, aliased to `docs/earnings.ics`)."""

    id = "us"
    display_name = "United States"
    timezone = "America/New_York"
    calendar_name = "Earnings US"
    legacy_aliases = ("earnings.ics",)

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
                events.append(replace(item, market=self.id))
            elif isinstance(item, dict):
                events.append(parse_event_dict(item, default_market=self.id))
        return events

    def fetch_events(self, window: DateWindow) -> list[Event]:
        """Fetch US earnings events from Finnhub and merge with manual US events."""
        tickers = self._get_tickers()
        manual_events = self._get_manual_events()

        if not tickers and not manual_events:
            raise ProviderError("No US tickers or manual_events configured for market 'us'.")

        api_events: list[Event] = []
        if tickers:
            active_provider = self._resolve_provider()
            fetched = active_provider.fetch_earnings(
                tickers,
                start_date=window.start,
                end_date=window.end,
            )
            api_events = [
                replace(ev, market=self.id, kind=EventKind.EARNINGS)
                for ev in fetched
            ]

        merged = merge_events(api_events, manual_events)
        if not merged:
            raise ProviderError(
                f"No US events found between {window.start.isoformat()} and {window.end.isoformat()}."
            )
        return merged
