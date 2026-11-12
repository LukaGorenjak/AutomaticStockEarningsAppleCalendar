"""Tests for Excel portfolio synchronization (`src/earnings_cal/sync_excel.py`) and EUMarket."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import openpyxl
import pytest
import requests
import yaml
from icalendar import Calendar

from earnings_cal.fetch import FinnhubProvider
from earnings_cal.main import run
from earnings_cal.models import load_config
from earnings_cal.sync_excel import (
    ExcelSyncError,
    extract_holdings_from_excel,
    git_push_tickers_yaml,
    load_sync_config,
    main as sync_main,
    sync_portfolio,
)


def _create_portfolio_workbook(
    path: Path,
    ilirika_rows: list[tuple[str, str, float | str | None]],
    ibkr_rows: list[tuple[str, str, float | str | None]],
) -> Path:
    """Create a realistic stacked-table Excel workbook matching `portfolio.xlsx`."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Portfelj"

    ws.append(["PORTFELJ – DELNICE (HOLDINGS) ILIRIKA"])
    ws.append(["Ticker", "Ime delnice", "Količina", "Trenutna cena (€)"])
    for ticker, name, qty in ilirika_rows:
        ws.append([ticker, name, qty, 100.0])
    ws.append(["SKUPAJ", None, None, None])
    ws.append([])
    ws.append(["PORTFELJ – DELNICE (HOLDINGS) IBKR"])
    ws.append(["Ticker", "Ime delnice", "Količina", "Trenutna cena (€)"])
    for ticker, name, qty in ibkr_rows:
        ws.append([ticker, name, qty, 50.0])
    ws.append(["SKUPAJ", None, None, None])

    wb.save(path)
    wb.close()
    return path


def _write_base_tickers_yaml(path: Path) -> Path:
    path.write_text(
        """display_timezone: Europe/Ljubljana
thresholds:
  max_age_days: 3
  min_upcoming_events: 1
  markets:
    us:
      max_age_days: 3
      min_upcoming_events: 1
    ljse:
      max_age_days: 3
      min_upcoming_events: 1
    macro:
      max_age_days: 3
      min_upcoming_events: 1
us:
  tickers:
    - AAPL
    - NVDA
  manual_events:
    - ticker: ASML
      date: "2027-01-27"
      note: "Manual entry preserved"
ljse:
  tickers:
    - KRKG
    - CICG
  events_file: ljse_events.yaml
  issuers:
    KRKG: "KRKA, d. d., Novo mesto"
    CICG: "CINKARNA CELJE, d.d., Celje"
    TLSG: "TELEKOM SLOVENIJE, d.d., Ljubljana"
    LKPG: "LUKA KOPER, d.d., Koper"
    NLBR: "NLB, d. d., Ljubljana"
macro:
  series:
    - ecb
    - fomc
  events_file: data/macro_events.yaml
""",
        encoding="utf-8",
    )
    return path


def _write_sync_config(path: Path, excel_path: Path) -> Path:
    path.write_text(
        f"""excel_path: "{excel_path.as_posix()}"
min_quantity: 0
ticker_column: "Ticker"
name_column: "Ime delnice"
quantity_column: "Količina"
stop_markers: [SKUPAJ, TOTAL]
ignore_tickers: [CASH, VWCE, VUAA, QUTM]
ignore_name_substrings: [ETF, Denar]
routing:
  PETG: {{ market: ljse, ticker: PETG }}
  KRKG: {{ market: ljse, ticker: KRKG }}
  LKPG: {{ market: ljse, ticker: LKPG }}
  CICG: {{ market: ljse, ticker: CICG }}
  ZVTG: {{ market: ljse, ticker: ZVTG }}
  TLSG: {{ market: ljse, ticker: TLSG }}
  NLBR: {{ market: ljse, ticker: NLBR }}
  NOV: {{ market: us, ticker: NVO }}
  RHM: {{ market: eu, ticker: RHM.DE }}
default_market: us
""",
        encoding="utf-8",
    )
    return path


