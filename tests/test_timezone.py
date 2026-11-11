"""Tests for local display timezone conversion, DST mismatch weeks, and event timings."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from icalendar import Calendar

from earnings_cal.build import build_calendar, render_ics
from earnings_cal.main import run
from earnings_cal.models import (
    DEFAULT_DISPLAY_TIMEZONE,
    Event,
    EventKind,
    EventTiming,
    load_config,
)


def test_config_display_timezone_default_custom_and_validation(tmp_path: Path) -> None:
    """display_timezone defaults to Europe/Ljubljana and validates IANA names via ZoneInfo."""
    default_cfg = tmp_path / "default.yaml"
    default_cfg.write_text("us:\n  tickers:\n    - AAPL\n", encoding="utf-8")
    assert load_config(default_cfg).display_timezone == DEFAULT_DISPLAY_TIMEZONE
    assert DEFAULT_DISPLAY_TIMEZONE == "Europe/Ljubljana"

    custom_cfg = tmp_path / "custom.yaml"
    custom_cfg.write_text(
        "display_timezone: Asia/Tokyo\nus:\n  tickers:\n    - AAPL\n",
        encoding="utf-8",
    )
    assert load_config(custom_cfg).display_timezone == "Asia/Tokyo"

    bad_cfg = tmp_path / "bad.yaml"
    bad_cfg.write_text("display_timezone: UTC+2\nus:\n  tickers: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid IANA display_timezone"):
        load_config(bad_cfg)


def test_bmo_and_amc_normal_weeks_winter_and_summer() -> None:
    """During aligned winter and summer weeks, US->Ljubljana offset is +6 hours."""
    # Winter (both on standard time: EST UTC-5 -> CET UTC+1, +6h)
    winter_bmo = Event(
        market="us",
        ticker="AAPL",
        date=date(2027, 1, 28),
        hour="bmo",
        market_timezone="America/New_York",
    )
    winter_amc = Event(
        market="us",
        ticker="MSFT",
        date=date(2027, 1, 28),
        hour="amc",
        market_timezone="America/New_York",
    )

    wb = winter_bmo.resolve_display("Europe/Ljubljana")
    assert wb.is_timed is False
    assert wb.local_date == date(2027, 1, 28)
    assert wb.summary == "AAPL earnings (approx. before 15:30 CET)"
    assert "approx., typical pre-market release" in wb.description
    assert "before 09:30 EST" in wb.description
    assert "not a confirmed exact time" in wb.description

    wa = winter_amc.resolve_display("Europe/Ljubljana")
    assert wa.is_timed is False
    assert wa.local_date == date(2027, 1, 28)
    assert wa.summary == "MSFT earnings (approx. after 22:00 CET)"
    assert "approx., typical after-close release" in wa.description
    assert "after 16:00 EST" in wa.description

    # Summer (both on DST: EDT UTC-4 -> CEST UTC+2, +6h)
    summer_bmo = Event(market="us", ticker="NVDA", date=date(2026, 7, 15), hour="bmo")
    summer_amc = Event(market="us", ticker="GOOGL", date=date(2026, 7, 15), hour="amc")

    assert summer_bmo.resolve_display("Europe/Ljubljana").summary == "NVDA earnings (approx. before 15:30 CEST)"
    assert summer_amc.resolve_display("Europe/Ljubljana").summary == "GOOGL earnings (approx. after 22:00 CEST)"


def test_bmo_and_amc_dst_mismatch_weeks_march_and_october() -> None:
    """In March and late-Oct/early-Nov DST mismatch weeks, US->Ljubljana offset is +5 hours."""
    # March mismatch week: US switched on 2026-03-08, EU switches on 2026-03-29
    march_bmo = Event(market="us", ticker="ORCL", date=date(2026, 3, 16), timing=EventTiming.BMO)
    march_amc = Event(market="us", ticker="ADBE", date=date(2026, 3, 16), timing=EventTiming.AMC)
    march_dmh = Event(market="us", ticker="FDX", date=date(2026, 3, 16), timing=EventTiming.DMH)

    mb = march_bmo.resolve_display("Europe/Ljubljana")
    assert mb.summary == "ORCL earnings (approx. before 14:30 CET)"
    assert "before 09:30 EDT" in mb.description

    ma = march_amc.resolve_display("Europe/Ljubljana")
    assert ma.summary == "ADBE earnings (approx. after 21:00 CET)"
    assert "after 16:00 EDT" in ma.description

    md = march_dmh.resolve_display("Europe/Ljubljana")
    assert md.summary == "FDX earnings (approx. 14:30-21:00 CET)"

    # Late October mismatch week: EU switched on 2026-10-25, US switches on 2026-11-01
    oct_bmo = Event(market="us", ticker="CAT", date=date(2026, 10, 28), hour="bmo")
    oct_amc = Event(market="us", ticker="META", date=date(2026, 10, 28), hour="amc")

    ob = oct_bmo.resolve_display("Europe/Ljubljana")
    assert ob.summary == "CAT earnings (approx. before 14:30 CET)"
    assert "before 09:30 EDT" in ob.description

    oa = oct_amc.resolve_display("Europe/Ljubljana")
    assert oa.summary == "META earnings (approx. after 21:00 CET)"
    assert "after 16:00 EDT" in oa.description


def test_exact_time_events_become_timed_vevents_and_handle_date_shift() -> None:
    """Exact-time events emit timed VEVENTs with DTEND duration and shift date across midnight."""
    # Confirmed US evening call at 18:30 EST -> 00:30 CET the NEXT day in Ljubljana!
    late_call = Event(
        market="us",
        ticker="AAPL",
        date=date(2026, 11, 25),
        kind=EventKind.EARNINGS,
        title="AAPL Q1 Earnings Call",
        time="18:30",
        duration_minutes=45,
        market_timezone="America/New_York",
    )
    # LJSE morning release at 08:15 CET in Ljubljana
    ljse_morning = Event(
        market="ljse",
        ticker="KRKG",
        date=date(2026, 11, 12),
        kind=EventKind.EARNINGS,
        title="KRKG Q1-Q3 2026 Interim Report",
        time="08:15",
        duration_minutes=60,
        market_timezone="Europe/Ljubljana",
    )

    ics_bytes = render_ics([late_call, ljse_morning], display_timezone="Europe/Ljubljana")
    cal = Calendar.from_ical(ics_bytes)
    vevents = [c for c in cal.walk() if c.name == "VEVENT"]
    assert len(vevents) == 2

    ljse_v, us_v = vevents[0], vevents[1]

    # LJSE event on Nov 12 08:15 CET
    assert str(ljse_v.get("summary")) == "KRKG Q1-Q3 2026 Interim Report"
    ljse_start = ljse_v.decoded("dtstart")
    ljse_end = ljse_v.decoded("dtend")
    assert isinstance(ljse_start, datetime)
    assert ljse_start == datetime(2026, 11, 12, 8, 15, tzinfo=ZoneInfo("Europe/Ljubljana"))
    assert ljse_end - ljse_start == timedelta(minutes=60)

    # US 18:30 EST on Nov 25 converts to 00:30 CET on Nov 26 (date shifted forward!)
    assert str(us_v.get("summary")) == "AAPL Q1 Earnings Call"
    us_start = us_v.decoded("dtstart")
    us_end = us_v.decoded("dtend")
    assert isinstance(us_start, datetime)
    assert us_start.date() == date(2026, 11, 26)
    assert us_start == datetime(2026, 11, 26, 0, 30, tzinfo=ZoneInfo("Europe/Ljubljana"))
    assert us_end - us_start == timedelta(minutes=45)
    assert "Local Time: 2026-11-26 00:30 CET (Europe/Ljubljana)" in str(us_v.get("description"))
    assert "Market Time: 2026-11-25 18:30 EST (America/New_York)" in str(us_v.get("description"))
    assert "corresponds to market date 2026-11-25" in str(us_v.get("description"))
    # UID stays anchored to the market event identity
    assert str(us_v.get("uid")) == "us-AAPL-2026-11-25-earnings@earnings-cal"


def test_unknown_timing_stays_all_day_without_fabricated_time() -> None:
    """Events with unknown timing stay all-day with no clock time in title."""
    unknown_ev = Event(
        market="ljse",
        ticker="CICG",
        date=date(2026, 11, 11),
        kind=EventKind.EARNINGS,
        title="CICG Interim Report",
    )
    assert unknown_ev.timing == EventTiming.UNKNOWN

    cal = build_calendar([unknown_ev], display_timezone="Europe/Ljubljana")
    vevents = [c for c in cal.walk() if c.name == "VEVENT"]
    assert len(vevents) == 1

    v = vevents[0]
    assert str(v.get("summary")) == "CICG Interim Report"
    dtstart = v.decoded("dtstart")
    assert type(dtstart) is date
    assert dtstart == date(2026, 11, 11)
    assert "Timing: Unknown (all-day" in str(v.get("description"))


def test_different_display_timezone_and_approximate_date_rollover(tmp_path: Path) -> None:
    """Non-default display_timezone converts both exact and approximate events and shifts dates when crossing midnight."""
    # In Asia/Tokyo (UTC+9):
    # - 16:00 EST on 2026-11-25 (UTC-5) -> +14h -> 06:00 JST on 2026-11-26 (next day!)
    # - 09:30 EST on 2026-11-25 -> +14h -> 23:30 JST on 2026-11-25 (same day!)
    # - LJSE 08:15 CET on 2026-11-12 (UTC+1) -> +8h -> 16:15 JST on 2026-11-12
    ljse_yaml = tmp_path / "ljse_events.yaml"
    ljse_yaml.write_text(
        """
