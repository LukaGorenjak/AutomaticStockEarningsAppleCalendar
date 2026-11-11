"""Change detection, JSONL audit logging, and human-readable summaries between runs."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from icalendar import Calendar

from earnings_cal.models import DEFAULT_DISPLAY_TIMEZONE, DateWindow, Event

DEFAULT_CHANGES_PATH = Path("data/changes.jsonl")
DEFAULT_MOVE_NOTE_DAYS = 14

_MOVED_NOTE_LINE_RE = re.compile(
    r"^Date changed from \d{4}-\d{2}-\d{2} on \d{4}-\d{2}-\d{2}$"
)


class ChangeType(StrEnum):
    """Supported classification of feed differences between consecutive runs."""

    DATE_CHANGED = "date_changed"
    REMOVED = "removed"
    ADDED = "added"
    DETAILS_CHANGED = "details_changed"


_CHANGE_PRIORITY: dict[ChangeType, int] = {
    ChangeType.DATE_CHANGED: 0,
    ChangeType.REMOVED: 1,
    ChangeType.ADDED: 2,
    ChangeType.DETAILS_CHANGED: 3,
}


@dataclass(frozen=True)
class IcsEventSnapshot:
    """Normalized representation of a VEVENT loaded from an existing `.ics` file."""

    uid: str
    market: str
    ticker: str
    local_date: date
    dtstart_iso: str
    summary: str
    description: str


@dataclass(frozen=True)
class CalendarChange:
    """Single detected change between a previous `.ics` feed and newly fetched events."""

    run_date: date
    market: str
    ticker: str
    uid: str
    change_type: ChangeType
    old_date: date | None = None
    new_date: date | None = None
    old_summary: str | None = None
    new_summary: str | None = None
    details: str | None = None

    @property
    def alert_key(self) -> str:
        """Stable key identifying this specific change for issue deduplication."""
        return "|".join(
            [
                self.change_type.value,
                self.market,
                self.uid,
                self.old_date.isoformat() if self.old_date else "",
                self.new_date.isoformat() if self.new_date else "",
            ]
        )

    @property
    def record_id(self) -> str:
        """Deterministic short hash uniquely identifying this run's change record."""
        payload = "|".join([self.run_date.isoformat(), self.alert_key])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        """Serialize change record to a deterministic JSON-compatible dictionary."""
        return {
            "record_id": self.record_id,
            "alert_key": self.alert_key,
            "run_date": self.run_date.isoformat(),
            "market": self.market,
            "ticker": self.ticker,
            "uid": self.uid,
            "change_type": self.change_type.value,
            "old_date": self.old_date.isoformat() if self.old_date else None,
            "new_date": self.new_date.isoformat() if self.new_date else None,
            "old_summary": self.old_summary,
            "new_summary": self.new_summary,
            "details": self.details,
        }


def strip_move_notes(description: str) -> str:
    """Remove generated `Date changed from ... on ...` lines for clean details diffing."""
    lines = [
        line
        for line in description.splitlines()
        if not _MOVED_NOTE_LINE_RE.match(line.strip())
    ]
    return "\n".join(lines).strip()


def _parse_uid_market_ticker(uid: str, fallback_market: str = "") -> tuple[str, str]:
    """Extract `(market, ticker)` from UID format `{market}-{ticker}-{period_or_date}-{kind}@earnings-cal`."""
    local_part = uid.split("@", 1)[0]
    parts = local_part.split("-")
    if len(parts) >= 3:
        return parts[0].strip().lower(), parts[1].strip().upper()
    if len(parts) == 2:
        return fallback_market.strip().lower(), parts[0].strip().upper()
    return fallback_market.strip().lower(), local_part.strip().upper()


def load_ics_snapshots(
    ics_path: Path | str,
    default_market: str = "",
) -> dict[str, IcsEventSnapshot]:
    """Load existing `.ics` VEVENTs keyed by UID. Returns `{}` if file does not exist."""
    path = Path(ics_path)
    if not path.exists():
        return {}

    raw_bytes = path.read_bytes()
    if not raw_bytes.strip():
        return {}

    cal = Calendar.from_ical(raw_bytes)
    snapshots: dict[str, IcsEventSnapshot] = {}

    for comp in cal.walk():
        if comp.name != "VEVENT":
            continue
        uid = str(comp.get("uid") or "").strip()
        if not uid or uid.endswith("-stale-warning@earnings-cal"):
            continue

        decoded_start = comp.decoded("dtstart")
        local_date = (
            decoded_start.date()
            if isinstance(decoded_start, datetime)
            else decoded_start
        )
        summary = str(comp.get("summary") or "").strip()
        description = str(comp.get("description") or "").strip()
        market, ticker = _parse_uid_market_ticker(uid, fallback_market=default_market)

        snapshots[uid] = IcsEventSnapshot(
            uid=uid,
            market=market,
            ticker=ticker,
            local_date=local_date,
            dtstart_iso=decoded_start.isoformat(),
            summary=summary,
            description=description,
        )

    return snapshots


