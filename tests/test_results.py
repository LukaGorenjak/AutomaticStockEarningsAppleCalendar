"""Tests for post-earnings results recording (`data/results.csv`), mocked providers, and CLI summary."""

from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from earnings_cal.main import run
from earnings_cal.models import Event, EventKind
from earnings_cal.results import (
    DailyClose,
    EarningsResultRow,
    EarningsSurpriseRecord,
    FinnhubSurpriseProvider,
    MockPriceProvider,
    MockSurpriseProvider,
    YahooAndLJSEPriceProvider,
    compute_1d_price_move_pct,
    load_results_csv,
    main as results_cli_main,
    render_results_summary,
    update_results,
)


def _write_test_config(tmp_path: Path) -> Path:
    ljse_yaml = tmp_path / "ljse_events.yaml"
    ljse_yaml.write_text(
        """
ljse_events:
  - ticker: CICG
    date: "2026-11-11"
    kind: earnings
    title: "CICG Q1-Q3 2026 Interim Report"
    note: "Unaudited 9M 2026 report"
    source_url: "https://seonet.ljse.si/?doc_id=91972"
  - ticker: KRKG
    date: "2026-11-20"
    kind: earnings
    time: "08:15"
    title: "KRKG Q1-Q3 2026 Interim Report"
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
    - MSFT
ljse:
  tickers:
    - CICG
    - KRKG
  events_file: "{ljse_yaml.name}"
""",
        encoding="utf-8",
    )
    return cfg


def test_idempotency_and_deterministic_csv_ordering(tmp_path: Path) -> None:
    """Running `update_results` twice with identical data produces identical CSV and preserves `recorded_at`."""
    cfg = _write_test_config(tmp_path)
    results_csv = tmp_path / "data" / "results.csv"

    past_events = [
        Event(
            market="us",
            ticker="MSFT",
            date=date(2026, 10, 28),
            hour="amc",
            quarter=1,
            fiscal_year=2027,
            eps_estimate=3.10,
            eps_actual=3.30,
        ),
        Event(
            market="us",
            ticker="AAPL",
            date=date(2026, 10, 29),
            hour="amc",
            quarter=4,
            fiscal_year=2026,
            eps_estimate=1.60,
            eps_actual=1.64,
        ),
        Event(
            market="ljse",
            ticker="CICG",
            date=date(2026, 11, 11),
            kind=EventKind.EARNINGS,
            title="CICG Q1-Q3 2026 Interim Report",
            note="Unaudited 9M 2026 report",
        ),
    ]

    price_provider = MockPriceProvider(
        {
            "AAPL": [("2026-10-29", 230.00), ("2026-10-30", 233.45)],  # +1.50%
            "MSFT": [("2026-10-28", 425.00), ("2026-10-29", 431.80)],  # +1.60%
            "LJSE:CICG": [("2026-11-10", 32.50), ("2026-11-11", 33.15)],  # +2.00%
        }
    )

    t1 = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)
    rows1 = update_results(
        config=cfg,
        results_path=results_csv,
        today=t1.date(),
        now=t1,
        surprise_provider=MockSurpriseProvider({}),
        price_provider=price_provider,
        past_events=past_events,
    )
    assert len(rows1) == 3
    assert [(r.market, r.ticker, r.fiscal_period) for r in rows1] == [
        ("us", "MSFT", "2027Q1"),
        ("us", "AAPL", "2026Q4"),
        ("ljse", "CICG", "2026Q1-Q3"),
    ]

    # Verify LJSE leaves eps_estimate / eps_actual / surprise_pct empty and stores manual_notes
    raw_dicts = list(csv.DictReader(results_csv.read_text(encoding="utf-8").splitlines()))
    cicg_dict = next(d for d in raw_dicts if d["ticker"] == "CICG")
    assert cicg_dict["eps_estimate"] == ""
    assert cicg_dict["eps_actual"] == ""
    assert cicg_dict["surprise_pct"] == ""
    assert cicg_dict["price_move_1d_pct"] == "2.00"
    assert cicg_dict["manual_notes"] == "Unaudited 9M 2026 report"

    first_csv_text = results_csv.read_text(encoding="utf-8")

    # Second run at a later timestamp -> idempotent! Does not overwrite recorded_at or duplicate rows
    t2 = datetime(2026, 11, 13, 6, 0, tzinfo=timezone.utc)
    rows2 = update_results(
        config=cfg,
        results_path=results_csv,
        today=t2.date(),
        now=t2,
        surprise_provider=MockSurpriseProvider({}),
        price_provider=price_provider,
        past_events=past_events,
    )
    assert len(rows2) == 3
    assert results_csv.read_text(encoding="utf-8") == first_csv_text


