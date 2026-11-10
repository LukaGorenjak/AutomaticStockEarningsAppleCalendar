"""iCalendar (.ics) generation and deterministic serialization."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from icalendar import Alarm, Calendar, Event, vDuration

from earnings_cal.models import EarningsEvent

# Fixed epoch timestamp so identical event data produces byte-for-byte identical .ics output.
DETERMINISTIC_DTSTAMP = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def merge_events(*event_groups: Iterable[EarningsEvent]) -> list[EarningsEvent]:
    """Merge and deduplicate events by UID, returning a deterministically sorted list.

    When an event's date moves (or a manual event overrides an API event with the
    same UID), the newer event replaces the existing one instead of duplicating.
    """
    by_uid: dict[str, EarningsEvent] = {}
    for group in event_groups:
        for event in group:
            by_uid[event.uid] = event

    return sorted(
        by_uid.values(),
        key=lambda e: (e.date.isoformat(), e.ticker, e.uid),
    )


def build_calendar(events: Iterable[EarningsEvent]) -> Calendar:
    """Build an `icalendar.Calendar` from a collection of `EarningsEvent` items."""
    cal = Calendar()
    cal.add("prodid", "-//Earnings Watchlist//earnings-cal//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("X-WR-CALNAME", "Earnings Watchlist")

    refresh_duration = vDuration(timedelta(days=1))
    refresh_duration.params["VALUE"] = "DURATION"
    cal.add("REFRESH-INTERVAL", refresh_duration)
    cal.add("X-PUBLISHED-TTL", vDuration(timedelta(days=1)))

    sorted_events = merge_events(events)

    for item in sorted_events:
        vevent = Event()
        vevent.add("uid", item.uid)
        vevent.add("summary", item.summary)
        vevent.add("description", item.description)
        vevent.add("dtstart", item.date)
        vevent.add("dtend", item.date + timedelta(days=1))
        vevent.add("dtstamp", DETERMINISTIC_DTSTAMP)
        vevent.add("transp", "TRANSPARENT")

        alarm = Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", f"Reminder: {item.summary}")
        alarm.add("trigger", timedelta(hours=-15))
        vevent.add_component(alarm)

        cal.add_component(vevent)

    return cal


def render_ics(events: Iterable[EarningsEvent]) -> bytes:
    """Serialize `events` into deterministic RFC 5545 `.ics` bytes."""
    cal = build_calendar(events)
    return cal.to_ical()


def write_calendar(events: Iterable[EarningsEvent], output_path: Path | str) -> bool:
    """Write `.ics` file only if content changed. Returns True if file was updated."""
    path = Path(output_path)
    new_bytes = render_ics(events)

    if path.exists():
        existing_bytes = path.read_bytes()
        if existing_bytes == new_bytes:
            return False

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(new_bytes)
    return True
