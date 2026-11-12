"""Post-earnings results tracker (`data/results.csv`), surprise & price providers, and summary CLI."""

from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import re
import sys
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from earnings_cal.fetch import (
    EarningsProvider,
    FinnhubProvider,
    MockEarningsProvider,
    ProviderError,
)
from earnings_cal.markets.ljse import DEFAULT_LJSE_EVENTS_FILE, load_ljse_events_yaml
from earnings_cal.models import (
     DateWindow,
    Event,
    EventKind,
    EventTiming,
    WatchlistConfig,
    load_config,
)

logger = logging.getLogger(__name__)

DEFAULT_RESULTS_PATH = Path("data/results.csv")
DEFAULT_RESULTS_LOOKBACK_DAYS = 30
FINNHUB_SURPRISES_URL = "https://finnhub.io/api/v1/stock/earnings"
YAHOO_CHART_URL_TEMPLATE = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
LJSE_PRICE_LIST_URL_TEMPLATE = (
    "https://rest.ljse.si/web/Bvt9fe2peQ7pwpyYqODM/price-list/XLJU/{date}/json"
)

RESULTS_CSV_COLUMNS: tuple[str, ...] = (
    "market",
    "ticker",
    "report_date",
    "fiscal_period",
    "eps_estimate",
    "eps_actual",
    "surprise_pct",
    "price_move_1d_pct",
    "recorded_at",
    "manual_notes",
)


@dataclass(frozen=True)
class EarningsSurpriseRecord:
    """Actual vs estimated EPS (and optional revenue) for a completed earnings report."""

    ticker: str
    fiscal_period: str | None = None
    quarter: int | None = None
    fiscal_year: int | None = None
    period_end: date | None = None
    report_date: date | None = None
    eps_estimate: float | None = None
    eps_actual: float | None = None
    surprise_pct: float | None = None
    revenue_estimate: float | None = None
    revenue_actual: float | None = None


@dataclass(frozen=True)
class DailyClose:
    """Single daily closing price for a security."""

    date: date
    close: float


@dataclass(frozen=True)
class EarningsResultRow:
    """One row in `data/results.csv`, uniquely keyed by `(market, ticker, fiscal_period)`."""

    market: str
    ticker: str
    report_date: date
    fiscal_period: str
    eps_estimate: float | None = None
    eps_actual: float | None = None
    surprise_pct: float | None = None
    price_move_1d_pct: float | None = None
    recorded_at: str = ""
    manual_notes: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        """Unique deduplication key: `(market, ticker, fiscal_period)`."""
        return (
            self.market.strip().lower(),
            self.ticker.strip().upper(),
            self.fiscal_period.strip(),
        )

    def to_csv_dict(self) -> dict[str, str]:
        """Format row values deterministically for `data/results.csv`."""
        return {
            "market": self.market.strip().lower(),
            "ticker": self.ticker.strip().upper(),
            "report_date": self.report_date.isoformat(),
            "fiscal_period": self.fiscal_period.strip(),
            "eps_estimate": _fmt_float(self.eps_estimate),
            "eps_actual": _fmt_float(self.eps_actual),
            "surprise_pct": _fmt_float(self.surprise_pct),
            "price_move_1d_pct": _fmt_float(self.price_move_1d_pct),
            "recorded_at": self.recorded_at.strip(),
            "manual_notes": self.manual_notes.strip(),
        }


def _fmt_float(value: float | None) -> str:
    if value is None:
        return ""
    return f"{float(value):.2f}"


def _parse_optional_float(raw: str | float | int | None) -> float | None:
    if raw is None:
        return None
    cleaned = str(raw).strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def compute_surprise_pct(
    eps_estimate: float | None,
    eps_actual: float | None,
    explicit_surprise_pct: float | None = None,
) -> float | None:
    """Compute percentage EPS surprise `((actual - estimate) / |estimate|) * 100`."""
    if explicit_surprise_pct is not None:
        return round(float(explicit_surprise_pct), 2)
    if eps_estimate is None or eps_actual is None:
        return None
    if abs(eps_estimate) < 1e-9:
        return None
    return round(((eps_actual - eps_estimate) / abs(eps_estimate)) * 100.0, 2)


def infer_fiscal_period(event: Event) -> str:
    """Derive a stable `fiscal_period` string for `(market, ticker, fiscal_period)` keying."""
    if event.fiscal_year is not None and event.quarter is not None:
        return f"{event.fiscal_year}Q{event.quarter}"
    if event.fiscal_year is not None:
        return f"FY{event.fiscal_year}"

    text = f"{event.title or ''} {event.description or ''} {event.note or ''}"
    m_q_range = re.search(r"\b(Q1[\s\-–—]+Q[234])\s+(20\d{2})\b", text, re.I)
    if m_q_range:
        q_part = re.sub(r"[\s–—]+", "-", m_q_range.group(1).upper())
        return f"{m_q_range.group(2)}{q_part}"

    m_q_single = re.search(r"\bQ([1-4])\s+(20\d{2})\b", text, re.I)
    if m_q_single:
        return f"{m_q_single.group(2)}Q{m_q_single.group(1)}"

    m_fy = re.search(
        r"\b(20\d{2})\s+(?:Annual Report|Preliminary Results|Unaudited Financial Statements)\b",
        text,
        re.I,
    )
    if m_fy:
        return f"FY{m_fy.group(1)}"

    return event.date.isoformat()


