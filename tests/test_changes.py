"""Tests for run-to-run change detection, window-edge handling, JSONL logging, and idempotency."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import requests
from icalendar import Calendar

from earnings_cal.build import render_ics, write_calendar
from earnings_cal.changes import (
    ChangeType,
    append_changes,
    diff_feed,
    get_recent_date_moves,
    load_change_records,
    render_alert_issue_payload,
    render_change_summary,
    render_commit_message,
)
from earnings_cal.fetch import FinnhubProvider
from earnings_cal.main import run
from earnings_cal.models import DateWindow, Event


def _mock_finnhub_response(json_data: dict[str, Any]) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = 200
    resp.headers = {}
    resp.text = json.dumps(json_data)
    resp.json.return_value = json_data
    return resp


def test_first_run_with_no_previous_file_and_idempotency(tmp_path: Path) -> None:
    """First run classifies initial events as `added` (no alert issue); second identical run reports nothing."""
    ljse_yaml = tmp_path / "ljse_events.yaml"
    ljse_yaml.write_text(
        """
ljse_events:
  - ticker: KRKG
    date: "2026-11-12"
    kind: earnings
    time: "08:15"
    source_url: "https://seonet.ljse.si/?doc_id=92042"
""",
        encoding="utf-8",
    )
    cfg = tmp_path / "tickers.yaml"
    cfg.write_text(
        f"""
us:
  tickers: []
  manual_events:
    - ticker: AAPL
      date: "2026-11-25"
      quarter: 1
      fiscal_year: 2027
      hour: amc
ljse:
  tickers:
    - KRKG
  events_file: "{ljse_yaml.name}"
