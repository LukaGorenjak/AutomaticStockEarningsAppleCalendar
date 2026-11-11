"""iCalendar (.ics) generation, deterministic serialization, and index.html builder."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from icalendar import Alarm, Calendar, Event as ICalEvent, vDuration

from earnings_cal.models import (
    DEFAULT_DISPLAY_TIMEZONE,
    EarningsEvent,
    Event,
    validate_timezone,
)

if TYPE_CHECKING:
    from earnings_cal.markets.base import Market
    from earnings_cal.results import EarningsResultRow

# Fixed epoch timestamp so identical event data produces byte-for-byte identical .ics output.
DETERMINISTIC_DTSTAMP = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
DEFAULT_PAGES_HOST = "lukagorenjak.github.io/AutomaticStockEarningsAppleCalendar"


def merge_events(*event_groups: Iterable[Event]) -> list[Event]:
    """Merge and deduplicate events by UID, returning a deterministically sorted list.

    When an event's date moves (or a manual event overrides an API event with the
    same UID), the newer event replaces the existing one instead of duplicating.
    """
    by_uid: dict[str, Event] = {}
    for group in event_groups:
        for event in group:
            by_uid[event.uid] = event

    return sorted(
        by_uid.values(),
        key=lambda e: (e.date.isoformat(), e.ticker, e.uid),
    )


def build_calendar(
    events: Iterable[Event],
    calendar_name: str = "Earnings Watchlist",
    timezone_name: str | None = None,
    display_timezone: str = DEFAULT_DISPLAY_TIMEZONE,
    move_notes: Mapping[str, str] | None = None,
) -> Calendar:
    """Build an `icalendar.Calendar` from `Event` items converted to `display_timezone`."""
    target_tz = validate_timezone(display_timezone, field_name="display_timezone")

    cal = Calendar()
    cal.add("prodid", "-//Earnings Watchlist//earnings-cal//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("X-WR-CALNAME", calendar_name)
    cal.add("X-WR-TIMEZONE", target_tz if display_timezone else (timezone_name or target_tz))

    refresh_duration = vDuration(timedelta(days=1))
    refresh_duration.params["VALUE"] = "DURATION"
    cal.add("REFRESH-INTERVAL", refresh_duration)
    cal.add("X-PUBLISHED-TTL", vDuration(timedelta(days=1)))

    sorted_events = merge_events(events)
    resolved_items = [(item, item.resolve_display(target_tz)) for item in sorted_events]
    resolved_items.sort(
        key=lambda pair: (
            pair[1].local_date.isoformat(),
            pair[1].start_dt.isoformat() if pair[1].start_dt else "",
            pair[0].ticker,
            pair[0].uid,
        )
    )

    for item, display in resolved_items:
        vevent = ICalEvent()
        vevent.add("uid", item.uid)
        vevent.add("summary", display.summary)
        description_text = display.description
        move_note = move_notes.get(item.uid) if move_notes else None
        if move_note and move_note not in description_text:
            description_text = f"{description_text}\n{move_note}"
        vevent.add("description", description_text)
        if item.source_url:
            vevent.add("url", item.source_url)

        if display.is_timed and display.start_dt is not None and display.end_dt is not None:
            vevent.add("dtstart", display.start_dt)
            vevent.add("dtend", display.end_dt)
            alarm_trigger = timedelta(minutes=-30)
        else:
            inclusive_end = display.local_end_date or display.local_date
            vevent.add("dtstart", display.local_date)
            vevent.add("dtend", inclusive_end + timedelta(days=1))
            alarm_trigger = timedelta(hours=-15)

        vevent.add("dtstamp", DETERMINISTIC_DTSTAMP)
        vevent.add("transp", "TRANSPARENT")

        alarm = Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", f"Reminder: {display.summary}")
        alarm.add("trigger", alarm_trigger)
        vevent.add_component(alarm)

        cal.add_component(vevent)

    return cal


def render_ics(
    events: Iterable[Event],
    calendar_name: str = "Earnings Watchlist",
    timezone_name: str | None = None,
    display_timezone: str = DEFAULT_DISPLAY_TIMEZONE,
    move_notes: Mapping[str, str] | None = None,
) -> bytes:
    """Serialize `events` into deterministic RFC 5545 `.ics` bytes."""
    cal = build_calendar(
        events,
        calendar_name=calendar_name,
        timezone_name=timezone_name,
        display_timezone=display_timezone,
        move_notes=move_notes,
    )
    return cal.to_ical()


def write_calendar(
    events: Iterable[Event],
    output_path: Path | str,
    calendar_name: str = "Earnings Watchlist",
    timezone_name: str | None = None,
    display_timezone: str = DEFAULT_DISPLAY_TIMEZONE,
    move_notes: Mapping[str, str] | None = None,
) -> bool:
    """Write `.ics` file only if content changed. Returns True if file was updated."""
    path = Path(output_path)
    new_bytes = render_ics(
        events,
        calendar_name=calendar_name,
        timezone_name=timezone_name,
        display_timezone=display_timezone,
        move_notes=move_notes,
    )

    if path.exists():
        existing_bytes = path.read_bytes()
        if existing_bytes == new_bytes:
            return False

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(new_bytes)
    return True


def render_index_html(
    markets: Sequence[Market | type[Market]],
    base_host: str = DEFAULT_PAGES_HOST,
    display_timezone: str = DEFAULT_DISPLAY_TIMEZONE,
    events_by_market: Mapping[str, Sequence[Event]] | Sequence[Event] | None = None,
    status_data: Mapping[str, Any] | None = None,
    changes_records: Sequence[Mapping[str, Any]] | None = None,
    results_rows: Sequence[EarningsResultRow | Mapping[str, Any]] | None = None,
    output_dir: Path | None = None,
    status_path: Path | str | None = None,
    changes_path: Path | str | None = None,
    results_path: Path | str | None = None,
) -> str:
    """Render static dashboard `docs/index.html` from existing markets, events, status, changes, and results."""
    from earnings_cal.dashboard import build_dashboard_payload, render_dashboard_html

    payload = build_dashboard_payload(
        markets=markets,
        base_host=base_host,
        display_timezone=display_timezone,
        events_by_market=events_by_market,
        status_data=status_data,
        changes_records=changes_records,
        results_rows=results_rows,
        output_dir=output_dir,
        status_path=status_path,
        changes_path=changes_path,
        results_path=results_path,
    )
    return render_dashboard_html(payload)


def write_index_html(
    markets: Sequence[Market | type[Market]],
    output_path: Path | str = Path("docs/index.html"),
    base_host: str = DEFAULT_PAGES_HOST,
    display_timezone: str = DEFAULT_DISPLAY_TIMEZONE,
    events_by_market: Mapping[str, Sequence[Event]] | Sequence[Event] | None = None,
    status_data: Mapping[str, Any] | None = None,
    changes_records: Sequence[Mapping[str, Any]] | None = None,
    results_rows: Sequence[EarningsResultRow | Mapping[str, Any]] | None = None,
    status_path: Path | str | None = None,
    changes_path: Path | str | None = None,
    results_path: Path | str | None = None,
) -> bool:
    """Write `docs/index.html` only if content changed. Returns True if file was updated."""
    path = Path(output_path)
    new_text = render_index_html(
        markets,
        base_host=base_host,
        display_timezone=display_timezone,
        events_by_market=events_by_market,
        status_data=status_data,
        changes_records=changes_records,
        results_rows=results_rows,
        output_dir=path.parent,
        status_path=status_path,
        changes_path=changes_path,
        results_path=results_path,
    )

    if path.exists():
        existing_text = path.read_text(encoding="utf-8")
        if existing_text == new_text:
            return False

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text, encoding="utf-8")
    return True