# ============================================================================
# Provider Interfaces (Earnings Surprises & Daily Prices)
# ============================================================================


class EarningsSurpriseProvider(ABC):
    """Abstract interface for fetching actual vs estimated EPS/revenue surprises."""

    @abstractmethod
    def fetch_surprises(
        self,
        ticker: str,
        start_date: date,
        end_date: date,
    ) -> list[EarningsSurpriseRecord]:
        """Return earnings surprise records for `ticker` around `[start_date, end_date]`."""


class PriceProvider(ABC):
    """Separate abstract interface for fetching daily closing prices to compute 1-day post-earnings moves.

    Kept behind its own provider abstraction because free price sources (Yahoo Finance,
    LJSE REST price list, Stooq, etc.) can change rate limits or formats independently
    of the earnings calendar API.
    """

    @abstractmethod
    def fetch_daily_closes(
        self,
        market: str,
        ticker: str,
        start_date: date,
        end_date: date,
    ) -> list[DailyClose]:
        """Fetch sorted daily closing prices for `(market, ticker)` within `[start_date, end_date]`."""


class FinnhubSurpriseProvider(EarningsSurpriseProvider):
    """Fetches actual vs estimated EPS & revenue from Finnhub free-tier endpoints:
    1. `GET /api/v1/calendar/earnings?symbol=...&from=...&to=...` (includes `epsActual`, `epsEstimate`, `revenueActual`, `revenueEstimate` for the last 1 month on free tier)
    2. `GET /api/v1/stock/earnings?symbol=...` (last 4 quarters of `actual`, `estimate`, `surprisePercent` on free tier)
    """

    def __init__(
        self,
        api_key: str | None = None,
        session: requests.Session | None = None,
        calendar_url: str = "https://finnhub.io/api/v1/calendar/earnings",
        surprises_url: str = FINNHUB_SURPRISES_URL,
        timeout: float = 15.0,
    ) -> None:
        resolved_key = (api_key if api_key is not None else os.environ.get("FINNHUB_API_KEY", "")).strip()
        if not resolved_key:
            raise ProviderError("Missing Finnhub API key for FinnhubSurpriseProvider.")
        self.api_key = resolved_key
        self.session = session or requests.Session()
        self.calendar_url = calendar_url
        self.surprises_url = surprises_url
        self.timeout = timeout

    def fetch_surprises(
        self,
        ticker: str,
        start_date: date,
        end_date: date,
    ) -> list[EarningsSurpriseRecord]:
        symbol = ticker.strip().upper()
        headers = {"X-Finnhub-Token": self.api_key, "Accept": "application/json"}
        records: list[EarningsSurpriseRecord] = []

        # 1. Try `/calendar/earnings` for `[start_date, end_date]` (gives exact report date + epsActual + revenueActual)
        try:
            resp = self.session.get(
                self.calendar_url,
                params={"symbol": symbol, "from": start_date.isoformat(), "to": end_date.isoformat()},
                headers=headers,
                timeout=self.timeout,
            )
            if resp.status_code == 200:
                payload = resp.json()
                items = payload.get("earningsCalendar") if isinstance(payload, dict) else []
                if isinstance(items, list):
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        if str(item.get("symbol") or symbol).strip().upper() != symbol:
                            continue
                        raw_dt = item.get("date")
                        rep_date = date.fromisoformat(str(raw_dt).strip()) if raw_dt else None
                        q = int(item["quarter"]) if item.get("quarter") is not None else None
                        fy = int(item["year"]) if item.get("year") is not None else None
                        fp = f"{fy}Q{q}" if (fy is not None and q is not None) else None
                        est = _parse_optional_float(item.get("epsEstimate"))
                        act = _parse_optional_float(item.get("epsActual"))
                        rev_est = _parse_optional_float(item.get("revenueEstimate"))
                        rev_act = _parse_optional_float(item.get("revenueActual"))
                        records.append(
                            EarningsSurpriseRecord(
                                ticker=symbol,
                                fiscal_period=fp,
                                quarter=q,
                                fiscal_year=fy,
                                report_date=rep_date,
                                eps_estimate=est,
                                eps_actual=act,
                                surprise_pct=compute_surprise_pct(est, act),
                                revenue_estimate=rev_est,
                                revenue_actual=rev_act,
                            )
                        )
        except Exception as exc:
            logger.warning("Finnhub /calendar/earnings surprise lookup failed for %s: %s", symbol, exc)

        # 2. Also query `/stock/earnings` (last 4 quarters on free tier, includes surprisePercent)
        try:
            resp2 = self.session.get(
                self.surprises_url,
                params={"symbol": symbol},
                headers=headers,
                timeout=self.timeout,
            )
            if resp2.status_code == 200:
                items2 = resp2.json()
                if isinstance(items2, list):
                    for item in items2:
                        if not isinstance(item, dict):
                            continue
                        q = int(item["quarter"]) if item.get("quarter") is not None else None
                        fy = int(item["year"]) if item.get("year") is not None else None
                        fp = f"{fy}Q{q}" if (fy is not None and q is not None) else None
                        raw_period = item.get("period")
                        p_end = date.fromisoformat(str(raw_period).strip()) if raw_period else None
                        est = _parse_optional_float(item.get("estimate"))
                        act = _parse_optional_float(item.get("actual"))
                        surp = _parse_optional_float(item.get("surprisePercent"))
                        records.append(
                            EarningsSurpriseRecord(
                                ticker=symbol,
                                fiscal_period=fp,
                                quarter=q,
                                fiscal_year=fy,
                                period_end=p_end,
                                eps_estimate=est,
                                eps_actual=act,
                                surprise_pct=compute_surprise_pct(est, act, surp),
                            )
                        )
        except Exception as exc:
            logger.warning("Finnhub /stock/earnings surprise lookup failed for %s: %s", symbol, exc)

        return records