""",
        encoding="utf-8",
    )

    docs_dir = tmp_path / "docs"
    changes_file = tmp_path / "data" / "changes.jsonl"
    commit_msg_file = tmp_path / "commit_msg.txt"
    alert_file = tmp_path / "alert.json"

    # Run 1: no previous .ics files exist yet
    rc1 = run(
        config_path=cfg,
        output_dir=docs_dir,
        today=date(2026, 11, 10),
        changes_path=changes_file,
        commit_msg_file=commit_msg_file,
        alert_file=alert_file,
    )
    assert rc1 == 0
    assert (docs_dir / "us.ics").exists()
    assert (docs_dir / "ljse.ics").exists()
    assert changes_file.exists()

    records1 = load_change_records(changes_file)
    assert len(records1) == 2
    assert {r["change_type"] for r in records1} == {"added"}
    # Pure additions do NOT create an alert issue file
    assert not alert_file.exists()
    assert "2 added" in commit_msg_file.read_text(encoding="utf-8")

    initial_jsonl_text = changes_file.read_text(encoding="utf-8")
    initial_us_bytes = (docs_dir / "us.ics").read_bytes()

    # Run 2: exact same state -> idempotent (reports nothing, appends nothing, keeps .ics identical)
    rc2 = run(
        config_path=cfg,
        output_dir=docs_dir,
        today=date(2026, 11, 10),
        changes_path=changes_file,
        commit_msg_file=commit_msg_file,
        alert_file=alert_file,
    )
    assert rc2 == 0
    assert changes_file.read_text(encoding="utf-8") == initial_jsonl_text
    assert (docs_dir / "us.ics").read_bytes() == initial_us_bytes
    assert not alert_file.exists()
    assert commit_msg_file.read_text(encoding="utf-8") == "chore: update market calendars"


def test_all_change_types_summary_and_alert_issue_rules(tmp_path: Path) -> None:
    """Classify added, removed, date_changed, and details_changed accurately with move notes."""
    prev_ics = tmp_path / "us.ics"
    window = DateWindow(start=date(2026, 11, 10), end=date(2027, 2, 8))

    old_events = [
        # 1. Will move date (date_changed)
        Event(market="us", ticker="AAPL", date=date(2026, 11, 25), hour="amc", quarter=1, fiscal_year=2027),
        # 2. Will change EPS estimate on same date (details_changed)
        Event(market="us", ticker="MSFT", date=date(2026, 11, 29), hour="amc", quarter=2, fiscal_year=2027, eps_estimate=3.10),
        # 3. Future event inside window that disappears (removed)
        Event(market="us", ticker="TSLA", date=date(2026, 12, 15), hour="amc", quarter=4, fiscal_year=2026),
    ]
    write_calendar(old_events, prev_ics, calendar_name="Earnings US")

    new_events = [
        Event(market="us", ticker="AAPL", date=date(2026, 12, 3), hour="amc", quarter=1, fiscal_year=2027),
        Event(market="us", ticker="MSFT", date=date(2026, 11, 29), hour="amc", quarter=2, fiscal_year=2027, eps_estimate=3.25),
        # 4. Brand-new event (added)
        Event(market="us", ticker="NVDA", date=date(2026, 12, 10), hour="bmo", quarter=3, fiscal_year=2027),
    ]

    changes = diff_feed(
        previous_ics_path=prev_ics,
        new_events=new_events,
        window=window,
        run_date=date(2026, 11, 11),
        market_id="us",
    )
    by_ticker = {c.ticker: c for c in changes}
    assert by_ticker["AAPL"].change_type == ChangeType.DATE_CHANGED
    assert by_ticker["AAPL"].old_date == date(2026, 11, 25)
    assert by_ticker["AAPL"].new_date == date(2026, 12, 3)

    assert by_ticker["MSFT"].change_type == ChangeType.DETAILS_CHANGED
    assert by_ticker["MSFT"].details == "description"

    assert by_ticker["TSLA"].change_type == ChangeType.REMOVED
    assert by_ticker["TSLA"].old_date == date(2026, 12, 15)

    assert by_ticker["NVDA"].change_type == ChangeType.ADDED
    assert by_ticker["NVDA"].new_date == date(2026, 12, 10)

    summary = render_change_summary(changes)
    assert "[us] AAPL: 2026-11-25 -> 2026-12-03" in summary
    assert "[us] TSLA: was 2026-12-15" in summary
    assert "[us] NVDA: 2026-12-10" in summary
    assert "[us] MSFT: 2026-11-29" in summary

    commit_msg = render_commit_message(changes)
    assert "1 moved, 1 removed, 1 added, 1 updated" in commit_msg.splitlines()[0]

    # Issue payload only includes date_changed and removed (not added/details_changed)
    alert = render_alert_issue_payload(changes)
    assert alert is not None
    assert "1 date change, 1 removal" in alert["title"]
    assert "AAPL" in alert["body"]
    assert "TSLA" in alert["body"]
    assert "MSFT" not in alert["body"]
    assert "NVDA" not in alert["body"]

    # Pure details_changed or added -> no alert issue
    assert render_alert_issue_payload([by_ticker["MSFT"], by_ticker["NVDA"]]) is None

    # Append-only & deduplicated JSONL persistence
    changes_file = tmp_path / "data" / "changes.jsonl"
    assert append_changes(changes, changes_file) == 4
    assert append_changes(changes, changes_file) == 0  # dedup identical records

    # Move note attached within 14 days without extra VALARM
    moves_day5 = get_recent_date_moves(
        changes_file,
        run_date=date(2026, 11, 16),
        events=new_events,
    )
    assert moves_day5 == {
        "us-AAPL-2027Q1-earnings@earnings-cal": "Date changed from 2026-11-25 on 2026-11-11"
    }

    ics_with_note = render_ics(new_events, move_notes=moves_day5)
    cal = Calendar.from_ical(ics_with_note)
    aapl_v = next(
        c
        for c in cal.walk()
        if c.name == "VEVENT" and str(c.get("uid")) == "us-AAPL-2027Q1-earnings@earnings-cal"
    )
    assert "Date changed from 2026-11-25 on 2026-11-11" in str(aapl_v.get("description"))
    assert len([c for c in aapl_v.walk() if c.name == "VALARM"]) == 1

    # On subsequent run within 14 days, diff_feed detects NO changes even with move note in prev .ics
    prev_ics.write_bytes(ics_with_note)
    assert (
        diff_feed(
            previous_ics_path=prev_ics,
            new_events=new_events,
            window=window,
            run_date=date(2026, 11, 16),
            market_id="us",
        )
        == []
    )

    # After >14 days, move note expires cleanly and still triggers zero spurious details_changed alerts
    moves_day15 = get_recent_date_moves(
        changes_file,
        run_date=date(2026, 11, 26),
        events=new_events,
    )
    assert moves_day15 == {}
    assert (
        diff_feed(
            previous_ics_path=prev_ics,
            new_events=new_events,
            window=window,
            run_date=date(2026, 11, 26),
            market_id="us",
        )
        == []
    )


def test_window_edge_handling_ignores_past_and_beyond_horizon_events(tmp_path: Path) -> None:
    """Only events inside `[window.start, window.end]` count as `removed`; scrolled-out events are ignored."""
    prev_ics = tmp_path / "us.ics"
    old_events = [
        # Scrolled into the past (< 2026-11-15) -> IGNORE
        Event(market="us", ticker="PAST", date=date(2026, 11, 14), quarter=3, fiscal_year=2026),
        # Exact start edge (== 2026-11-15) missing -> REMOVED
        Event(market="us", ticker="TODAY", date=date(2026, 11, 15), quarter=3, fiscal_year=2026),
        # Inside window (2026-12-01) missing -> REMOVED
        Event(market="us", ticker="INSIDE", date=date(2026, 12, 1), quarter=4, fiscal_year=2026),
        # Beyond horizon (> 2027-02-13) missing -> IGNORE
        Event(market="us", ticker="HORIZON", date=date(2027, 2, 14), quarter=1, fiscal_year=2027),
    ]
    write_calendar(old_events, prev_ics, calendar_name="Earnings US")

    active_window = DateWindow(start=date(2026, 11, 15), end=date(2027, 2, 13))
    changes = diff_feed(
        previous_ics_path=prev_ics,
        new_events=[],
        window=active_window,
        run_date=date(2026, 11, 15),
        market_id="us",
    )

    assert [c.ticker for c in changes] == ["INSIDE", "TODAY"]
    assert all(c.change_type == ChangeType.REMOVED for c in changes)


def test_end_to_end_date_move_through_run_pipeline(tmp_path: Path) -> None:
    """Full run() pipeline logs move to data/changes.jsonl, embeds 14-day note, and creates alert JSON."""
    ljse_yaml = tmp_path / "ljse_events.yaml"
    ljse_yaml.write_text(
        """
