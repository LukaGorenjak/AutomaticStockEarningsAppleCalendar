"""iCalendar (.ics) generation, deterministic serialization, and index.html builder."""

from __future__ import annotations

import html
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from icalendar import Alarm, Calendar, Event as ICalEvent, vDuration

from earnings_cal.models import (
    DEFAULT_DISPLAY_TIMEZONE,
    EarningsEvent,
    Event,
    validate_timezone,
)

if TYPE_CHECKING:
    from earnings_cal.markets.base import Market

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
            vevent.add("dtstart", display.local_date)
            vevent.add("dtend", display.local_date + timedelta(days=1))
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
) -> str:
    """Render `docs/index.html` listing every registered market feed and its `webcal://` link."""
    clean_host = base_host.strip().rstrip("/")
    tz_label = html.escape(validate_timezone(display_timezone, field_name="display_timezone"))
    rows_html: list[str] = []

    for m in markets:
        m_id = html.escape(str(getattr(m, "id", "")))
        display_name = html.escape(str(getattr(m, "display_name", m_id)))
        cal_name = html.escape(str(getattr(m, "calendar_name", display_name)))
        tz_name = html.escape(str(getattr(m, "timezone", "UTC")))
        feed_file = f"{m_id}.ics"
        webcal_url = f"webcal://{clean_host}/{feed_file}"
        aliases = getattr(m, "legacy_aliases", ()) or ()
        alias_note = ""
        if aliases:
            alias_list = ", ".join(f"<code>{html.escape(a)}</code>" for a in aliases)
            alias_note = f"<br><small>Legacy alias: {alias_list} (please migrate to <code>{feed_file}</code>)</small>"

        rows_html.append(
            f"      <tr>\n"
            f"        <td><strong>{display_name}</strong> (<code>{m_id}</code>){alias_note}</td>\n"
            f"        <td><code>{cal_name}</code></td>\n"
            f"        <td><code>{tz_name}</code></td>\n"
            f'        <td><a class="webcal-link" data-feed="{feed_file}" href="{webcal_url}">{webcal_url}</a></td>\n'
            f'        <td><a href="{feed_file}">{feed_file}</a></td>\n'
            f"      </tr>"
        )

    table_body = "\n".join(rows_html)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Earnings &amp; Market Events Calendars</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; max-width: 860px; margin: 2rem auto; padding: 0 1rem; color: #1d1d1f; line-height: 1.5; }}
    h1 {{ font-size: 1.6rem; margin-bottom: 0.4rem; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 1.2rem; }}
    th, td {{ text-align: left; padding: 0.65rem 0.75rem; border-bottom: 1px solid #e5e5ea; vertical-align: top; }}
    th {{ background: #f5f5f7; font-weight: 600; }}
    code {{ background: #f2f2f7; padding: 0.15rem 0.35rem; border-radius: 4px; font-size: 0.9em; }}
    a {{ color: #0066cc; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    small {{ color: #6e6e73; }}
  </style>
</head>
<body>
  <h1>Market Calendar Feeds (<code>.ics</code>)</h1>
  <p>Subscribe in Apple Calendar (macOS / iOS) using the <code>webcal://</code> links below. Event times are converted to <code>{tz_label}</code>.</p>
  <table>
    <thead>
      <tr>
        <th>Market</th>
        <th>Calendar Name (<code>X-WR-CALNAME</code>)</th>
        <th>Market Timezone</th>
        <th>Subscribe (<code>webcal://</code>)</th>
        <th>Direct Download</th>
      </tr>
    </thead>
    <tbody>
{table_body}
    </tbody>
  </table>
  <script>
    (function () {{
      if (!window.location.host) return;
      var basePath = window.location.pathname.replace(/\\/?(index\\.html)?$/, "/");
      var links = document.querySelectorAll("a.webcal-link[data-feed]");
      for (var i = 0; i < links.length; i++) {{
        var feed = links[i].getAttribute("data-feed");
        var url = "webcal://" + window.location.host + basePath + feed;
        links[i].setAttribute("href", url);
        links[i].textContent = url;
      }}
    }})();
  </script>
</body>
</html>
"""


def write_index_html(
    markets: Sequence[Market | type[Market]],
    output_path: Path | str = Path("docs/index.html"),
    base_host: str = DEFAULT_PAGES_HOST,
    display_timezone: str = DEFAULT_DISPLAY_TIMEZONE,
) -> bool:
    """Write `docs/index.html` only if content changed. Returns True if file was updated."""
    path = Path(output_path)
    new_text = render_index_html(
        markets,
        base_host=base_host,
        display_timezone=display_timezone,
    )

    if path.exists():
        existing_text = path.read_text(encoding="utf-8")
        if existing_text == new_text:
            return False

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text, encoding="utf-8")
    return True
