"""Unit and integration tests for earnings_cal."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests
from icalendar import Calendar

from earnings_cal.build import build_calendar, merge_events, render_ics, write_calendar
from earnings_cal.fetch import FinnhubProvider, ProviderError
from earnings_cal.main import run
from earnings_cal.models import EarningsEvent, load_config


def _mock_response(
    status_code: int = 200,
    json_data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.text = str(json_data or "")
    resp.json.return_value = json_data if json_data is not None else {}
    return resp


def test_uid_stability_and_date_move_updates_instead_of_duplicating() -> None:
    """When an earnings date moves for the same fiscal quarter, UID stays identical and updates."""
    initial_event = EarningsEvent(
        ticker="AAPL",
        date=date(2027, 1, 28),
        hour="amc",
        quarter=1,
        fiscal_year=2027,
        eps_estimate=2.35,
    )
    moved_event = EarningsEvent(
        ticker="AAPL",
        date=date(2027, 2, 4),
        hour="amc",
        quarter=1,
        fiscal_year=2027,
        eps_estimate=2.40,
    )

    assert initial_event.uid == "AAPL-2027Q1@earnings-cal"
    assert moved_event.uid == "AAPL-2027Q1@earnings-cal"

    merged = merge_events([initial_event], [moved_event])
    assert len(merged) == 1
    assert merged[0].date == date(2027, 2, 4)
    assert merged[0].eps_estimate == 2.40

    cal = Calendar.from_ical(render_ics(merged))
    vevents = [comp for comp in cal.walk() if comp.name == "VEVENT"]
    assert len(vevents) == 1
    assert str(vevents[0].get("uid")) == "AAPL-2027Q1@earnings-cal"
    assert vevents[0].decoded("dtstart") == date(2027, 2, 4)


def test_uid_fallback_when_quarter_missing() -> None:
    """Fallback UID uses {TICKER}-{date}@earnings-cal when quarter/fiscalYear is absent."""
    ev = EarningsEvent(ticker="ASML", date=date(2027, 1, 27), note="Q4 results")
    assert ev.uid == "ASML-2027-01-27@earnings-cal"


def test_no_duplicate_events_and_deterministic_output_on_rerun(tmp_path: Path) -> None:
    """Re-running with identical data produces no duplicates and byte-identical .ics output."""
    config_file = tmp_path / "tickers.yaml"
    config_file.write_text(
        "tickers:\n  - AAPL\n  - NVDA\n",
        encoding="utf-8",
    )
    output_ics = tmp_path / "docs" / "earnings.ics"

    session = MagicMock(spec=requests.Session)

    def side_effect(url: str, params: dict[str, str], **kwargs: Any) -> MagicMock:
        symbol = params["symbol"]
        if symbol == "AAPL":
            return _mock_response(
                200,
                {
                    "earningsCalendar": [
                        {
                            "symbol": "AAPL",
                            "date": "2026-11-25",
                            "hour": "amc",
                            "quarter": 1,
                            "year": 2027,
                            "epsEstimate": 2.35,
                        },
                        # Duplicate entry for same quarter with updated date in same payload
                        {
                            "symbol": "AAPL",
                            "date": "2026-11-26",
                            "hour": "amc",
                            "quarter": 1,
                            "year": 2027,
                            "epsEstimate": 2.38,
                        },
                    ]
                },
            )
        return _mock_response(
            200,
            {
                "earningsCalendar": [
                    {
                        "symbol": "NVDA",
                        "date": "2026-11-19",
                        "hour": "bmo",
                        "quarter": 3,
                        "year": 2027,
                        "epsEstimate": 0.89,
                    }
                ]
            },
        )

    session.get.side_effect = side_effect
    provider = FinnhubProvider(api_key="test-key", session=session)

    rc1 = run(
        config_path=config_file,
        output_path=output_ics,
        days=90,
        provider=provider,
        today=date(2026, 11, 10),
    )
    assert rc1 == 0
    first_bytes = output_ics.read_bytes()

    # Re-run with the exact same data
    rc2 = run(
        config_path=config_file,
        output_path=output_ics,
        days=90,
        provider=provider,
        today=date(2026, 11, 10),
    )
    assert rc2 == 0
    second_bytes = output_ics.read_bytes()

    assert first_bytes == second_bytes
    assert write_calendar(
        merge_events(
            provider.fetch_earnings(["AAPL", "NVDA"], date(2026, 11, 10), date(2027, 2, 8))
        ),
        output_ics,
    ) is False

    cal = Calendar.from_ical(second_bytes)
    vevents = [comp for comp in cal.walk() if comp.name == "VEVENT"]
    assert len(vevents) == 2
    uids = [str(v.get("uid")) for v in vevents]
    assert uids == ["NVDA-2027Q3@earnings-cal", "AAPL-2027Q1@earnings-cal"]


def test_manual_events_merge_and_parse_cleanly_with_icalendar(tmp_path: Path) -> None:
    """Manual events from tickers.yaml merge with API events and parse back cleanly."""
    config_file = tmp_path / "tickers.yaml"
    config_file.write_text(
        """
tickers:
  - AAPL
  - MSFT
manual_events:
  - ticker: NTO
    date: 2026-12-05
    note: "Manual earnings check for Nintendo"
