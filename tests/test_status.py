"""Tests for docs/status.json, staleness thresholds, fake-clock lifecycle, and stale calendar warnings."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests
from icalendar import Calendar

from earnings_cal.changes import load_change_records
from earnings_cal.fetch import FinnhubProvider
from earnings_cal.main import run
from earnings_cal.markets import MARKET_REGISTRY, DateWindow, Market
from earnings_cal.models import Event, load_config
from earnings_cal.status import STALE_WARNING_UID_SUFFIX, load_status, update_seonet_source_status


def _mock_finnhub_response(
    status_code: int = 200,
    json_data: dict[str, Any] | None = None,
) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.headers = {}
    resp.text = json.dumps(json_data or {})
    resp.json.return_value = json_data if json_data is not None else {}
    return resp


def test_configurable_thresholds_validation(tmp_path: Path) -> None:
    """Config parses default and custom staleness thresholds and rejects invalid entries."""
    default_cfg = tmp_path / "default.yaml"
    default_cfg.write_text("us:\n  tickers: [AAPL]\n", encoding="utf-8")
    loaded_default = load_config(default_cfg)
    assert loaded_default.thresholds.max_age_days == 3
    assert loaded_default.thresholds.min_upcoming_events == 1
    assert loaded_default.thresholds.get_source_max_age_days("api") == 3
    assert loaded_default.thresholds.get_source_max_age_days("seonet") == 3

    custom_cfg = tmp_path / "custom.yaml"
    custom_cfg.write_text(
        """
thresholds:
  max_age_days: 5
  min_upcoming_events: 2
  sources:
    seonet:
      max_age_days: 10
  markets:
    us:
      max_age_days: 2
      min_upcoming_events: 3
us:
  tickers: [AAPL]
