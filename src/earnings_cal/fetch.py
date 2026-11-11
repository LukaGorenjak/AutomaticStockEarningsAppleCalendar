"""Earnings calendar data providers (Finnhub and swappable provider interface)."""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from datetime import date, timedelta
from typing import Any

import requests

from earnings_cal.models import EarningsEvent

logger = logging.getLogger(__name__)

FINNHUB_EARNINGS_URL = "https://finnhub.io/api/v1/calendar/earnings"
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class ProviderError(RuntimeError):
    """Raised when an earnings provider fails or returns no data for all tickers."""


class EarningsProvider(ABC):
    """Abstract interface for fetching upcoming earnings dates.

    Swap in another implementation (e.g. AlphaVantageProvider) without changing
    calendar generation or CLI orchestration.
    """

    @abstractmethod
    def fetch_earnings(
        self,
        tickers: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> list[EarningsEvent]:
        """Fetch earnings events for `tickers` within `[start_date, end_date]`."""


class FinnhubProvider(EarningsProvider):
    """Fetches upcoming earnings dates from the Finnhub `/calendar/earnings` endpoint.

    Finnhub API details verified against documentation:
    - Endpoint: `GET https://finnhub.io/api/v1/calendar/earnings`
    - Query params: `from` (YYYY-MM-DD), `to` (YYYY-MM-DD), `symbol` (ticker)
    - Auth: `X-Finnhub-Token` header (or `token` query param)
    - Free tier rate limit: 60 calls/minute (HTTP 429 when exceeded)
    """

    def __init__(
        self,
        api_key: str | None = None,
        session: requests.Session | None = None,
        base_url: str = FINNHUB_EARNINGS_URL,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        timeout: float = 15.0,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        resolved_key = (api_key if api_key is not None else os.environ.get("FINNHUB_API_KEY", "")).strip()
        if not resolved_key:
            raise ProviderError(
                "Missing Finnhub API key. Set the FINNHUB_API_KEY environment variable."
            )
        self.api_key = resolved_key
        self.session = session or requests.Session()
        self.base_url = base_url
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.timeout = timeout
        self._sleep = sleep_fn

    def fetch_earnings(
        self,
        tickers: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> list[EarningsEvent]:
        """Fetch earnings one ticker at a time for `[start_date, end_date]`.

        Skips and warns on individual ticker failures. Raises `ProviderError` if
        every ticker fails or if the API returns zero events across all tickers.
        """
        normalized_tickers = [t.strip().upper() for t in tickers if t and t.strip()]
        if not normalized_tickers:
            return []

        events: list[EarningsEvent] = []
        succeeded_tickers = 0

        for ticker in normalized_tickers:
            try:
                ticker_events = self._fetch_single_ticker(ticker, start_date, end_date)
                succeeded_tickers += 1
                events.extend(ticker_events)
            except Exception as exc:
                logger.warning("Skipping ticker %s due to error: %s", ticker, exc)

        if succeeded_tickers == 0:
            raise ProviderError(
                f"Finnhub API requests failed for all {len(normalized_tickers)} ticker(s)."
            )

        if not events:
            raise ProviderError(
                f"Finnhub API returned no earnings events for any of the "
                f"{len(normalized_tickers)} ticker(s) between {start_date} and {end_date}."
            )

        return events

    def _fetch_single_ticker(
        self,
        ticker: str,
        start_date: date,
        end_date: date,
    ) -> list[EarningsEvent]:
        params = {
            "symbol": ticker,
            "from": start_date.isoformat(),
            "to": end_date.isoformat(),
        }
        headers = {
            "X-Finnhub-Token": self.api_key,
            "Accept": "application/json",
        }

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(
                    self.base_url,
                    params=params,
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt < self.max_retries:
                    delay = self.backoff_seconds * (2**attempt)
                    logger.warning(
                        "Network error fetching %s (attempt %d/%d): %s. Retrying in %.1fs...",
                        ticker,
                        attempt + 1,
                        self.max_retries + 1,
                        exc,
                        delay,
                    )
                    self._sleep(delay)
                    continue
                raise ProviderError(f"Request failed for {ticker}: {exc}") from exc

            if response.status_code in RETRYABLE_STATUS_CODES:
                last_error = ProviderError(
                    f"HTTP {response.status_code} for {ticker}"
                )
                if attempt < self.max_retries:
                    delay = self._compute_retry_delay(response, attempt)
                    logger.warning(
                        "HTTP %d (rate limit / transient error) for %s (attempt %d/%d). "
                        "Retrying in %.1fs...",
                        response.status_code,
                        ticker,
                        attempt + 1,
                        self.max_retries + 1,
                        delay,
                    )
                    self._sleep(delay)
                    continue
                raise last_error

            if response.status_code >= 400:
                raise ProviderError(
                    f"HTTP {response.status_code} for {ticker}: {response.text[:200]}"
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise ProviderError(f"Invalid JSON response for {ticker}") from exc

            return self._parse_payload(ticker, payload, start_date, end_date)

        raise last_error or ProviderError(f"Exhausted retries for {ticker}")

    def _compute_retry_delay(self, response: requests.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After") if response.headers else None
        if retry_after:
            try:
                return max(float(retry_after), 0.0)
            except ValueError:
                pass
        return self.backoff_seconds * (2**attempt)

    @staticmethod
    def _parse_payload(
        requested_ticker: str,
        payload: Any,
        start_date: date,
        end_date: date,
    ) -> list[EarningsEvent]:
        if not isinstance(payload, dict):
            raise ProviderError(
                f"Unexpected response format for {requested_ticker}: expected JSON object"
            )

        raw_items = payload.get("earningsCalendar")
        if raw_items is None:
            raise ProviderError(
                f"Missing 'earningsCalendar' key in response for {requested_ticker}"
            )
        if not isinstance(raw_items, list):
            raise ProviderError(
                f"Expected 'earningsCalendar' to be a list for {requested_ticker}"
            )

        parsed: list[EarningsEvent] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue

            symbol = str(item.get("symbol") or requested_ticker).strip().upper()
            if symbol != requested_ticker.upper():
                continue

            raw_date = item.get("date")
            if not raw_date:
                continue
            try:
                event_date = date.fromisoformat(str(raw_date).strip())
            except ValueError:
                logger.warning("Skipping invalid date %r for %s", raw_date, symbol)
                continue

            if not (start_date <= event_date <= end_date):
                continue

            quarter = _optional_int(item.get("quarter"))
            fiscal_year = _optional_int(item.get("year"))
            eps_estimate = _optional_float(item.get("epsEstimate"))
            eps_actual = _optional_float(item.get("epsActual"))
            revenue_estimate = _optional_float(item.get("revenueEstimate"))
            revenue_actual = _optional_float(item.get("revenueActual"))
            hour = str(item.get("hour") or "").strip().lower() or None
            status = _extract_status(item)

            parsed.append(
                EarningsEvent(
                    ticker=symbol,
                    date=event_date,
                    hour=hour,
                    quarter=quarter,
                    fiscal_year=fiscal_year,
                    eps_estimate=eps_estimate,
                    eps_actual=eps_actual,
                    revenue_estimate=revenue_estimate,
                    revenue_actual=revenue_actual,
                    status=status,
                )
            )

        return parsed


class MockEarningsProvider(EarningsProvider):
    """Deterministic mock provider for offline testing and `--mock` verification."""

    def fetch_earnings(
        self,
        tickers: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> list[EarningsEvent]:
        normalized = [t.strip().upper() for t in tickers if t and t.strip()]
        if not normalized:
            raise ProviderError("No tickers provided to MockEarningsProvider.")

        sample_specs: dict[str, tuple[int, str | None, int, int, float]] = {
            "AAPL": (14, "amc", 1, 2027, 2.35),
            "MSFT": (18, "amc", 2, 2027, 3.12),
            "NVDA": (21, "amc", 3, 2027, 0.89),
            "GOOGL": (25, "amc", 4, 2026, 2.15),
            "AMZN": (28, "amc", 4, 2026, 1.48),
            "META": (32, "amc", 4, 2026, 6.75),
            "TSLA": (35, "amc", 4, 2026, 0.78),
        }
        past_sample_specs: dict[str, tuple[date, str | None, int, int, float, float]] = {
            "AAPL": (date(2026, 10, 29), "amc", 4, 2026, 1.60, 1.64),
            "MSFT": (date(2026, 10, 28), "amc", 1, 2027, 3.10, 3.30),
            "GOOGL": (date(2026, 10, 27), "amc", 3, 2026, 1.85, 2.12),
            "AMZN": (date(2026, 10, 30), "amc", 3, 2026, 1.14, 1.43),
            "META": (date(2026, 10, 28), "amc", 3, 2026, 5.25, 6.03),
            "TSLA": (date(2026, 10, 21), "amc", 3, 2026, 0.58, 0.72),
        }

        events: list[EarningsEvent] = []
        for idx, ticker in enumerate(normalized):
            if (end_date - start_date).days <= 45 and start_date < date(2026, 11, 1):
                if ticker in past_sample_specs:
                    p_date, p_hour, p_q, p_fy, p_est, p_act = past_sample_specs[ticker]
                    if start_date <= p_date <= end_date:
                        events.append(
                            EarningsEvent(
                                ticker=ticker,
                                date=p_date,
                                hour=p_hour,
                                quarter=p_q,
                                fiscal_year=p_fy,
                                eps_estimate=p_est,
                                eps_actual=p_act,
                            )
                        )
                continue

            offset_days, hour, quarter, fiscal_year, eps = sample_specs.get(
                ticker,
                (10 + (idx * 5) % 60, "bmo", 4, start_date.year, 1.50),
            )
            event_date = min(start_date + timedelta(days=offset_days), end_date)
            events.append(
                EarningsEvent(
                    ticker=ticker,
                    date=event_date,
                    hour=hour,
                    quarter=quarter,
                    fiscal_year=fiscal_year,
                    eps_estimate=eps,
                )
            )
        return events


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_status(item: dict[str, Any]) -> str | None:
    """Extract confirmed/estimated status if the API payload provides it."""
    if "confirmed" in item and item["confirmed"] is not None:
        return "Confirmed" if bool(item["confirmed"]) else "Estimated"
    raw_status = item.get("status")
    if isinstance(raw_status, str) and raw_status.strip():
        cleaned = raw_status.strip().lower()
        if cleaned in {"confirmed", "estimated", "tentative"}:
            return cleaned.capitalize()
        return raw_status.strip()
    return None