""",
        encoding="utf-8",
    )
    output_ics = tmp_path / "docs" / "earnings.ics"

    session = MagicMock(spec=requests.Session)

    def side_effect(url: str, params: dict[str, str], **kwargs: Any) -> MagicMock:
        if params["symbol"] == "AAPL":
            return _mock_response(
                200,
                {
                    "earningsCalendar": [
                        {
                            "symbol": "AAPL",
                            "date": "2026-11-20",
                            "hour": "bmo",
                            "quarter": 1,
                            "year": 2027,
                            "epsEstimate": 2.10,
                            "confirmed": True,
                        }
                    ]
                },
            )
        return _mock_response(
            200,
            {
                "earningsCalendar": [
                    {
                        "symbol": "MSFT",
                        "date": "2026-12-10",
                        "hour": "",
                        "quarter": 2,
                        "year": 2027,
                        "epsEstimate": None,
                        "status": "estimated",
                    }
                ]
            },
        )

    session.get.side_effect = side_effect
    provider = FinnhubProvider(api_key="test-key", session=session)

    rc = run(
        config_path=config_file,
        output_path=output_ics,
        days=90,
        provider=provider,
        today=date(2026, 11, 10),
    )
    assert rc == 0
    assert output_ics.exists()

    raw_text = output_ics.read_text(encoding="utf-8")
    assert "X-WR-CALNAME:Earnings Watchlist" in raw_text
    assert "REFRESH-INTERVAL;VALUE=DURATION:P1D" in raw_text
    assert "X-PUBLISHED-TTL:P1D" in raw_text
    assert "TRIGGER:-PT15H" in raw_text

    cal = Calendar.from_ical(output_ics.read_bytes())
    vevents = [comp for comp in cal.walk() if comp.name == "VEVENT"]
    assert len(vevents) == 3

    summaries = [str(v.get("summary")) for v in vevents]
    assert summaries == [
        "AAPL earnings (BMO)",
        "NTO earnings",
        "MSFT earnings",
    ]

    # Verify all-day date objects (not datetime)
    for vevent in vevents:
        dtstart = vevent.decoded("dtstart")
        assert isinstance(dtstart, date)
        alarms = [c for c in vevent.walk() if c.name == "VALARM"]
        assert len(alarms) == 1
        assert alarms[0].decoded("trigger") == timedelta(hours=-15)

    aapl_desc = str(vevents[0].get("description"))
    assert "Fiscal Period: Q1 2027" in aapl_desc
    assert "EPS Estimate: $2.10" in aapl_desc
    assert "Status: Confirmed" in aapl_desc

    nto_desc = str(vevents[1].get("description"))
    assert "Manual earnings check for Nintendo" in nto_desc

    msft_desc = str(vevents[2].get("description"))
    assert "Fiscal Period: Q2 2027" in msft_desc
    assert "Status: Estimated" in msft_desc


def test_rate_limit_retry_and_single_failed_ticker_skipped() -> None:
    """HTTP 429 retries with backoff; a single failing ticker is warned and skipped."""
    session = MagicMock(spec=requests.Session)
    sleep_calls: list[float] = []

    aapl_responses = [
        _mock_response(429, headers={"Retry-After": "2"}),
        _mock_response(
            200,
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
            },
        ),
    ]

    def side_effect(url: str, params: dict[str, str], **kwargs: Any) -> MagicMock:
        if params["symbol"] == "AAPL":
            return aapl_responses.pop(0)
        # BAD ticker fails permanently with HTTP 404
        return _mock_response(404, {"error": "Not found"})

    session.get.side_effect = side_effect
    provider = FinnhubProvider(
        api_key="test-key",
        session=session,
        max_retries=2,
        backoff_seconds=0.5,
        sleep_fn=sleep_calls.append,
    )

    events = provider.fetch_earnings(
        ["AAPL", "BAD"],
        start_date=date(2026, 11, 10),
        end_date=date(2027, 2, 8),
    )
    assert len(events) == 1
    assert events[0].ticker == "AAPL"
    assert sleep_calls == [2.0]


def test_api_failure_or_empty_does_not_overwrite_existing_ics(tmp_path: Path) -> None:
    """When API fails or returns nothing for all tickers, existing .ics is untouched and exit is non-zero."""
    config_file = tmp_path / "tickers.yaml"
    config_file.write_text("tickers:\n  - AAPL\n", encoding="utf-8")

    output_ics = tmp_path / "docs" / "earnings.ics"
    output_ics.parent.mkdir(parents=True, exist_ok=True)
    sentinel_content = b"BEGIN:VCALENDAR\r\nPRODID:EXISTING\r\nEND:VCALENDAR\r\n"
    output_ics.write_bytes(sentinel_content)

    # Case 1: API returns empty earningsCalendar for all tickers
    empty_session = MagicMock(spec=requests.Session)
    empty_session.get.return_value = _mock_response(200, {"earningsCalendar": []})
    empty_provider = FinnhubProvider(api_key="test-key", session=empty_session, max_retries=0)

    rc_empty = run(
        config_path=config_file,
        output_path=output_ics,
        provider=empty_provider,
        today=date(2026, 11, 10),
    )
    assert rc_empty != 0
    assert output_ics.read_bytes() == sentinel_content

    # Case 2: API throws HTTP 500 for all tickers
    fail_session = MagicMock(spec=requests.Session)
    fail_session.get.return_value = _mock_response(500, {"error": "Internal Server Error"})
    fail_provider = FinnhubProvider(api_key="test-key", session=fail_session, max_retries=0)

    rc_fail = run(
        config_path=config_file,
        output_path=output_ics,
        provider=fail_provider,
        today=date(2026, 11, 10),
    )
    assert rc_fail != 0
    assert output_ics.read_bytes() == sentinel_content
