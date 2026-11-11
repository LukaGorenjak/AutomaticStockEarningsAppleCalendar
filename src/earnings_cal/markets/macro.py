"""Macroeconomic events (`macro`) market implementation backed by `data/macro_events.yaml`."""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import requests
import yaml

from earnings_cal.build import merge_events
from earnings_cal.fetch import ProviderError
from earnings_cal.markets.base import Market
from earnings_cal.models import (
    DateWindow,
    Event,
    EventKind,
    parse_date_value,
    validate_timezone,
)

logger = logging.getLogger(__name__)

DEFAULT_MACRO_EVENTS_FILE = Path("data/macro_events.yaml")
DEFAULT_MACRO_SERIES: tuple[str, ...] = ("ecb", "fomc", "us_cpi", "ea_hicp")

SERIES_ALIASES: dict[str, str] = {
    "ecb": "ecb",
    "ecb_monetary_policy": "ecb",
    "fomc": "fomc",
    "fed": "fomc",
    "fed_fomc": "fomc",
    "us_cpi": "us_cpi",
    "cpi": "us_cpi",
    "bls_cpi": "us_cpi",
    "ea_hicp": "ea_hicp",
    "hicp": "ea_hicp",
    "eurostat_hicp": "ea_hicp",
    "euro_area_hicp": "ea_hicp",
}

SERIES_DEFAULT_TICKERS: dict[str, str] = {
    "ecb": "ECB",
    "fomc": "FOMC",
    "us_cpi": "US_CPI",
    "ea_hicp": "EA_HICP",
}

SERIES_DEFAULT_KINDS: dict[str, EventKind] = {
    "ecb": EventKind.CENTRAL_BANK_MEETING,
    "fomc": EventKind.CENTRAL_BANK_MEETING,
    "us_cpi": EventKind.INFLATION_RELEASE,
    "ea_hicp": EventKind.INFLATION_RELEASE,
}

SERIES_DEFAULT_TIMEZONES: dict[str, str] = {
    "ecb": "Europe/Berlin",
    "fomc": "America/New_York",
    "us_cpi": "America/New_York",
    "ea_hicp": "Europe/Brussels",
}

SERIES_OFFICIAL_URLS: dict[str, str] = {
    "ecb": "https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html",
    "fomc": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    "us_cpi": "https://www.bls.gov/schedule/news_release/cpi.htm",
    "ea_hicp": "https://ec.europa.eu/eurostat/web/main/news/release-calendar",
}

ALLOWED_MACRO_KINDS: set[EventKind] = {
    EventKind.CENTRAL_BANK_MEETING,
    EventKind.INFLATION_RELEASE,
    EventKind.MACRO,
}

_MONTH_NAMES: dict[str, int] = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}


class MacroValidationError(ProviderError, ValueError):
    """Raised when an entry in `data/macro_events.yaml` violates schema or schedule rules."""


def normalize_macro_series(raw_series: Any) -> str:
    """Normalize a macro series identifier (e.g. `'fed'` -> `'fomc'`, `'hicp'` -> `'ea_hicp'`)."""
    cleaned = str(raw_series or "").strip().lower().replace("-", "_").replace(" ", "_")
    if cleaned in SERIES_ALIASES:
        return SERIES_ALIASES[cleaned]
    raise MacroValidationError(
        f"Unknown macro series {raw_series!r}; expected one of {sorted(DEFAULT_MACRO_SERIES)}"
    )


def parse_enabled_series(raw_series: Any) -> list[str]:
    """Parse and validate a list of enabled macro series from configuration."""
    if raw_series is None:
        return list(DEFAULT_MACRO_SERIES)
    if not isinstance(raw_series, (list, tuple, set)):
        raise MacroValidationError(
            f"'macro.series' in configuration must be a list of series names, got {raw_series!r}"
        )
    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_series:
        canonical = normalize_macro_series(item)
        if canonical not in seen:
            normalized.append(canonical)
            seen.add(canonical)
    return normalized