""",
        encoding="utf-8",
    )
    loaded_custom = load_config(custom_cfg)
    assert loaded_custom.thresholds.get_market_max_age_days("us") == 2
    assert loaded_custom.thresholds.get_market_min_upcoming_events("us") == 3
    assert loaded_custom.thresholds.get_source_max_age_days("seonet") == 10
    assert loaded_custom.thresholds.get_source_max_age_days("api") == 5

    bad_cfg = tmp_path / "bad.yaml"
    bad_cfg.write_text("thresholds:\n  max_age_days: -1\n", encoding="utf-8")
    with pytest.raises(ValueError, match=">= 0"):
        load_config(bad_cfg)


def test_status_lifecycle_first_run_fresh_stale_and_recovered_with_fake_clock(
    tmp_path: Path,
) -> None:
    """Test first run -> fresh failure -> stale failure (with warning VEVENT) -> recovery using fake clock."""
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
thresholds:
  max_age_days: 3
  min_upcoming_events: 1
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
    status_file = docs_dir / "status.json"
    changes_file = tmp_path / "data" / "changes.jsonl"

    ok_session = MagicMock(spec=requests.Session)
    ok_session.get.return_value = _mock_finnhub_response(
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
    )
    ok_provider = FinnhubProvider(api_key="test-key", session=ok_session)

    fail_session = MagicMock(spec=requests.Session)
    fail_session.get.return_value = _mock_finnhub_response(500, {"error": "Server Error"})
    fail_provider = FinnhubProvider(api_key="test-key", session=fail_session, max_retries=0)

    # 1. FIRST RUN (Day 0: 2026-11-10 06:00 UTC) -> Healthy!
    t0 = datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc)
    rc0 = run(
        config_path=cfg,
        output_dir=docs_dir,
        provider=ok_provider,
        today=t0.date(),
        now=t0,
        changes_path=changes_file,
        status_path=status_file,
    )
    assert rc0 == 0
    assert status_file.exists()

    s0 = load_status(status_file)
    assert s0["markets"]["us"]["last_success_at"] == t0.isoformat()
    assert s0["markets"]["us"]["last_success_date"] == "2026-11-10"
    assert s0["markets"]["us"]["upcoming_events"] == 1
    assert s0["markets"]["us"]["age_days"] == 0
    assert s0["markets"]["us"]["stale"] is False
    assert s0["markets"]["us"]["healthy"] is True
    assert s0["markets"]["us"]["errors"] == []
    assert s0["sources"]["api"]["healthy"] is True
    assert s0["sources"]["api"]["last_success_at"] == t0.isoformat()
    assert "seonet" in s0["sources"]

    day0_us_bytes = (docs_dir / "us.ics").read_bytes()

    # 2. FRESH FAILURE (Day 2: 2026-11-12 06:00 UTC, age = 2 <= 3) -> Preserves good .ics & last_success_at
    t2 = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)
    rc2 = run(
        config_path=cfg,
        output_dir=docs_dir,
        provider=fail_provider,
        today=t2.date(),
        now=t2,
        changes_path=changes_file,
        status_path=status_file,
    )
    assert rc2 == 1
    assert (docs_dir / "us.ics").read_bytes() == day0_us_bytes  # feed preserved untouched!

    s2 = load_status(status_file)
    assert s2["markets"]["us"]["last_success_at"] == t0.isoformat()  # timestamp NEVER overwritten on failure
    assert s2["markets"]["us"]["age_days"] == 2
    assert s2["markets"]["us"]["stale"] is False  # within 3-day threshold
    assert s2["markets"]["us"]["healthy"] is False
    assert len(s2["markets"]["us"]["errors"]) == 1
    assert s2["sources"]["api"]["last_success_at"] == t0.isoformat()
    assert s2["sources"]["api"]["stale"] is False
    assert s2["sources"]["api"]["healthy"] is False

    # 3. STALE FAILURE (Day 4: 2026-11-14 06:00 UTC, age = 4 > 3) -> Stale! Injects warning VEVENT
    t4 = datetime(2026, 11, 14, 6, 0, tzinfo=timezone.utc)
    rc4 = run(
        config_path=cfg,
        output_dir=docs_dir,
        provider=fail_provider,
        today=t4.date(),
        now=t4,
        changes_path=changes_file,
        status_path=status_file,
    )
    assert rc4 == 1

    s4 = load_status(status_file)
    assert s4["markets"]["us"]["last_success_at"] == t0.isoformat()
    assert s4["markets"]["us"]["age_days"] == 4
    assert s4["markets"]["us"]["stale"] is True
    assert s4["markets"]["us"]["healthy"] is False
    assert s4["sources"]["api"]["stale"] is True

    # Verify WARNING VEVENT injected alongside preserved AAPL event in us.ics and earnings.ics
    for feed_name in ("us.ics", "earnings.ics"):
        cal4 = Calendar.from_ical((docs_dir / feed_name).read_bytes())
        vevents4 = {str(v.get("uid")): v for v in cal4.walk() if v.name == "VEVENT"}
        assert "us-AAPL-2027Q1-earnings@earnings-cal" in vevents4  # good event preserved!
        warn_uid = f"us{STALE_WARNING_UID_SUFFIX}"
        assert warn_uid in vevents4
        warn_v = vevents4[warn_uid]
        assert str(warn_v.get("summary")) == "WARNING: us data is stale (last update 2026-11-10)"
        assert warn_v.decoded("dtstart") == date(2026, 11, 14)

    # Running a second stale day (Day 5) updates the same stable UID cleanly without duplicating
    t5 = datetime(2026, 11, 15, 6, 0, tzinfo=timezone.utc)
    run(
        config_path=cfg,
        output_dir=docs_dir,
        provider=fail_provider,
        today=t5.date(),
        now=t5,
        changes_path=changes_file,
        status_path=status_file,
    )
    cal5 = Calendar.from_ical((docs_dir / "us.ics").read_bytes())
    vevents5 = [v for v in cal5.walk() if v.name == "VEVENT"]
    assert len(vevents5) == 2
    warn_events5 = [v for v in vevents5 if str(v.get("uid")) == f"us{STALE_WARNING_UID_SUFFIX}"]
    assert len(warn_events5) == 1
    assert warn_events5[0].decoded("dtstart") == date(2026, 11, 15)

    # 4. RECOVERED (Day 6: 2026-11-16 06:00 UTC) -> Warning disappears, status healthy again!
    t6 = datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)
    rc6 = run(
        config_path=cfg,
        output_dir=docs_dir,
        provider=ok_provider,
        today=t6.date(),
        now=t6,
        changes_path=changes_file,
        status_path=status_file,
    )
    assert rc6 == 0

    s6 = load_status(status_file)
    assert s6["markets"]["us"]["last_success_at"] == t6.isoformat()
    assert s6["markets"]["us"]["last_success_date"] == "2026-11-16"
    assert s6["markets"]["us"]["age_days"] == 0
    assert s6["markets"]["us"]["stale"] is False
    assert s6["markets"]["us"]["healthy"] is True
    assert s6["markets"]["us"]["errors"] == []
    assert s6["sources"]["api"]["healthy"] is True
    assert s6["sources"]["api"]["stale"] is False

    cal6 = Calendar.from_ical((docs_dir / "us.ics").read_bytes())
    uids6 = [str(v.get("uid")) for v in cal6.walk() if v.name == "VEVENT"]
    assert uids6 == ["us-AAPL-2027Q1-earnings@earnings-cal"]

    # Warning injection/removal never polluted data/changes.jsonl
    recorded_uids = {rec["uid"] for rec in load_change_records(changes_file)}
    assert f"us{STALE_WARNING_UID_SUFFIX}" not in recorded_uids


def test_zero_or_below_minimum_upcoming_events_triggers_stale_and_preserves_good_feed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Suspicious drop below min_upcoming_events fails run, keeps good events, and injects warning."""
    upcoming_state = {"drop_to_zero": False}

    class FlakyDummyMarket(Market):
        id = "dummy"
        display_name = "Dummy Market"
        timezone = "Europe/Ljubljana"
        calendar_name = "Events Dummy"

        def fetch_events(self, window: DateWindow) -> list[Event]:
            if upcoming_state["drop_to_zero"]:
                # Return only a past event -> upcoming_events == 0 (< min_upcoming_events=1)
                return [
                    Event(
                        market=self.id,
                        ticker="OLD",
                        date=date(2026, 11, 1),
                    )
                ]
            return [
                Event(
                    market=self.id,
                    ticker="GOOD",
                    date=date(2026, 11, 20),
                )
            ]

    monkeypatch.setitem(MARKET_REGISTRY, FlakyDummyMarket.id, FlakyDummyMarket)

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
thresholds:
  max_age_days: 3
  min_upcoming_events: 1
