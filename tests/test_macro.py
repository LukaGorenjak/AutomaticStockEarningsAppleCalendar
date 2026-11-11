"""Tests for the Macro Calendar (`macro`) market, YAML validation, multi-day meetings, and UID stability."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
import requests
from icalendar import Calendar

from earnings_cal.build import render_ics
from earnings_cal.main import run
from earnings_cal.markets import (
    DateWindow,
    MacroMarket,
    get_market_registry,
    get_registered_market_ids,
    instantiate_markets,
    load_macro_events_yaml,
)
from earnings_cal.markets.macro import (
    DEFAULT_MACRO_EVENTS_FILE,
    MacroValidationError,
    parse_bls_cpi_schedule_html,
    parse_ecb_schedule_html,
    parse_fomc_schedule_html,
    verify_macro_schedule_online,
)
from earnings_cal.models import EventKind, EventTiming, load_config


def test_registry_discovers_macro_market_and_metadata() -> None:
    """`macro` market is registered in `MARKET_REGISTRY` and produces `macro.ics` ('Macro Calendar')."""
    registry = get_market_registry()
    assert "macro" in registry
    assert "macro" in get_registered_market_ids()

    markets = {m.id: m for m in instantiate_markets()}
    macro = markets["macro"]
    assert isinstance(macro, MacroMarket)
    assert macro.id == "macro"
    assert macro.display_name == "Macroeconomic Events"
    assert macro.calendar_name == "Macro Calendar"
    assert macro.feed_filename == "macro.ics"


def test_repo_macro_events_yaml_validates_all_four_series() -> None:
    """Repository `data/macro_events.yaml` loads cleanly and covers `ecb`, `fomc`, `us_cpi`, `ea_hicp`."""
    events = load_macro_events_yaml(DEFAULT_MACRO_EVENTS_FILE)
    assert len(events) >= 60

    tickers = {ev.ticker for ev in events}
    assert tickers == {"ECB", "FOMC", "US_CPI", "EA_HICP"}

    kinds = {ev.kind for ev in events}
    assert kinds == {EventKind.CENTRAL_BANK_MEETING, EventKind.INFLATION_RELEASE}

    for ev in events:
        assert ev.market == "macro"
        assert ev.source_url and ev.source_url.startswith("https://")
        if ev.kind == EventKind.CENTRAL_BANK_MEETING:
            assert ev.end_date is not None and ev.end_date > ev.date
            assert ev.decision_date == ev.end_date
            assert ev.decision_time in {"14:00", "14:15"}
            assert ev.timing == EventTiming.UNKNOWN
        else:
            assert ev.timing == EventTiming.EXACT
            assert ev.time in {"08:30", "11:00"}


@pytest.mark.parametrize(
    ("yaml_body", "expected_error_match"),
    [
        (
            """
macro_events:
  - series: unknown_series
    date: "2026-12-08"
    kind: central_bank_meeting
    source_url: "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
""",
            "Unknown macro series",
        ),
        (
            """
macro_events:
  - series: fomc
    date: "2026-12-09"
    end_date: "2026-12-08"
    kind: central_bank_meeting
    source_url: "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
""",
            "end_date .* before date",
        ),
        (
            """
macro_events:
  - series: fomc
    date: "2026-12-08"
    end_date: "2026-12-09"
    decision_date: "2026-12-10"
    kind: central_bank_meeting
    source_url: "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
""",
            "decision_date .* outside meeting span",
        ),
        (
            """
macro_events:
  - series: us_cpi
    date: "2026-12-10"
    kind: inflation_release
    time: "08:30"
    source_url: "ftp://invalid.example.com"
""",
            "http\\(s\\) 'source_url'",
        ),
        (
            """
macro_events:
  - series: ecb
    date: "2026-12-16"
    end_date: "2026-12-17"
    time: "14:15"
    kind: central_bank_meeting
    source_url: "https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html"
""",
            "must be an all-day event; use 'decision_time' instead of 'time'",
        ),
        (
            """
macro_events:
  - series: us_cpi
    date: "2026-12-10"
    kind: earnings
    source_url: "https://www.bls.gov/schedule/news_release/cpi.htm"
