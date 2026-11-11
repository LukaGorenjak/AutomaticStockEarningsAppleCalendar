"""Data models, timezone conversions, and configuration loader for earnings_cal."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

DEFAULT_DISPLAY_TIMEZONE = "Europe/Ljubljana"

DEFAULT_MARKET_TIMEZONES: dict[str, str] = {
    "us": "America/New_York",
    "ljse": "Europe/Ljubljana",
}


class EventKind(StrEnum):
    """Supported market event categories."""

    EARNINGS = "earnings"
    DIVIDEND = "dividend"
    EX_DIVIDEND = "ex_dividend"
    AGM = "agm"
    MACRO = "macro"
    OTHER = "other"


class EventTiming(StrEnum):
    """Supported event timing categories relative to market hours."""

    BMO = "bmo"
    AMC = "amc"
    DMH = "dmh"
    EXACT = "exact"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DisplayTiming:
    """Resolved local display representation of an `Event` in `display_timezone`."""

    display_timezone: str
    is_timed: bool
    local_date: date
    start_dt: datetime | None
    end_dt: datetime | None
    summary: str
    description: str
    date_shifted: bool = False


@dataclass(frozen=True)
class DateWindow:
    """Inclusive date range `[start, end]` for fetching market events."""

    start: date
    end: date

    def contains(self, target: date) -> bool:
        return self.start <= target <= self.end


def validate_timezone(tz_name: str, field_name: str = "timezone") -> str:
    """Validate an IANA timezone name via `zoneinfo.ZoneInfo` and return it."""
    cleaned = str(tz_name or "").strip()
    if not cleaned:
        raise ValueError(f"'{field_name}' must be a non-empty IANA timezone name")
    try:
        ZoneInfo(cleaned)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Invalid IANA {field_name}: {cleaned!r}") from exc
    return cleaned


def _parse_clock_time(raw_time: str | dt_time) -> dt_time:
    if isinstance(raw_time, dt_time):
        return raw_time.replace(tzinfo=None)
    cleaned = str(raw_time).strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", cleaned)
    if not m:
        raise ValueError(f"Invalid time format {raw_time!r}; expected 'HH:MM' or 'HH:MM:SS'")
    hour = int(m.group(1))
    minute = int(m.group(2))
    second = int(m.group(3) or 0)
    return dt_time(hour, minute, second)


def _normalize_timing(raw_timing: Any) -> EventTiming | None:
    if raw_timing is None:
        return None
    if isinstance(raw_timing, EventTiming):
        return raw_timing
    cleaned = str(raw_timing).strip().lower().replace("-", "_").replace(" ", "_")
    if cleaned in {"exact_time", "exact"}:
        return EventTiming.EXACT
    if not cleaned:
        return None
    return EventTiming(cleaned)


def _default_timezone_for_market(market_id: str) -> str:
    if market_id in DEFAULT_MARKET_TIMEZONES:
        return DEFAULT_MARKET_TIMEZONES[market_id]
    try:
        from earnings_cal.markets.registry import get_market_registry

        entry = get_market_registry().get(market_id)
        if entry is not None:
            tz = getattr(entry, "timezone", None)
            if isinstance(tz, str) and tz.strip():
                return tz.strip()
    except Exception:
        pass
    return "UTC"


@dataclass(frozen=True)
class Event:
    """Represents a single market calendar event with original market date, timezone, and timing."""

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
    market_timezone: str | None = None
    timing: EventTiming | str | None = None
    duration_minutes: int = 60

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

        resolved_tz = validate_timezone(
            self.market_timezone or _default_timezone_for_market(normalized_market),
            field_name="market_timezone",
        )
        object.__setattr__(self, "market_timezone", resolved_tz)

        normalized_time: str | None = None
        if isinstance(self.time, dt_time):
            normalized_time = self.time.strftime("%H:%M")
        elif self.time is not None:
            cleaned_time = str(self.time).strip()
            if cleaned_time:
                parsed = _parse_clock_time(cleaned_time)
                normalized_time = parsed.strftime("%H:%M")
        object.__setattr__(self, "time", normalized_time)

        normalized_hour: str | None = None
        if self.hour is not None:
            cleaned_hour = str(self.hour).strip().lower()
            normalized_hour = cleaned_hour or None
        object.__setattr__(self, "hour", normalized_hour)

        explicit_timing = _normalize_timing(self.timing)
        if explicit_timing is not None:
            resolved_timing = explicit_timing
        elif normalized_time is not None:
            resolved_timing = EventTiming.EXACT
        elif normalized_hour in {EventTiming.BMO.value, EventTiming.AMC.value, EventTiming.DMH.value}:
            resolved_timing = EventTiming(normalized_hour)
        else:
            resolved_timing = EventTiming.UNKNOWN

        if resolved_timing == EventTiming.EXACT and normalized_time is None:
            raise ValueError(f"Event {normalized_ticker} has timing='exact' but no 'time'")
        if resolved_timing in {EventTiming.BMO, EventTiming.AMC, EventTiming.DMH} and normalized_hour is None:
            object.__setattr__(self, "hour", resolved_timing.value)
        object.__setattr__(self, "timing", resolved_timing)

        duration = int(self.duration_minutes)
        if duration <= 0:
            raise ValueError(f"Event 'duration_minutes' must be positive, got {duration}")
        object.__setattr__(self, "duration_minutes", duration)

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

    @property
    def market_date(self) -> date:
        """Original local date in the event's market timezone."""
        return self.date

    @property
    def timezone(self) -> str:
        """IANA timezone name of the event's home market."""
        return self.market_timezone or "UTC"

    def _default_title(self) -> str:
        if self.kind == EventKind.EARNINGS:
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

    def resolve_display(self, display_timezone: str = DEFAULT_DISPLAY_TIMEZONE) -> DisplayTiming:
        """Convert this event's market-local date/timing into `display_timezone` via `ZoneInfo`."""
        target_tz_name = validate_timezone(display_timezone, field_name="display_timezone")
        target_tz = ZoneInfo(target_tz_name)
        market_tz_name = self.timezone
        market_tz = ZoneInfo(market_tz_name)
        base_title = self.title or self._default_title()
        base_desc_lines = [self.description] if self.description else []

        timing = self.timing if isinstance(self.timing, EventTiming) else EventTiming(self.timing)

        if timing == EventTiming.EXACT and self.time is not None:
            parsed_time = _parse_clock_time(self.time)
            market_dt = datetime.combine(self.date, parsed_time, tzinfo=market_tz)
            local_dt = market_dt.astimezone(target_tz)
            end_dt = local_dt + timedelta(minutes=self.duration_minutes)
            date_shifted = local_dt.date() != self.date

            extra_lines = [
                f"Local Time: {local_dt.strftime('%Y-%m-%d %H:%M')} {local_dt.tzname()} ({target_tz_name})",
                f"Market Time: {market_dt.strftime('%Y-%m-%d %H:%M')} {market_dt.tzname()} ({market_tz_name})",
            ]
            if date_shifted:
                extra_lines.append(
                    f"Date Shift: {local_dt.date().isoformat()} ({target_tz_name}) "
                    f"corresponds to market date {self.date.isoformat()} ({market_tz_name})"
                )

            return DisplayTiming(
                display_timezone=target_tz_name,
                is_timed=True,
                local_date=local_dt.date(),
                start_dt=local_dt,
                end_dt=end_dt,
                summary=base_title,
                description="\n".join(base_desc_lines + extra_lines),
                date_shifted=date_shifted,
            )

        if timing == EventTiming.BMO:
            market_dt = datetime.combine(self.date, dt_time(9, 30), tzinfo=market_tz)
            local_dt = market_dt.astimezone(target_tz)
            local_date = local_dt.date()
            date_shifted = local_date != self.date

            local_label = f"before {local_dt.strftime('%H:%M')} {local_dt.tzname()}"
            market_label = f"before {market_dt.strftime('%H:%M')} {market_dt.tzname()}"
            summary = f"{base_title} (approx. {local_label})"
            extra_lines = [
                f"Timing: approx. {local_label} ({target_tz_name}) — approx., typical pre-market release "
                f"({market_label} {market_tz_name}; not a confirmed exact time)"
            ]
            if date_shifted:
                extra_lines.append(
                    f"Date Shift: {local_date.isoformat()} ({target_tz_name}) "
                    f"corresponds to market date {self.date.isoformat()} ({market_tz_name})"
                )
            return DisplayTiming(
                display_timezone=target_tz_name,
                is_timed=False,
                local_date=local_date,
                start_dt=None,
                end_dt=None,
                summary=summary,
                description="\n".join(base_desc_lines + extra_lines),
                date_shifted=date_shifted,
            )

        if timing == EventTiming.AMC:
            market_dt = datetime.combine(self.date, dt_time(16, 0), tzinfo=market_tz)
            local_dt = market_dt.astimezone(target_tz)
            local_date = local_dt.date()
            date_shifted = local_date != self.date

            local_label = f"after {local_dt.strftime('%H:%M')} {local_dt.tzname()}"
            market_label = f"after {market_dt.strftime('%H:%M')} {market_dt.tzname()}"
            summary = f"{base_title} (approx. {local_label})"
            extra_lines = [
                f"Timing: approx. {local_label} ({target_tz_name}) — approx., typical after-close release "
                f"({market_label} {market_tz_name}; not a confirmed exact time)"
            ]
            if date_shifted:
                extra_lines.append(
                    f"Date Shift: {local_date.isoformat()} ({target_tz_name}) "
                    f"corresponds to market date {self.date.isoformat()} ({market_tz_name})"
                )
            return DisplayTiming(
                display_timezone=target_tz_name,
                is_timed=False,
                local_date=local_date,
                start_dt=None,
                end_dt=None,
                summary=summary,
                description="\n".join(base_desc_lines + extra_lines),
                date_shifted=date_shifted,
            )

        if timing == EventTiming.DMH:
            market_start = datetime.combine(self.date, dt_time(9, 30), tzinfo=market_tz)
            market_end = datetime.combine(self.date, dt_time(16, 0), tzinfo=market_tz)
            local_start = market_start.astimezone(target_tz)
            local_end = market_end.astimezone(target_tz)
            local_date = local_start.date()
            date_shifted = local_date != self.date or local_end.date() != self.date

            if local_end.date() != local_start.date():
                local_range = (
                    f"{local_start.strftime('%H:%M')} {local_start.tzname()}-"
                    f"{local_end.strftime('%Y-%m-%d %H:%M')} {local_end.tzname()}"
                )
            else:
                local_range = (
                    f"{local_start.strftime('%H:%M')}-{local_end.strftime('%H:%M')} {local_end.tzname()}"
                )
            market_range = (
                f"{market_start.strftime('%H:%M')}-{market_end.strftime('%H:%M')} {market_end.tzname()}"
            )
            summary = f"{base_title} (approx. {local_range})"
            extra_lines = [
                f"Timing: approx. {local_range} ({target_tz_name}) — approx., typical during-market-hours release "
                f"({market_range} {market_tz_name}; not a confirmed exact time)"
            ]
            if date_shifted:
                extra_lines.append(
                    f"Date Shift: local dates {local_start.date().isoformat()}..{local_end.date().isoformat()} "
                    f"({target_tz_name}) correspond to market date {self.date.isoformat()} ({market_tz_name})"
                )
            return DisplayTiming(
                display_timezone=target_tz_name,
                is_timed=False,
                local_date=local_date,
                start_dt=None,
                end_dt=None,
                summary=summary,
                description="\n".join(base_desc_lines + extra_lines),
                date_shifted=date_shifted,
            )

        # EventTiming.UNKNOWN
        extra_lines = [
            f"Timing: Unknown (all-day; market date: {self.date.isoformat()}, market timezone: {market_tz_name})"
        ]
        return DisplayTiming(
            display_timezone=target_tz_name,
            is_timed=False,
            local_date=self.date,
            start_dt=None,
            end_dt=None,
            summary=base_title,
            description="\n".join(base_desc_lines + extra_lines),
            date_shifted=False,
        )

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
        """Default VEVENT summary in `DEFAULT_DISPLAY_TIMEZONE`."""
        return self.resolve_display(DEFAULT_DISPLAY_TIMEZONE).summary