def test_sync_excel_handles_stacked_tables_aliases_etfs_add_and_delete(tmp_path: Path) -> None:
    """Stacked Ilirika + IBKR tables route stocks, filter ETFs/CASH/0-qty, and reflect additions/deletions."""
    xlsx = _create_portfolio_workbook(
        tmp_path / "portfolio.xlsx",
        ilirika_rows=[
            ("PETG", "Petrol", 480),
            ("KRKG", "Krka", 67),
            ("LKPG", "Luka Koper", 238),
            ("CICG", "Cinkarna Celje", 137),
            ("ZVTG", "Zavarovalnica Triglav", 100),
            ("TLSG", "Telekom Slovenije", 30),
            ("VWCE", "Vanguard FTSE All-World", 16),
            ("NLBR", "Nova Ljubljanska Banka", 5),
            ("VUAA", "Vanguard S&P 500", 6),
            ("CASH", "Denar", 1),
        ],
        ibkr_rows=[
            ("QUTM", "Vaneck Quantum Computing ETF", 27),
            ("NOV", "Novo Nordisk", 5),
            ("MSFT", "Microsoft Corp.", "0,5"),
            ("IREN", "Iren LTD", 4),
            ("MCD", "Mc Donald's Corp.", 0.5),
            ("RHM", "Rheinmetall AG", 0.1),
            ("ABAT", "American Battery Technology", 20),
            ("TTWO", "Take Two Interactive", 0.2),
            ("VWCE", "Vanguard FTSE All-World", 0.2),
        ],
    )
    tickers_cfg = _write_base_tickers_yaml(tmp_path / "tickers.yaml")
    sync_cfg_path = _write_sync_config(tmp_path / "portfolio_sync.yaml", xlsx)

    # Dry-run first leaves tickers.yaml untouched
    before_text = tickers_cfg.read_text(encoding="utf-8")
    _, dry_diff, _ = sync_portfolio(
        config_path=tickers_cfg,
        sync_config_path=sync_cfg_path,
        dry_run=True,
    )
    assert dry_diff.changed is True
    assert tickers_cfg.read_text(encoding="utf-8") == before_text

    # Apply live sync
    resolution, diff, pushed = sync_portfolio(
        config_path=tickers_cfg,
        sync_config_path=sync_cfg_path,
        dry_run=False,
    )
    assert pushed is False
    assert diff.changed is True
    assert diff.final_by_market["us"] == ["ABAT", "IREN", "MCD", "MSFT", "NVO", "TTWO"]
    assert diff.final_by_market["ljse"] == ["CICG", "KRKG", "LKPG", "NLBR", "PETG", "TLSG", "ZVTG"]
    assert diff.final_by_market["eu"] == ["RHM.DE"]
    assert "NOV -> us:NVO" in resolution.routed_aliases
    assert "RHM -> eu:RHM.DE" in resolution.routed_aliases

    # Verify AAPL/NVDA removed and non-ticker sections preserved + valid under load_config()
    assert diff.removed_by_market["us"] == ["AAPL", "NVDA"]
    loaded = load_config(tickers_cfg)
    assert loaded.display_timezone == "Europe/Ljubljana"
    assert len(loaded.manual_events) == 1
    assert loaded.manual_events[0].ticker == "ASML"
    assert loaded.thresholds.get_market_min_upcoming_events("eu") == 0
    assert loaded.get_market_section("macro")["series"] == ["ecb", "fomc"]

    # Now simulate user editing Excel: sell ABAT (qty=0), delete RHM, add GOOGL + SAP.DE + custom ETF
    _create_portfolio_workbook(
        xlsx,
        ilirika_rows=[
            ("PETG", "Petrol", 480),
            ("KRKG", "Krka", 67),
        ],
        ibkr_rows=[
            ("NOV", "Novo Nordisk", 5),
            ("MSFT", "Microsoft Corp.", 0.5),
            ("ABAT", "American Battery Technology", 0),
            ("GOOGL", "Alphabet Inc.", 2),
            ("SAP.DE", "SAP SE", 1),
            ("EUNL", "iShares Core MSCI World ETF", 10),
        ],
    )
    _, diff2, _ = sync_portfolio(
        config_path=tickers_cfg,
        sync_config_path=sync_cfg_path,
    )
    assert diff2.final_by_market["us"] == ["GOOGL", "MSFT", "NVO"]
    assert diff2.final_by_market["ljse"] == ["KRKG", "PETG"]
    assert diff2.final_by_market["eu"] == ["SAP.DE"]
    assert "ABAT" in diff2.removed_by_market["us"]
    assert "RHM.DE" in diff2.removed_by_market["eu"]