us:
  tickers: [AAPL]
ljse:
  tickers: [KRKG]
  events_file: "{ljse_yaml.name}"
dummy:
  min_upcoming_events: 1
""",
        encoding="utf-8",
    )

    docs_dir = tmp_path / "docs"
    status_file = docs_dir / "status.json"
    t0 = datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc)

    # Healthy initial run
    assert (
        run(
            config_path=cfg,
            output_dir=docs_dir,
            today=t0.date(),
            now=t0,
            use_mock=True,
            status_path=status_file,
        )
        == 0
    )

    # Day 1: sudden drop to 0 upcoming events
    upcoming_state["drop_to_zero"] = True
    t1 = datetime(2026, 11, 11, 6, 0, tzinfo=timezone.utc)
    rc1 = run(
        config_path=cfg,
        output_dir=docs_dir,
        today=t1.date(),
        now=t1,
        use_mock=True,
        status_path=status_file,
    )
    assert rc1 == 1

    st = load_status(status_file)
    dummy_st = st["markets"]["dummy"]
    assert dummy_st["last_success_at"] == t0.isoformat()  # preserved!
    assert dummy_st["upcoming_events"] == 0
    assert dummy_st["stale"] is True
    assert dummy_st["healthy"] is False
    assert "below minimum threshold" in dummy_st["errors"][0]

    # Good event from Day 0 is preserved AND warning VEVENT is injected for today
    cal = Calendar.from_ical((docs_dir / "dummy.ics").read_bytes())
    uids = {str(v.get("uid")): str(v.get("summary")) for v in cal.walk() if v.name == "VEVENT"}
    assert "dummy-GOOD-2026-11-20-earnings@earnings-cal" in uids
    assert uids[f"dummy{STALE_WARNING_UID_SUFFIX}"] == "WARNING: dummy data is stale (last update 2026-11-10)"


def test_seonet_source_status_fake_clock(tmp_path: Path) -> None:
    """update_seonet_source_status tracks seonet source freshness, errors, and staleness."""
    status_file = tmp_path / "status.json"
    t0 = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)
    s0 = update_seonet_source_status(status_file, succeeded=True, now=t0, max_age_days=3)
    assert s0["sources"]["seonet"]["healthy"] is True
    assert s0["sources"]["seonet"]["stale"] is False

    t4 = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)
    s4 = update_seonet_source_status(
        status_file,
        succeeded=False,
        errors=["HTTP 503 fetching SEOnet RSS"],
        now=t4,
        max_age_days=3,
    )
    seonet = s4["sources"]["seonet"]
    assert seonet["last_success_at"] == t0.isoformat()
    assert seonet["age_days"] == 4
    assert seonet["stale"] is True
    assert seonet["healthy"] is False
    assert seonet["errors"] == ["HTTP 503 fetching SEOnet RSS"]
