"""Ljubljana Stock Exchange (`ljse`) market implementation backed by `ljse_events.yaml`."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from earnings_cal.build import merge_events
from earnings_cal.fetch import ProviderError
from earnings_cal.markets.base import Market
from earnings_cal.models import DateWindow, Event, EventKind, parse_date_value

logger = logging.getLogger(__name__)

DEFAULT_LJSE_EVENTS_FILE = Path("ljse_events.yaml")


def load_ljse_events_yaml(
    path: Path | str = DEFAULT_LJSE_EVENTS_FILE,
    allowed_tickers: list[str] | set[str] | None = None,
    window: DateWindow | None = None,
) -> list[Event]:
    """Load structured LJSE events from `ljse_events.yaml`.

    Supports either a top-level list of event mappings or a mapping containing
    `ljse_events:` / `events:`. Entries with `type: TODO` or `kind: TODO` are
    skipped with a warning until classified.
    """
    events_path = Path(path)
    if not events_path.exists():
        raise ProviderError(f"LJSE events file not found: {events_path}")

    try:
        raw = yaml.safe_load(events_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ProviderError(f"Failed to parse YAML in {events_path}: {exc}") from exc

    if raw is None:
        raw_items: list[Any] = []
    elif isinstance(raw, list):
        raw_items = raw
    elif isinstance(raw, dict):
        raw_items = raw.get("ljse_events") or raw.get("events") or []
        if not isinstance(raw_items, list):
            raise ProviderError(f"Expected a list of events in {events_path}")
    else:
        raise ProviderError(f"Unexpected YAML structure in {events_path}: {type(raw).__name__}")

    allowed_set = (
        {str(t).strip().upper() for t in allowed_tickers if str(t).strip()}
        if allowed_tickers
        else None
    )

    events: list[Event] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            raise ProviderError(f"Invalid event entry in {events_path}: {entry!r}")
        if "ticker" not in entry or "date" not in entry:
            raise ProviderError(f"LJSE event missing required 'ticker' or 'date': {entry!r}")

        ticker = str(entry["ticker"]).strip().upper()
        if allowed_set is not None and ticker not in allowed_set:
            continue

        raw_kind = str(entry.get("kind") or entry.get("type") or EventKind.EARNINGS.value).strip()
        if raw_kind.upper() == "TODO":
            logger.warning("Skipping unclassified LJSE event (type: TODO) for %s on %s", ticker, entry["date"])
            continue

        try:
            kind = EventKind(raw_kind.lower().replace("-", "_"))
        except ValueError as exc:
            raise ProviderError(
                f"Invalid event kind {raw_kind!r} for {ticker} in {events_path}"
            ) from exc

        event_date = parse_date_value(entry["date"])
        if window is not None and not window.contains(event_date):
            continue

        quarter = int(entry["quarter"]) if entry.get("quarter") is not None else None
        fiscal_year = int(entry["fiscal_year"]) if entry.get("fiscal_year") is not None else None
        eps_estimate = (
            float(entry["eps_estimate"]) if entry.get("eps_estimate") is not None else None
        )

        events.append(
            Event(
                market="ljse",
                ticker=ticker,
                date=event_date,
                kind=kind,
                title=str(entry["title"]).strip() if entry.get("title") else None,
                time=str(entry["time"]).strip() if entry.get("time") is not None else None,
                description=str(entry["description"]).strip() if entry.get("description") else None,
                source_url=str(entry["source_url"]).strip() if entry.get("source_url") else None,
                hour=str(entry["hour"]).strip() if entry.get("hour") else None,
                quarter=quarter,
                fiscal_year=fiscal_year,
                eps_estimate=eps_estimate,
                status=str(entry["status"]).strip() if entry.get("status") else None,
                note=str(entry["note"]).strip() if entry.get("note") else None,
                market_timezone=LJSEMarket.timezone,
                timing=entry.get("timing"),
                duration_minutes=int(entry["duration_minutes"]) if entry.get("duration_minutes") is not None else 60,
            )
        )

    return merge_events(events)


class LJSEMarket(Market):
    """Ljubljana Stock Exchange market (`docs/ljse.ics`) sourced from `ljse_events.yaml`."""

    id = "ljse"
    display_name = "Ljubljana Stock Exchange"
    timezone = "Europe/Ljubljana"
    calendar_name = "Events LJSE"

    def _resolve_events_file(self) -> Path:
        configured = self.config.get("events_file") or self.options.get("ljse_events_path")
        if configured:
            candidate = Path(configured)
            if not candidate.is_absolute():
                in_config_dir = self.config_dir / candidate
                if in_config_dir.exists():
                    return in_config_dir
            return candidate

        in_config_dir = self.config_dir / DEFAULT_LJSE_EVENTS_FILE
        if in_config_dir.exists():
            return in_config_dir
        return DEFAULT_LJSE_EVENTS_FILE

    def _get_tickers(self) -> list[str]:
        raw_tickers = self.config.get("tickers") or []
        return [str(t).strip().upper() for t in raw_tickers if t and str(t).strip()]

    def fetch_events(self, window: DateWindow) -> list[Event]:
        """Fetch LJSE events from `ljse_events.yaml` filtered to watchlist and window."""
        events_file = self._resolve_events_file()
        tickers = self._get_tickers()

        # Use a minimum 365-day lookahead for LJSE annual financial calendars if window is shorter
        effective_window = DateWindow(
            start=window.start,
            end=max(window.end, window.start.replace(year=window.start.year + 1)),
        )
        events = load_ljse_events_yaml(
            path=events_file,
            allowed_tickers=tickers or None,
            window=effective_window,
        )
        if not events:
            raise ProviderError(
                f"No LJSE events found in {events_file} between "
                f"{effective_window.start.isoformat()} and {effective_window.end.isoformat()}."
            )
        return events
