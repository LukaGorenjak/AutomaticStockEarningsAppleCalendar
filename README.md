# Earnings Watchlist Calendar (`earnings-cal`)

Generates a deterministic iCalendar (`.ics`) feed of upcoming stock earnings dates for a customizable watchlist, refreshed daily by GitHub Actions and served via GitHub Pages so Apple Calendar (macOS & iOS) can subscribe via a `webcal://` URL.

## Features

- **Finnhub Earnings Calendar Integration**: Queries `GET https://finnhub.io/api/v1/calendar/earnings` per ticker over a 90-day lookahead window (`from=YYYY-MM-DD`, `to=YYYY-MM-DD`, `symbol=TICKER`), staying well within Finnhub's free-tier limit of 60 calls/minute.
- **Swappable Provider Interface**: `EarningsProvider` in `src/earnings_cal/fetch.py` cleanly decouples API fetching from calendar generation so another source (e.g., Alpha Vantage) can be swapped in later.
- **Resilient Error Handling**:
  - Retries HTTP `429` rate limits and transient `5xx` errors with exponential backoff (honoring `Retry-After` when present).
  - Warns and skips individual failing tickers without crashing the run.
  - Preserves the existing `docs/earnings.ics` and exits non-zero if the API fails or returns zero events across all watchlist tickers.
- **Apple Calendar Ready**:
  - One all-day `VEVENT` per earnings release (`AAPL earnings (BMO)` / `(AMC)` / `AAPL earnings`).
  - Stable UID per fiscal period (`{TICKER}-{fiscalYear}Q{quarter}@earnings-cal`, falling back to `{TICKER}-{date}@earnings-cal`), so date shifts update the existing calendar entry instead of duplicating.
  - Built-in `VALARM` firing at 09:00 local time the day before (`TRIGGER:-PT15H`).
  - Calendar metadata (`X-WR-CALNAME:Earnings Watchlist`, `REFRESH-INTERVAL;VALUE=DURATION:P1D`, `X-PUBLISHED-TTL:P1D`).
  - Byte-for-byte deterministic output so GitHub Actions only commits when an event actually changes.

---

## Project Structure

```text
.
├── .github/workflows/
│   ├── seonet.yml                 # Daily cron + manual workflow to watch LJSE SEOnet & open issues
│   └── update.yml                 # Daily cron + manual workflow to refresh docs/earnings.ics
├── docs/earnings.ics              # Published iCalendar feed served by GitHub Pages
├── state/seonet_seen.json         # Processed SEOnet announcement IDs (deterministic JSON)
├── src/earnings_cal/
│   ├── __init__.py
│   ├── __main__.py                # Entry point for `python -m earnings_cal`
│   ├── build.py                   # Deterministic iCalendar (.ics) builder & deduplication
│   ├── fetch.py                   # EarningsProvider interface & FinnhubProvider
│   ├── main.py                    # CLI argument parsing & orchestration
│   ├── models.py                  # EarningsEvent model & tickers.yaml loader
│   └── seonet/
│       ├── __init__.py
│       ├── __main__.py            # Entry point for `python -m earnings_cal.seonet`
│       ├── cli.py                 # SEOnet watcher pipeline & CLI
│       ├── dates.py               # Numeric & Slovene/English candidate date extraction
│       ├── documents.py           # Announcement HTML parsing & PDF text extraction
│       ├── feed.py                # SEOnet RSS feed client & parser
│       ├── filter.py              # Issuer & financial calendar title matching
│       ├── report.py              # Markdown report & YAML snippet renderer
│       └── state.py               # Seen announcement ID persistence
├── tests/
│   ├── fixtures/                  # Saved real SEOnet RSS, HTML, and PDF samples
│   ├── test_earnings_cal.py       # Pytest suite for Finnhub/iCalendar generation
│   └── test_seonet.py             # Pytest suite for SEOnet watcher (offline fixtures)
├── pyproject.toml
├── requirements.txt
└── tickers.yaml                   # Watchlist tickers, manual_events, and ljse_issuers
```