def _describe_detail_diff(
    old_snap: IcsEventSnapshot,
    new_summary: str,
    new_desc_clean: str,
    new_start_iso: str,
) -> str:
    changed_parts: list[str] = []
    if old_snap.summary != new_summary:
        changed_parts.append("summary")
    if strip_move_notes(old_snap.description) != new_desc_clean:
        changed_parts.append("description")
    if old_snap.dtstart_iso != new_start_iso:
        changed_parts.append("time")
    return ", ".join(changed_parts) or "details"


def diff_feed(
    previous_ics_path: Path | str,
    new_events: Iterable[Event],
    window: DateWindow,
    run_date: date,
    market_id: str = "",
    display_timezone: str = DEFAULT_DISPLAY_TIMEZONE,
) -> list[CalendarChange]:
    """Compare previous `.ics` snapshots against `new_events` by UID within `window`.

    - Events present in `new_events` but not in previous `.ics` -> `added`.
    - Events in both with different local display date -> `date_changed`.
    - Events in both with same date but changed summary/description/time -> `details_changed`.
    - Events in previous `.ics` missing from `new_events`:
      - Ignored if `old.local_date < window.start` (scrolled into past) or
        `old.local_date > window.end` (beyond fetch horizon).
      - Classified as `removed` ONLY when `window.start <= old.local_date <= window.end`.
    """
    prev_by_uid = load_ics_snapshots(previous_ics_path, default_market=market_id)

    new_by_uid: dict[str, tuple[Event, str, str, str, date]] = {}
    for ev in new_events:
        display = ev.resolve_display(display_timezone)
        clean_desc = strip_move_notes(display.description)
        start_iso = (
            display.start_dt.isoformat()
            if display.is_timed and display.start_dt is not None
            else display.local_date.isoformat()
        )
        new_by_uid[ev.uid] = (
            ev,
            display.summary.strip(),
            clean_desc,
            start_iso,
            display.local_date,
        )

    changes: list[CalendarChange] = []

    for uid, (ev, new_summary, new_desc_clean, new_start_iso, new_date) in new_by_uid.items():
        ev_market = market_id or ev.market
        old_snap = prev_by_uid.get(uid)
        if old_snap is None:
            changes.append(
                CalendarChange(
                    run_date=run_date,
                    market=ev_market,
                    ticker=ev.ticker,
                    uid=uid,
                    change_type=ChangeType.ADDED,
                    old_date=None,
                    new_date=new_date,
                    new_summary=new_summary,
                )
            )
            continue

        if old_snap.local_date != new_date:
            changes.append(
                CalendarChange(
                    run_date=run_date,
                    market=ev_market,
                    ticker=ev.ticker,
                    uid=uid,
                    change_type=ChangeType.DATE_CHANGED,
                    old_date=old_snap.local_date,
                    new_date=new_date,
                    old_summary=old_snap.summary,
                    new_summary=new_summary,
                )
            )
            continue

        old_desc_clean = strip_move_notes(old_snap.description)
        if (
            old_snap.summary != new_summary
            or old_desc_clean != new_desc_clean
            or old_snap.dtstart_iso != new_start_iso
        ):
            changes.append(
                CalendarChange(
                    run_date=run_date,
                    market=ev_market,
                    ticker=ev.ticker,
                    uid=uid,
                    change_type=ChangeType.DETAILS_CHANGED,
                    old_date=old_snap.local_date,
                    new_date=new_date,
                    old_summary=old_snap.summary,
                    new_summary=new_summary,
                    details=_describe_detail_diff(old_snap, new_summary, new_desc_clean, new_start_iso),
                )
            )

    for uid, old_snap in prev_by_uid.items():
        if uid in new_by_uid:
            continue
        # Only report "removed" for future events inside the active fetch window
        if not window.contains(old_snap.local_date):
            continue
        changes.append(
            CalendarChange(
                run_date=run_date,
                market=market_id or old_snap.market,
                ticker=old_snap.ticker,
                uid=uid,
                change_type=ChangeType.REMOVED,
                old_date=old_snap.local_date,
                new_date=None,
                old_summary=old_snap.summary,
            )
        )

    return sorted(
        changes,
        key=lambda c: (
            _CHANGE_PRIORITY[c.change_type],
            c.market,
            c.ticker,
            c.uid,
        ),
    )