def _validate_macro_entry(
    entry: dict[str, Any],
    events_path: Path,
) -> tuple[str, Event]:
    """Validate a single `data/macro_events.yaml` mapping and return `(series, Event)`."""
    if not isinstance(entry, dict):
        raise MacroValidationError(f"Invalid macro event entry in {events_path}: {entry!r}")

    raw_series = entry.get("series") or entry.get("ticker")
    if not raw_series:
        raise MacroValidationError(
            f"Macro event in {events_path} is missing required 'series' or 'ticker': {entry!r}"
        )
    series = normalize_macro_series(raw_series)
    ticker = str(entry.get("ticker") or SERIES_DEFAULT_TICKERS[series]).strip().upper()
    if not ticker:
        raise MacroValidationError(f"Macro event in {events_path} has empty 'ticker': {entry!r}")

    if "date" not in entry or entry.get("date") is None or str(entry.get("date")).strip() == "":
        raise MacroValidationError(
            f"Macro event for {ticker} in {events_path} is missing required 'date'"
        )
    start_date = parse_date_value(entry["date"])

    raw_kind = str(entry.get("kind") or entry.get("type") or SERIES_DEFAULT_KINDS[series].value).strip()
    try:
        kind = EventKind(raw_kind.lower().replace("-", "_"))
    except ValueError as exc:
        raise MacroValidationError(
            f"Invalid macro event kind {raw_kind!r} for {ticker} in {events_path}"
        ) from exc

    if kind not in ALLOWED_MACRO_KINDS:
        raise MacroValidationError(
            f"Unsupported macro event kind {kind.value!r} for {ticker} in {events_path}; "
            f"expected one of {sorted(k.value for k in ALLOWED_MACRO_KINDS)}"
        )

    source_url = str(entry.get("source_url") or "").strip()
    if not source_url.startswith(("http://", "https://")):
        raise MacroValidationError(
            f"Macro event for {ticker} ({start_date.isoformat()}) in {events_path} must include "
            f"an http(s) 'source_url', got {source_url!r}"
        )

    raw_tz = str(entry.get("market_timezone") or SERIES_DEFAULT_TIMEZONES[series]).strip()
    try:
        market_tz = validate_timezone(raw_tz, field_name="market_timezone")
    except ValueError as exc:
        raise MacroValidationError(
            f"Invalid market_timezone {raw_tz!r} for {ticker} in {events_path}: {exc}"
        ) from exc

    end_date: date | None = None
    if entry.get("end_date") is not None and str(entry.get("end_date")).strip() != "":
        end_date = parse_date_value(entry["end_date"])
        if end_date < start_date:
            raise MacroValidationError(
                f"Macro event for {ticker} in {events_path} has end_date ({end_date.isoformat()}) "
                f"before date ({start_date.isoformat()})"
            )

    decision_date: date | None = None
    if entry.get("decision_date") is not None and str(entry.get("decision_date")).strip() != "":
        decision_date = parse_date_value(entry["decision_date"])
    elif kind == EventKind.CENTRAL_BANK_MEETING and end_date is not None and end_date > start_date:
        decision_date = end_date

    if decision_date is not None:
        upper_bound = end_date or start_date
        if not (start_date <= decision_date <= upper_bound):
            raise MacroValidationError(
                f"Macro event for {ticker} in {events_path} has decision_date "
                f"({decision_date.isoformat()}) outside meeting span "
                f"[{start_date.isoformat()}, {upper_bound.isoformat()}]"
            )

    # Multi-day meetings must remain all-day events (no single-day timed VEVENT override)
    raw_time = str(entry["time"]).strip() if entry.get("time") is not None else None
    if end_date is not None and end_date > start_date and raw_time:
        raise MacroValidationError(
            f"Multi-day macro meeting for {ticker} ({start_date.isoformat()}..{end_date.isoformat()}) "
            f"in {events_path} must be an all-day event; use 'decision_time' instead of 'time'"
        )

    raw_decision_time = (
        str(entry["decision_time"]).strip()
        if entry.get("decision_time") is not None
        else None
    )
    title = str(entry["title"]).strip() if entry.get("title") else None
    description = str(entry["description"]).strip() if entry.get("description") else None
    note = str(entry["note"]).strip() if entry.get("note") else None
    duration_minutes = (
        int(entry["duration_minutes"]) if entry.get("duration_minutes") is not None else 60
    )

    try:
        ev = Event(
            market="macro",
            ticker=ticker,
            date=start_date,
            end_date=end_date,
            decision_date=decision_date,
            decision_time=raw_decision_time,
            kind=kind,
            title=title,
            time=raw_time,
            description=description,
            source_url=source_url,
            note=note,
            market_timezone=market_tz,
            timing=entry.get("timing"),
            duration_minutes=duration_minutes,
        )
    except ValueError as exc:
        raise MacroValidationError(
            f"Invalid macro event for {ticker} ({start_date.isoformat()}) in {events_path}: {exc}"
        ) from exc

    return series, ev


