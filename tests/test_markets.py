"""Tests for the multi-market architecture, registry discovery, UID uniqueness, and failure isolation."""

from __future__ import annotations

from datetime import date
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests
from icalendar import Calendar

from earnings_cal.fetch import FinnhubProvider
from earnings_cal.main import run
from earnings_cal.markets import (
    MARKET_REGISTRY,
    DateWindow,
    EUMarket,
    LJSEMarket,
    Market,
    USMarket,
    get_market_registry,
    get_registered_market_ids,
    instantiate_markets,
    register_market,
)
from earnings_cal.models import Event, EventKind, load_config


def _mock_finnhub_response(
    status_code: int = 200,
    json_data: dict[str, Any] | None = None,
) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.headers = {}
    resp.text = str(json_data or "")
    resp.json.return_value = json_data if json_data is not None else {}
    return resp


def test_registry_discovery_and_market_metadata() -> None:
    """Built-in `us` and `ljse` markets are registered with required metadata."""
    registry = get_market_registry()
    assert "us" in registry
    assert "ljse" in registry
    assert get_registered_market_ids()[:2] == ["us", "ljse"]

    markets = {m.id: m for m in instantiate_markets()}
    us = markets["us"]
    assert isinstance(us, USMarket)
    assert us.id == "us"
    assert us.display_name == "United States"
    assert us.timezone == "America/New_York"
    assert us.calendar_name == "Earnings US"
    assert us.feed_filename == "us.ics"
    assert "earnings.ics" in us.legacy_aliases

    ljse = markets["ljse"]
    assert isinstance(ljse, LJSEMarket)
    assert ljse.id == "ljse"
    assert ljse.display_name == "Ljubljana Stock Exchange"
    assert ljse.timezone == "Europe/Ljubljana"
    assert ljse.calendar_name == "Events LJSE"
    assert ljse.feed_filename == "ljse.ics"

    eu = markets["eu"]
    assert isinstance(eu, EUMarket)
    assert eu.id == "eu"
    assert eu.display_name == "Europe"
    assert eu.timezone == "Europe/Berlin"
    assert eu.calendar_name == "Events EU"
    assert eu.feed_filename == "eu.ics"
    assert eu.optional_when_unconfigured is True


def test_uid_uniqueness_across_markets_and_event_kinds() -> None:
    """UIDs include `{market}-{ticker}-{date or fiscal period}-{kind}@earnings-cal`."""
    us_ev = Event(
        market="us",
        ticker="KRKG",
        date=date(2026, 11, 12),
        kind=EventKind.EARNINGS,
    )
    ljse_ev = Event(
        market="ljse",
        ticker="KRKG",
        date=date(2026, 11, 12),
        kind=EventKind.EARNINGS,
        time="08:15",
        title="KRKG Q1-Q3 2026 Interim Report",
        source_url="https://seonet.ljse.si/?doc_id=92042",
    )
    ljse_div = Event(
        market="ljse",
        ticker="KRKG",
        date=date(2026, 11, 12),
        kind=EventKind.DIVIDEND,
    )
    ljse_ex_div = Event(
        market="ljse",
        ticker="KRKG",
        date=date(2026, 11, 12),
        kind=EventKind.EX_DIVIDEND,
    )
    ljse_agm = Event(
        market="ljse",
        ticker="KRKG",
        date=date(2026, 11, 12),
        kind=EventKind.AGM,
    )
    macro_ev = Event(
        market="us",
        ticker="CPI",
        date=date(2026, 11, 12),
        kind=EventKind.MACRO,
    )
    us_quarterly = Event(
        market="us",
        ticker="AAPL",
        date=date(2027, 1, 28),
        quarter=1,
        fiscal_year=2027,
        kind=EventKind.EARNINGS,
    )

    assert us_ev.uid == "us-KRKG-2026-11-12-earnings@earnings-cal"
    assert ljse_ev.uid == "ljse-KRKG-2026-11-12-earnings@earnings-cal"
    assert ljse_div.uid == "ljse-KRKG-2026-11-12-dividend@earnings-cal"
    assert ljse_ex_div.uid == "ljse-KRKG-2026-11-12-ex_dividend@earnings-cal"
    assert ljse_agm.uid == "ljse-KRKG-2026-11-12-agm@earnings-cal"
    assert macro_ev.uid == "us-CPI-2026-11-12-macro@earnings-cal"
    assert us_quarterly.uid == "us-AAPL-2027Q1-earnings@earnings-cal"

    all_uids = {
        us_ev.uid,
        ljse_ev.uid,
        ljse_div.uid,
        ljse_ex_div.uid,
        ljse_agm.uid,
        macro_ev.uid,
        us_quarterly.uid,
    }
    assert len(all_uids) == 7
    assert "Time: 08:15" in ljse_ev.description
    assert "Source: https://seonet.ljse.si/?doc_id=92042" in ljse_ev.description

    # Verify dataclasses.replace() does not duplicate generated description lines
    cloned = replace(us_quarterly, market="us", kind=EventKind.EARNINGS)
    assert cloned.description.count("Fiscal Period: Q1 2027") == 1


def test_dummy_third_market_added_in_test_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding a third market requires only a Market class, registry entry, and config section."""

    class XetraMarket(Market):
        id = "xetra"
        display_name = "Deutsche Börse Xetra"
        timezone = "Europe/Berlin"
        calendar_name = "Events Xetra"

        def fetch_events(self, window: DateWindow) -> list[Event]:
            tickers = [str(t).upper() for t in self.config.get("tickers", [])]
            return [
                Event(
                    market=self.id,
                    ticker=t,
                    date=window.start,
                    kind=EventKind.AGM,
                    title=f"{t} Annual General Meeting",
                    time="10:00",
                    description=f"Annual General Meeting for {t}",
                    source_url="https://www.boerse-frankfurt.de/",
                )
                for t in tickers
            ]

    # Temporarily register the dummy third market in MARKET_REGISTRY
    monkeypatch.setitem(MARKET_REGISTRY, XetraMarket.id, XetraMarket)

    ljse_yaml = tmp_path / "ljse_events.yaml"
    ljse_yaml.write_text(
        """
