"""Data models and configuration loader for earnings_cal."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml


class EventKind(StrEnum):
    """Supported market event categories."""

    EARNINGS = "earnings"
    DIVIDEND = "dividend"
    EX_DIVIDEND = "ex_dividend"
    AGM = "agm"
    MACRO = "macro"
    OTHER = "other"


@dataclass(frozen=True)
class DateWindow:
    """Inclusive date range `[start, end]` for fetching market events."""

    start: date
    end: date

    def contains(self, target: date) -> bool:
        return self.start <= target <= self.end


@dataclass(frozen=True)
class Event:
    """Represents a single market calendar event (earnings, dividend, AGM, macro, etc.)."""

    ticker: str
    date: date
    market: str = "us"
    kind: EventKind | str = EventKind.EARNINGS
    title: str | None = None
    time: str | dt_time | None = None
    description: str | None = None
    source_url: str | None = None
    hour: str | None = None
    quarter: int | None = None
    fiscal_year: int | None = None
    eps_estimate: float | None = None
    status: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        normalized_market = str(self.market or "us").strip().lower()
        if not normalized_market:
            raise ValueError("Event 'market' must be a non-empty string")
        object.__setattr__(self, "market", normalized_market)

        normalized_ticker = str(self.ticker or "").strip().upper()
        if not normalized_ticker:
            raise ValueError("Event 'ticker' must be a non-empty string")
        object.__setattr__(self, "ticker", normalized_ticker)

        if isinstance(self.kind, EventKind):
            coerced_kind = self.kind
        else:
            raw_kind = str(self.kind or EventKind.EARNINGS.value).strip().lower().replace("-", "_")
            coerced_kind = EventKind(raw_kind)
        object.__setattr__(self, "kind", coerced_kind)

        if self.hour is not None:
            normalized_hour = str(self.hour).strip().lower()
            object.__setattr__(self, "hour", normalized_hour or None)

        if isinstance(self.time, dt_time):
            object.__setattr__(self, "time", self.time.strftime("%H:%M"))
        elif self.time is not None:
            cleaned_time = str(self.time).strip()
            object.__setattr__(self, "time", cleaned_time or None)

        if self.source_url is not None:
            cleaned_url = str(self.source_url).strip()
            object.__setattr__(self, "source_url", cleaned_url or None)

        if self.note is not None:
            cleaned_note = str(self.note).strip()
            object.__setattr__(self, "note", cleaned_note or None)

        resolved_title = (str(self.title).strip() if self.title else "") or self._default_title()
        object.__setattr__(self, "title", resolved_title)

        resolved_description = self._build_description(self.description)
        object.__setattr__(self, "description", resolved_description)

    def _default_title(self) -> str:
        if self.kind == EventKind.EARNINGS:
            if self.hour == "bmo":
                return f"{self.ticker} earnings (BMO)"
            if self.hour == "amc":
                return f"{self.ticker} earnings (AMC)"
            return f"{self.ticker} earnings"
        if self.kind == EventKind.DIVIDEND:
            return f"{self.ticker} dividend"
        if self.kind == EventKind.EX_DIVIDEND:
            return f"{self.ticker} ex-dividend"
        if self.kind == EventKind.AGM:
            return f"{self.ticker} AGM"
        if self.kind == EventKind.MACRO:
            return f"{self.ticker} macro"
        return f"{self.ticker} {self.kind.value}"

    def _build_description(self, explicit_description: str | None) -> str:
        lines: list[str] = []
        existing_lines: set[str] = set()
        if explicit_description and str(explicit_description).strip():
            cleaned_desc = str(explicit_description).strip()
            lines.append(cleaned_desc)
            existing_lines = {part.strip() for part in cleaned_desc.splitlines() if part.strip()}

        def _append_line(line: str) -> None:
            if line not in existing_lines and line not in lines:
                lines.append(line)

        if self.quarter is not None and self.fiscal_year is not None:
            _append_line(f"Fiscal Period: Q{self.quarter} {self.fiscal_year}")
        elif self.fiscal_year is not None:
            _append_line(f"Fiscal Year: {self.fiscal_year}")
        elif self.quarter is not None:
            _append_line(f"Fiscal Quarter: Q{self.quarter}")

        if self.eps_estimate is not None:
            _append_line(f"EPS Estimate: ${self.eps_estimate:.2f}")

        if self.time:
            _append_line(f"Time: {self.time}")

        if self.status:
            _append_line(f"Status: {self.status}")

        if self.note and self.note not in existing_lines:
            _append_line(f"Note: {self.note}")

        if self.source_url and not any(self.source_url in line for line in lines):
            _append_line(f"Source: {self.source_url}")

        if not lines:
            if self.kind == EventKind.EARNINGS:
                lines.append(f"{self.ticker} earnings release")
            else:
                lines.append(f"{self.ticker} {self.kind.value.replace('_', ' ')} event")

        return "\n".join(lines)

    @property
    def uid(self) -> str:
        """Stable UID per event including market ID and event kind.

        Format: `{market}-{ticker}-{date or fiscal period}-{kind}@earnings-cal`
        """
        if self.fiscal_year is not None and self.quarter is not None:
            period_or_date = f"{self.fiscal_year}Q{self.quarter}"
        else:
            period_or_date = self.date.isoformat()
        return f"{self.market}-{self.ticker}-{period_or_date}-{self.kind.value}@earnings-cal"

    @property
    def summary(self) -> str:
        """Calendar VEVENT summary (alias for `title`)."""
        return self.title or self._default_title()


# Backward-compatible alias for existing callers and tests
EarningsEvent = Event


@dataclass(frozen=True)
class WatchlistConfig:
    """Parsed configuration from tickers.yaml supporting per-market sections."""

    tickers: list[str] = field(default_factory=list)
    tickers_ljse: list[str] = field(default_factory=list)
    manual_events: list[Event] = field(default_factory=list)
    ljse_issuers: dict[str, str] = field(default_factory=dict)
    markets: dict[str, dict[str, Any]] = field(default_factory=dict)
    config_dir: Path = field(default_factory=lambda: Path("."))
    is_legacy_only: bool = False

    def get_market_section(self, market_id: str) -> dict[str, Any]:
        """Return the configuration dictionary for `market_id`."""
        return dict(self.markets.get(market_id.lower(), {}))


LEGACY_CONFIG_KEYS = {"tickers", "tickers-ljse", "manual_events", "ljse_issuers"}


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


def parse_date_value(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value.strip())
    raise ValueError(f"Unsupported date value: {value!r}")


def parse_event_dict(entry: dict[str, Any], default_market: str = "us") -> Event:
    """Parse a raw YAML event mapping into an `Event`."""
    if not isinstance(entry, dict):
        raise ValueError(f"Each event item must be a mapping, got {entry!r}")
    if "ticker" not in entry or "date" not in entry:
        raise ValueError(f"Event is missing required 'ticker' or 'date': {entry!r}")

    quarter = int(entry["quarter"]) if entry.get("quarter") is not None else None
    fiscal_year = int(entry["fiscal_year"]) if entry.get("fiscal_year") is not None else None
    eps_estimate = (
        float(entry["eps_estimate"]) if entry.get("eps_estimate") is not None else None
    )
    raw_kind = entry.get("kind") or entry.get("type") or EventKind.EARNINGS
    market_id = str(entry.get("market") or default_market).strip().lower()

    return Event(
        market=market_id,
        ticker=str(entry["ticker"]),
        date=parse_date_value(entry["date"]),
        kind=raw_kind,
        title=str(entry["title"]).strip() if entry.get("title") else None,
        time=str(entry["time"]).strip() if entry.get("time") is not None else None,
        description=str(entry["description"]).strip() if entry.get("description") else None,
        source_url=str(entry["source_url"]).strip() if entry.get("source_url") else None,
        hour=str(entry["hour"]) if entry.get("hour") is not None else None,
        quarter=quarter,
        fiscal_year=fiscal_year,
        eps_estimate=eps_estimate,
        status=str(entry["status"]) if entry.get("status") is not None else None,
        note=str(entry["note"]).strip() if entry.get("note") else None,
    )


def _parse_manual_events(raw_manual: Any, market_id: str = "us") -> list[Event]:
    if raw_manual is None:
        return []
    if not isinstance(raw_manual, list):
        raise ValueError("'manual_events' in configuration must be a list")
    return [parse_event_dict(entry, default_market=market_id) for entry in raw_manual]


def _known_market_ids() -> set[str]:
    try:
        from earnings_cal.markets.registry import get_registered_market_ids

        return set(get_registered_market_ids())
    except Exception:
        return {"us", "ljse"}


def load_config(path: Path | str) -> WatchlistConfig:
    """Load per-market watchlists (and legacy top-level keys) from YAML."""
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Expected a YAML mapping in {config_path}, got {type(raw).__name__}")

    registered_ids = _known_market_ids()
    allowed_keys = LEGACY_CONFIG_KEYS | registered_ids | {"markets"}
    unknown_keys = sorted(set(raw.keys()) - allowed_keys)
    if unknown_keys:
        raise ValueError(
            f"Unknown configuration key(s) in {config_path}: {', '.join(unknown_keys)}"
        )

    markets_map: dict[str, dict[str, Any]] = {}

    # Support optional nested `markets:` mapping as well as top-level `<market_id>:` sections
    if "markets" in raw:
        if not isinstance(raw["markets"], dict):
            raise ValueError("'markets' in configuration must be a mapping")
        for m_id, m_cfg in raw["markets"].items():
            if not isinstance(m_cfg, dict):
                raise ValueError(f"Configuration for market '{m_id}' must be a mapping")
            markets_map[str(m_id).strip().lower()] = dict(m_cfg)

    for key, val in raw.items():
        if key in LEGACY_CONFIG_KEYS or key == "markets":
            continue
        if val is None:
            markets_map[key.lower()] = {}
        elif isinstance(val, dict):
            markets_map[key.lower()] = dict(val)
        else:
            raise ValueError(f"Configuration section for market '{key}' must be a mapping")

    has_market_sections = bool(markets_map)

    # Parse legacy top-level fields if present
    legacy_tickers = _parse_ticker_list(raw.get("tickers"), "tickers")
    legacy_tickers_ljse = _parse_ticker_list(raw.get("tickers-ljse"), "tickers-ljse")
    legacy_manual = _parse_manual_events(raw.get("manual_events"), market_id="us")
    legacy_issuers = _parse_ljse_issuers(raw.get("ljse_issuers"))

    # Merge US section with legacy top-level US fields
    us_section = dict(markets_map.get("us", {}))
    us_tickers = _parse_ticker_list(us_section.get("tickers"), "us.tickers") or legacy_tickers
    us_manual = (
        _parse_manual_events(us_section.get("manual_events"), market_id="us")
        if "manual_events" in us_section
        else legacy_manual
    )
    us_section["tickers"] = us_tickers
    us_section["manual_events"] = us_manual
    if us_tickers or us_manual or "us" in markets_map:
        markets_map["us"] = us_section

    # Merge LJSE section with legacy top-level LJSE fields
    ljse_section = dict(markets_map.get("ljse", {}))
    ljse_tickers = (
        _parse_ticker_list(ljse_section.get("tickers"), "ljse.tickers")
        or legacy_tickers_ljse
    )
    raw_section_issuers = ljse_section.get("issuers") or ljse_section.get("ljse_issuers")
    ljse_issuers = _parse_ljse_issuers(raw_section_issuers) if raw_section_issuers is not None else legacy_issuers
    ljse_section["tickers"] = ljse_tickers
    ljse_section["issuers"] = ljse_issuers
    if ljse_tickers or ljse_issuers or "ljse" in markets_map:
        markets_map["ljse"] = ljse_section

    return WatchlistConfig(
        tickers=us_tickers,
        tickers_ljse=ljse_tickers,
        manual_events=us_manual,
        ljse_issuers=ljse_issuers,
        markets=markets_map,
        config_dir=config_path.parent,
        is_legacy_only=not has_market_sections,
    )
