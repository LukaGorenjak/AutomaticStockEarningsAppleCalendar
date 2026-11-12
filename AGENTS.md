# Project Context & Agent Guidelines (`earnings-cal`)

This file preserves the living architecture description and maintenance rules for `earnings-cal` across Antigravity sessions.

## Project Summary

**Multi-Market Earnings & Financial Events Calendar (`earnings-cal`)** generates deterministic iCalendar (`.ics`) feeds of upcoming stock earnings, corporate actions, and macroeconomic events per market, refreshed daily by GitHub Actions and published via GitHub Pages for Apple Calendar (`webcal://`) and a zero-dependency static dashboard (`docs/index.html`).

- **Markets (`src/earnings_cal/markets/` & `registry.py`):**
  - `us` (`USMarket`, `docs/us.ics` + legacy alias `docs/earnings.ics`, `America/New_York`, Finnhub + `manual_events`)
  - `ljse` (`LJSEMarket`, `docs/ljse.ics`, `Europe/Ljubljana`, curated `ljse_events.yaml` + weekly `seonet` scrape cache + RSS watcher `src/earnings_cal/seonet/`)
  - `macro` (`MacroMarket`, `docs/macro.ics`, `Europe/Ljubljana`, `data/macro_events.yaml` for `ecb`, `fomc`, `us_cpi`, `ea_hicp` + `--verify-online`)
  - `eu` (`EUMarket`, `docs/eu.ics`, `Europe/Berlin`, best-effort Finnhub + `manual_events`, `optional_when_unconfigured = True`)
- **Core Pipeline (`src/earnings_cal/`):**
  - `models.py`: `Event`, `EventKind`, `EventTiming`, `DateWindow`, `ZoneInfo` local conversion, strict `load_config()` schema validation for `tickers.yaml`.
  - `build.py` & `dashboard.py`: deterministic `.ics` serialization and self-contained dark-mode `docs/index.html` with embedded JSON (`<script id="dashboard-data">`).
  - `status.py`: `docs/status.json` health tracking and in-calendar `WARNING: {market} data is stale` `VEVENT` injection.
  - `changes.py`: run-to-run `.ics` diffing (`data/changes.jsonl`), 14-day move notes, commit summaries, and alert issue payloads.
  - `results.py`: post-earnings EPS surprise + 1-day price reaction log (`data/results.csv`, Yahoo Finance v8 chart + LJSE REST price list).
  - `sync_excel.py`: local Windows Excel portfolio sync (`portfolio_sync.yaml`, `scripts/sync_portfolio.bat`) mapping stacked tables from `portfolio.xlsx` into `tickers.yaml` (`us.tickers`, `ljse.tickers`, `eu.tickers`).
- **Workflows (`.github/workflows/`):**
  - `update.yml`: daily cron + push to `tickers.yaml` / `ljse_events.yaml` rebuilding feeds, status, changes, results, dashboard, and alert/stale issues.
  - `ljse.yml`: weekly SEOnet scrape updating `state/ljse_cache.json` and rebuilding feeds.

## Mandatory Maintenance Rules

1. **Keep Documentation in Sync:** Whenever new features, markets, CLI flags, configuration files, or workflows are added, modified, or removed, **update `README.md` (and this `AGENTS.md` if architecture changes)** in the same change so documentation always matches the actual codebase on disk.
2. **Portfolio Privacy:** Never commit `portfolio.xlsx`, share quantities, cost basis, or EUR portfolio values to Git. Only normalized ticker symbols belong in `tickers.yaml`.
3. **Config Schema Awareness:** `load_config()` in `src/earnings_cal/models.py` rejects unknown top-level keys in `tickers.yaml`; keep Excel sync rules in `portfolio_sync.yaml` (or update `load_config()` explicitly if changing `tickers.yaml` schema).
4. **Verify Before Reporting:** Run `.venv\Scripts\pytest.exe -v` after edits.
