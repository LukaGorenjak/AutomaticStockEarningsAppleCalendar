"""Static dashboard generator (`docs/index.html`) built strictly from existing local data."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from icalendar import Calendar

from earnings_cal.changes import load_change_records
from earnings_cal.models import (
    DEFAULT_DISPLAY_TIMEZONE,
    Event,
    EventTiming,
    validate_timezone,
)
from earnings_cal.results import EarningsResultRow, load_results_csv, sort_results_rows
from earnings_cal.status import STALE_WARNING_UID_SUFFIX, load_status

if TYPE_CHECKING:
    from earnings_cal.markets.base import Market

DEFAULT_PAGES_HOST = "lukagorenjak.github.io/AutomaticStockEarningsAppleCalendar"

_LOCAL_TIME_LINE_RE = re.compile(r"^Local Time:\s*(.+)$", re.M)
_TIMING_APPROX_LINE_RE = re.compile(r"^Timing:\s*(approx\.[^—\n]+)", re.M)
_DECISION_RELEASE_LINE_RE = re.compile(r"^Decision Release Time:\s*([^/\n]+)", re.M)
_APPROX_SUMMARY_SUFFIX_RE = re.compile(r"\s*\((approx\.[^)]+)\)\s*$")


def _format_event_local_time_label(event: Event, display_tz: str) -> tuple[str, str]:
    """Return `(local_time_label, sort_time_key)` for an `Event` in `display_tz`."""
    display = event.resolve_display(display_tz)
    timing = event.timing if isinstance(event.timing, EventTiming) else EventTiming(event.timing)

    if display.is_timed and display.start_dt is not None:
        tz_abbr = display.start_dt.tzname() or display_tz
        return f"{display.start_dt.strftime('%H:%M')} {tz_abbr}", display.start_dt.strftime("%H:%M")

    if timing in {EventTiming.BMO, EventTiming.AMC, EventTiming.DMH}:
        m = _APPROX_SUMMARY_SUFFIX_RE.search(display.summary)
        if m:
            return m.group(1).strip(), "12:00"
        return f"approx. ({timing.value.upper()})", "12:00"

    if event.decision_date is not None and event.decision_time is not None:
        m_dec = _DECISION_RELEASE_LINE_RE.search(display.description)
        if m_dec:
            return f"All-day (decision: {m_dec.group(1).strip()})", "00:00"

    if display.local_end_date and display.local_end_date > display.local_date:
        return f"All-day ({display.local_date.isoformat()} .. {display.local_end_date.isoformat()})", "00:00"

    return "All-day", "00:00"


def serialize_event_for_dashboard(
    event: Event,
    display_timezone: str = DEFAULT_DISPLAY_TIMEZONE,
) -> dict[str, Any]:
    """Convert an `Event` into a deterministic dictionary for the dashboard JSON payload."""
    display = event.resolve_display(display_timezone)
    local_time_label, sort_time = _format_event_local_time_label(event, display_timezone)
    clean_title = _APPROX_SUMMARY_SUFFIX_RE.sub("", display.summary).strip() or display.summary
    end_date_iso = (
        display.local_end_date.isoformat()
        if display.local_end_date and display.local_end_date > display.local_date
        else None
    )
    kind_str = event.kind.value if hasattr(event.kind, "value") else str(event.kind)

    return {
        "uid": event.uid,
        "market": event.market,
        "ticker": event.ticker,
        "kind": kind_str,
        "title": clean_title,
        "summary": display.summary,
        "date": display.local_date.isoformat(),
        "end_date": end_date_iso,
        "local_time": local_time_label,
        "sort_time": sort_time,
        "is_timed": display.is_timed,
        "source_url": event.source_url or "",
        "description": display.description,
    }


def load_events_from_ics_for_dashboard(
    ics_path: Path | str,
    market_id: str,
) -> list[dict[str, Any]]:
    """Fallback loader extracting dashboard event dicts from an existing `.ics` file when a market wasn't fetched in-memory."""
    path = Path(ics_path)
    if not path.exists():
        return []
    raw_bytes = path.read_bytes()
    if not raw_bytes.strip():
        return []

    cal = Calendar.from_ical(raw_bytes)
    items: list[dict[str, Any]] = []
    for comp in cal.walk():
        if comp.name != "VEVENT":
            continue
        uid = str(comp.get("uid") or "").strip()
        if not uid or uid.endswith(STALE_WARNING_UID_SUFFIX):
            continue

        local_part = uid.split("@", 1)[0]
        parts = local_part.split("-")
        mkt = parts[0].lower() if len(parts) >= 3 else market_id.lower()
        ticker = parts[1].upper() if len(parts) >= 3 else local_part.upper()
        kind = parts[-1].lower() if len(parts) >= 4 else "earnings"

        dtstart = comp.decoded("dtstart")
        dtend = comp.decoded("dtend") if comp.get("dtend") else None
        is_timed = isinstance(dtstart, datetime)
        if is_timed:
            local_date = dtstart.date()
            end_date_iso = None
            tz_abbr = dtstart.tzname() or ""
            local_time = f"{dtstart.strftime('%H:%M')} {tz_abbr}".strip()
            sort_time = dtstart.strftime("%H:%M")
        else:
            local_date = dtstart
            end_date_iso = None
            if isinstance(dtend, date) and (dtend - local_date).days > 1:
                inclusive_end = date.fromordinal(dtend.toordinal() - 1)
                end_date_iso = inclusive_end.isoformat()
            sort_time = "00:00"
            local_time = f"All-day ({local_date.isoformat()} .. {end_date_iso})" if end_date_iso else "All-day"

        summary = str(comp.get("summary") or "").strip()
        description = str(comp.get("description") or "").strip()
        url = str(comp.get("url") or "").strip()

        if not is_timed:
            m_approx = _APPROX_SUMMARY_SUFFIX_RE.search(summary)
            if m_approx:
                local_time = m_approx.group(1).strip()
                sort_time = "12:00"
            else:
                m_dec = _DECISION_RELEASE_LINE_RE.search(description)
                if m_dec:
                    local_time = f"All-day (decision: {m_dec.group(1).strip()})"

        clean_title = _APPROX_SUMMARY_SUFFIX_RE.sub("", summary).strip() or summary
        items.append(
            {
                "uid": uid,
                "market": mkt,
                "ticker": ticker,
                "kind": kind,
                "title": clean_title,
                "summary": summary,
                "date": local_date.isoformat(),
                "end_date": end_date_iso,
                "local_time": local_time,
                "sort_time": sort_time,
                "is_timed": is_timed,
                "source_url": url,
                "description": description,
            }
        )

    return sorted(items, key=lambda d: (d["date"], d["sort_time"], d["market"], d["ticker"], d["uid"]))


