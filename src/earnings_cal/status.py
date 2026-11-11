"""Health status tracking (`docs/status.json`) and in-calendar stale warnings."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from icalendar import Calendar, Event as ICalEvent, vDuration

from earnings_cal.build import DETERMINISTIC_DTSTAMP
from earnings_cal.models import (
    DEFAULT_DISPLAY_TIMEZONE,
    MonitoringThresholds,
    validate_timezone,
)

logger = logging.getLogger(__name__)

DEFAULT_STATUS_PATH = Path("docs/status.json")
STALE_WARNING_UID_SUFFIX = "-stale-warning@earnings-cal"


def stale_warning_uid(market_id: str) -> str:
    """Return stable VEVENT UID for a market's stale-data warning event."""
    return f"{market_id.strip().lower()}{STALE_WARNING_UID_SUFFIX}"


def stale_warning_summary(market_id: str, last_success_date: str | date | None) -> str:
    """Return required stale warning summary: `WARNING: {market} data is stale (last update {date})`."""
    if isinstance(last_success_date, date):
        date_str = last_success_date.isoformat()
    elif last_success_date:
        date_str = str(last_success_date)
    else:
        date_str = "never"
    return f"WARNING: {market_id.strip().lower()} data is stale (last update {date_str})"


def _normalize_utc(dt: datetime | None, fallback_date: date | None = None) -> datetime:
    if dt is None:
        if fallback_date is not None:
            return datetime.combine(fallback_date, datetime.min.time(), tzinfo=timezone.utc)
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return _normalize_utc(parsed)
    except ValueError:
        return None