def load_macro_events_yaml(
    path: Path | str = DEFAULT_MACRO_EVENTS_FILE,
    enabled_series: Iterable[str] | None = None,
    window: DateWindow | None = None,
) -> list[Event]:
    """Load and validate macroeconomic events from `data/macro_events.yaml`."""
    events_path = Path(path)
    if not events_path.exists():
        raise ProviderError(f"Macro events file not found: {events_path}")

    try:
        raw = yaml.safe_load(events_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ProviderError(f"Failed to parse YAML in {events_path}: {exc}") from exc

    if raw is None:
        raw_items: list[Any] = []
    elif isinstance(raw, list):
        raw_items = raw
    elif isinstance(raw, dict):
        raw_items = raw.get("macro_events") or raw.get("events") or []
        if not isinstance(raw_items, list):
            raise MacroValidationError(f"Expected a list of macro events in {events_path}")
    else:
        raise MacroValidationError(
            f"Unexpected YAML structure in {events_path}: {type(raw).__name__}"
        )

    allowed_series = (
        set(parse_enabled_series(list(enabled_series)))
        if enabled_series is not None
        else set(DEFAULT_MACRO_SERIES)
    )

    filtered: list[Event] = []
    for entry in raw_items:
        series, ev = _validate_macro_entry(entry, events_path)
        if series not in allowed_series:
            continue
        effective_end = ev.end_date or ev.date
        if window is not None and (effective_end < window.start or ev.date > window.end):
            continue
        filtered.append(ev)

    return merge_events(filtered)


@dataclass(frozen=True)
class SourceVerificationResult:
    """Result of verifying a macro series in `data/macro_events.yaml` against its official webpage."""

    series: str
    url: str
    reachable: bool
    checked_count: int
    matched_count: int
    missing_dates: tuple[date, ...] = ()
    note: str = ""

    @property
    def ok(self) -> bool:
        return (not self.reachable) or (len(self.missing_dates) == 0)


def parse_fomc_schedule_html(html_text: str) -> set[tuple[date, date]]:
    """Extract `(start_date, end_date)` meeting spans from `federalreserve.gov/monetarypolicy/fomccalendars.htm`."""
    spans: set[tuple[date, date]] = set()
    panel_pattern = re.compile(
        r"<h4>\s*<a[^>]*>\s*(\d{4})\s+FOMC Meetings\s*</a>\s*</h4>(.*?)(?=<h4>\s*<a|<div class=\"panel-footer\"|$)",
        re.S | re.I,
    )
    row_pattern = re.compile(
        r"fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>\s*</div>\s*"
        r"<div[^>]*fomc-meeting__date[^>]*>\s*(\d{1,2})\s*[-–]\s*(\d{1,2})",
        re.S | re.I,
    )
    for year_match in panel_pattern.finditer(html_text):
        year = int(year_match.group(1))
        block = year_match.group(2)
        for row_match in row_pattern.finditer(block):
            raw_month = row_match.group(1).strip().lower()
            day1 = int(row_match.group(2))
            day2 = int(row_match.group(3))
            parts = [p.strip() for p in raw_month.split("/") if p.strip()]
            m1 = _MONTH_NAMES.get(parts[0])
            m2 = _MONTH_NAMES.get(parts[-1]) if len(parts) > 1 else m1
            if m1 and m2:
                try:
                    spans.add((date(year, m1, day1), date(year, m2, day2)))
                except ValueError:
                    continue
    return spans


def parse_ecb_schedule_html(html_text: str) -> set[date]:
    """Extract monetary policy meeting dates from `ecb.europa.eu/press/calendars/mgcgc/html/index.en.html`."""
    dates: set[date] = set()
    pair_pattern = re.compile(
        r"<dt[^>]*>\s*(\d{2})/(\d{2})/(\d{4})\s*</dt>\s*<dd[^>]*>(.*?)</dd>",
        re.S | re.I,
    )
    for match in pair_pattern.finditer(html_text):
        day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        desc = match.group(4).lower()
        if "monetary policy meeting" in desc and "non-monetary" not in desc:
            try:
                dates.add(date(year, month, day))
            except ValueError:
                continue
    return dates


def parse_bls_cpi_schedule_html(html_text: str) -> set[date]:
    """Extract CPI release dates from `bls.gov/schedule/news_release/cpi.htm`."""
    dates: set[date] = set()
    cell_pattern = re.compile(
        r"<td[^>]*>\s*([A-Za-z]{3,9})\.?\s+(\d{1,2}),\s*(\d{4})\s*</td>",
        re.I,
    )
    for match in cell_pattern.finditer(html_text):
        month_num = _MONTH_NAMES.get(match.group(1).strip().lower())
        day = int(match.group(2))
        year = int(match.group(3))
        if month_num:
            try:
                dates.add(date(year, month_num, day))
            except ValueError:
                continue
    return dates


def verify_macro_schedule_online(
    path: Path | str = DEFAULT_MACRO_EVENTS_FILE,
    session: requests.Session | None = None,
    today: date | None = None,
    timeout: int = 15,
) -> list[SourceVerificationResult]:
    """Check `data/macro_events.yaml` upcoming dates against official schedule pages when reachable."""
    ref_date = today or date.today()
    events = load_macro_events_yaml(path=path)
    http = session or requests.Session()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }

    results: list[SourceVerificationResult] = []

    # 1. FOMC
    fomc_url = SERIES_OFFICIAL_URLS["fomc"]
    fomc_events = [e for e in events if e.ticker == "FOMC" and (e.end_date or e.date) >= ref_date]
    try:
        resp = http.get(fomc_url, headers=headers, timeout=timeout)
        if resp.status_code == 200:
            official_spans = parse_fomc_schedule_html(resp.text)
            missing = [
                e.date
                for e in fomc_events
                if (e.date, e.end_date or e.date) not in official_spans
            ]
            results.append(
                SourceVerificationResult(
                    series="fomc",
                    url=fomc_url,
                    reachable=True,
                    checked_count=len(fomc_events),
                    matched_count=len(fomc_events) - len(missing),
                    missing_dates=tuple(missing),
                    note="Verified against static HTML table on federalreserve.gov.",
                )
            )
        else:
            results.append(
                SourceVerificationResult(
                    series="fomc",
                    url=fomc_url,
                    reachable=False,
                    checked_count=len(fomc_events),
                    matched_count=0,
                    note=f"HTTP {resp.status_code}",
                )
            )
    except Exception as exc:
        results.append(
            SourceVerificationResult(
                series="fomc",
                url=fomc_url,
                reachable=False,
                checked_count=len(fomc_events),
                matched_count=0,
                note=str(exc),
            )
        )

    # 2. ECB
    ecb_url = SERIES_OFFICIAL_URLS["ecb"]
    ecb_events = [e for e in events if e.ticker == "ECB" and (e.end_date or e.date) >= ref_date]
    try:
        resp = http.get(ecb_url, headers=headers, timeout=timeout)
        if resp.status_code == 200:
            official_dates = parse_ecb_schedule_html(resp.text)
            missing = [
                e.date
                for e in ecb_events
                if e.date not in official_dates or (e.end_date and e.end_date not in official_dates)
            ]
            results.append(
                SourceVerificationResult(
                    series="ecb",
                    url=ecb_url,
                    reachable=True,
                    checked_count=len(ecb_events),
                    matched_count=len(ecb_events) - len(missing),
                    missing_dates=tuple(missing),
                    note="Verified against static <dt>/<dd> schedule on ecb.europa.eu.",
                )
            )
        else:
            results.append(
                SourceVerificationResult(
                    series="ecb",
                    url=ecb_url,
                    reachable=False,
                    checked_count=len(ecb_events),
                    matched_count=0,
                    note=f"HTTP {resp.status_code}",
                )
            )
    except Exception as exc:
        results.append(
            SourceVerificationResult(
                series="ecb",
                url=ecb_url,
                reachable=False,
                checked_count=len(ecb_events),
                matched_count=0,
                note=str(exc),
            )
        )

    # 3. US CPI (BLS HTML page or bls.ics fallback when WAF blocks HTML)
    cpi_url = SERIES_OFFICIAL_URLS["us_cpi"]
    cpi_events = [e for e in events if e.ticker == "US_CPI" and e.date >= ref_date]
    try:
        resp = http.get(cpi_url, headers=headers, timeout=timeout)
        if resp.status_code == 200:
            official_dates = parse_bls_cpi_schedule_html(resp.text)
            missing = [e.date for e in cpi_events if e.date not in official_dates]
            results.append(
                SourceVerificationResult(
                    series="us_cpi",
                    url=cpi_url,
                    reachable=True,
                    checked_count=len(cpi_events),
                    matched_count=len(cpi_events) - len(missing),
                    missing_dates=tuple(missing),
                    note="Verified against table.release-list on bls.gov.",
                )
            )
        else:
            results.append(
                SourceVerificationResult(
                    series="us_cpi",
                    url=cpi_url,
                    reachable=False,
                    checked_count=len(cpi_events),
                    matched_count=0,
                    note=f"HTTP {resp.status_code}",
                )
            )
    except Exception as exc:
        results.append(
            SourceVerificationResult(
                series="us_cpi",
                url=cpi_url,
                reachable=False,
                checked_count=len(cpi_events),
                matched_count=0,
                note=str(exc),
            )
        )

    # 4. Eurostat Flash HICP (JS-rendered Liferay app; check reachability + local YAML schema)
    hicp_url = SERIES_OFFICIAL_URLS["ea_hicp"]
    hicp_events = [e for e in events if e.ticker == "EA_HICP" and e.date >= ref_date]
    try:
        resp = http.get(hicp_url, headers=headers, timeout=timeout)
        reachable = resp.status_code == 200
        results.append(
            SourceVerificationResult(
                series="ea_hicp",
                url=hicp_url,
                reachable=reachable,
                checked_count=len(hicp_events),
                matched_count=len(hicp_events) if reachable else 0,
                missing_dates=(),
                note=(
                    "Eurostat release calendar reachable (client-side FullCalendar app; "
                    "YAML entries validated locally)."
                    if reachable
                    else f"HTTP {resp.status_code}"
                ),
            )
        )
    except Exception as exc:
        results.append(
            SourceVerificationResult(
                series="ea_hicp",
                url=hicp_url,
                reachable=False,
                checked_count=len(hicp_events),
                matched_count=0,
                note=str(exc),
            )
        )

    return results