def build_dashboard_payload(
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
    max_changes: int = 50,
) -> dict[str, Any]:
    """Assemble the deterministic embedded JSON payload for `docs/index.html`."""
    clean_host = base_host.strip().rstrip("/")
    target_tz = validate_timezone(display_timezone, field_name="display_timezone")

    # 1. Markets / Feeds metadata
    feeds_list: list[dict[str, Any]] = []
    for m in markets:
        m_id = str(getattr(m, "id", "")).strip().lower()
        display_name = str(getattr(m, "display_name", m_id))
        cal_name = str(getattr(m, "calendar_name", display_name))
        tz_name = str(getattr(m, "timezone", "UTC"))
        feed_file = f"{m_id}.ics"
        webcal_url = f"webcal://{clean_host}/{feed_file}"
        aliases = [str(a) for a in (getattr(m, "legacy_aliases", ()) or ())]
        feeds_list.append(
            {
                "id": m_id,
                "display_name": display_name,
                "calendar_name": cal_name,
                "timezone": tz_name,
                "feed_file": feed_file,
                "webcal_url": webcal_url,
                "legacy_aliases": aliases,
            }
        )

    # 2. Upcoming events across markets
    serialized_events: list[dict[str, Any]] = []
    if isinstance(events_by_market, Mapping):
        for m_id in sorted(events_by_market.keys()):
            for ev in events_by_market[m_id]:
                serialized_events.append(serialize_event_for_dashboard(ev, target_tz))
        if output_dir is not None:
            for f_meta in feeds_list:
                if f_meta["id"] not in events_by_market:
                    ics_file = output_dir / f_meta["feed_file"]
                    serialized_events.extend(load_events_from_ics_for_dashboard(ics_file, f_meta["id"]))
    elif events_by_market is not None:
        for ev in events_by_market:
            serialized_events.append(serialize_event_for_dashboard(ev, target_tz))
    elif output_dir is not None:
        for f_meta in feeds_list:
            ics_file = output_dir / f_meta["feed_file"]
            serialized_events.extend(load_events_from_ics_for_dashboard(ics_file, f_meta["id"]))

    # Deduplicate by UID and sort deterministically
    by_uid: dict[str, dict[str, Any]] = {item["uid"]: item for item in serialized_events}
    ordered_events = sorted(
        by_uid.values(),
        key=lambda d: (d["date"], d["sort_time"], d["market"], d["ticker"], d["uid"]),
    )

    # 3. Status / Health Bar (`docs/status.json`)
    if status_data is not None:
        raw_status = dict(status_data)
    elif status_path is not None and Path(status_path).exists():
        raw_status = load_status(status_path)
    elif output_dir is not None and (output_dir / "status.json").exists():
        raw_status = load_status(output_dir / "status.json")
    else:
        raw_status = {"markets": {}, "sources": {}}

    status_markets: dict[str, Any] = {}
    for m_id, entry in sorted((raw_status.get("markets") or {}).items()):
        if not isinstance(entry, dict):
            continue
        status_markets[str(m_id)] = {
            "last_success_date": entry.get("last_success_date"),
            "upcoming_events": entry.get("upcoming_events", 0),
            "min_upcoming_events": entry.get("min_upcoming_events", 1),
            "max_age_days": entry.get("max_age_days", 3),
            "age_days": entry.get("age_days"),
            "stale": bool(entry.get("stale", False)),
            "healthy": bool(entry.get("healthy", True)),
            "errors": list(entry.get("errors") or []),
        }

    status_sources: dict[str, Any] = {}
    for src_id, entry in sorted((raw_status.get("sources") or {}).items()):
        if not isinstance(entry, dict):
            continue
        status_sources[str(src_id)] = {
            "last_success_date": entry.get("last_success_date"),
            "max_age_days": entry.get("max_age_days", 3),
            "age_days": entry.get("age_days"),
            "stale": bool(entry.get("stale", False)),
            "healthy": bool(entry.get("healthy", True)),
            "errors": list(entry.get("errors") or []),
        }

    # 4. Recent Changes (`data/changes.jsonl`)
    if changes_records is not None:
        raw_changes = [dict(c) for c in changes_records]
    elif changes_path is not None and Path(changes_path).exists():
        raw_changes = load_change_records(changes_path)
    elif output_dir is not None and (output_dir.parent / "data" / "changes.jsonl").exists():
        raw_changes = load_change_records(output_dir.parent / "data" / "changes.jsonl")
    else:
        raw_changes = []

    priority_map = {"date_changed": 0, "removed": 1, "added": 2, "details_changed": 3}
    sorted_changes = sorted(
        raw_changes,
        key=lambda c: (
            str(c.get("run_date") or ""),
            -priority_map.get(str(c.get("change_type") or ""), 9),
            str(c.get("market") or ""),
            str(c.get("ticker") or ""),
            str(c.get("uid") or ""),
        ),
        reverse=True,
    )[:max_changes]

    normalized_changes: list[dict[str, Any]] = []
    for c in sorted_changes:
        normalized_changes.append(
            {
                "record_id": str(c.get("record_id") or ""),
                "run_date": str(c.get("run_date") or ""),
                "market": str(c.get("market") or ""),
                "ticker": str(c.get("ticker") or ""),
                "change_type": str(c.get("change_type") or ""),
                "old_date": c.get("old_date"),
                "new_date": c.get("new_date"),
                "old_summary": c.get("old_summary"),
                "new_summary": c.get("new_summary"),
                "details": c.get("details"),
                "uid": str(c.get("uid") or ""),
            }
        )

    # 5. Results History (`data/results.csv`)
    loaded_results: list[EarningsResultRow] = []
    if results_rows is not None:
        for item in results_rows:
            if isinstance(item, EarningsResultRow):
                loaded_results.append(item)
            elif isinstance(item, Mapping):
                loaded_results.append(
                    EarningsResultRow(
                        market=str(item.get("market") or ""),
                        ticker=str(item.get("ticker") or ""),
                        report_date=(
                            item["report_date"]
                            if isinstance(item.get("report_date"), date)
                            else date.fromisoformat(str(item.get("report_date")))
                        ),
                        fiscal_period=str(item.get("fiscal_period") or ""),
                        eps_estimate=item.get("eps_estimate"),
                        eps_actual=item.get("eps_actual"),
                        surprise_pct=item.get("surprise_pct"),
                        price_move_1d_pct=item.get("price_move_1d_pct"),
                        recorded_at=str(item.get("recorded_at") or ""),
                        manual_notes=str(item.get("manual_notes") or ""),
                    )
                )
    elif results_path is not None and Path(results_path).exists():
        loaded_results = load_results_csv(results_path)
    elif output_dir is not None and (output_dir.parent / "data" / "results.csv").exists():
        loaded_results = load_results_csv(output_dir.parent / "data" / "results.csv")

    ordered_results = list(reversed(sort_results_rows(loaded_results)))
    serialized_results: list[dict[str, Any]] = [
        {
            "market": r.market,
            "ticker": r.ticker,
            "report_date": r.report_date.isoformat(),
            "fiscal_period": r.fiscal_period,
            "eps_estimate": r.eps_estimate,
            "eps_actual": r.eps_actual,
            "surprise_pct": r.surprise_pct,
            "price_move_1d_pct": r.price_move_1d_pct,
            "recorded_at": r.recorded_at,
            "manual_notes": r.manual_notes,
        }
        for r in ordered_results
    ]

    all_kinds = sorted({ev["kind"] for ev in ordered_events})

    return {
        "display_timezone": target_tz,
        "feeds": feeds_list,
        "status": {
            "markets": status_markets,
            "sources": status_sources,
        },
        "upcoming_events": ordered_events,
        "event_kinds": all_kinds,
        "recent_changes": normalized_changes,
        "results": serialized_results,
    }