def load_status(path: Path | str = DEFAULT_STATUS_PATH) -> dict[str, Any]:
    """Load existing `docs/status.json` or return an empty baseline dictionary."""
    status_path = Path(path)
    if not status_path.exists():
        return {"markets": {}, "sources": {}}
    try:
        raw = json.loads(status_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except Exception as exc:
        logger.warning("Failed to parse %s (%s); starting fresh status.", status_path, exc)
    return {"markets": {}, "sources": {}}


def write_status(
    status_data: dict[str, Any],
    path: Path | str = DEFAULT_STATUS_PATH,
) -> bool:
    """Write deterministic JSON to `docs/status.json`. Returns True if content changed."""
    status_path = Path(path)
    rendered = json.dumps(status_data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if status_path.exists() and status_path.read_text(encoding="utf-8") == rendered:
        return False
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(rendered, encoding="utf-8")
    return True


def evaluate_source_status(
    source_name: str,
    prev_entry: dict[str, Any] | None,
    now_utc: datetime,
    max_age_days: int,
    succeeded: bool | None,
    errors: Sequence[str] = (),
) -> dict[str, Any]:
    """Build updated source status entry (`api` or `seonet`), never losing `last_success_at` on failure."""
    prev = dict(prev_entry or {})
    error_list = [str(e) for e in errors if str(e).strip()]

    if succeeded is True:
        last_success_at: str | None = now_utc.isoformat()
        last_success_date: str | None = now_utc.date().isoformat()
        last_attempt_at: str | None = now_utc.isoformat()
        error_list = []
    elif succeeded is False:
        last_success_at = prev.get("last_success_at")
        last_success_date = prev.get("last_success_date")
        last_attempt_at = now_utc.isoformat()
    else:
        # Source was not invoked on this run (e.g. weekly SEOnet during daily Finnhub run)
        last_success_at = prev.get("last_success_at")
        last_success_date = prev.get("last_success_date")
        last_attempt_at = prev.get("last_attempt_at")
        if not error_list:
            error_list = list(prev.get("errors") or [])

    prev_dt = _parse_iso_dt(last_success_at)
    if prev_dt is not None:
        elapsed = max(timedelta(0), now_utc - prev_dt)
        age_days: int | None = elapsed.days
        aged_out = elapsed > timedelta(days=max_age_days)
    else:
        age_days = None
        aged_out = False

    stale = aged_out
    healthy = (not error_list) and (not stale)

    return {
        "last_attempt_at": last_attempt_at,
        "last_success_at": last_success_at,
        "last_success_date": last_success_date,
        "max_age_days": max_age_days,
        "age_days": age_days,
        "stale": stale,
        "healthy": healthy,
        "errors": error_list,
    }


def evaluate_market_status(
    market_id: str,
    prev_entry: dict[str, Any] | None,
    now_utc: datetime,
    max_age_days: int,
    min_upcoming_events: int,
    upcoming_events: int | None,
    errors: Sequence[str] = (),
) -> dict[str, Any]:
    """Build updated market status entry, preserving good timestamps and event counts on failure."""
    prev = dict(prev_entry or {})
    error_list = [str(e) for e in errors if str(e).strip()]

    # Suspicious drop below minimum expected upcoming events is treated as a threshold breach
    below_min = (
        upcoming_events is not None
        and not error_list
        and upcoming_events < min_upcoming_events
    )
    if below_min:
        error_list.append(
            f"Upcoming events ({upcoming_events}) below minimum threshold ({min_upcoming_events})"
        )

    run_succeeded = (not error_list) and (upcoming_events is not None)

    if run_succeeded:
        last_success_at: str | None = now_utc.isoformat()
        last_success_date: str | None = now_utc.date().isoformat()
        reported_upcoming = int(upcoming_events)
    else:
        last_success_at = prev.get("last_success_at")
        last_success_date = prev.get("last_success_date")
        # Preserve previous good event count when fetch crashed; keep actual suspicious count if 0 returned?
        # Wait: reporting actual observed count (or previous count when fetch threw) makes errors clear.
        reported_upcoming = (
            int(upcoming_events)
            if upcoming_events is not None
            else int(prev.get("upcoming_events") or 0)
        )

    prev_dt = _parse_iso_dt(last_success_at)
    if prev_dt is not None:
        elapsed = max(timedelta(0), now_utc - prev_dt)
        age_days: int | None = elapsed.days
        aged_out = elapsed > timedelta(days=max_age_days)
    else:
        age_days = None
        aged_out = False

    has_prior_success = last_success_at is not None
    stale = aged_out or (below_min and has_prior_success) or (below_min and upcoming_events == 0)
    healthy = run_succeeded and (not stale)

    return {
        "last_attempt_at": now_utc.isoformat(),
        "last_success_at": last_success_at,
        "last_success_date": last_success_date,
        "upcoming_events": reported_upcoming,
        "min_upcoming_events": min_upcoming_events,
        "max_age_days": max_age_days,
        "age_days": age_days,
        "stale": stale,
        "healthy": healthy,
        "errors": error_list,
    }


def inject_stale_warning_ics(
    feed_path: Path | str,
    market_id: str,
    warning_date: date,
    last_success_date: str | date | None,
    errors: Sequence[str],
    calendar_name: str,
    display_timezone: str = DEFAULT_DISPLAY_TIMEZONE,
) -> bool:
    """Inject an all-day warning VEVENT into an existing `.ics` feed without dropping good events.

    If `feed_path` exists, preserves all non-warning VEVENTs intact and adds/updates
    the single warning VEVENT (`{market_id}-stale-warning@earnings-cal`) on `warning_date`.
    If `feed_path` does not exist yet (e.g. first run failure with 0 prior events), creates
    a minimal calendar containing only the warning event.
    """
    path = Path(feed_path)
    target_tz = validate_timezone(display_timezone, field_name="display_timezone")
    warn_uid = stale_warning_uid(market_id)

    preserved_vevents: list[Any] = []
    if path.exists():
        raw_bytes = path.read_bytes()
        if raw_bytes.strip():
            existing_cal = Calendar.from_ical(raw_bytes)
            for comp in existing_cal.walk():
                if comp.name == "VEVENT":
                    uid = str(comp.get("uid") or "").strip()
                    if uid and uid != warn_uid:
                        preserved_vevents.append(comp)

    cal = Calendar()
    cal.add("prodid", "-//Earnings Watchlist//earnings-cal//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("X-WR-CALNAME", calendar_name)
    cal.add("X-WR-TIMEZONE", target_tz)

    refresh_duration = vDuration(timedelta(days=1))
    refresh_duration.params["VALUE"] = "DURATION"
    cal.add("REFRESH-INTERVAL", refresh_duration)
    cal.add("X-PUBLISHED-TTL", vDuration(timedelta(days=1)))

    for comp in preserved_vevents:
        cal.add_component(comp)

    warn_vevent = ICalEvent()
    warn_vevent.add("uid", warn_uid)
    warn_vevent.add("summary", stale_warning_summary(market_id, last_success_date))
    last_str = (
        last_success_date.isoformat()
        if isinstance(last_success_date, date)
        else (str(last_success_date) if last_success_date else "never")
    )
    err_text = "; ".join(str(e) for e in errors if str(e).strip()) or "Data age exceeded threshold."
    warn_vevent.add(
        "description",
        f"Market '{market_id}' data is stale or incomplete (last good update: {last_str}).\n"
        f"Details: {err_text}\n"
        f"This warning disappears automatically once the feed recovers.",
    )
    warn_vevent.add("dtstart", warning_date)
    warn_vevent.add("dtend", warning_date + timedelta(days=1))
    warn_vevent.add("dtstamp", DETERMINISTIC_DTSTAMP)
    warn_vevent.add("transp", "TRANSPARENT")
    cal.add_component(warn_vevent)

    new_bytes = cal.to_ical()
    if path.exists() and path.read_bytes() == new_bytes:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(new_bytes)
    return True


def build_status_snapshot(
    prev_status: dict[str, Any],
    markets_entries: dict[str, dict[str, Any]],
    sources_entries: dict[str, dict[str, Any]],
    thresholds: MonitoringThresholds,
    now_utc: datetime,
) -> dict[str, Any]:
    """Assemble the complete `docs/status.json` dictionary."""
    market_thresholds = {}
    for m_id in sorted(set(markets_entries.keys()) | set(thresholds.market_min_upcoming_events.keys())):
        market_thresholds[m_id] = {
            "max_age_days": thresholds.get_market_max_age_days(m_id),
            "min_upcoming_events": thresholds.get_market_min_upcoming_events(m_id),
        }

    source_thresholds = {}
    for src in sorted(set(sources_entries.keys()) | {"api", "seonet"}):
        source_thresholds[src] = {
            "max_age_days": thresholds.get_source_max_age_days(src),
        }

    return {
        "generated_at": now_utc.isoformat(),
        "thresholds": {
            "default_max_age_days": thresholds.max_age_days,
            "default_min_upcoming_events": thresholds.min_upcoming_events,
            "markets": market_thresholds,
            "sources": source_thresholds,
        },
        "markets": dict(sorted(markets_entries.items())),
        "sources": dict(sorted(sources_entries.items())),
    }


def update_seonet_source_status(
    status_path: Path | str,
    succeeded: bool,
    errors: Sequence[str] = (),
    now: datetime | None = None,
    max_age_days: int = 3,
) -> dict[str, Any]:
    """Update just the `sources.seonet` entry in `docs/status.json` (used by SEOnet CLI/scraper)."""
    now_utc = _normalize_utc(now)
    existing = load_status(status_path)
    sources = dict(existing.get("sources") or {})
    sources["seonet"] = evaluate_source_status(
        source_name="seonet",
        prev_entry=sources.get("seonet"),
        now_utc=now_utc,
        max_age_days=max_age_days,
        succeeded=succeeded,
        errors=errors,
    )
    existing["generated_at"] = now_utc.isoformat()
    existing["sources"] = dict(sorted(sources.items()))
    write_status(existing, status_path)
    return existing
