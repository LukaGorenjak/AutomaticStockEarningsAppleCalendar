# Multi-Market Earnings & Financial Events Calendar (`earnings-cal`)

Generates deterministic iCalendar (`.ics`) feeds of upcoming stock earnings and corporate events per market, refreshed daily by GitHub Actions and served via GitHub Pages so Apple Calendar (macOS & iOS) can subscribe via `webcal://` URLs.

## Published Feeds

| Market | Feed File | `X-WR-CALNAME` | IANA Timezone | Subscription URL (`webcal://`) |
| --- | --- | --- | --- | --- |
| **United States (`us`)** | `docs/us.ics` | `Earnings US` | `America/New_York` | `webcal://<user>.github.io/<repo>/us.ics` |
| **Ljubljana Stock Exchange (`ljse`)** | `docs/ljse.ics` | `Events LJSE` | `Europe/Ljubljana` | `webcal://<user>.github.io/<repo>/ljse.ics` |
| **Legacy US Alias** | `docs/earnings.ics` | `Earnings US` | `America/New_York` | `webcal://<user>.github.io/<repo>/earnings.ics` |

> **Migration notice (`docs/earnings.ics` -> `docs/us.ics`):**
> `docs/earnings.ics` is preserved as an identical alias of `docs/us.ics` so existing Apple Calendar subscriptions continue working without interruption. Subscribers should migrate their subscription URL to `webcal://<user>.github.io/<repo>/us.ics` (and optionally subscribe to `webcal://<user>.github.io/<repo>/ljse.ics` for Ljubljana Stock Exchange events). A live directory of all registered feeds is also published at `https://<user>.github.io/<repo>/` (`docs/index.html`).

---

## Features

- **Modular Multi-Market Design (`src/earnings_cal/markets/`)**:
  - Each market implements `Market` (`id`, `display_name`, `timezone`, `calendar_name`, `fetch_events(window) -> list[Event]`) in its own module and is registered in `src/earnings_cal/markets/registry.py`.
  - One `.ics` feed per market (`docs/us.ics`, `docs/ljse.ics`) plus an auto-generated `docs/index.html` listing every registered feed and its `webcal://` link.
- **Extended `Event` & `EventKind` Model**:
  - Supports `EventKind` (`earnings`, `dividend`, `ex_dividend`, `agm`, `macro`, `other`), `market`, `ticker`, `title`, `date`, optional `time`, `description`, and `source_url`.
  - Market-scoped deterministic UIDs: `{market}-{ticker}-{date or fiscal period}-{kind}@earnings-cal` (e.g. `us-AAPL-2027Q1-earnings@earnings-cal`, `ljse-KRKG-2026-11-12-earnings@earnings-cal`).
- **Run-to-Run Change Detection & Alerts (`data/changes.jsonl`)**:
  - Before overwriting any `.ics` feed, compares the previous committed feed against new events by UID (`added`, `removed`, `date_changed`, `details_changed`).
  - Events that simply scrolled behind `today` or past the lookahead horizon are ignored (only future events inside the active fetch window are reported as `removed`).
  - Appends deterministic records to `data/changes.jsonl`, writes human-readable summaries into the workflow commit message, opens one deduplicated GitHub issue per run when any `date_changed` or `removed` occurs, and appends `"Date changed from X on <run date>"` to moved event descriptions for 14 days without extra `VALARM`s.
- **Failure Isolation Across Markets**:
  - One failing market never blocks the others: its error is logged, its existing `.ics` file in `docs/` is preserved untouched, all healthy markets are updated, and the CLI exits non-zero at the end.
- **Apple Calendar Ready & Local Timezone Conversion (`zoneinfo`)**:
  - Configurable `display_timezone` in `tickers.yaml` (default: `Europe/Ljubljana`), converted dynamically via Python `zoneinfo.ZoneInfo` so US/EU DST mismatch weeks (March and late October / early November) and cross-midnight date shifts are handled accurately with no hardcoded UTC offsets.
  - **Exact-time events** (`timing: exact` with `time`, e.g. `08:15` or a confirmed call time) emit timed `VEVENT`s (`DTSTART;TZID=...` + `DTEND` with duration) and a 30-minute `VALARM` reminder (`TRIGGER:-PT30M`).
  - **Approximate Finnhub timings** (`bmo` ~ before `09:30 ET`, `amc` ~ after `16:00 ET`, `dmh` ~ `09:30–16:00 ET`) and `unknown` timings remain all-day `VEVENT`s (`TRIGGER:-PT15H`), with the converted local approximate time clearly labelled in the title suffix and description (never presented as a confirmed time).

---

## Project Structure