def _render_health_bar_html(status_block: Mapping[str, Any], feeds: Sequence[Mapping[str, Any]]) -> str:
    sources = status_block.get("sources") or {}
    markets = status_block.get("markets") or {}
    pills: list[str] = []

    if not sources and not markets:
        return (
            '<div class="health-bar" id="health-bar">'
            '<span class="health-pill health-unknown">Health status: No status.json recorded yet</span>'
            "</div>"
        )

    for src_id, info in sorted(sources.items()):
        stale = bool(info.get("stale", False))
        healthy = bool(info.get("healthy", True))
        last_dt = info.get("last_success_date") or "never"
        css_cls = "health-stale" if stale else ("health-ok" if healthy else "health-warn")
        state_badge = "STALE" if stale else ("OK" if healthy else "WARN")
        err_list = info.get("errors") or []
        title_attr = html.escape("; ".join(str(e) for e in err_list)) if err_list else "Source healthy"
        pills.append(
            f'<div class="health-pill {css_cls}" data-source="{html.escape(src_id)}" title="{title_attr}">'
            f'<span class="health-dot"></span>'
            f'<strong>Source: {html.escape(src_id.upper())}</strong> '
            f'<span class="health-meta">({state_badge} · last update: {html.escape(str(last_dt))})</span>'
            f"</div>"
        )

    for m_id, info in sorted(markets.items()):
        stale = bool(info.get("stale", False))
        healthy = bool(info.get("healthy", True))
        last_dt = info.get("last_success_date") or "never"
        upcoming_n = info.get("upcoming_events", 0)
        css_cls = "health-stale" if stale else ("health-ok" if healthy else "health-warn")
        state_badge = "STALE" if stale else ("OK" if healthy else "WARN")
        err_list = info.get("errors") or []
        title_attr = html.escape("; ".join(str(e) for e in err_list)) if err_list else "Market feed healthy"
        pills.append(
            f'<div class="health-pill {css_cls}" data-market-health="{html.escape(m_id)}" title="{title_attr}">'
            f'<span class="health-dot"></span>'
            f'<strong>Market: {html.escape(m_id.upper())}</strong> '
            f'<span class="health-meta">({state_badge} · {upcoming_n} upcoming · last update: {html.escape(str(last_dt))})</span>'
            f"</div>"
        )

    return '<div class="health-bar" id="health-bar">\n      ' + "\n      ".join(pills) + "\n    </div>"