class MockSurpriseProvider(EarningsSurpriseProvider):
    """Deterministic mock surprise provider for offline testing and `--mock` runs."""

    def __init__(self, data: Mapping[str, Sequence[EarningsSurpriseRecord]] | None = None) -> None:
        self._data: dict[str, list[EarningsSurpriseRecord]] = {
            k.upper(): list(v) for k, v in (data or {}).items()
        }

    def fetch_surprises(
        self,
        ticker: str,
        start_date: date,
        end_date: date,
    ) -> list[EarningsSurpriseRecord]:
        symbol = ticker.strip().upper()
        if symbol in self._data:
            return list(self._data[symbol])

        default_specs: dict[str, tuple[str, date, float, float]] = {
            "AAPL": ("2026Q4", date(2026, 10, 29), 1.60, 1.64),
            "MSFT": ("2027Q1", date(2026, 10, 28), 3.10, 3.30),
            "GOOGL": ("2026Q3", date(2026, 10, 27), 1.85, 2.12),
            "AMZN": ("2026Q3", date(2026, 10, 30), 1.14, 1.43),
            "META": ("2026Q3", date(2026, 10, 28), 5.25, 6.03),
            "TSLA": ("2026Q3", date(2026, 10, 21), 0.58, 0.72),
        }
        if symbol in default_specs:
            fp, rep_dt, est, act = default_specs[symbol]
            if start_date <= rep_dt <= end_date:
                return [
                    EarningsSurpriseRecord(
                        ticker=symbol,
                        fiscal_period=fp,
                        report_date=rep_dt,
                        eps_estimate=est,
                        eps_actual=act,
                        surprise_pct=compute_surprise_pct(est, act),
                    )
                ]
        return []


