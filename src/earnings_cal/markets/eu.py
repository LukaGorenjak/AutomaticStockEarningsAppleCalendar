"""European equities (`eu`) market implementation backed by Finnhub (best-effort) + manual events."""

from __future__ import annotations

import logging
from dataclasses import replace

from earnings_cal.build import merge_events
from earnings_cal.fetch import ProviderError
from earnings_cal.markets.base import EquitiesAPIMarket
from earnings_cal.models import DateWindow, Event, EventKind

logger = logging.getLogger(__name__)


def _is_nonfatal_eu_coverage_gap(exc: ProviderError) -> bool:
    """Return True when Finnhub returned empty/unsupported coverage for European symbols."""
    msg = str(exc)
    if "returned no earnings events" in msg:
        return True
    if "Finnhub API requests failed for all" in msg and "HTTP 403" in msg:
        return True
    return False


class EUMarket(EquitiesAPIMarket):
    """European equities earnings market (`docs/eu.ics`), e.g. Xetra (`RHM.DE`)."""

    id = "eu"
    display_name = "Europe"
    timezone = "Europe/Berlin"
    calendar_name = "Events EU"
    optional_when_unconfigured = True

    def fetch_events(self, window: DateWindow) -> list[Event]:
        """Fetch EU earnings events via Finnhub (falling back gracefully on empty/403 tier gaps) + manual events."""
        tickers = self._get_tickers()
        manual_events = [
            ev for ev in self._get_manual_events() if window.contains(ev.date)
        ]
        all_manual = self._get_manual_events()

        if not tickers and not all_manual:
            logger.info("No EU tickers or manual_events configured; emitting empty EU calendar.")
            return []

        api_events: list[Event] = []
        if tickers and self.config.get("use_api", True):
            active_provider = self._resolve_provider()
            try:
                fetched = active_provider.fetch_earnings(
                    tickers,
                    start_date=window.start,
                    end_date=window.end,
                )
                api_events = [
                    replace(
                        ev,
                        market=self.id,
                        kind=EventKind.EARNINGS,
                        market_timezone=self.timezone,
                    )
                    for ev in fetched
                ]
            except ProviderError as exc:
                if _is_nonfatal_eu_coverage_gap(exc):
                    logger.info(
                        "Finnhub returned no accessible EU earnings for %s (%s); using manual EU events only.",
                        tickers,
                        exc,
                    )
                else:
                    raise

        return merge_events(api_events, manual_events)
