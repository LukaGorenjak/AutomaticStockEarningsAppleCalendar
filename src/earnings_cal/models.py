"""Data models and configuration loader for earnings_cal."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class EarningsEvent:
    """Represents a single company earnings event."""

    ticker: str
    date: date
    hour: str | None = None
    quarter: int | None = None
    fiscal_year: int | None = None
    eps_estimate: float | None = None
    status: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ticker", self.ticker.strip().upper())
        if self.hour is not None:
            normalized_hour = self.hour.strip().lower()
            object.__setattr__(self, "hour", normalized_hour or None)

    @property
    def uid(self) -> str:
        """Stable UID per event.

        Format: `{TICKER}-{fiscalYear}Q{quarter}@earnings-cal`,
        falling back to `{TICKER}-{date}@earnings-cal` when fiscal year or quarter is missing.
        """
        if self.fiscal_year is not None and self.quarter is not None:
            return f"{self.ticker}-{self.fiscal_year}Q{self.quarter}@earnings-cal"
        return f"{self.ticker}-{self.date.isoformat()}@earnings-cal"

    @property
    def summary(self) -> str:
        """Event title: `AAPL earnings (BMO)` / `(AMC)` / `AAPL earnings`."""
        if self.hour == "bmo":
            return f"{self.ticker} earnings (BMO)"
        if self.hour == "amc":
            return f"{self.ticker} earnings (AMC)"
        return f"{self.ticker} earnings"

    @property
    def description(self) -> str:
        """Human-readable event description with fiscal period, EPS estimate, status, and notes."""
        lines: list[str] = []
        if self.quarter is not None and self.fiscal_year is not None:
            lines.append(f"Fiscal Period: Q{self.quarter} {self.fiscal_year}")
        elif self.fiscal_year is not None:
            lines.append(f"Fiscal Year: {self.fiscal_year}")
        elif self.quarter is not None:
            lines.append(f"Fiscal Quarter: Q{self.quarter}")

        if self.eps_estimate is not None:
            lines.append(f"EPS Estimate: ${self.eps_estimate:.2f}")

        if self.status:
            lines.append(f"Status: {self.status}")

        if self.note:
            lines.append(f"Note: {self.note}")

        if not lines:
            lines.append(f"{self.ticker} earnings release")

        return "\n".join(lines)


@dataclass(frozen=True)
class WatchlistConfig:
    """Parsed configuration from tickers.yaml."""

    tickers: list[str] = field(default_factory=list)
    tickers_ljse: list[str] = field(default_factory=list)
    manual_events: list[EarningsEvent] = field(default_factory=list)
    ljse_issuers: dict[str, str] = field(default_factory=dict)


ALLOWED_CONFIG_KEYS = {"tickers", "tickers-ljse", "manual_events", "ljse_issuers"}


def _parse_ticker_list(raw_value: Any, field_name: str) -> list[str]:
    if raw_value is None:
        return []
    if not isinstance(raw_value, list):
        raise ValueError(f"'{field_name}' in configuration must be a list of ticker symbols")

    tickers: list[str] = []
    seen: set[str] = set()
    for item in raw_value:
        symbol = str(item).strip().upper()
        if symbol and symbol not in seen:
            tickers.append(symbol)
            seen.add(symbol)
    return tickers


def _parse_ljse_issuers(raw_issuers: Any) -> dict[str, str]:
    """Validate and normalize the `ljse_issuers` mapping (ticker -> exact SEOnet issuer name)."""
    if raw_issuers is None:
        return {}
    if not isinstance(raw_issuers, dict):
        raise ValueError("'ljse_issuers' in configuration must be a mapping of ticker: issuer_name")

    parsed: dict[str, str] = {}
    seen_names: dict[str, str] = {}
    for raw_ticker, raw_name in raw_issuers.items():
        if not isinstance(raw_ticker, str) or not raw_ticker.strip():
            raise ValueError(f"Invalid ticker key in 'ljse_issuers': {raw_ticker!r}")
        ticker = raw_ticker.strip().upper()
        if any(ch.isspace() for ch in ticker):
            raise ValueError(f"Ticker key in 'ljse_issuers' must not contain whitespace: {raw_ticker!r}")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError(
                f"Issuer name for '{ticker}' in 'ljse_issuers' must be a non-empty string, got {raw_name!r}"
            )
        issuer_name = raw_name.strip()
        if issuer_name in seen_names and seen_names[issuer_name] != ticker:
            raise ValueError(
                f"Duplicate SEOnet issuer name {issuer_name!r} mapped to both "
                f"{seen_names[issuer_name]} and {ticker}"
            )
        parsed[ticker] = issuer_name
        seen_names[issuer_name] = ticker

    return parsed


def _parse_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value.strip())
    raise ValueError(f"Unsupported date value: {value!r}")


def load_config(path: Path | str) -> WatchlistConfig:
    """Load watchlist tickers, optional manual_events, and optional ljse_issuers from YAML."""
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Expected a YAML mapping in {config_path}, got {type(raw).__name__}")

    unknown_keys = sorted(set(raw.keys()) - ALLOWED_CONFIG_KEYS)
    if unknown_keys:
        raise ValueError(
            f"Unknown configuration key(s) in {config_path}: {', '.join(unknown_keys)}"
        )

    tickers = _parse_ticker_list(raw.get("tickers"), "tickers")
    tickers_ljse = _parse_ticker_list(raw.get("tickers-ljse"), "tickers-ljse")

    raw_manual = raw.get("manual_events") or []
    if not isinstance(raw_manual, list):
        raise ValueError("'manual_events' in configuration must be a list")

    manual_events: list[EarningsEvent] = []
    for entry in raw_manual:
        if not isinstance(entry, dict):
            raise ValueError(f"Each manual_events item must be a mapping, got {entry!r}")
        if "ticker" not in entry or "date" not in entry:
            raise ValueError(f"Manual event is missing required 'ticker' or 'date': {entry!r}")

        quarter = int(entry["quarter"]) if entry.get("quarter") is not None else None
        fiscal_year = int(entry["fiscal_year"]) if entry.get("fiscal_year") is not None else None
        eps_estimate = (
            float(entry["eps_estimate"]) if entry.get("eps_estimate") is not None else None
        )

        manual_events.append(
            EarningsEvent(
                ticker=str(entry["ticker"]),
                date=_parse_date(entry["date"]),
                hour=str(entry["hour"]) if entry.get("hour") is not None else None,
                quarter=quarter,
                fiscal_year=fiscal_year,
                eps_estimate=eps_estimate,
                status=str(entry["status"]) if entry.get("status") is not None else None,
                note=str(entry["note"]).strip() if entry.get("note") else None,
            )
        )

    ljse_issuers = _parse_ljse_issuers(raw.get("ljse_issuers"))

    return WatchlistConfig(
        tickers=tickers,
        tickers_ljse=tickers_ljse,
        manual_events=manual_events,
        ljse_issuers=ljse_issuers,
    )