def render_dashboard_html(payload: Mapping[str, Any]) -> str:
    """Render the self-contained static HTML dashboard (`docs/index.html`) from `payload`."""
    tz_label = html.escape(str(payload.get("display_timezone") or DEFAULT_DISPLAY_TIMEZONE))
    feeds: Sequence[Mapping[str, Any]] = payload.get("feeds") or []
    events: Sequence[Mapping[str, Any]] = payload.get("upcoming_events") or []
    kinds: Sequence[str] = payload.get("event_kinds") or []
    changes: Sequence[Mapping[str, Any]] = payload.get("recent_changes") or []
    results: Sequence[Mapping[str, Any]] = payload.get("results") or []
    status_block: Mapping[str, Any] = payload.get("status") or {}

    health_bar_html = _render_health_bar_html(status_block, feeds)

    # Section 1: Feed cards / table rows with webcal:// subscribe buttons
    feed_rows: list[str] = []
    for f in feeds:
        m_id = html.escape(str(f["id"]))
        display_name = html.escape(str(f["display_name"]))
        cal_name = html.escape(str(f["calendar_name"]))
        tz_name = html.escape(str(f["timezone"]))
        feed_file = html.escape(str(f["feed_file"]))
        webcal_url = html.escape(str(f["webcal_url"]))
        aliases = f.get("legacy_aliases") or []
        alias_note = ""
        if aliases:
            alias_list = ", ".join(f"<code>{html.escape(str(a))}</code>" for a in aliases)
            alias_note = f"<br><small>Legacy alias: {alias_list} (please migrate to <code>{feed_file}</code>)</small>"

        feed_rows.append(
            f"          <tr>\n"
            f"            <td><strong>{display_name}</strong> (<code>{m_id}</code>){alias_note}</td>\n"
            f"            <td><code>{cal_name}</code></td>\n"
            f"            <td><code>{tz_name}</code></td>\n"
            f'            <td><a class="btn-subscribe webcal-link" data-feed="{feed_file}" href="{webcal_url}">Subscribe (webcal://)</a> '
            f'<small class="webcal-url-text">{webcal_url}</small></td>\n'
            f'            <td><a href="{feed_file}">{feed_file}</a></td>\n'
            f"          </tr>"
        )
    feeds_tbody = "\n".join(feed_rows)

    # Market & Kind filter options
    market_options = ['<option value="all">All markets</option>'] + [
        f'<option value="{html.escape(str(f["id"]))}">{html.escape(str(f["display_name"]))} ({html.escape(str(f["id"]))})</option>'
        for f in feeds
    ]
    kind_options = ['<option value="all">All event kinds</option>'] + [
        f'<option value="{html.escape(k)}">{html.escape(k.replace("_", " "))}</option>'
        for k in kinds
    ]

    # Section 2: Upcoming Events initial rows
    event_rows: list[str] = []
    for ev in events:
        m_id = html.escape(str(ev["market"]))
        ticker = html.escape(str(ev["ticker"]))
        kind = html.escape(str(ev["kind"]))
        title = html.escape(str(ev["title"]))
        dt_str = html.escape(str(ev["date"]))
        end_dt = ev.get("end_date")
        date_label = f"{dt_str} &rarr; {html.escape(str(end_dt))}" if end_dt else dt_str
        local_time = html.escape(str(ev["local_time"]))
        src_url = str(ev.get("source_url") or "").strip()
        title_cell = (
            f'<a href="{html.escape(src_url)}" target="_blank" rel="noopener">{title}</a>'
            if src_url
            else title
        )
        event_rows.append(
            f'          <tr data-market="{m_id}" data-kind="{kind}" data-date="{dt_str}" data-ticker="{ticker}">\n'
            f"            <td><code>{dt_str}</code>{f' <small>&rarr; {html.escape(str(end_dt))}</small>' if end_dt else ''}</td>\n"
            f"            <td><span class=\"badge badge-market\">{m_id}</span></td>\n"
            f"            <td><strong>{ticker}</strong></td>\n"
            f"            <td><span class=\"badge badge-kind\">{kind}</span></td>\n"
            f"            <td>{title_cell}</td>\n"
            f"            <td><code>{local_time}</code></td>\n"
            f"          </tr>"
        )

    events_empty_style = "" if not events else ' style="display:none;"'
    events_table_style = ' style="display:none;"' if not events else ""

    # Section 3: Recent Changes rows
    change_rows: list[str] = []
    for c in changes:
        run_dt = html.escape(str(c.get("run_date") or ""))
        m_id = html.escape(str(c.get("market") or ""))
        ticker = html.escape(str(c.get("ticker") or ""))
        ctype = str(c.get("change_type") or "")
        ctype_esc = html.escape(ctype)
        old_d = c.get("old_date")
        new_d = c.get("new_date")
        summary_txt = html.escape(str(c.get("new_summary") or c.get("old_summary") or c.get("uid") or ""))
        if ctype == "date_changed":
            detail_html = f"<strong>{html.escape(str(old_d))} &rarr; {html.escape(str(new_d))}</strong> ({summary_txt})"
        elif ctype == "removed":
            detail_html = f"Removed future event (was <code>{html.escape(str(old_d))}</code> · {summary_txt})"
        elif ctype == "added":
            detail_html = f"Added on <code>{html.escape(str(new_d))}</code> ({summary_txt})"
        else:
            extra = f" [{html.escape(str(c.get('details')))}]" if c.get("details") else ""
            detail_html = f"Updated on <code>{html.escape(str(new_d))}</code>{extra} ({summary_txt})"

        change_rows.append(
            f"          <tr>\n"
            f"            <td><code>{run_dt}</code></td>\n"
            f"            <td><span class=\"badge badge-market\">{m_id}</span></td>\n"
            f"            <td><strong>{ticker}</strong></td>\n"
            f"            <td><span class=\"badge badge-change badge-{ctype_esc}\">{ctype_esc}</span></td>\n"
            f"            <td>{detail_html}</td>\n"
            f"          </tr>"
        )

    # Section 4: Results History rows
    result_rows: list[str] = []
    for r in results:
        m_id = html.escape(str(r.get("market") or ""))
        ticker = html.escape(str(r.get("ticker") or ""))
        rep_dt = html.escape(str(r.get("report_date") or ""))
        fp = html.escape(str(r.get("fiscal_period") or ""))
        est = r.get("eps_estimate")
        act = r.get("eps_actual")
        surp = r.get("surprise_pct")
        move = r.get("price_move_1d_pct")
        notes = html.escape(str(r.get("manual_notes") or ""))

        est_str = f"{float(est):.2f}" if est is not None else "&mdash;"
        act_str = f"{float(act):.2f}" if act is not None else "&mdash;"
        if surp is not None:
            s_val = float(surp)
            s_cls = "num-pos" if s_val > 0 else ("num-neg" if s_val < 0 else "")
            surp_str = f'<span class="{s_cls}">{s_val:+.2f}%</span>'
        else:
            surp_str = "&mdash;"

        if move is not None:
            m_val = float(move)
            m_cls = "num-pos" if m_val > 0 else ("num-neg" if m_val < 0 else "")
            move_str = f'<span class="{m_cls}">{m_val:+.2f}%</span>'
        else:
            move_str = "&mdash;"

        result_rows.append(
            f"          <tr>\n"
            f"            <td><code>{rep_dt}</code></td>\n"
            f"            <td><span class=\"badge badge-market\">{m_id}</span></td>\n"
            f"            <td><strong>{ticker}</strong></td>\n"
            f"            <td><code>{fp}</code></td>\n"
            f"            <td>{est_str}</td>\n"
            f"            <td>{act_str}</td>\n"
            f"            <td>{surp_str}</td>\n"
            f"            <td>{move_str}</td>\n"
            f"            <td>{notes or '&mdash;'}</td>\n"
            f"          </tr>"
        )

    embedded_json = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False).replace(
        "</", "<\\/"
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Earnings &amp; Market Events Dashboard</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f8f9fb;
      --card-bg: #ffffff;
      --text: #1d1d1f;
      --muted: #6e6e73;
      --border: #e5e5ea;
      --th-bg: #f2f3f7;
      --code-bg: #eef0f5;
      --accent: #0066cc;
      --accent-fg: #ffffff;
      --ok-bg: #e8f8ee;
      --ok-fg: #137333;
      --ok-border: #a8dab5;
      --warn-bg: #fef7e0;
      --warn-fg: #b06000;
      --warn-border: #fdd663;
      --stale-bg: #fce8e6;
      --stale-fg: #c5221f;
      --stale-border: #f6aea9;
      --pos: #137333;
      --neg: #c5221f;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #0f1115;
        --card-bg: #181b22;
        --text: #e8eaed;
        --muted: #9aa0a6;
        --border: #2d323e;
        --th-bg: #222631;
        --code-bg: #262b38;
        --accent: #4da3ff;
        --accent-fg: #0b121e;
        --ok-bg: #132a1c;
        --ok-fg: #81c995;
        --ok-border: #285436;
        --warn-bg: #2f2611;
        --warn-fg: #fdd663;
        --warn-border: #5e4b1c;
        --stale-bg: #361518;
        --stale-fg: #f28b82;
        --stale-border: #70262b;
        --pos: #81c995;
        --neg: #f28b82;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      max-width: 1120px;
      margin: 0 auto;
      padding: 1.5rem 1rem 3rem;
      line-height: 1.5;
    }}
    header {{ margin-bottom: 1.25rem; }}
    h1 {{ font-size: 1.75rem; margin: 0 0 0.35rem; }}
    h2 {{ font-size: 1.2rem; margin: 0 0 0.75rem; }}
    p.subtitle {{ margin: 0 0 1rem; color: var(--muted); }}
    section.card {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 1.15rem 1.25rem;
      margin-bottom: 1.25rem;
    }}
    .health-bar {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.6rem;
      margin-bottom: 1.25rem;
    }}
    .health-pill {{
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      padding: 0.4rem 0.75rem;
      border-radius: 999px;
      font-size: 0.85rem;
      border: 1px solid var(--border);
      background: var(--card-bg);
    }}
    .health-dot {{
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: currentColor;
    }}
    .health-ok {{ background: var(--ok-bg); color: var(--ok-fg); border-color: var(--ok-border); }}
    .health-warn {{ background: var(--warn-bg); color: var(--warn-fg); border-color: var(--warn-border); }}
    .health-stale {{
      background: var(--stale-bg);
      color: var(--stale-fg);
      border-color: var(--stale-border);
      font-weight: 600;
    }}
    .table-wrap {{ width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.92rem; }}
    th, td {{
      text-align: left;
      padding: 0.6rem 0.7rem;
      border-bottom: 1px solid var(--border);
      vertical-align: top;
    }}
    th {{ background: var(--th-bg); font-weight: 600; white-space: nowrap; }}
    th.sortable {{ cursor: pointer; user-select: none; }}
    th.sortable:hover {{ text-decoration: underline; }}
    code {{
      background: var(--code-bg);
      padding: 0.12rem 0.35rem;
      border-radius: 4px;
      font-size: 0.88em;
    }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .btn-subscribe {{
      display: inline-block;
      background: var(--accent);
      color: var(--accent-fg);
      font-weight: 600;
      padding: 0.3rem 0.65rem;
      border-radius: 6px;
      font-size: 0.85rem;
      margin-right: 0.4rem;
    }}
    .btn-subscribe:hover {{ text-decoration: none; opacity: 0.92; }}
    .controls {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.65rem;
      align-items: center;
      margin-bottom: 0.85rem;
    }}
    .controls label {{ font-size: 0.88rem; color: var(--muted); }}
    .controls select, .controls input {{
      background: var(--bg);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 0.38rem 0.6rem;
      font-size: 0.9rem;
    }}
    .badge {{
      display: inline-block;
      padding: 0.1rem 0.45rem;
      border-radius: 4px;
      font-size: 0.78rem;
      background: var(--code-bg);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }}
    .badge-date_changed {{ background: var(--warn-bg); color: var(--warn-fg); }}
    .badge-removed {{ background: var(--stale-bg); color: var(--stale-fg); }}
    .badge-added {{ background: var(--ok-bg); color: var(--ok-fg); }}
    .num-pos {{ color: var(--pos); font-weight: 600; }}
    .num-neg {{ color: var(--neg); font-weight: 600; }}
    .empty-state {{
      padding: 1rem;
      color: var(--muted);
      background: var(--bg);
      border-radius: 6px;
      text-align: center;
      font-size: 0.92rem;
    }}
    small {{ color: var(--muted); }}
    @media (max-width: 640px) {{
      body {{ padding: 1rem 0.65rem 2rem; }}
      section.card {{ padding: 0.9rem; }}
      th, td {{ padding: 0.5rem; font-size: 0.85rem; }}
      .webcal-url-text {{ display: block; margin-top: 0.25rem; word-break: break-all; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Market Calendar Feeds &amp; Events Dashboard</h1>
    <p class="subtitle">Subscribe in Apple Calendar (macOS / iOS) via <code>webcal://</code>. All event times are shown in <code>{tz_label}</code>.</p>
    <!-- (5) Data Health Bar from docs/status.json -->
    {health_bar_html}
  </header>

  <!-- (1) Feed Links with webcal:// Subscribe Buttons -->
  <section class="card" id="section-feeds">
    <h2>1. Calendar Feeds (<code>.ics</code>)</h2>
    <div class="table-wrap">
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
{feeds_tbody}
        </tbody>
      </table>
    </div>
  </section>

  <!-- (2) Upcoming Events Table (Sortable & Filterable by Market and Kind) -->
  <section class="card" id="section-upcoming">
    <h2>2. Upcoming Events (<span id="upcoming-count">{len(events)}</span>)</h2>
    <div class="controls">
      <label for="filter-market">Market:</label>
      <select id="filter-market">
        {"".join(market_options)}
      </select>
      <label for="filter-kind">Kind:</label>
      <select id="filter-kind">
        {"".join(kind_options)}
      </select>
      <label for="filter-search">Search:</label>
      <input type="search" id="filter-search" placeholder="Filter ticker or title...">
    </div>
    <div id="upcoming-empty" class="empty-state"{events_empty_style}>No upcoming events match the current filter.</div>
    <div class="table-wrap"{events_table_style} id="upcoming-table-wrap">
      <table id="upcoming-table">
        <thead>
          <tr>
            <th class="sortable" data-sort="date">Date &#8597;</th>
            <th class="sortable" data-sort="market">Market &#8597;</th>
            <th class="sortable" data-sort="ticker">Ticker &#8597;</th>
            <th class="sortable" data-sort="kind">Kind &#8597;</th>
            <th class="sortable" data-sort="title">Event &#8597;</th>
            <th>Local Time (<code>{tz_label}</code>)</th>
          </tr>
        </thead>
        <tbody id="upcoming-tbody">
{chr(10).join(event_rows)}
        </tbody>
      </table>
    </div>
  </section>

  <!-- (3) Recent Changes from data/changes.jsonl -->
  <section class="card" id="section-changes">
    <h2>3. Recent Changelog (<span id="changes-count">{len(changes)}</span>)</h2>
    {('<div class="empty-state" id="changes-empty">No changelog entries recorded yet.</div>' if not changes else f'''<div class="table-wrap">
      <table id="changes-table">
        <thead>
          <tr>
            <th>Run Date</th>
            <th>Market</th>
            <th>Ticker</th>
            <th>Change Type</th>
            <th>Details</th>
          </tr>
        </thead>
        <tbody>
{chr(10).join(change_rows)}
        </tbody>
      </table>
    </div>''')}
  </section>

  <!-- (4) Post-Earnings Results History from data/results.csv -->
  <section class="card" id="section-results">
    <h2>4. Post-Earnings Results History (<span id="results-count">{len(results)}</span>)</h2>
    {('<div class="empty-state" id="results-empty">No post-earnings results recorded yet.</div>' if not results else f'''<div class="table-wrap">
      <table id="results-table">
        <thead>
          <tr>
            <th>Report Date</th>
            <th>Market</th>
            <th>Ticker</th>
            <th>Fiscal Period</th>
            <th>EPS Est.</th>
            <th>EPS Act.</th>
            <th>Surprise (%)</th>
            <th>1D Move (%)</th>
            <th>Notes</th>
          </tr>
        </thead>
        <tbody>
{chr(10).join(result_rows)}
        </tbody>
      </table>
    </div>''')}
  </section>

  <!-- Embedded Dashboard Data (JSON) -->
  <script id="dashboard-data" type="application/json">
{embedded_json}
  </script>

  <!-- Vanilla JS for webcal:// host resolution, filtering, and column sorting -->
  <script>
    (function () {{
      if (window.location.host) {{
        var basePath = window.location.pathname.replace(/\\/?(index\\.html)?$/, "/");
        var links = document.querySelectorAll("a.webcal-link[data-feed]");
        for (var i = 0; i < links.length; i++) {{
          var feed = links[i].getAttribute("data-feed");
          var url = "webcal://" + window.location.host + basePath + feed;
          links[i].setAttribute("href", url);
          var urlText = links[i].parentNode.querySelector(".webcal-url-text");
          if (urlText) urlText.textContent = url;
        }}
      }}

      var dataEl = document.getElementById("dashboard-data");
      if (!dataEl) return;
      var payload = JSON.parse(dataEl.textContent || "{{}}");
      var events = (payload.upcoming_events || []).slice();
      var sortKey = "date";
      var sortAsc = true;

      var marketSelect = document.getElementById("filter-market");
      var kindSelect = document.getElementById("filter-kind");
      var searchInput = document.getElementById("filter-search");
      var tbody = document.getElementById("upcoming-tbody");
      var countEl = document.getElementById("upcoming-count");
      var emptyEl = document.getElementById("upcoming-empty");
      var tableWrap = document.getElementById("upcoming-table-wrap");

      function escapeHtml(str) {{
        return String(str || "")
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;");
      }}

      function renderEvents() {{
        if (!tbody) return;
        var mVal = marketSelect ? marketSelect.value : "all";
        var kVal = kindSelect ? kindSelect.value : "all";
        var qVal = searchInput ? searchInput.value.trim().toLowerCase() : "";

        var filtered = events.filter(function (ev) {{
          if (mVal !== "all" && ev.market !== mVal) return false;
          if (kVal !== "all" && ev.kind !== kVal) return false;
          if (qVal) {{
            var hay = (ev.ticker + " " + ev.title + " " + ev.kind + " " + ev.market).toLowerCase();
            if (hay.indexOf(qVal) === -1) return false;
          }}
          return true;
        }});

        filtered.sort(function (a, b) {{
          var va = String(a[sortKey] || "");
          var vb = String(b[sortKey] || "");
          if (sortKey === "date") {{
            va = a.date + " " + (a.sort_time || "");
            vb = b.date + " " + (b.sort_time || "");
          }}
          var cmp = va.localeCompare(vb);
          return sortAsc ? cmp : -cmp;
        }});

        if (countEl) countEl.textContent = String(filtered.length);
        if (filtered.length === 0) {{
          tbody.innerHTML = "";
          if (emptyEl) emptyEl.style.display = "";
          if (tableWrap) tableWrap.style.display = "none";
          return;
        }}
        if (emptyEl) emptyEl.style.display = "none";
        if (tableWrap) tableWrap.style.display = "";

        var rowsHtml = filtered.map(function (ev) {{
          var endNote = ev.end_date ? " <small>&rarr; " + escapeHtml(ev.end_date) + "</small>" : "";
          var titleHtml = ev.source_url
            ? '<a href="' + escapeHtml(ev.source_url) + '" target="_blank" rel="noopener">' + escapeHtml(ev.title) + "</a>"
            : escapeHtml(ev.title);
          return (
            '<tr data-market="' + escapeHtml(ev.market) + '" data-kind="' + escapeHtml(ev.kind) + '">' +
            "<td><code>" + escapeHtml(ev.date) + "</code>" + endNote + "</td>" +
            '<td><span class="badge badge-market">' + escapeHtml(ev.market) + "</span></td>" +
            "<td><strong>" + escapeHtml(ev.ticker) + "</strong></td>" +
            '<td><span class="badge badge-kind">' + escapeHtml(ev.kind) + "</span></td>" +
            "<td>" + titleHtml + "</td>" +
            "<td><code>" + escapeHtml(ev.local_time) + "</code></td>" +
            "</tr>"
          );
        }});
        tbody.innerHTML = rowsHtml.join("\\n");
      }}

      if (marketSelect) marketSelect.addEventListener("change", renderEvents);
      if (kindSelect) kindSelect.addEventListener("change", renderEvents);
      if (searchInput) searchInput.addEventListener("input", renderEvents);

      var headers = document.querySelectorAll("#upcoming-table th.sortable[data-sort]");
      for (var j = 0; j < headers.length; j++) {{
        headers[j].addEventListener("click", function () {{
          var key = this.getAttribute("data-sort");
          if (sortKey === key) {{
            sortAsc = !sortAsc;
          }} else {{
            sortKey = key;
            sortAsc = true;
          }}
          renderEvents();
        }});
      }}
    }})();
  </script>
</body>
</html>
"""
