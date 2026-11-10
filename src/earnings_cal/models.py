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
    manual_events: list[EarningsEvent] = field(default_factory=list)


def _parse_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value.strip())
    raise ValueError(f"Unsupported date value: {value!r}")


def load_config(path: Path | str) -> WatchlistConfig:
    """Load watchlist tickers and optional manual_events from a YAML file."""
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Expected a YAML mapping in {config_path}, got {type(raw).__name__}")

    raw_tickers = raw.get("tickers") or []
    if not isinstance(raw_tickers, list):
        raise ValueError("'tickers' in configuration must be a list of ticker symbols")

    tickers: list[str] = []
    seen_tickers: set[str] = set()
    for item in raw_tickers:
        symbol = str(item).strip().upper()
        if symbol and symbol not in seen_tickers:
            tickers.append(symbol)
            seen_tickers.add(symbol)

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

    return WatchlistConfig(tickers=tickers, manual_events=manual_events)