class YahooAndLJSEPriceProvider(PriceProvider):
    """Default free daily price provider:
    - US equities (`market='us'`): Yahoo Finance v8 chart JSON API (`query1.finance.yahoo.com/v8/finance/chart/{symbol}`)
    - LJSE equities (`market='ljse'`): Ljubljana Stock Exchange official REST price list (`rest.ljse.si/.../price-list/XLJU/{date}/json`)
    """

    def __init__(
        self,
        session: requests.Session | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self._headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        }

    def fetch_daily_closes(
        self,
        market: str,
        ticker: str,
        start_date: date,
        end_date: date,
    ) -> list[DailyClose]:
        mkt = market.strip().lower()
        symbol = ticker.strip().upper()
        if mkt == "ljse":
            return self._fetch_ljse_closes(symbol, start_date, end_date)
        return self._fetch_yahoo_closes(symbol, start_date, end_date)

    def _fetch_yahoo_closes(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> list[DailyClose]:
        p1 = int(datetime.combine(start_date, dt_time.min, tzinfo=timezone.utc).timestamp())
        p2 = int(
            datetime.combine(end_date + timedelta(days=1), dt_time.min, tzinfo=timezone.utc).timestamp()
        )
        url = YAHOO_CHART_URL_TEMPLATE.format(symbol=symbol)
        resp = self.session.get(
            url,
            params={"interval": "1d", "period1": p1, "period2": p2},
            headers=self._headers,
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise ProviderError(f"Yahoo Finance HTTP {resp.status_code} for {symbol}")

        payload = resp.json()
        results = ((payload.get("chart") or {}).get("result")) or []
        if not results:
            return []

        first = results[0]
        timestamps = first.get("timestamp") or []
        quotes = ((first.get("indicators") or {}).get("quote")) or [{}]
        closes = (quotes[0] if quotes else {}).get("close") or []

        by_date: dict[date, DailyClose] = {}
        for ts, close_val in zip(timestamps, closes):
            if ts is None or close_val is None:
                continue
            dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
            if start_date <= dt <= end_date:
                by_date[dt] = DailyClose(date=dt, close=float(close_val))

        return sorted(by_date.values(), key=lambda c: c.date)

    def _fetch_ljse_closes(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> list[DailyClose]:
        closes: list[DailyClose] = []
        cursor = start_date
        while cursor <= end_date:
            if cursor.weekday() < 5:  # Monday..Friday
                url = LJSE_PRICE_LIST_URL_TEMPLATE.format(date=cursor.isoformat())
                try:
                    resp = self.session.get(url, headers=self._headers, timeout=self.timeout)
                    if resp.status_code == 200:
                        payload = resp.json()
                        for sec in payload.get("securities") or []:
                            if str(sec.get("symbol") or "").strip().upper() != symbol:
                                continue
                            trade_dt_str = str(sec.get("trade_date") or sec.get("date") or "").strip()
                            raw_close = sec.get("close_price")
                            if trade_dt_str == cursor.isoformat() and raw_close is not None:
                                closes.append(DailyClose(date=cursor, close=float(raw_close)))
                                break
                except Exception as exc:
                    logger.debug("LJSE price lookup skipped on %s for %s: %s", cursor, symbol, exc)
            cursor += timedelta(days=1)
        return sorted(closes, key=lambda c: c.date)


class MockPriceProvider(PriceProvider):
    """Deterministic mock daily price provider for offline tests and `--mock` mode."""

    def __init__(
        self,
        closes_by_symbol: Mapping[str, Sequence[DailyClose | tuple[date | str, float]]] | None = None,
    ) -> None:
        self._closes: dict[str, list[DailyClose]] = {}
        for key, items in (closes_by_symbol or {}).items():
            parsed_list: list[DailyClose] = []
            for item in items:
                if isinstance(item, DailyClose):
                    parsed_list.append(item)
                else:
                    raw_d, raw_c = item
                    d = date.fromisoformat(raw_d) if isinstance(raw_d, str) else raw_d
                    parsed_list.append(DailyClose(date=d, close=float(raw_c)))
            self._closes[key.upper()] = sorted(parsed_list, key=lambda c: c.date)

    def fetch_daily_closes(
        self,
        market: str,
        ticker: str,
        start_date: date,
        end_date: date,
    ) -> list[DailyClose]:
        symbol = ticker.strip().upper()
        scoped_key = f"{market.strip().upper()}:{symbol}"
        if scoped_key in self._closes:
            return [c for c in self._closes[scoped_key] if start_date <= c.date <= end_date]
        if symbol in self._closes:
            return [c for c in self._closes[symbol] if start_date <= c.date <= end_date]

        default_series: dict[str, list[tuple[date, float]]] = {
            "AAPL": [(date(2026, 10, 29), 230.00), (date(2026, 10, 30), 233.45)],
            "MSFT": [(date(2026, 10, 28), 425.00), (date(2026, 10, 29), 431.80)],
            "GOOGL": [(date(2026, 10, 27), 180.00), (date(2026, 10, 28), 185.40)],
            "AMZN": [(date(2026, 10, 30), 205.00), (date(2026, 11, 2), 217.30)],
            "META": [(date(2026, 10, 28), 580.00), (date(2026, 10, 29), 568.40)],
            "TSLA": [(date(2026, 10, 21), 240.00), (date(2026, 10, 22), 268.80)],
            "CICG": [(date(2026, 11, 10), 32.50), (date(2026, 11, 11), 33.15)],
        }
        series = default_series.get(symbol, [])
        return [
            DailyClose(date=d, close=c)
            for d, c in series
            if start_date <= d <= end_date
        ]


# ============================================================================
# 1-Day Price Move Calculation (Timing-Aware: BMO / DMH / UNKNOWN vs AMC)
# ============================================================================


def compute_1d_price_move_pct(
    event: Event,
    closes: Sequence[DailyClose],
) -> float | None:
    """Compute the 1-trading-day percentage price reaction after `event`.

    - For `amc` (After Market Close) releases on `report_date`:
      - Pre-release close = last trading day on or before `report_date`
      - Post-release close = first trading day strictly after `report_date`
    - For `bmo`, `dmh`, `exact` (pre/during market), or `unknown` releases on `report_date`:
      - Pre-release close = last trading day strictly before `report_date`
      - Post-release close = first trading day on or after `report_date`
    """
    ordered = sorted(closes, key=lambda c: c.date)
    if len(ordered) < 2:
        return None

    timing = event.timing if isinstance(event.timing, EventTiming) else EventTiming(event.timing)
    is_after_close = timing == EventTiming.AMC or (
        timing == EventTiming.EXACT
        and event.time is not None
        and event.time >= "16:00"
    )

    if is_after_close:
        before_candidates = [c for c in ordered if c.date <= event.date]
        after_candidates = [c for c in ordered if c.date > event.date]
    else:
        before_candidates = [c for c in ordered if c.date < event.date]
        after_candidates = [c for c in ordered if c.date >= event.date]

    if not before_candidates or not after_candidates:
        return None

    pre_close = before_candidates[-1].close
    post_close = after_candidates[0].close
    if abs(pre_close) < 1e-9:
        return None

    return round(((post_close - pre_close) / pre_close) * 100.0, 2)


# ============================================================================
# CSV Load / Save / Deterministic Merge
# ============================================================================


def load_results_csv(path: Path | str = DEFAULT_RESULTS_PATH) -> list[EarningsResultRow]:
    """Load existing rows from `data/results.csv`. Returns `[]` if file does not exist."""
    csv_path = Path(path)
    if not csv_path.exists():
        return []

    text = csv_path.read_text(encoding="utf-8")
    if not text.strip():
        return []

    reader = csv.DictReader(io.StringIO(text))
    rows: list[EarningsResultRow] = []
    for raw in reader:
        if not raw:
            continue
        market = str(raw.get("market") or "").strip().lower()
        ticker = str(raw.get("ticker") or "").strip().upper()
        rep_date_str = str(raw.get("report_date") or "").strip()
        fiscal_period = str(raw.get("fiscal_period") or "").strip()
        if not (market and ticker and rep_date_str and fiscal_period):
            continue
        rows.append(
            EarningsResultRow(
                market=market,
                ticker=ticker,
                report_date=date.fromisoformat(rep_date_str),
                fiscal_period=fiscal_period,
                eps_estimate=_parse_optional_float(raw.get("eps_estimate")),
                eps_actual=_parse_optional_float(raw.get("eps_actual")),
                surprise_pct=_parse_optional_float(raw.get("surprise_pct")),
                price_move_1d_pct=_parse_optional_float(raw.get("price_move_1d_pct")),
                recorded_at=str(raw.get("recorded_at") or "").strip(),
                manual_notes=str(raw.get("manual_notes") or "").strip(),
            )
        )
    return rows


def sort_results_rows(rows: Iterable[EarningsResultRow]) -> list[EarningsResultRow]:
    """Deterministically sort rows by `(report_date, market, ticker, fiscal_period)`."""
    return sorted(
        rows,
        key=lambda r: (
            r.report_date.isoformat(),
            r.market.strip().lower(),
            r.ticker.strip().upper(),
            r.fiscal_period.strip(),
        ),
    )


def write_results_csv(
    rows: Iterable[EarningsResultRow],
    path: Path | str = DEFAULT_RESULTS_PATH,
) -> bool:
    """Write deterministically ordered `data/results.csv`. Returns True if file changed."""
    csv_path = Path(path)
    ordered = sort_results_rows(rows)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(RESULTS_CSV_COLUMNS), lineterminator="\n")
    writer.writeheader()
    for row in ordered:
        writer.writerow(row.to_csv_dict())

    new_text = buf.getvalue()
    if csv_path.exists() and csv_path.read_text(encoding="utf-8") == new_text:
        return False

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text(new_text, encoding="utf-8")
    return True


def _is_row_complete_within_window(row: EarningsResultRow) -> bool:
    """Check whether an existing row already has all expected fields populated."""
    if row.market == "ljse":
        return row.price_move_1d_pct is not None
    return (
        row.eps_estimate is not None
        and row.eps_actual is not None
        and row.surprise_pct is not None
        and row.price_move_1d_pct is not None
    )


def merge_result_row(
    existing: EarningsResultRow,
    candidate: EarningsResultRow,
    correction: bool = False,
    in_retry_window: bool = True,
) -> tuple[EarningsResultRow, bool]:
    """Combine `existing` and `candidate` for the same `(market, ticker, fiscal_period)` key.

    Rules:
    - Never overwrite an existing non-empty field unless `correction=True`.
    - When `correction=False` and `in_retry_window=True`, fill in ONLY previously
      missing (`None` / empty) fields when late data arrives, and update `recorded_at`
      only if at least one missing field was newly populated.
    """
    if correction:
        updated = EarningsResultRow(
            market=existing.market,
            ticker=existing.ticker,
            report_date=candidate.report_date,
            fiscal_period=existing.fiscal_period,
            eps_estimate=(
                candidate.eps_estimate
                if candidate.eps_estimate is not None
                else existing.eps_estimate
            ),
            eps_actual=(
                candidate.eps_actual
                if candidate.eps_actual is not None
                else existing.eps_actual
            ),
            surprise_pct=(
                candidate.surprise_pct
                if candidate.surprise_pct is not None
                else existing.surprise_pct
            ),
            price_move_1d_pct=(
                candidate.price_move_1d_pct
                if candidate.price_move_1d_pct is not None
                else existing.price_move_1d_pct
            ),
            recorded_at=candidate.recorded_at or existing.recorded_at,
            manual_notes=candidate.manual_notes or existing.manual_notes,
        )
        changed = updated.to_csv_dict() != existing.to_csv_dict()
        return (updated if changed else existing), changed

    if not in_retry_window:
        return existing, False

    new_est = existing.eps_estimate if existing.eps_estimate is not None else candidate.eps_estimate
    new_act = existing.eps_actual if existing.eps_actual is not None else candidate.eps_actual
    new_surp = (
        existing.surprise_pct
        if existing.surprise_pct is not None
        else (candidate.surprise_pct if candidate.surprise_pct is not None else compute_surprise_pct(new_est, new_act))
    )
    new_move = (
        existing.price_move_1d_pct
        if existing.price_move_1d_pct is not None
        else candidate.price_move_1d_pct
    )
    new_notes = existing.manual_notes if existing.manual_notes else candidate.manual_notes

    filled_any = (
        (existing.eps_estimate is None and new_est is not None)
        or (existing.eps_actual is None and new_act is not None)
        or (existing.surprise_pct is None and new_surp is not None)
        or (existing.price_move_1d_pct is None and new_move is not None)
        or (not existing.manual_notes and bool(new_notes))
    )
    if not filled_any:
        return existing, False

    updated = replace(
        existing,
        eps_estimate=new_est,
        eps_actual=new_act,
        surprise_pct=new_surp,
        price_move_1d_pct=new_move,
        recorded_at=candidate.recorded_at or existing.recorded_at,
        manual_notes=new_notes,
    )
    return updated, True


# ============================================================================
# Past Watchlist Event Discovery & Results Recording Pipeline
# ============================================================================


def _discover_past_watchlist_earnings(
    config: WatchlistConfig,
    window_start: date,
    yesterday: date,
    earnings_provider: EarningsProvider | None,
    use_mock: bool,
) -> list[Event]:
    """Find past `earnings` events in `[window_start, yesterday]` across watchlist markets."""
    if window_start > yesterday:
        return []

    past_window = DateWindow(start=window_start, end=yesterday)
    discovered: list[Event] = []

    # 1. US & EU watchlists (Finnhub past window + manual_events)
    active_provider = earnings_provider
    if active_provider is None:
        env_key = os.environ.get("FINNHUB_API_KEY", "").strip()
        if use_mock or env_key.lower() == "mock":
            active_provider = MockEarningsProvider()
        elif env_key:
            try:
                active_provider = FinnhubProvider(api_key=env_key)
            except ProviderError as exc:
                logger.warning("Skipping API past earnings lookup: %s", exc)

    for api_market_id in ("us", "eu"):
        if api_market_id != "us" and api_market_id not in config.markets:
            continue
        section = config.get_market_section(api_market_id)
        raw_tickers = section.get("tickers") if api_market_id != "us" else (section.get("tickers") or config.tickers)
        api_tickers = [str(t).strip().upper() for t in (raw_tickers or []) if str(t).strip()]
        raw_manual = section.get("manual_events") if api_market_id != "us" else (section.get("manual_events") or config.manual_events)
        manual_list: list[Event] = list(raw_manual or [])

        if api_tickers and active_provider is not None and section.get("use_api", True):
            try:
                api_past = active_provider.fetch_earnings(
                    api_tickers,
                    start_date=window_start,
                    end_date=yesterday,
                )
                for ev in api_past:
                    if ev.kind == EventKind.EARNINGS and past_window.contains(ev.date):
                        discovered.append(
                            replace(ev, market=api_market_id, kind=EventKind.EARNINGS)
                            if ev.market != api_market_id
                            else ev
                        )
            except Exception as exc:
                logger.info(
                    "No past %s API earnings returned for [%s, %s]: %s",
                    api_market_id.upper(),
                    window_start,
                    yesterday,
                    exc,
                )

        for ev in manual_list:
            if ev.kind == EventKind.EARNINGS and past_window.contains(ev.date):
                discovered.append(
                    replace(ev, market=api_market_id) if ev.market != api_market_id else ev
                )

    # 2. LJSE watchlist (`ljse_events.yaml`, only `kind == EventKind.EARNINGS`)
    ljse_section = config.get_market_section("ljse")
    ljse_tickers = [
        str(t).strip().upper()
        for t in (ljse_section.get("tickers") or config.tickers_ljse)
        if str(t).strip()
    ]
    configured_ljse_file = ljse_section.get("events_file") or DEFAULT_LJSE_EVENTS_FILE
    ljse_path = Path(configured_ljse_file)
    if not ljse_path.is_absolute():
        candidate = config.config_dir / ljse_path
        if candidate.exists():
            ljse_path = candidate

    if ljse_path.exists():
        try:
            ljse_events = load_ljse_events_yaml(
                path=ljse_path,
                allowed_tickers=ljse_tickers or None,
                window=past_window,
            )
            for ev in ljse_events:
                if ev.kind == EventKind.EARNINGS:
                    discovered.append(ev)
        except Exception as exc:
            logger.warning("Skipping LJSE past earnings lookup from %s: %s", ljse_path, exc)

    # Deduplicate by `(market, ticker, fiscal_period)` keeping the latest event definition
    by_key: dict[tuple[str, str, str], Event] = {}
    for ev in discovered:
        fp = infer_fiscal_period(ev)
        by_key[(ev.market.lower(), ev.ticker.upper(), fp)] = ev

    return sorted(by_key.values(), key=lambda e: (e.date.isoformat(), e.market, e.ticker))


def _match_surprise_for_event(
    event: Event,
    fiscal_period: str,
    surprises: Sequence[EarningsSurpriseRecord],
) -> EarningsSurpriseRecord | None:
    """Match the most relevant `EarningsSurpriseRecord` for `event`."""
    for rec in surprises:
        if rec.fiscal_period and rec.fiscal_period == fiscal_period:
            return rec
    for rec in surprises:
        if rec.report_date and abs((rec.report_date - event.date).days) <= 5:
            return rec
    return None


def update_results(
    config: WatchlistConfig | Path | str = Path("tickers.yaml"),
    results_path: Path | str = DEFAULT_RESULTS_PATH,
    today: date | None = None,
    now: datetime | None = None,
    lookback_days: int = DEFAULT_RESULTS_LOOKBACK_DAYS,
    correction: bool = False,
    earnings_provider: EarningsProvider | None = None,
    surprise_provider: EarningsSurpriseProvider | None = None,
    price_provider: PriceProvider | None = None,
    use_mock: bool = False,
    past_events: Sequence[Event] | None = None,
) -> list[EarningsResultRow]:
    """Find past earnings events in the watchlist and record/update rows in `data/results.csv`.

    - Key = `(market, ticker, fiscal_period)` — no duplicates.
    - Existing rows are never overwritten unless `correction=True`.
    - Within the limited retry window (`lookback_days`), missing fields (`None`) on
      incomplete rows are retried and filled in when late data arrives without
      touching already-recorded fields.
    - Missing data from providers is never treated as an error.
    """
    now_utc = (
        (now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc))
        if now is not None
        else (
            datetime.combine(today, dt_time.min, tzinfo=timezone.utc)
            if today is not None
            else datetime.now(timezone.utc)
        )
    )
    ref_today = today or now_utc.date()
    recorded_ts = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    loaded_cfg = load_config(config) if isinstance(config, (Path, str)) else config
    window_start = ref_today - timedelta(days=lookback_days)
    yesterday = ref_today - timedelta(days=1)

    existing_rows = load_results_csv(results_path)
    by_key: dict[tuple[str, str, str], EarningsResultRow] = {
        row.key: row for row in existing_rows
    }

    if past_events is not None:
        candidate_events = [
            ev
            for ev in past_events
            if ev.kind == EventKind.EARNINGS and window_start <= ev.date <= yesterday
        ]
    else:
        candidate_events = _discover_past_watchlist_earnings(
            config=loaded_cfg,
            window_start=window_start,
            yesterday=yesterday,
            earnings_provider=earnings_provider,
            use_mock=use_mock,
        )

    # Resolve surprise provider
    active_surprise = surprise_provider
    if active_surprise is None:
        env_key = os.environ.get("FINNHUB_API_KEY", "").strip()
        if use_mock or env_key.lower() == "mock":
            active_surprise = MockSurpriseProvider()
        elif env_key:
            try:
                active_surprise = FinnhubSurpriseProvider(api_key=env_key)
            except ProviderError:
                active_surprise = None

    # Resolve price provider
    active_price = price_provider
    if active_price is None:
        active_price = MockPriceProvider() if use_mock else YahooAndLJSEPriceProvider()

    for ev in candidate_events:
        fp = infer_fiscal_period(ev)
        row_key = (ev.market.lower(), ev.ticker.upper(), fp)
        existing = by_key.get(row_key)
        in_window = window_start <= ev.date <= yesterday

        if existing is not None and not correction:
            if not in_window or _is_row_complete_within_window(existing):
                continue

        eps_est: float | None = ev.eps_estimate if ev.market != "ljse" else None
        eps_act: float | None = ev.eps_actual if ev.market != "ljse" else None
        surp_pct: float | None = compute_surprise_pct(eps_est, eps_act)

        if ev.market != "ljse" and active_surprise is not None and (eps_est is None or eps_act is None):
            try:
                surprises = active_surprise.fetch_surprises(
                    ev.ticker,
                    start_date=ev.date - timedelta(days=7),
                    end_date=ev.date + timedelta(days=7),
                )
                matched = _match_surprise_for_event(ev, fp, surprises)
                if matched is not None:
                    if eps_est is None and matched.eps_estimate is not None:
                        eps_est = matched.eps_estimate
                    if eps_act is None and matched.eps_actual is not None:
                        eps_act = matched.eps_actual
                    surp_pct = compute_surprise_pct(eps_est, eps_act, matched.surprise_pct)
            except Exception as exc:
                logger.warning("Surprise provider failed for %s (%s); recording available data: %s", ev.ticker, fp, exc)

        move_1d: float | None = None
        if active_price is not None:
            try:
                closes = active_price.fetch_daily_closes(
                    market=ev.market,
                    ticker=ev.ticker,
                    start_date=ev.date - timedelta(days=7),
                    end_date=ev.date + timedelta(days=7),
                )
                move_1d = compute_1d_price_move_pct(ev, closes)
            except Exception as exc:
                logger.warning("Price provider failed for %s (%s); leaving price_move_1d_pct empty: %s", ev.ticker, fp, exc)

        manual_notes = (ev.note or "").strip() if ev.market == "ljse" else ""
        candidate_row = EarningsResultRow(
            market=ev.market.lower(),
            ticker=ev.ticker.upper(),
            report_date=ev.date,
            fiscal_period=fp,
            eps_estimate=eps_est,
            eps_actual=eps_act,
            surprise_pct=surp_pct,
            price_move_1d_pct=move_1d,
            recorded_at=recorded_ts,
            manual_notes=manual_notes,
        )

        if existing is None:
            by_key[row_key] = candidate_row
        else:
            merged_row, _changed = merge_result_row(
                existing=existing,
                candidate=candidate_row,
                correction=correction,
                in_retry_window=in_window,
            )
            by_key[row_key] = merged_row

    final_rows = sort_results_rows(by_key.values())
    if final_rows or Path(results_path).exists():
        write_results_csv(final_rows, results_path)
    return final_rows


# ============================================================================
# Summary CLI (`python -m earnings_cal.results summary`)
# ============================================================================


def render_results_summary(rows: Sequence[EarningsResultRow]) -> str:
    """Render a human-readable summary of average EPS surprise and 1-day price move per ticker."""
    if not rows:
        return "No earnings results recorded in data/results.csv."

    grouped: dict[tuple[str, str], list[EarningsResultRow]] = {}
    for row in sort_results_rows(rows):
        grouped.setdefault((row.market, row.ticker), []).append(row)

    lines: list[str] = [
        f"{'Market':<8} {'Ticker':<8} {'Reports':>7} {'Avg Surprise (%)':>18} {'Avg 1D Move (%)':>17}",
        "-" * 62,
    ]
    for (market, ticker), ticker_rows in sorted(grouped.items()):
        surprises = [r.surprise_pct for r in ticker_rows if r.surprise_pct is not None]
        moves = [r.price_move_1d_pct for r in ticker_rows if r.price_move_1d_pct is not None]
        avg_surp = f"{(sum(surprises) / len(surprises)):+.2f}%" if surprises else "N/A"
        avg_move = f"{(sum(moves) / len(moves)):+.2f}%" if moves else "N/A"
        lines.append(
            f"{market:<8} {ticker:<8} {len(ticker_rows):>7} {avg_surp:>18} {avg_move:>17}"
        )

    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m earnings_cal.results",
        description="Record post-earnings results to data/results.csv or print per-ticker summary.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="summary",
        choices=("summary", "update"),
        help="Command to run: 'summary' (print averages per ticker) or 'update' (record past results)",
    )
    parser.add_argument(
        "--results-file",
        type=Path,
        default=DEFAULT_RESULTS_PATH,
        help="Path to results CSV file (default: data/results.csv)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("tickers.yaml"),
        help="Path to watchlist YAML file (default: tickers.yaml)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_RESULTS_LOOKBACK_DAYS,
        help="Lookback retry window in days for past earnings (default: 30)",
    )
    parser.add_argument(
        "--correction",
        action="store_true",
        help="Allow overwriting existing non-empty values in data/results.csv",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use deterministic mock providers instead of live APIs",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args(argv)

    if args.command == "update":
        rows = update_results(
            config=args.config,
            results_path=args.results_file,
            lookback_days=args.lookback_days,
            correction=args.correction,
            use_mock=args.mock,
        )
        print(f"Recorded {len(rows)} total row(s) in {args.results_file}.")
        return 0

    rows = load_results_csv(args.results_file)
    print(render_results_summary(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