ljse_events:
  - ticker: KRKG
    date: "2026-11-12"
    kind: earnings
    time: "08:15"
    title: "KRKG Q1-Q3 2026 Report"
    source_url: "https://seonet.ljse.si/?doc_id=92042"
""",
        encoding="utf-8",
    )

    config_path = tmp_path / "tickers.yaml"
    config_path.write_text(
        f"""
us:
  tickers:
    - AAPL
ljse:
  tickers:
    - KRKG
  events_file: "{ljse_yaml.name}"
xetra:
  tickers:
    - SAP
    - SIE
""",
        encoding="utf-8",
    )

    docs_dir = tmp_path / "docs"
    rc = run(
        config_path=config_path,
        output_dir=docs_dir,
        today=date(2026, 11, 10),
        use_mock=True,
    )
    assert rc == 0

    # Verify all per-market feeds + legacy earnings.ics alias + index.html exist
    assert (docs_dir / "us.ics").exists()
    assert (docs_dir / "ljse.ics").exists()
    assert (docs_dir / "xetra.ics").exists()
    assert (docs_dir / "earnings.ics").exists()
    assert (docs_dir / "index.html").exists()

    # Verify earnings.ics is an identical alias of us.ics
    assert (docs_dir / "earnings.ics").read_bytes() == (docs_dir / "us.ics").read_bytes()

    # Verify each feed's X-WR-CALNAME and event UIDs
    us_cal = Calendar.from_ical((docs_dir / "us.ics").read_bytes())
    assert str(us_cal.get("X-WR-CALNAME")) == "Earnings US"

    ljse_cal = Calendar.from_ical((docs_dir / "ljse.ics").read_bytes())
    assert str(ljse_cal.get("X-WR-CALNAME")) == "Events LJSE"
    ljse_vevents = [c for c in ljse_cal.walk() if c.name == "VEVENT"]
    assert len(ljse_vevents) == 1
    assert str(ljse_vevents[0].get("uid")) == "ljse-KRKG-2026-11-12-earnings@earnings-cal"

    xetra_cal = Calendar.from_ical((docs_dir / "xetra.ics").read_bytes())
    assert str(xetra_cal.get("X-WR-CALNAME")) == "Events Xetra"
    xetra_vevents = [c for c in xetra_cal.walk() if c.name == "VEVENT"]
    assert len(xetra_vevents) == 2
    xetra_uids = [str(v.get("uid")) for v in xetra_vevents]
    assert xetra_uids == [
        "xetra-SAP-2026-11-10-agm@earnings-cal",
        "xetra-SIE-2026-11-10-agm@earnings-cal",
    ]

    # Verify docs/index.html lists all 3 markets with their webcal:// links
    index_html = (docs_dir / "index.html").read_text(encoding="utf-8")
    assert "webcal://" in index_html
    assert "us.ics" in index_html
    assert "ljse.ics" in index_html
    assert "xetra.ics" in index_html
    assert "Events Xetra" in index_html
    assert "Earnings US" in index_html
    assert "Events LJSE" in index_html


def test_one_market_failure_isolation(tmp_path: Path) -> None:
    """When one market fails, its old .ics file is kept, other markets still build, and exit is non-zero."""
    ljse_yaml = tmp_path / "ljse_events.yaml"
    ljse_yaml.write_text(
        """
ljse_events:
  - ticker: KRKG
    date: "2026-11-12"
    kind: earnings
    title: "KRKG Interim Report"
    source_url: "https://seonet.ljse.si/?doc_id=92042"
""",
        encoding="utf-8",
    )

    config_path = tmp_path / "tickers.yaml"
    config_path.write_text(
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
    docs_dir.mkdir(parents=True, exist_ok=True)
    sentinel_us = b"BEGIN:VCALENDAR\r\nPRODID:OLD-US\r\nEND:VCALENDAR\r\n"
    (docs_dir / "us.ics").write_bytes(sentinel_us)
    (docs_dir / "earnings.ics").write_bytes(sentinel_us)

    # Simulate Finnhub HTTP 500 failure for the US market while LJSE succeeds
    fail_session = MagicMock(spec=requests.Session)
    fail_session.get.return_value = _mock_finnhub_response(500, {"error": "Internal Error"})
    failing_us_provider = FinnhubProvider(api_key="test-key", session=fail_session, max_retries=0)

    rc = run(
        config_path=config_path,
        output_dir=docs_dir,
        provider=failing_us_provider,
        today=date(2026, 11, 10),
    )

    # Exit code must be non-zero because US failed
    assert rc != 0

    # US feed and its alias must preserve their old files untouched
    assert (docs_dir / "us.ics").read_bytes() == sentinel_us
    assert (docs_dir / "earnings.ics").read_bytes() == sentinel_us

    # LJSE feed must still have been built and updated cleanly
    assert (docs_dir / "ljse.ics").exists()
    ljse_cal = Calendar.from_ical((docs_dir / "ljse.ics").read_bytes())
    assert str(ljse_cal.get("X-WR-CALNAME")) == "Events LJSE"
    vevents = [c for c in ljse_cal.walk() if c.name == "VEVENT"]
    assert len(vevents) == 1
    assert str(vevents[0].get("uid")) == "ljse-KRKG-2026-11-12-earnings@earnings-cal"