```text
.
├── .github/workflows/
│   ├── ljse.yml                   # Weekly cron + push workflow for LJSE SEOnet cache
│   ├── seonet.yml                 # Daily SEOnet RSS watcher opening review issues
│   └── update.yml                 # Daily cron + manual workflow building all registered markets
├── docs/
│   ├── index.html                 # Auto-generated directory of all market feeds & webcal:// links
│   ├── us.ics                     # US equities earnings feed (X-WR-CALNAME: Earnings US)
│   ├── ljse.ics                   # LJSE corporate events feed (X-WR-CALNAME: Events LJSE)
│   └── earnings.ics               # Legacy alias of docs/us.ics
├── data/
│   └── changes.jsonl              # Append-only deterministic log of detected feed changes
├── src/earnings_cal/
│   ├── __init__.py
│   ├── __main__.py                # Entry point for `python -m earnings_cal`
│   ├── build.py                   # Deterministic iCalendar (.ics) & docs/index.html builder
│   ├── fetch.py                   # EarningsProvider interface & FinnhubProvider
│   ├── main.py                    # Multi-market CLI orchestration & failure isolation
│   ├── models.py                  # Event, EventKind, DateWindow & YAML config loader
│   ├── markets/
│   │   ├── __init__.py
│   │   ├── base.py                # Market abstract base class
│   │   ├── registry.py            # Central MARKET_REGISTRY
│   │   ├── us.py                  # USMarket (Finnhub + manual_events)
│   │   └── ljse.py                # LJSEMarket (ljse_events.yaml)
│   └── seonet/                    # SEOnet RSS watcher & PDF candidate date extractor
├── tests/
│   ├── fixtures/                  # Saved real SEOnet RSS, HTML, and PDF samples
│   ├── test_changes.py            # Run-to-run diffing, window-edge handling & idempotency tests
│   ├── test_earnings_cal.py       # Finnhub & iCalendar unit/integration tests
│   ├── test_markets.py            # Multi-market registry, dummy market, UID & isolation tests
│   ├── test_seonet.py             # SEOnet watcher & scraper tests
│   └── test_timezone.py           # ZoneInfo conversions, DST mismatch weeks & timed vs all-day tests
├── ljse_events.yaml               # Curated LJSE events (earnings, AGM, ex_dividend, dividend)
├── tickers.yaml                   # Per-market watchlists and configuration sections
├── pyproject.toml
└── requirements.txt
```

---

## Configuration (`tickers.yaml` & `ljse_events.yaml`)

Set your local `display_timezone` (any valid IANA timezone, default `Europe/Ljubljana`) and define each market's watchlist in its own section in `tickers.yaml`:

```yaml
display_timezone: Europe/Ljubljana

us:
  tickers:
    - AAPL
    - MSFT
    - NVDA
    - GOOGL
    - AMZN
    - META
    - TSLA
  manual_events:
    - ticker: ASML
      date: "2027-01-27"
      note: "Q4 2026 earnings release (manual entry)"
    - ticker: TSM
      date: "2027-01-14"
      note: "Q4 2026 earnings release"

ljse:
  tickers:
    - KRKG
    - CICG
    - TLSG
    - LKPG
  events_file: ljse_events.yaml
  issuers:
    KRKG: "KRKA, d. d., Novo mesto"
    CICG: "CINKARNA CELJE, d.d., Celje"
    TLSG: "TELEKOM SLOVENIJE, d.d., Ljubljana"
    LKPG: "LUKA KOPER, d.d., Koper"
    NLBR: "NLB, d. d., Ljubljana"
```

LJSE corporate events live in `ljse_events.yaml` and support:
- `earnings`
- `dividend_decision` (requires `ticker`, `date`, `source_url`, `amount_per_share`)
- `ex_dividend` (requires `ticker`, `date`, `source_url`)
- `record_date` (requires `ticker`, `date`, `source_url`)
- `dividend_payment` (requires `ticker`, `date`, `source_url`; optional `amount_per_share` shown in the calendar description when stated)
- `agm` (requires `ticker`, `date`, `source_url`; optional `time` for timed VEVENTs)

When both `ex_dividend` and `record_date` come from an announcement, validation enforces `ex_dividend < record_date` (never derived from one another unless explicitly flagged with `computed: true` and `computed_note`). The SEOnet watcher (`python -m earnings_cal.seonet`) monitors both **financial calendar** announcements and **dividend / AGM** announcements (`dividend`, `izplačilo dividend`, `presečni dan`, `skupščina`, `ex-dividend`, `annual general meeting`) and outputs manual-review Markdown reports with `type: TODO` snippets without auto-writing to `ljse_events.yaml` or `.ics`.

---

## How to Add a Market

Adding a new market requires **one module, one registry entry, one config section, and its tests** — with **no edits to core code** (`models.py`, `build.py`, `main.py`, or `.github/workflows/update.yml`):

1. **Create a market module** in `src/earnings_cal/markets/<market_id>.py` subclassing `Market`:
   ```python
   from earnings_cal.markets.base import Market
   from earnings_cal.models import DateWindow, Event, EventKind

   class XetraMarket(Market):
       id = "xetra"
       display_name = "Deutsche Börse Xetra"
       timezone = "Europe/Berlin"
       calendar_name = "Events Xetra"

       def fetch_events(self, window: DateWindow) -> list[Event]:
           tickers = [str(t).upper() for t in self.config.get("tickers", [])]
           # Fetch and return list[Event] with market=self.id within [window.start, window.end]
           return [...]
   ```
2. **Register the market** in `src/earnings_cal/markets/registry.py`:
   ```python
   MARKET_REGISTRY: dict[str, type[Market] | Market] = {
       USMarket.id: USMarket,
       LJSEMarket.id: LJSEMarket,
       XetraMarket.id: XetraMarket,
   }
   ```
3. **Add its config section** in `tickers.yaml`:
   ```yaml
   xetra:
     tickers:
       - SAP
       - SIE
   ```
4. **Add tests** in `tests/test_markets.py` and run `pytest`. Running `python -m earnings_cal` will automatically generate `docs/xetra.ics` and list it with its `webcal://` link in `docs/index.html`.

---

## Local Usage & Testing

```bash
# Build all registered markets locally using deterministic mock Finnhub data
python -m earnings_cal --mock

# Build with a live Finnhub API key
export FINNHUB_API_KEY="your_finnhub_api_key_here"
python -m earnings_cal

# Run the full test suite
pytest -v
```