def test_partial_data_and_late_data_arriving_without_overwriting_existing_fields(
    tmp_path: Path,
) -> None:
    """Missing data is not an error; late data fills in empty fields on subsequent runs without overwriting existing values."""
    cfg = _write_test_config(tmp_path)
    results_csv = tmp_path / "data" / "results.csv"

    aapl_event = Event(
        market="us",
        ticker="AAPL",
        date=date(2026, 10, 29),
        hour="amc",
        quarter=4,
        fiscal_year=2026,
        eps_estimate=1.60,
        eps_actual=None,  # Not reported yet on Run 1!
    )

    # Run 1 (Morning after AMC report): eps_estimate=1.60 known, eps_actual missing, next-day close missing
    t1 = datetime(2026, 10, 30, 6, 0, tzinfo=timezone.utc)
    rows1 = update_results(
        config=cfg,
        results_path=results_csv,
        today=t1.date(),
        now=t1,
        surprise_provider=MockSurpriseProvider({"AAPL": []}),
        price_provider=MockPriceProvider({"AAPL": [("2026-10-29", 230.00)]}),  # only report-day close so far
        past_events=[aapl_event],
    )
    assert len(rows1) == 1
    r1 = rows1[0]
    assert r1.eps_estimate == 1.60
    assert r1.eps_actual is None
    assert r1.surprise_pct is None
    assert r1.price_move_1d_pct is None
    assert r1.recorded_at == "2026-10-30T06:00:00Z"

    # Run 2 (Next day): eps_actual (1.64) and Oct 30 closing price (233.45 -> +1.50%) arrive!
    # Also suppose the provider now reports a slightly different estimate (1.62) — existing 1.60 MUST NOT be overwritten!
    late_surprise_provider = MockSurpriseProvider(
        {
            "AAPL": [
                EarningsSurpriseRecord(
                    ticker="AAPL",
                    fiscal_period="2026Q4",
                    report_date=date(2026, 10, 29),
                    eps_estimate=1.62,
                    eps_actual=1.64,
                )
            ]
        }
    )
    late_price_provider = MockPriceProvider(
        {"AAPL": [("2026-10-29", 230.00), ("2026-10-30", 233.45)]}
    )
    t2 = datetime(2026, 10, 31, 6, 0, tzinfo=timezone.utc)
    rows2 = update_results(
        config=cfg,
        results_path=results_csv,
        today=t2.date(),
        now=t2,
        surprise_provider=late_surprise_provider,
        price_provider=late_price_provider,
        past_events=[aapl_event],
    )
    assert len(rows2) == 1
    r2 = rows2[0]
    assert r2.eps_estimate == 1.60  # Preserved original non-empty estimate!
    assert r2.eps_actual == 1.64  # Filled in late actual!
    assert r2.surprise_pct == 2.50  # ((1.64 - 1.60) / 1.60) * 100 = +2.50%
    assert r2.price_move_1d_pct == 1.50  # ((233.45 - 230.00) / 230.00) * 100 = +1.50%
    assert r2.recorded_at == "2026-10-31T06:00:00Z"


def test_duplicate_prevention_and_correction_flag(tmp_path: Path) -> None:
    """Key `(market, ticker, fiscal_period)` prevents duplicates; existing values only change when `correction=True`."""
    cfg = _write_test_config(tmp_path)
    results_csv = tmp_path / "data" / "results.csv"

    ev_initial = Event(
        market="us",
        ticker="AAPL",
        date=date(2026, 10, 29),
        hour="amc",
        quarter=4,
        fiscal_year=2026,
        eps_estimate=1.60,
        eps_actual=1.64,
    )
    p_initial = MockPriceProvider({"AAPL": [("2026-10-29", 230.00), ("2026-10-30", 233.45)]})
    t1 = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)
    update_results(
        config=cfg,
        results_path=results_csv,
        today=t1.date(),
        now=t1,
        surprise_provider=MockSurpriseProvider({}),
        price_provider=p_initial,
        past_events=[ev_initial],
    )

    # Attempt to record a restatement (eps_actual=1.70, price_move=+3.00%) WITHOUT correction flag
    ev_restated = Event(
        market="us",
        ticker="AAPL",
        date=date(2026, 10, 29),
        hour="amc",
        quarter=4,
        fiscal_year=2026,
        eps_estimate=1.60,
        eps_actual=1.70,
    )
    p_restated = MockPriceProvider({"AAPL": [("2026-10-29", 230.00), ("2026-10-30", 236.90)]})
    t2 = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
    rows_no_corr = update_results(
        config=cfg,
        results_path=results_csv,
        today=t2.date(),
        now=t2,
        correction=False,
        surprise_provider=MockSurpriseProvider({}),
        price_provider=p_restated,
        past_events=[ev_restated],
    )
    assert len(rows_no_corr) == 1
    assert rows_no_corr[0].eps_actual == 1.64  # NOT overwritten!
    assert rows_no_corr[0].price_move_1d_pct == 1.50

    # Now pass `correction=True` -> updates existing row in place without creating a duplicate
    rows_corr = update_results(
        config=cfg,
        results_path=results_csv,
        today=t2.date(),
        now=t2,
        correction=True,
        surprise_provider=MockSurpriseProvider({}),
        price_provider=p_restated,
        past_events=[ev_restated],
    )
    assert len(rows_corr) == 1
    assert rows_corr[0].eps_actual == 1.70
    assert rows_corr[0].surprise_pct == 6.25
    assert rows_corr[0].price_move_1d_pct == 3.00