# Backward-compatible alias for existing callers and tests
EarningsEvent = Event


@dataclass(frozen=True)
class WatchlistConfig:
    """Parsed configuration from tickers.yaml supporting per-market sections and display_timezone."""

    tickers: list[str] = field(default_factory=list)
    tickers_ljse: list[str] = field(default_factory=list)
    manual_events: list[Event] = field(default_factory=list)
    ljse_issuers: dict[str, str] = field(default_factory=dict)
    markets: dict[str, dict[str, Any]] = field(default_factory=dict)
    config_dir: Path = field(default_factory=lambda: Path("."))
    is_legacy_only: bool = False
    display_timezone: str = DEFAULT_DISPLAY_TIMEZONE

    def __post_init__(self) -> None:
        validated = validate_timezone(self.display_timezone, field_name="display_timezone")
        object.__setattr__(self, "display_timezone", validated)

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
    duration_minutes = (
        int(entry["duration_minutes"]) if entry.get("duration_minutes") is not None else 60
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
        market_timezone=(
            str(entry["market_timezone"]).strip()
            if entry.get("market_timezone")
            else None
        ),
        timing=entry.get("timing"),
        duration_minutes=duration_minutes,
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
    """Load `display_timezone`, per-market watchlists, and legacy keys from YAML."""
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Expected a YAML mapping in {config_path}, got {type(raw).__name__}")

    registered_ids = _known_market_ids()
    allowed_keys = LEGACY_CONFIG_KEYS | registered_ids | {"markets", "display_timezone"}
    unknown_keys = sorted(set(raw.keys()) - allowed_keys)
    if unknown_keys:
        raise ValueError(
            f"Unknown configuration key(s) in {config_path}: {', '.join(unknown_keys)}"
        )

    raw_display_tz = raw.get("display_timezone", DEFAULT_DISPLAY_TIMEZONE)
    display_timezone = validate_timezone(raw_display_tz, field_name="display_timezone")

    markets_map: dict[str, dict[str, Any]] = {}

    if "markets" in raw:
        if not isinstance(raw["markets"], dict):
            raise ValueError("'markets' in configuration must be a mapping")
        for m_id, m_cfg in raw["markets"].items():
            if not isinstance(m_cfg, dict):
                raise ValueError(f"Configuration for market '{m_id}' must be a mapping")
            markets_map[str(m_id).strip().lower()] = dict(m_cfg)

    for key, val in raw.items():
        if key in LEGACY_CONFIG_KEYS or key in {"markets", "display_timezone"}:
            continue
        if val is None:
            markets_map[key.lower()] = {}
        elif isinstance(val, dict):
            markets_map[key.lower()] = dict(val)
        else:
            raise ValueError(f"Configuration section for market '{key}' must be a mapping")

    has_market_sections = bool(markets_map)

    legacy_tickers = _parse_ticker_list(raw.get("tickers"), "tickers")
    legacy_tickers_ljse = _parse_ticker_list(raw.get("tickers-ljse"), "tickers-ljse")
    legacy_manual = _parse_manual_events(raw.get("manual_events"), market_id="us")
    legacy_issuers = _parse_ljse_issuers(raw.get("ljse_issuers"))

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
        display_timezone=display_timezone,
    )