ljse_events:
  - ticker: KRKG
    date: "2026-11-12"
    kind: earnings
    time: "08:15"
    title: "KRKG Q1-Q3 2026 Interim Report"
""",
        encoding="utf-8",
    )
    cfg = tmp_path / "tickers.yaml"
    cfg.write_text(
        f"""
display_timezone: Asia/Tokyo
us:
  tickers: []
  manual_events:
    - ticker: AAPL
      date: "2026-11-25"
      hour: amc
    - ticker: WMT
      date: "2026-11-25"
      hour: bmo
ljse:
  tickers:
    - KRKG
  events_file: "{ljse_yaml.name}"
""",
        encoding="utf-8",
    )

    docs_dir = tmp_path / "docs"
    rc = run(config_path=cfg, output_dir=docs_dir, today=date(2026, 11, 10))
    assert rc == 0

    us_cal = Calendar.from_ical((docs_dir / "us.ics").read_bytes())
    assert str(us_cal.get("X-WR-TIMEZONE")) == "Asia/Tokyo"
    us_events = {str(v.get("uid")): v for v in us_cal.walk() if v.name == "VEVENT"}

    wmt = us_events["us-WMT-2026-11-25-earnings@earnings-cal"]
    assert str(wmt.get("summary")) == "WMT earnings (approx. before 23:30 JST)"
    assert wmt.decoded("dtstart") == date(2026, 11, 25)

    aapl = us_events["us-AAPL-2026-11-25-earnings@earnings-cal"]
    assert str(aapl.get("summary")) == "AAPL earnings (approx. after 06:00 JST)"
    # Converted time crosses midnight into Nov 26 in Tokyo!
    assert aapl.decoded("dtstart") == date(2026, 11, 26)
    assert "Date Shift: 2026-11-26 (Asia/Tokyo) corresponds to market date 2026-11-25 (America/New_York)" in str(
        aapl.get("description")
    )

    ljse_cal = Calendar.from_ical((docs_dir / "ljse.ics").read_bytes())
    ljse_v = [v for v in ljse_cal.walk() if v.name == "VEVENT"][0]
    assert ljse_v.decoded("dtstart") == datetime(2026, 11, 12, 16, 15, tzinfo=ZoneInfo("Asia/Tokyo"))