def test_bmo_vs_amc_1d_price_move_calculation() -> None:
    """BMO compares `T-1 -> T` closes while AMC compares `T -> T+1` closes."""
    closes = [
        DailyClose(date=date(2026, 10, 28), close=100.0),
        DailyClose(date=date(2026, 10, 29), close=105.0),
        DailyClose(date=date(2026, 10, 30), close=115.5),
    ]
    bmo_ev = Event(market="us", ticker="TSM", date=date(2026, 10, 29), hour="bmo")
    amc_ev = Event(market="us", ticker="AAPL", date=date(2026, 10, 29), hour="amc")

    # BMO on Oct 29 reacts during Oct 29 session: 100.0 -> 105.0 (+5.00%)
    assert compute_1d_price_move_pct(bmo_ev, closes) == 5.00
    # AMC on Oct 29 reacts during Oct 30 session: 105.0 -> 115.5 (+10.00%)
    assert compute_1d_price_move_pct(amc_ev, closes) == 10.00


def test_finnhub_surprise_and_yahoo_ljse_price_providers_with_mocked_http() -> None:
    """Test `FinnhubSurpriseProvider` and `YahooAndLJSEPriceProvider` HTTP parsing."""
    session = MagicMock(spec=requests.Session)

    def _get(url: str, **kwargs: object) -> MagicMock:
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        if "calendar/earnings" in url:
            resp.json.return_value = {
                "earningsCalendar": [
                    {
                        "symbol": "AAPL",
                        "date": "2026-10-29",
                        "quarter": 4,
                        "year": 2026,
                        "epsEstimate": 1.60,
                        "epsActual": 1.64,
                        "revenueEstimate": 94000000000,
                        "revenueActual": 94930000000,
                    }
                ]
            }
        elif "stock/earnings" in url:
            resp.json.return_value = [
                {
                    "symbol": "AAPL",
                    "period": "2026-09-30",
                    "quarter": 4,
                    "year": 2026,
                    "estimate": 1.60,
                    "actual": 1.64,
                    "surprisePercent": 2.50,
                }
            ]
        elif "finance.yahoo.com" in url:
            resp.json.return_value = {
                "chart": {
                    "result": [
                        {
                            "timestamp": [1793251200, 1793337600],
                            "indicators": {"quote": [{"close": [230.0, 234.6]}]},
                        }
                    ]
                }
            }
        elif "rest.ljse.si" in url:
            resp.json.return_value = {
                "securities": [
                    {
                        "symbol": "CICG",
                        "trade_date": "2026-11-11",
                        "close_price": "33.1500",
                    }
                ]
            }
        return resp

    session.get.side_effect = _get

    sp = FinnhubSurpriseProvider(api_key="test-key", session=session)
    surprises = sp.fetch_surprises("AAPL", date(2026, 10, 25), date(2026, 11, 2))
    assert len(surprises) == 2
    assert surprises[0].eps_actual == 1.64
    assert surprises[0].surprise_pct == 2.50

    pp = YahooAndLJSEPriceProvider(session=session)
    us_closes = pp.fetch_daily_closes("us", "AAPL", date(2026, 10, 25), date(2026, 11, 5))
    assert len(us_closes) == 2
    ljse_closes = pp.fetch_daily_closes("ljse", "CICG", date(2026, 11, 11), date(2026, 11, 11))
    assert len(ljse_closes) == 1
    assert ljse_closes[0].close == 33.15


def test_results_summary_cli_and_main_integration(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`python -m earnings_cal.results summary` prints per-ticker average surprise and 1D move."""
    cfg = _write_test_config(tmp_path)
    docs_dir = tmp_path / "docs"
    results_csv = tmp_path / "data" / "results.csv"

    # Run full `earnings_cal.main.run` in mock mode on 2026-11-12
    rc = run(
        config_path=cfg,
        output_dir=docs_dir,
        today=date(2026, 11, 12),
        use_mock=True,
        results_path=results_csv,
    )
    assert rc == 0
    assert results_csv.exists()

    loaded = load_results_csv(results_csv)
    assert len(loaded) == 3  # AAPL, MSFT, CICG

    rc_cli = results_cli_main(["summary", "--results-file", str(results_csv)])
    assert rc_cli == 0
    out = capsys.readouterr().out
    assert "AAPL" in out
    assert "+2.50%" in out
    assert "+1.50%" in out
    assert "MSFT" in out
    assert "+6.45%" in out
    assert "+1.60%" in out
    assert "CICG" in out
    assert "N/A" in out
    assert "+2.00%" in out
    assert render_results_summary([]) == "No earnings results recorded in data/results.csv."