def load_change_records(changes_path: Path | str = DEFAULT_CHANGES_PATH) -> list[dict[str, Any]]:
    """Load existing JSONL change records from `data/changes.jsonl`."""
    path = Path(changes_path)
    if not path.exists():
        return []

    records: list[dict[str, Any]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        records.append(json.loads(line))
    return records


def append_changes(
    changes: Sequence[CalendarChange],
    changes_path: Path | str = DEFAULT_CHANGES_PATH,
) -> int:
    """Append newly detected changes deterministically to `data/changes.jsonl`.

    Deduplicates against existing `(record_id)` lines in the file so identical
    runs never append duplicate entries. Returns the count of appended records.
    """
    if not changes:
        return 0

    path = Path(changes_path)
    existing = load_change_records(path)
    existing_ids = {str(rec.get("record_id", "")) for rec in existing}

    ordered = sorted(
        changes,
        key=lambda c: (
            c.run_date.isoformat(),
            _CHANGE_PRIORITY[c.change_type],
            c.market,
            c.ticker,
            c.uid,
        ),
    )

    lines_to_append: list[str] = []
    for item in ordered:
        rec_id = item.record_id
        if rec_id in existing_ids:
            continue
        existing_ids.add(rec_id)
        lines_to_append.append(json.dumps(item.to_dict(), sort_keys=True, ensure_ascii=False))

    if not lines_to_append:
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    chunk = "\n".join(lines_to_append) + "\n"
    if path.exists():
        current_text = path.read_text(encoding="utf-8")
        if current_text and not current_text.endswith("\n"):
            chunk = "\n" + chunk
        path.write_text(current_text + chunk, encoding="utf-8")
    else:
        path.write_text(chunk, encoding="utf-8")

    return len(lines_to_append)


def get_recent_date_moves(
    changes_path: Path | str,
    run_date: date,
    events: Iterable[Event] = (),
    display_timezone: str = DEFAULT_DISPLAY_TIMEZONE,
    max_age_days: int = DEFAULT_MOVE_NOTE_DAYS,
) -> dict[str, str]:
    """Return `{uid: 'Date changed from X on <run date>'}` for events moved within `max_age_days`."""
    records = load_change_records(changes_path)
    if not records:
        return {}

    current_dates = {
        ev.uid: ev.resolve_display(display_timezone).local_date
        for ev in events
    }

    latest_moves: dict[str, tuple[date, str, str]] = {}
    for rec in records:
        if rec.get("change_type") != ChangeType.DATE_CHANGED.value:
            continue
        uid = str(rec.get("uid") or "").strip()
        old_str = str(rec.get("old_date") or "").strip()
        new_str = str(rec.get("new_date") or "").strip()
        rec_run_str = str(rec.get("run_date") or "").strip()
        if not (uid and old_str and new_str and rec_run_str):
            continue

        rec_run_date = date.fromisoformat(rec_run_str)
        age = (run_date - rec_run_date).days
        if age < 0 or age > max_age_days:
            continue

        if uid in current_dates and current_dates[uid].isoformat() != new_str:
            continue

        prev_entry = latest_moves.get(uid)
        if prev_entry is None or rec_run_date >= prev_entry[0]:
            latest_moves[uid] = (rec_run_date, old_str, rec_run_str)

    return {
        uid: f"Date changed from {old_str} on {move_date}"
        for uid, (_dt, old_str, move_date) in latest_moves.items()
    }


def has_alertable_changes(changes: Sequence[CalendarChange]) -> bool:
    """Return True if `changes` contains any `date_changed` or `removed` entries."""
    return any(
        c.change_type in {ChangeType.DATE_CHANGED, ChangeType.REMOVED}
        for c in changes
    )


def render_change_summary(changes: Sequence[CalendarChange]) -> str:
    """Render a concise human-readable summary with prominent date changes."""
    if not changes:
        return "No calendar changes detected."

    moved = [c for c in changes if c.change_type == ChangeType.DATE_CHANGED]
    removed = [c for c in changes if c.change_type == ChangeType.REMOVED]
    added = [c for c in changes if c.change_type == ChangeType.ADDED]
    details = [c for c in changes if c.change_type == ChangeType.DETAILS_CHANGED]

    lines: list[str] = []
    if moved:
        lines.append("Date changes:")
        for c in moved:
            lines.append(
                f"- [{c.market}] {c.ticker}: {c.old_date.isoformat()} -> {c.new_date.isoformat()} ({c.new_summary or c.uid})"
            )
    if removed:
        lines.append("Removed:")
        for c in removed:
            lines.append(
                f"- [{c.market}] {c.ticker}: was {c.old_date.isoformat()} ({c.old_summary or c.uid})"
            )
    if added:
        lines.append("Added:")
        for c in added:
            lines.append(
                f"- [{c.market}] {c.ticker}: {c.new_date.isoformat()} ({c.new_summary or c.uid})"
            )
    if details:
        lines.append("Details changed:")
        for c in details:
            note = f" [{c.details}]" if c.details else ""
            lines.append(
                f"- [{c.market}] {c.ticker}: {c.new_date.isoformat()}{note} ({c.new_summary or c.uid})"
            )

    return "\n".join(lines)


def render_commit_message(changes: Sequence[CalendarChange]) -> str:
    """Render git commit message (subject + body) from `changes`."""
    if not changes:
        return "chore: update market calendars"

    moved_n = sum(1 for c in changes if c.change_type == ChangeType.DATE_CHANGED)
    removed_n = sum(1 for c in changes if c.change_type == ChangeType.REMOVED)
    added_n = sum(1 for c in changes if c.change_type == ChangeType.ADDED)
    details_n = sum(1 for c in changes if c.change_type == ChangeType.DETAILS_CHANGED)

    parts: list[str] = []
    if moved_n:
        parts.append(f"{moved_n} moved")
    if removed_n:
        parts.append(f"{removed_n} removed")
    if added_n:
        parts.append(f"{added_n} added")
    if details_n:
        parts.append(f"{details_n} updated")

    subject = f"chore: update market calendars ({', '.join(parts)})"
    body = render_change_summary(changes)
    return f"{subject}\n\n{body}\n"


def render_alert_issue_payload(changes: Sequence[CalendarChange]) -> dict[str, Any] | None:
    """Build JSON payload for GitHub issue creation if alertable changes exist, else None."""
    alert_items = [
        c
        for c in changes
        if c.change_type in {ChangeType.DATE_CHANGED, ChangeType.REMOVED}
    ]
    if not alert_items:
        return None

    run_date_str = alert_items[0].run_date.isoformat()
    moved = [c for c in alert_items if c.change_type == ChangeType.DATE_CHANGED]
    removed = [c for c in alert_items if c.change_type == ChangeType.REMOVED]

    title_parts: list[str] = []
    if moved:
        title_parts.append(f"{len(moved)} date change{'s' if len(moved) != 1 else ''}")
    if removed:
        title_parts.append(f"{len(removed)} removal{'s' if len(removed) != 1 else ''}")
    title = f"Calendar alert ({run_date_str}): {', '.join(title_parts)}"

    digest_input = "\n".join(sorted(c.alert_key for c in alert_items))
    digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:12]

    body_lines: list[str] = [
        f"<!-- calendar-changes-id: {digest} -->",
        f"# Calendar Changes Detected ({run_date_str})",
        "",
    ]

    if moved:
        body_lines.append("## Date Changes")
        body_lines.append("")
        body_lines.append("| Market | Ticker | Old Date | New Date | Event |")
        body_lines.append("| --- | --- | --- | --- | --- |")
        for c in moved:
            body_lines.append(
                f"| `{c.market}` | `{c.ticker}` | `{c.old_date.isoformat()}` | "
                f"`{c.new_date.isoformat()}` | {c.new_summary or c.uid} |"
            )
        body_lines.append("")

    if removed:
        body_lines.append("## Removed Future Events")
        body_lines.append("")
        body_lines.append("| Market | Ticker | Previous Date | Event |")
        body_lines.append("| --- | --- | --- | --- |")
        for c in removed:
            body_lines.append(
                f"| `{c.market}` | `{c.ticker}` | `{c.old_date.isoformat()}` | {c.old_summary or c.uid} |"
            )
        body_lines.append("")

    for c in alert_items:
        body_lines.append(f"<!-- change-key: {c.alert_key} -->")

    return {
        "digest": digest,
        "title": title,
        "body": "\n".join(body_lines) + "\n",
        "items": [
            {
                "alert_key": c.alert_key,
                "change_type": c.change_type.value,
                "market": c.market,
                "ticker": c.ticker,
                "line": (
                    f"| `{c.market}` | `{c.ticker}` | `{c.old_date.isoformat()}` | "
                    f"`{c.new_date.isoformat()}` | {c.new_summary or c.uid} |"
                    if c.change_type == ChangeType.DATE_CHANGED
                    else f"| `{c.market}` | `{c.ticker}` | `{c.old_date.isoformat()}` | {c.old_summary or c.uid} |"
                ),
            }
            for c in alert_items
        ],
        "run_date": run_date_str,
    }