""",
            "Unsupported macro event kind",
        ),
    ],
)
def test_macro_yaml_validation_errors(
    tmp_path: Path,
    yaml_body: str,
    expected_error_match: str,
) -> None:
    """Invalid macro YAML entries fail validation with clear error messages."""
    yaml_file = tmp_path / "bad_macro.yaml"
    yaml_file.write_text(yaml_body, encoding="utf-8")
    with pytest.raises(MacroValidationError, match=expected_error_match):
        load_macro_events_yaml(yaml_file)


def test_multi_day_meeting_rendering_and_local_time_conversion() -> None:
    """Multi-day meetings render as multi-day all-day VEVENTs with decision day & converted time in description; inflation releases render as timed VEVENTs in display_timezone."""
    events = load_macro_events_yaml(
        DEFAULT_MACRO_EVENTS_FILE,
        window=DateWindow(start=date(2026, 12, 1), end=date(2026, 12, 20)),
    )
    by_ticker = {ev.ticker: ev for ev in events}
    assert set(by_ticker.keys()) == {"FOMC", "ECB", "US_CPI", "EA_HICP"}

    ics_bytes = render_ics(
        events,
        calendar_name="Macro Calendar",
        display_timezone="Europe/Ljubljana",
    )
    cal = Calendar.from_ical(ics_bytes)
    assert str(cal.get("X-WR-CALNAME")) == "Macro Calendar"

    vevents = {str(v.get("uid")): v for v in cal.walk() if v.name == "VEVENT"}

    # 1. Multi-day FOMC meeting (2026-12-08 to 2026-12-09 inclusive -> DTEND=2026-12-10)
    fomc_uid = "macro-FOMC-2026-12-08-central_bank_meeting@earnings-cal"
    assert fomc_uid in vevents
    fomc_v = vevents[fomc_uid]
    assert str(fomc_v.get("summary")) == "FOMC decision"
    assert fomc_v.decoded("dtstart") == date(2026, 12, 8)
    assert fomc_v.decoded("dtend") == date(2026, 12, 10)  # 2-day all-day event!
    fomc_desc = str(fomc_v.get("description"))
    assert "Meeting Dates: 2026-12-08 to 2026-12-09" in fomc_desc
    assert "Decision Day: 2026-12-09" in fomc_desc
    # 14:00 EST (America/New_York) -> 20:00 CET (Europe/Ljubljana)
    assert (
        "Decision Release Time: 2026-12-09 20:00 CET (Europe/Ljubljana) / "
        "2026-12-09 14:00 EST (America/New_York)"
    ) in fomc_desc

    # 2. Multi-day ECB meeting (2026-12-16 to 2026-12-17 inclusive -> DTEND=2026-12-18)
    ecb_uid = "macro-ECB-2026-12-16-central_bank_meeting@earnings-cal"
    assert ecb_uid in vevents
    ecb_v = vevents[ecb_uid]
    assert str(ecb_v.get("summary")) == "ECB decision"
    assert ecb_v.decoded("dtstart") == date(2026, 12, 16)
    assert ecb_v.decoded("dtend") == date(2026, 12, 18)
    ecb_desc = str(ecb_v.get("description"))
    assert "Decision Day: 2026-12-17" in ecb_desc
    assert "Decision Release Time: 2026-12-17 14:15 CET (Europe/Ljubljana)" in ecb_desc

    # 3. Timed US CPI release (08:30 America/New_York -> 14:30 Europe/Ljubljana)
    cpi_uid = "macro-US_CPI-2026-12-10-inflation_release@earnings-cal"
    assert cpi_uid in vevents
    cpi_v = vevents[cpi_uid]
    assert str(cpi_v.get("summary")) == "US CPI"
    assert cpi_v.decoded("dtstart") == datetime(
        2026, 12, 10, 14, 30, tzinfo=ZoneInfo("Europe/Ljubljana")
    )
    assert cpi_v.decoded("dtend") == datetime(
        2026, 12, 10, 15, 30, tzinfo=ZoneInfo("Europe/Ljubljana")
    )

    # 4. Timed Eurostat Flash HICP release (11:00 Europe/Brussels -> 11:00 Europe/Ljubljana)
    hicp_uid = "macro-EA_HICP-2026-12-01-inflation_release@earnings-cal"
    assert hicp_uid in vevents
    hicp_v = vevents[hicp_uid]
    assert str(hicp_v.get("summary")) == "Euro area flash HICP"
    assert hicp_v.decoded("dtstart") == datetime(
        2026, 12, 1, 11, 0, tzinfo=ZoneInfo("Europe/Ljubljana")
    )


def test_uid_stability_and_configurable_macro_series_filtering(tmp_path: Path) -> None:
    """UIDs remain deterministic across runs and `macro.series` filters the enabled macro sets."""
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
    cfg_path = tmp_path / "tickers.yaml"
    cfg_path.write_text(
        f"""