def test_sync_excel_permission_error_gives_helpful_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Locked Excel workbooks raise a clear `ExcelSyncError` telling user to close Excel."""
    xlsx = tmp_path / "locked.xlsx"
    xlsx.write_bytes(b"placeholder")
    sync_cfg = load_sync_config(tmp_path / "missing.yaml")

    def _raise_perm(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(openpyxl, "load_workbook", _raise_perm)
    with pytest.raises(ExcelSyncError, match="Close Microsoft Excel"):
        extract_holdings_from_excel(xlsx, sync_cfg)


def test_sync_excel_cli_and_git_safety_refuses_unrelated_staged_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`git_push_tickers_yaml` refuses to commit if unrelated staged changes exist; CLI exits 0 cleanly."""
    xlsx = _create_portfolio_workbook(
        tmp_path / "portfolio.xlsx",
        ilirika_rows=[("KRKG", "Krka", 10)],
        ibkr_rows=[("MSFT", "Microsoft", 1)],
    )
    tickers_cfg = _write_base_tickers_yaml(tmp_path / "tickers.yaml")
    sync_cfg_path = _write_sync_config(tmp_path / "portfolio_sync.yaml", xlsx)

    rc = sync_main([
        "--excel",
        str(xlsx),
        "--config",
        str(tickers_cfg),
        "--sync-config",
        str(sync_cfg_path),
    ])
    assert rc == 0
    raw = yaml.safe_load(tickers_cfg.read_text(encoding="utf-8"))
    assert raw["us"]["tickers"] == ["MSFT"]
    assert raw["ljse"]["tickers"] == ["KRKG"]

    class _FakeProc:
        def __init__(self, rc: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = rc
            self.stdout = stdout
            self.stderr = stderr

    def _fake_run_git(args, cwd):
        if args[:2] == ["diff", "--cached"] and "--name-only" in args:
            return _FakeProc(0, stdout="README.md\n")
        return _FakeProc(0)

    monkeypatch.setattr("earnings_cal.sync_excel._run_git", _fake_run_git)
    with pytest.raises(ExcelSyncError, match="unrelated files are already staged"):
        git_push_tickers_yaml(tickers_cfg, repo_root=tmp_path)

    # Verify clean push order: diff -> add -> commit -> pull --rebase --autostash -> push
    calls: list[list[str]] = []

    def _clean_run_git(args, cwd):
        calls.append(list(args))
        if args[:2] == ["diff", "--quiet"]:
            return _FakeProc(1)
        return _FakeProc(0)

    monkeypatch.setattr("earnings_cal.sync_excel._run_git", _clean_run_git)
    assert git_push_tickers_yaml(tickers_cfg, repo_root=tmp_path) is True
    assert ["pull", "--rebase", "--autostash"] in calls
    assert calls.index(["pull", "--rebase", "--autostash"]) < calls.index(["push"])


def test_eu_market_handles_finnhub_empty_and_403_gracefully_but_fails_on_500(
    tmp_path: Path,
) -> None:
    """`EUMarket` emits `docs/eu.ics` cleanly on 0 events/403 tier gaps when min_upcoming_events=0, and fails on HTTP 500."""
    ljse_yaml = tmp_path / "ljse_events.yaml"
    ljse_yaml.write_text(
        """ljse_events:
  - ticker: KRKG
    date: "2026-11-12"
    kind: earnings
    source_url: "https://seonet.ljse.si/?doc_id=92042"
""",
        encoding="utf-8",
    )
    cfg = tmp_path / "tickers.yaml"
    cfg.write_text(
        f"""thresholds:
  markets:
    eu:
      max_age_days: 3
      min_upcoming_events: 0
us:
  tickers: []
  manual_events:
    - ticker: MSFT
      date: "2026-11-25"
ljse:
  tickers: [KRKG]
  events_file: "{ljse_yaml.name}"
eu:
  tickers: [RHM.DE]
  manual_events:
    - ticker: RHM.DE
      date: "2026-11-18"
      note: "Q3 results"
""",
        encoding="utf-8",
    )

    docs_dir = tmp_path / "docs"

    # 1. Mock 403 tier gap from Finnhub on RHM.DE -> preserves manual EU event cleanly with rc=0
    resp_403 = MagicMock(spec=requests.Response)
    resp_403.status_code = 403
    resp_403.headers = {}
    resp_403.text = "You don't have access to this resource."
    session_403 = MagicMock(spec=requests.Session)
    session_403.get.return_value = resp_403
    provider_403 = FinnhubProvider(api_key="test-key", session=session_403, max_retries=0)

    rc_403 = run(
        config_path=cfg,
        output_dir=docs_dir,
        provider=provider_403,
        today=date(2026, 11, 10),
    )
    assert rc_403 == 0
    assert (docs_dir / "eu.ics").exists()
    eu_cal = Calendar.from_ical((docs_dir / "eu.ics").read_bytes())
    assert str(eu_cal.get("X-WR-CALNAME")) == "Events EU"
    eu_vevents = [c for c in eu_cal.walk() if c.name == "VEVENT"]
    assert len(eu_vevents) == 1
    assert str(eu_vevents[0].get("uid")) == "eu-RHM.DE-2026-11-18-earnings@earnings-cal"

    # 2. HTTP 500 server outage still fails EU market in isolation
    resp_500 = MagicMock(spec=requests.Response)
    resp_500.status_code = 500
    resp_500.headers = {}
    resp_500.text = "Internal Error"
    session_500 = MagicMock(spec=requests.Session)
    session_500.get.return_value = resp_500
    provider_500 = FinnhubProvider(api_key="test-key", session=session_500, max_retries=0)

    rc_500 = run(
        config_path=cfg,
        output_dir=docs_dir,
        provider=provider_500,
        today=date(2026, 11, 10),
    )
    assert rc_500 == 1
