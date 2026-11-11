"""Ljubljana Stock Exchange (`ljse`) market implementation backed by `ljse_events.yaml`."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from earnings_cal.build import merge_events
from earnings_cal.fetch import ProviderError
from earnings_cal.markets.base import Market
from earnings_cal.models import (
    DateWindow,
    Event,
    EventKind,
    _parse_amount_per_share,
    parse_date_value,
)

logger = logging.getLogger(__name__)

DEFAULT_LJSE_EVENTS_FILE = Path("ljse_events.yaml")


class LJSEValidationError(ProviderError, ValueError):
    """Raised when an entry in `ljse_events.yaml` violates schema or settlement rules."""


CORPORATE_EVENT_REQUIRED_FIELDS: dict[EventKind, tuple[str, ...]] = {
    EventKind.DIVIDEND_DECISION: ("ticker", "date", "source_url", "amount_per_share"),
    EventKind.EX_DIVIDEND: ("ticker", "date", "source_url"),
    EventKind.RECORD_DATE: ("ticker", "date", "source_url"),
    EventKind.DIVIDEND_PAYMENT: ("ticker", "date", "source_url"),
    EventKind.AGM: ("ticker", "date", "source_url"),
}


def _validate_corporate_entry(
    entry: dict[str, Any],
    kind: EventKind,
    ticker: str,
    events_path: Path,
) -> tuple[float | None, bool, str | None]:
    """Validate required fields, amount formatting, and computed-date flagging per event type."""
    required_fields = CORPORATE_EVENT_REQUIRED_FIELDS.get(kind, ("ticker", "date"))
    missing = [
        fld
        for fld in required_fields
        if entry.get(fld) is None or str(entry.get(fld)).strip() == ""
    ]
    if missing:
        raise LJSEValidationError(
            f"LJSE event for {ticker} ({kind.value}) in {events_path} is missing required field(s): "
            f"{', '.join(missing)}"
        )

    source_url = str(entry.get("source_url") or "").strip()
    if kind in CORPORATE_EVENT_REQUIRED_FIELDS and not source_url.startswith(("http://", "https://")):
        raise LJSEValidationError(
            f"LJSE event for {ticker} ({kind.value}) in {events_path} must have an http(s) source_url, "
            f"got {source_url!r}"
        )

    raw_amount = entry.get("amount_per_share")
    amount_per_share: float | None = None
    if raw_amount is not None and str(raw_amount).strip() != "":
        try:
            amount_per_share = _parse_amount_per_share(raw_amount)
        except Exception as exc:
            raise LJSEValidationError(
                f"Invalid amount_per_share {raw_amount!r} for {ticker} ({kind.value}) in {events_path}"
            ) from exc
        if amount_per_share is not None and amount_per_share <= 0:
            raise LJSEValidationError(
                f"amount_per_share must be positive for {ticker} ({kind.value}) in {events_path}, "
                f"got {amount_per_share}"
            )

    computed = bool(entry.get("computed", False) or entry.get("computed_from") or entry.get("derived_from_record_date"))
    computed_note = (
        str(entry["computed_note"]).strip()
        if entry.get("computed_note")
        else (str(entry["note"]).strip() if entry.get("note") else None)
    )
    if computed and not computed_note:
        raise LJSEValidationError(
            f"Computed {kind.value} date for {ticker} in {events_path} must include "
            "'computed_note' or 'note' explaining the settlement rule assumption"
        )

    return amount_per_share, computed, computed_note


def _validate_ex_vs_record_dates(events: list[Event], events_path: Path) -> None:
    """Ensure ex_dividend dates never equal or follow matching record_date events."""
    grouped: dict[tuple[str, str], dict[EventKind, list[Event]]] = {}
    for ev in events:
        if ev.kind not in {EventKind.EX_DIVIDEND, EventKind.RECORD_DATE}:
            continue
        key = (ev.ticker, ev.source_url or "")
        grouped.setdefault(key, {}).setdefault(ev.kind, []).append(ev)

    for (ticker, _src), by_kind in grouped.items():
        ex_list = by_kind.get(EventKind.EX_DIVIDEND, [])
        rec_list = by_kind.get(EventKind.RECORD_DATE, [])
        for ex_ev in ex_list:
            for rec_ev in rec_list:
                if ex_ev.date >= rec_ev.date:
                    raise LJSEValidationError(
                        f"Invalid dividend dates for {ticker} in {events_path}: "
                        f"ex_dividend ({ex_ev.date.isoformat()}) must precede record_date "
                        f"({rec_ev.date.isoformat()}); never copy record_date into ex_dividend."
                    )


def load_ljse_events_yaml(
    path: Path | str = DEFAULT_LJSE_EVENTS_FILE,
    allowed_tickers: list[str] | set[str] | None = None,
    window: DateWindow | None = None,
) -> list[Event]:
    """Load and validate structured LJSE events from `ljse_events.yaml`.

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
            raise LJSEValidationError(f"Expected a list of events in {events_path}")
    else:
        raise LJSEValidationError(f"Unexpected YAML structure in {events_path}: {type(raw).__name__}")

    allowed_set = (
        {str(t).strip().upper() for t in allowed_tickers if str(t).strip()}
        if allowed_tickers
        else None
    )

    all_validated: list[Event] = []
    filtered_events: list[Event] = []

    for entry in raw_items:
        if not isinstance(entry, dict):
            raise LJSEValidationError(f"Invalid event entry in {events_path}: {entry!r}")
        if "ticker" not in entry or "date" not in entry:
            raise LJSEValidationError(f"LJSE event missing required 'ticker' or 'date': {entry!r}")

        ticker = str(entry["ticker"]).strip().upper()
        if not ticker:
            raise LJSEValidationError(f"LJSE event has empty 'ticker': {entry!r}")

        raw_kind = str(entry.get("kind") or entry.get("type") or EventKind.EARNINGS.value).strip()
        if raw_kind.upper() == "TODO":
            logger.warning("Skipping unclassified LJSE event (type: TODO) for %s on %s", ticker, entry["date"])
            continue

        try:
            kind = EventKind(raw_kind.lower().replace("-", "_"))
        except ValueError as exc:
            raise LJSEValidationError(
                f"Invalid event kind {raw_kind!r} for {ticker} in {events_path}"
            ) from exc

        amount_per_share, computed, computed_note = _validate_corporate_entry(
            entry=entry,
            kind=kind,
            ticker=ticker,
            events_path=events_path,
        )

        event_date = parse_date_value(entry["date"])
        quarter = int(entry["quarter"]) if entry.get("quarter") is not None else None
        fiscal_year = int(entry["fiscal_year"]) if entry.get("fiscal_year") is not None else None
        eps_estimate = (
            float(entry["eps_estimate"]) if entry.get("eps_estimate") is not None else None
        )

        ev = Event(
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
            amount_per_share=amount_per_share,
            currency=str(entry.get("currency") or "EUR").strip() or "EUR",
            computed=computed,
            computed_note=computed_note,
        )
        all_validated.append(ev)

        if allowed_set is not None and ticker not in allowed_set:
            continue
        if window is not None and not window.contains(event_date):
            continue
        filtered_events.append(ev)

    _validate_ex_vs_record_dates(all_validated, events_path)
    return merge_events(filtered_events)


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