ljse_events:
  - ticker: KRKG
    date: "2026-11-20"
    kind: earnings
    source_url: "https://seonet.ljse.si/?doc_id=92042"
""",
        encoding="utf-8",
    )
    cfg = tmp_path / "tickers.yaml"
    cfg.write_text(
        f"""
us:
  tickers:
    - AAPL
ljse:
  tickers:
    - KRKG
  events_file: "{ljse_yaml.name}"
""",
        encoding="utf-8",
    )

    docs_dir = tmp_path / "docs"
    changes_file = tmp_path / "data" / "changes.jsonl"
    commit_msg_file = tmp_path / "commit_msg.txt"
    alert_file = tmp_path / "alert.json"

    session1 = MagicMock(spec=requests.Session)
    session1.get.return_value = _mock_finnhub_response(
        {
            "earningsCalendar": [
                {
                    "symbol": "AAPL",
                    "date": "2026-11-25",
                    "hour": "amc",
                    "quarter": 1,
                    "year": 2027,
                    "epsEstimate": 2.35,
                }
            ]
        }
    )
    provider1 = FinnhubProvider(api_key="test-key", session=session1)
    assert (
        run(
            config_path=cfg,
            output_dir=docs_dir,
            provider=provider1,
            today=date(2026, 11, 10),
            changes_path=changes_file,
            commit_msg_file=commit_msg_file,
            alert_file=alert_file,
        )
        == 0
    )
    assert not alert_file.exists()

    # Run 2: AAPL moves from 2026-11-25 -> 2026-12-02
    session2 = MagicMock(spec=requests.Session)
    session2.get.return_value = _mock_finnhub_response(
        {
            "earningsCalendar": [
                {
                    "symbol": "AAPL",
                    "date": "2026-12-02",
                    "hour": "amc",
                    "quarter": 1,
                    "year": 2027,
                    "epsEstimate": 2.35,
                }
            ]
        }
    )
    provider2 = FinnhubProvider(api_key="test-key", session=session2)
    assert (
        run(
            config_path=cfg,
            output_dir=docs_dir,
            provider=provider2,
            today=date(2026, 11, 11),
            changes_path=changes_file,
            commit_msg_file=commit_msg_file,
            alert_file=alert_file,
        )
        == 0
    )

    assert alert_file.exists()
    alert_data = json.loads(alert_file.read_text(encoding="utf-8"))
    assert "1 date change" in alert_data["title"]
    assert "2026-11-25" in alert_data["body"]
    assert "2026-12-02" in alert_data["body"]
    assert "[us] AAPL: 2026-11-25 -> 2026-12-02" in commit_msg_file.read_text(encoding="utf-8")

    # Verify move note is present in docs/us.ics and docs/earnings.ics alias without adding extra VALARM
    for feed_name in ("us.ics", "earnings.ics"):
        cal = Calendar.from_ical((docs_dir / feed_name).read_bytes())
        vevent = [c for c in cal.walk() if c.name == "VEVENT"][0]
        assert "Date changed from 2026-11-25 on 2026-11-11" in str(vevent.get("description"))
        assert len([c for c in vevent.walk() if c.name == "VALARM"]) == 1

    # Run 3: repeat identical state -> idempotent! No alert, no new JSONL line
    assert (
        run(
            config_path=cfg,
            output_dir=docs_dir,
            provider=provider2,
            today=date(2026, 11, 12),
            changes_path=changes_file,
            commit_msg_file=commit_msg_file,
            alert_file=alert_file,
        )
        == 0
    )
    assert not alert_file.exists()
    assert len(load_change_records(changes_file)) == 3  # 2 initial adds + 1 date_changed