---

## Configuration (`tickers.yaml`)

Edit `tickers.yaml` in the repository root to customize your stock watchlist and optional manual events (for non-US listings or dates not covered by the free Finnhub tier):

```yaml
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

ljse_issuers:
  KRKG: "KRKA, d. d., Novo mesto"
  NLBR: "NLB, d. d., Ljubljana"
  TLSG: "TELEKOM SLOVENIJE, d.d., Ljubljana"
  LKPG: "LUKA KOPER, d.d., Koper"
```

---

## Local Setup & Usage

### Prerequisites

- Python **3.11+**

### 1. Create a virtual environment and install dependencies

**macOS / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

**Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .
```

### 2. Run the generator locally

With a real Finnhub API key:

**macOS / Linux:**
```bash
export FINNHUB_API_KEY="your_finnhub_api_key_here"
python -m earnings_cal
```

**Windows (PowerShell):**
```powershell
$env:FINNHUB_API_KEY = "your_finnhub_api_key_here"
python -m earnings_cal
```

To test locally without a live key (using deterministic mocked responses):

```bash
python -m earnings_cal --mock
# or set FINNHUB_API_KEY=mock
```

This writes the generated calendar to `docs/earnings.ics`.

### 3. Run tests

```bash
pytest
```

---

## End-to-End Deployment Guide

### Step 1: Get a Free Finnhub API Key

1. Go to [https://finnhub.io/register](https://finnhub.io/register) and sign up for a free account.
2. Once signed in, copy your **API Key** from the [Finnhub Dashboard](https://finnhub.io/dashboard).

### Step 2: Add the GitHub Repository Secret

1. Push this project to a GitHub repository.
2. In your GitHub repository, navigate to **Settings** -> **Secrets and variables** -> **Actions**.
3. Click **New repository secret**.
4. Set:
   - **Name**: `FINNHUB_API_KEY`
   - **Secret**: Paste your Finnhub API key.
5. Click **Add secret**.

### Step 3: Trigger the Initial Workflow (Optional)

1. Go to the **Actions** tab in your GitHub repository.
2. Select **Update Earnings Calendar** on the left.
3. Click **Run workflow** -> **Run workflow** to verify the secret works and refresh `docs/earnings.ics` immediately.

### Step 4: Enable GitHub Pages (`main` branch, `/docs` folder)

1. In your GitHub repository, navigate to **Settings** -> **Pages**.
2. Under **Build and deployment**:
   - **Source**: Select **Deploy from a branch**.
   - **Branch**: Select **`main`** and folder **`/docs`**.
3. Click **Save**.
4. Wait ~1 minute for GitHub Pages to publish. Your feed will be hosted at:
   ```text
   https://<user>.github.io/<repo>/earnings.ics
   ```
   *(Note: GitHub Pages URLs are public even for private repositories if Pages is enabled on a paid plan, or for any public repository on GitHub Free.)*

---

## Subscribing in Apple Calendar (`webcal://`)

Replace `<user>` with your GitHub username and `<repo>` with your repository name in the subscription URL below:

```text
webcal://<user>.github.io/<repo>/earnings.ics
```

### On Mac (macOS Apple Calendar)

1. Open the **Calendar** app.
2. From the menu bar, choose **File** -> **New Calendar Subscription…** (or press `Option + Command + S`).
3. Enter your `webcal://` URL:
   ```text
   webcal://<user>.github.io/<repo>/earnings.ics
   ```
   and click **Subscribe**.
4. Confirm the settings:
   - **Name**: `Earnings Watchlist` (pre-filled from `X-WR-CALNAME`)
   - **Location**: Select **iCloud** (recommended so the subscription syncs automatically to your iPhone/iPad) or **On My Mac**.
   - **Auto-refresh**: Select **Every day** (pre-filled from `REFRESH-INTERVAL` / `X-PUBLISHED-TTL`).
   - Make sure **Remove: Alerts** is **unchecked** so you receive the 09:00 day-before notifications.