us:
  tickers: [AAPL]
ljse:
  tickers: [KRKG]
  events_file: "{ljse_yaml.name}"
macro:
  series:
    - fomc
    - us_cpi
  events_file: "{DEFAULT_MACRO_EVENTS_FILE.resolve().as_posix()}"
""",
        encoding="utf-8",
    )

    cfg = load_config(cfg_path)
    docs_dir = tmp_path / "docs"
    rc = run(
        config_path=cfg_path,
        output_dir=docs_dir,
        today=date(2026, 11, 10),
        use_mock=True,
    )
    assert rc == 0
    assert (docs_dir / "macro.ics").exists()

    cal = Calendar.from_ical((docs_dir / "macro.ics").read_bytes())
    assert str(cal.get("X-WR-CALNAME")) == "Macro Calendar"
    uids = [str(v.get("uid")) for v in cal.walk() if v.name == "VEVENT"]
    assert any(u.startswith("macro-FOMC-") for u in uids)
    assert any(u.startswith("macro-US_CPI-") for u in uids)
    assert not any(u.startswith("macro-ECB-") for u in uids)
    assert not any(u.startswith("macro-EA_HICP-") for u in uids)

    # Rerunning produces byte-for-byte identical macro.ics and stable UIDs
    first_bytes = (docs_dir / "macro.ics").read_bytes()
    assert (
        run(
            config_path=cfg_path,
            output_dir=docs_dir,
            today=date(2026, 11, 10),
            use_mock=True,
        )
        == 0
    )
    assert (docs_dir / "macro.ics").read_bytes() == first_bytes
    assert cfg.get_market_section("macro")["series"] == ["fomc", "us_cpi"]


def test_online_schedule_parsers_and_verification_helper() -> None:
    """Validation helper parses official FOMC, ECB, and BLS HTML schedules and verifies YAML entries."""
    sample_fomc_html = """
    <h4><a id="42828">2026 FOMC Meetings</a></h4>
    <div class="row fomc-meeting">
      <div class="fomc-meeting__month"><strong>December</strong></div>
      <div class="fomc-meeting__date">8-9*</div>
    </div>
    """
    assert parse_fomc_schedule_html(sample_fomc_html) == {(date(2026, 12, 8), date(2026, 12, 9))}

    sample_ecb_html = """
    <dt>16/12/2026</dt>
    <dd>Governing Council of the ECB: monetary policy meeting in Frankfurt (Day 1)</dd>
    <dt>17/12/2026</dt>
    <dd>Governing Council of the ECB: monetary policy meeting in Frankfurt (Day 2), followed by press conference</dd>
    <dt>25/11/2026</dt>
    <dd>Governing Council of the ECB: non-monetary policy meeting (in Frankfurt)</dd>
    """
    assert parse_ecb_schedule_html(sample_ecb_html) == {date(2026, 12, 16), date(2026, 12, 17)}

    sample_bls_html = """
    <table class="release-list">
      <tr><td>November 2026</td><td>Dec. 10, 2026</td><td>08:30 AM</td></tr>
      <tr><td>December 2026</td><td>Jan. 13, 2027</td><td>08:30 AM</td></tr>
    </table>
    """
    assert parse_bls_cpi_schedule_html(sample_bls_html) == {date(2026, 12, 10), date(2027, 1, 13)}

    mock_session = MagicMock(spec=requests.Session)

    def _side_effect(url: str, **kwargs: object) -> MagicMock:
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        if "federalreserve.gov" in url:
            resp.text = sample_fomc_html
        elif "ecb.europa.eu" in url:
            resp.text = sample_ecb_html
        elif "bls.gov" in url:
            resp.text = sample_bls_html
        else:
            resp.text = "<html>Eurostat</html>"
        return resp

    mock_session.get.side_effect = _side_effect
    results = verify_macro_schedule_online(
        DEFAULT_MACRO_EVENTS_FILE,
        session=mock_session,
        today=date(2026, 12, 1),
    )
    by_series = {r.series: r for r in results}
    assert set(by_series.keys()) == {"fomc", "ecb", "us_cpi", "ea_hicp"}
    assert by_series["fomc"].matched_count >= 1
    assert by_series["ecb"].matched_count >= 1
    assert by_series["us_cpi"].matched_count >= 2
    assert by_series["ea_hicp"].ok is True
