"""United States (`us`) market implementation backed by Finnhub."""

from __future__ import annotations

from dataclasses import replace

from earnings_cal.build import merge_events
from earnings_cal.fetch import ProviderError
from earnings_cal.markets.base import EquitiesAPIMarket
from earnings_cal.models import DateWindow, Event, EventKind


class USMarket(EquitiesAPIMarket):
    """US equities earnings market (`docs/us.ics`, aliased to `docs/earnings.ics`)."""

    id = "us"
    display_name = "United States"
    timezone = "America/New_York"
    calendar_name = "Earnings US"
    legacy_aliases = ("earnings.ics",)

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
                replace(ev, market=self.id, kind=EventKind.EARNINGS, market_timezone=self.timezone)
                for ev in fetched
            ]

        merged = merge_events(api_events, manual_events)
        if not merged:
            raise ProviderError(
                f"No US events found between {window.start.isoformat()} and {window.end.isoformat()}."
            )
        return merged