5. Click **OK**.

### On iPhone / iPad (iOS Apple Calendar)

*(If you chose **iCloud** as the location when subscribing on Mac, the calendar will appear automatically on your iPhone. To subscribe directly on iPhone instead:)*

1. Open the **Calendar** app (or go to **Settings** -> **Apps** -> **Calendar** -> **Calendar Accounts** -> **Add Account** -> **Other** -> **Add Subscribed Calendar**).
2. In the **Calendar** app: tap **Calendars** at the bottom of the screen -> tap **Add Calendar** (bottom left) -> tap **Add Subscription Calendar**.
3. Paste your `webcal://` URL:
   ```text
   webcal://<user>.github.io/<repo>/earnings.ics
   ```
4. Tap **Subscribe** (or **Next**), ensure **Alerts** is toggled **On**, and tap **Add**.

---

## SEOnet Watcher (Ljubljana Stock Exchange / LJSE)

The SEOnet watcher monitors Ljubljana Stock Exchange (LJSE) public announcements on [seonet.ljse.si](https://seonet.ljse.si) for newly published financial calendars (`Finančni koledar` / `Financial calendar`) from your watched Slovenian issuers.

**Important:** The watcher is strictly **read-only for your calendar** — it **never** edits `ljse_events.yaml` or `docs/earnings.ics` automatically. Instead, it extracts candidate dates and source lines into a Markdown report for your manual review.

### How to Add an Issuer to `ljse_issuers`

SEOnet identifies companies by their exact registered issuer name (found in the `<author>` / `<category>` field of the SEOnet RSS feed at `https://seonet.ljse.si/rss/`), not by ticker. Add entries under `ljse_issuers` in `tickers.yaml` mapping your ticker to the exact SEOnet issuer name:

```yaml
ljse_issuers:
  KRKG: "KRKA, d. d., Novo mesto"
  NLBR: "NLB, d. d., Ljubljana"
  TLSG: "TELEKOM SLOVENIJE, d.d., Ljubljana"
  LKPG: "LUKA KOPER, d.d., Koper"
```

### Running the SEOnet Watcher Locally

Run against the live SEOnet RSS feed:

```bash
python -m earnings_cal.seonet
```

Or test offline against the saved fixtures (no network requests, does not modify `state/seonet_seen.json`):

```bash
python -m earnings_cal.seonet --feed-file tests/fixtures/rss_en.xml --fixtures-dir tests/fixtures --no-write-state
```

Reports are printed to stdout and saved as `.md` files in `reports/`. Processed announcement IDs are persisted in `state/seonet_seen.json` so announcements are only reported once (failed downloads are left unseen and retried next run).

### Automated Issues & Manual Review Workflow

1. **Where issues show up:** The GitHub Actions workflow `.github/workflows/seonet.yml` runs daily at **07:15 UTC** (and on manual `workflow_dispatch`) using only the built-in `GITHUB_TOKEN` — no extra secrets required. Whenever a watched issuer publishes a new financial calendar, it opens one GitHub Issue in your repository's **Issues** tab titled:
   ```text
   New financial calendar: <TICKER>
   ```
   (Deduplicated by announcement ID across both open and closed issues, so closing an issue will never cause it to reopen.)
2. **Manual review step:**
   - Open the GitHub Issue and inspect the extracted candidate dates table alongside each raw source line (and check any attachment flagged `needs manual reading` or external link if the issuer didn't attach a PDF).
   - Copy the events you want from the issue's **Ready-to-paste YAML** block into `ljse_events.yaml`.
   - Replace `type: TODO` with the appropriate event type (e.g. `annual`, `q1`, `q2`, `q3`, `dividend`, `agm`), remove quiet-period or unwanted dates, and close the issue.
