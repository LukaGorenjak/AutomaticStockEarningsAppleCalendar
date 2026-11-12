"""Earnings calendar data providers (Finnhub and swappable provider interface)."""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from earnings_cal.models import EarningsEvent

logger = logging.getLogger(__name__)

FINNHUB_EARNINGS_URL = "https://finnhub.io/api/v1/calendar/earnings"
YAHOO_FC_URL = "https://fc.yahoo.com"
YAHOO_CRUMB_URL = "https://query2.finance.yahoo.com/v1/test/getcrumb"
YAHOO_QUOTE_SUMMARY_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
YAHOO_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
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
        ticker_errors: list[str] = []

        for ticker in normalized_tickers:
            try:
                ticker_events = self._fetch_single_ticker(ticker, start_date, end_date)
                succeeded_tickers += 1
                events.extend(ticker_events)
            except Exception as exc:
                logger.warning("Skipping ticker %s due to error: %s", ticker, exc)
                ticker_errors.append(str(exc))

        if succeeded_tickers == 0:
            detail = "; ".join(ticker_errors)
            raise ProviderError(
                f"Finnhub API requests failed for all {len(normalized_tickers)} ticker(s): {detail}"
                if detail
                else f"Finnhub API requests failed for all {len(normalized_tickers)} ticker(s)."
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


class YahooFinanceProvider(EarningsProvider):
    """Keyless earnings provider backed by Yahoo Finance `v10/finance/quoteSummary` (`calendarEvents` + `earningsHistory`).

    Works without an API key for both US symbols (e.g. `MSFT`, `NVO`) and international/European
    exchange-suffixed symbols (e.g. `RHM.DE`), and serves as an automatic fallback when
    `FINNHUB_API_KEY` is not configured or when Finnhub free tier lacks coverage.
    """

    def __init__(
        self,
        session: requests.Session | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self._crumb: str | None = None

    def _ensure_crumb(self) -> str:
        if self._crumb:
            return self._crumb
        headers = {"User-Agent": YAHOO_USER_AGENT, "Accept": "*/*"}
        try:
            self.session.get(YAHOO_FC_URL, headers=headers, timeout=self.timeout)
        except requests.RequestException:
            pass
        try:
            resp = self.session.get(YAHOO_CRUMB_URL, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ProviderError(f"Yahoo Finance crumb request failed: {exc}") from exc
        if resp.status_code >= 400 or not resp.text.strip():
            raise ProviderError(f"Yahoo Finance crumb HTTP {resp.status_code}")
        self._crumb = resp.text.strip()
        return self._crumb

    def fetch_earnings(
        self,
        tickers: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> list[EarningsEvent]:
        normalized_tickers = [t.strip().upper() for t in tickers if t and t.strip()]
        if not normalized_tickers:
            return []

        crumb = self._ensure_crumb()
        events: list[EarningsEvent] = []
        succeeded_tickers = 0
        ticker_errors: list[str] = []

        for ticker in normalized_tickers:
            try:
                ticker_events = self._fetch_single_ticker(ticker, crumb, start_date, end_date)
                succeeded_tickers += 1
                events.extend(ticker_events)
            except Exception as exc:
                logger.warning("Skipping ticker %s on Yahoo Finance due to error: %s", ticker, exc)
                ticker_errors.append(str(exc))

        if succeeded_tickers == 0:
            detail = "; ".join(ticker_errors)
            raise ProviderError(
                f"Yahoo Finance requests failed for all {len(normalized_tickers)} ticker(s): {detail}"
                if detail
                else f"Yahoo Finance requests failed for all {len(normalized_tickers)} ticker(s)."
            )

        if not events:
            raise ProviderError(
                f"Yahoo Finance returned no earnings events for any of the "
                f"{len(normalized_tickers)} ticker(s) between {start_date} and {end_date}."
            )

        return events

    def _fetch_single_ticker(
        self,
        ticker: str,
        crumb: str,
        start_date: date,
        end_date: date,
    ) -> list[EarningsEvent]:
        url = YAHOO_QUOTE_SUMMARY_URL.format(symbol=ticker)
        params = {
            "modules": "calendarEvents,earningsHistory,earningsTrend",
            "crumb": crumb,
        }
        headers = {"User-Agent": YAHOO_USER_AGENT, "Accept": "application/json"}
        try:
            resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ProviderError(f"Yahoo Finance request failed for {ticker}: {exc}") from exc

        if resp.status_code >= 400:
            raise ProviderError(f"Yahoo Finance HTTP {resp.status_code} for {ticker}")

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ProviderError(f"Invalid Yahoo Finance JSON for {ticker}") from exc

        results = ((payload or {}).get("quoteSummary") or {}).get("result") or []
        if not results or not isinstance(results[0], dict):
            return []

        return self._parse_quote_summary(ticker, results[0], start_date, end_date)

    @staticmethod
    def _parse_quote_summary(
        ticker: str,
        summary_obj: dict[str, Any],
        start_date: date,
        end_date: date,
    ) -> list[EarningsEvent]:
        cal_earnings = ((summary_obj.get("calendarEvents") or {}).get("earnings")) or {}
        raw_dates = cal_earnings.get("earningsDate") or []
        if not isinstance(raw_dates, list) or not raw_dates:
            return []

        first_dt_item = raw_dates[0] if isinstance(raw_dates[0], dict) else {}
        fmt_date = str(first_dt_item.get("fmt") or "").strip()
        raw_ts = first_dt_item.get("raw")

        event_date: date | None = None
        if fmt_date:
            try:
                event_date = date.fromisoformat(fmt_date)
            except ValueError:
                event_date = None
        if event_date is None and isinstance(raw_ts, (int, float)):
            event_date = datetime.fromtimestamp(raw_ts, tz=timezone.utc).date()

        if event_date is None or not (start_date <= event_date <= end_date):
            return []

        # Infer BMO / AMC / DMH in the ticker's local exchange timezone
        hour: str | None = None
        if isinstance(raw_ts, (int, float)):
            local_tz = ZoneInfo("Europe/Berlin" if "." in ticker else "America/New_York")
            dt_local = datetime.fromtimestamp(raw_ts, tz=timezone.utc).astimezone(local_tz)
            if dt_local.hour < 10:
                hour = "bmo"
            elif dt_local.hour >= 16:
                hour = "amc"
            else:
                hour = "dmh"

        is_est = cal_earnings.get("isEarningsDateEstimate")
        status = "Estimated" if is_est is True else ("Confirmed" if is_est is False else None)

        eps_estimate = _optional_float((cal_earnings.get("earningsAverage") or {}).get("raw"))
        revenue_estimate = _optional_float((cal_earnings.get("revenueAverage") or {}).get("raw"))
        eps_actual: float | None = None

        quarter: int | None = None
        fiscal_year: int | None = None

        # Match against earningsTrend ("0q") for upcoming or earningsHistory ("-1q") for recent past
        history_list = ((summary_obj.get("earningsHistory") or {}).get("history")) or []
        trend_list = ((summary_obj.get("earningsTrend") or {}).get("trend")) or []

        for item in trend_list:
            if isinstance(item, dict) and item.get("period") == "0q":
                end_str = str(item.get("endDate") or "").strip()
                try:
                    q_end = date.fromisoformat(end_str)
                    quarter = (q_end.month - 1) // 3 + 1
                    fiscal_year = q_end.year
                except ValueError:
                    pass
                break

        if isinstance(history_list, list) and history_list:
            last_hist = history_list[-1] if isinstance(history_list[-1], dict) else {}
            hist_eps_act = _optional_float((last_hist.get("epsActual") or {}).get("raw"))
            hist_eps_est = _optional_float((last_hist.get("epsEstimate") or {}).get("raw"))
            hist_q_fmt = str((last_hist.get("quarter") or {}).get("fmt") or "").strip()
            # If this event is already reported (status Confirmed & EPS actual present on latest history)
            if is_est is False and hist_eps_act is not None and event_date <= date.today():
                eps_actual = hist_eps_act
                if hist_eps_est is not None:
                    eps_estimate = hist_eps_est
                if hist_q_fmt:
                    try:
                        hq_date = date.fromisoformat(hist_q_fmt)
                        quarter = (hq_date.month - 1) // 3 + 1
                        fiscal_year = hq_date.year
                    except ValueError:
                        pass

        if quarter is None:
            quarter = (event_date.month - 1) // 3 + 1
        if fiscal_year is None:
            fiscal_year = event_date.year

        return [
            EarningsEvent(
                ticker=ticker.upper(),
                date=event_date,
                hour=hour,
                quarter=quarter,
                fiscal_year=fiscal_year,
                eps_estimate=eps_estimate,
                eps_actual=eps_actual,
                revenue_estimate=revenue_estimate,
                status=status,
            )
        ]


class FallbackEarningsProvider(EarningsProvider):
    """Tries `primary` first and falls back to `fallback` for missing tickers or on primary failure."""

    def __init__(self, primary: EarningsProvider, fallback: EarningsProvider) -> None:
        self.primary = primary
        self.fallback = fallback

    def fetch_earnings(
        self,
        tickers: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> list[EarningsEvent]:
        normalized = [t.strip().upper() for t in tickers if t and t.strip()]
        if not normalized:
            return []

        primary_events: list[EarningsEvent] = []
        primary_error: Exception | None = None
        try:
            primary_events = self.primary.fetch_earnings(normalized, start_date, end_date)
        except Exception as exc:
            primary_error = exc
            logger.info("Primary provider did not return all events (%s); trying fallback provider.", exc)

        covered = {ev.ticker.upper() for ev in primary_events}
        missing = [t for t in normalized if t not in covered]
        if not missing:
            return primary_events

        try:
            fallback_events = self.fallback.fetch_earnings(missing, start_date, end_date)
            return primary_events + fallback_events
        except Exception as fb_exc:
            if primary_events:
                return primary_events
            if primary_error is not None:
                raise ProviderError(f"{primary_error}; fallback also failed: {fb_exc}") from fb_exc
            raise


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