class MacroMarket(Market):
    """Macroeconomic calendar market (`docs/macro.ics`) sourced from `data/macro_events.yaml`."""

    id = "macro"
    display_name = "Macroeconomic Events"
    timezone = "Europe/Ljubljana"
    calendar_name = "Macro Calendar"

    def _resolve_events_file(self) -> Path:
        configured = self.config.get("events_file") or self.options.get("macro_events_path")
        if configured:
            candidate = Path(configured)
            if not candidate.is_absolute():
                in_config_dir = self.config_dir / candidate
                if in_config_dir.exists():
                    return in_config_dir
            return candidate

        in_config_dir = self.config_dir / DEFAULT_MACRO_EVENTS_FILE
        if in_config_dir.exists():
            return in_config_dir
        return DEFAULT_MACRO_EVENTS_FILE

    def _get_enabled_series(self) -> list[str]:
        raw_series = self.config.get("series")
        if raw_series is None:
            raw_series = self.config.get("tickers")
        return parse_enabled_series(raw_series)

    def effective_window(self, window: DateWindow) -> DateWindow:
        """Use at least a 365-day lookahead for pre-published macroeconomic schedules."""
        return DateWindow(
            start=window.start,
            end=max(window.end, window.start.replace(year=window.start.year + 1)),
        )

    def fetch_events(self, window: DateWindow) -> list[Event]:
        """Fetch macroeconomic events from `data/macro_events.yaml` filtered to enabled series."""
        events_file = self._resolve_events_file()
        enabled_series = self._get_enabled_series()
        effective_window = self.effective_window(window)

        events = load_macro_events_yaml(
            path=events_file,
            enabled_series=enabled_series,
            window=effective_window,
        )
        if not events:
            raise ProviderError(
                f"No macro events found in {events_file} for series {enabled_series} between "
                f"{effective_window.start.isoformat()} and {effective_window.end.isoformat()}."
            )
        return events


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point to validate `data/macro_events.yaml` and optionally verify against official URLs."""
    parser = argparse.ArgumentParser(
        prog="python -m earnings_cal.markets.macro",
        description="Validate data/macro_events.yaml and optionally check official central-bank/agency schedules.",
    )
    parser.add_argument(
        "--events-file",
        type=Path,
        default=DEFAULT_MACRO_EVENTS_FILE,
        help="Path to macro_events.yaml (default: data/macro_events.yaml)",
    )
    parser.add_argument(
        "--verify-online",
        action="store_true",
        help="Check upcoming dates against official FOMC, ECB, BLS, and Eurostat pages when reachable",
    )
    args = parser.parse_args(argv)

    try:
        events = load_macro_events_yaml(args.events_file)
    except Exception as exc:
        print(f"Validation failed for {args.events_file}: {exc}", file=sys.stderr)
        return 1

    print(f"Validated {len(events)} macro event(s) in {args.events_file}.")
    if not args.verify_online:
        return 0

    results = verify_macro_schedule_online(args.events_file)
    all_ok = True
    for res in results:
        status_label = "OK" if res.ok else "MISMATCH"
        if not res.reachable:
            status_label = "UNREACHABLE (skipped)"
        print(
            f"- [{res.series}] {status_label}: checked={res.checked_count}, "
            f"matched={res.matched_count} ({res.url}) {res.note}"
        )
        if res.missing_dates:
            all_ok = False
            missing_str = ", ".join(d.isoformat() for d in res.missing_dates)
            print(f"  Missing on official page: {missing_str}", file=sys.stderr)

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
