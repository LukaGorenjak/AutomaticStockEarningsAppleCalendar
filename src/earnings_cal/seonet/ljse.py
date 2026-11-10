"""Automatic weekly SEOnet scraper for `tickers-ljse` symbols."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from earnings_cal.models import EarningsEvent, WatchlistConfig, load_config
from earnings_cal.seonet.dates import DateCandidate, extract_candidate_dates
from earnings_cal.seonet.documents import (
    _PlainHTMLTextParser,
    _http_get_with_retry,
    fetch_announcement_content,
)
from earnings_cal.seonet.feed import USER_AGENT
from earnings_cal.seonet.filter import is_financial_calendar_title, normalize_text

logger = logging.getLogger(__name__)

SEONET_HOME_URL = "https://seonet.ljse.si/"
SEONET_SEARCH_URL = "https://seonet.ljse.si/default.aspx"
DEFAULT_LJSE_CACHE_PATH = Path("state/ljse_cache.json")

# Phrases indicating an actual financial results / earnings report release.
_EARNINGS_INCLUDE_KEYWORDS = (
    "porocil",       # letno/polletno/cetrtletno porocilo, porocilo o poslovanju
    "rezultat",      # preliminarni rezultati, nerevidirani rezultati
    "ocen",          # predhodna ocena poslovanja
    "poslovanj",     # informacija o poslovanju
    "izkaz",         # racunovodski izkazi
    "annual report",
    "semi-annual",
    "half-year",
    "quarterly",
    "interim",
    "preliminary results",
    "financial statements",
    "business results",
    "unaudited",
)

# Phrases indicating non-earnings calendar entries (quiet periods, AGMs, dividends, boilerplate).
_EARNINGS_EXCLUDE_KEYWORDS = (
    "tiho obdobje",
    "tihega obdobja",
    "tiha obdobja",
    "quiet period",
    "silent period",
    "skupscin",
    "dividend",
    "presecni dan",
    "upravicenost",
    "nakazil",
    "izplacil",
    "sklic",
    "sklep",
    "izjava o upravljanju",
    "izjava o skladnosti",
    "obvestilo bo objavljeno",
    "announcement will be available",
    "spletni strani druzbe",
    "financnega koledarja",
    "datum:",
    "date:",
)


class _SymbolSelectParser(HTMLParser):
    """Parse `<select name="field.symbol">` options from SEOnet HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_symbol_select = False
        self._current_value: str | None = None
        self._current_text: list[str] = []
        self.symbols: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        tag_l = tag.lower()
        if tag_l == "select" and attr_map.get("name") == "field.symbol":
            self._in_symbol_select = True
        elif self._in_symbol_select and tag_l == "option":
            self._current_value = attr_map.get("value", "").strip()
            self._current_text = []

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()
        if tag_l == "select" and self._in_symbol_select:
            self._in_symbol_select = False
        elif self._in_symbol_select and tag_l == "option" and self._current_value is not None:
            ticker = "".join(self._current_text).strip().upper()
            if ticker and self._current_value and ticker not in self.symbols:
                self.symbols[ticker] = self._current_value
            self._current_value = None
            self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._in_symbol_select and self._current_value is not None:
            self._current_text.append(data)


class _SearchResultsParser(HTMLParser):
    """Parse announcement rows from SEOnet's `table.data_table` search results."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_table = False
        self._in_tbody = False
        self._in_tr = False
        self._in_td = False
        self._current_doc_id: str | None = None
        self._current_cells: list[str] = []
        self._current_cell_parts: list[str] = []
        self.rows: list[tuple[str, str, str, str]] = []  # (doc_id, pub_str, issuer, title)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        tag_l = tag.lower()

        if tag_l == "table" and "data_table" in attr_map.get("class", "").split():
            self._in_table = True
        elif self._in_table and tag_l == "tbody":
            self._in_tbody = True
        elif self._in_tbody and tag_l == "tr":
            self._in_tr = True
            self._current_cells = []
            onclick = attr_map.get("onclick", "")
            m = re.search(r"doc_id=(\d+)", onclick)
            self._current_doc_id = m.group(1) if m else None
        elif self._in_tr and tag_l == "td":
            self._in_td = True
            self._current_cell_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()
        if tag_l == "td" and self._in_td:
            self._in_td = False
            self._current_cells.append(" ".join("".join(self._current_cell_parts).split()))
        elif tag_l == "tr" and self._in_tr:
            self._in_tr = False
            if self._current_doc_id and len(self._current_cells) >= 5:
                pub_str = self._current_cells[1]
                issuer = self._current_cells[2]
                title = self._current_cells[4]
                self.rows.append((self._current_doc_id, pub_str, issuer, title))
            self._current_doc_id = None
        elif tag_l == "tbody" and self._in_tbody:
            self._in_tbody = False
        elif tag_l == "table" and self._in_table:
            self._in_table = False

    def handle_data(self, data: str) -> None:
        if self._in_td:
            self._current_cell_parts.append(data)


def parse_symbol_map(html_text: str) -> dict[str, str]:
    """Extract `{TICKER: seonet_symbol_id}` from SEOnet's search form HTML."""
    parser = _SymbolSelectParser()
    parser.feed(html_text)
    parser.close()
    return parser.symbols


def parse_search_results(html_text: str) -> list[tuple[str, str, str, str]]:
    """Extract `(doc_id, published_str, issuer, title)` rows from SEOnet search HTML."""
    parser = _SearchResultsParser()
    parser.feed(html_text)
    parser.close()
    return parser.rows


def _parse_seonet_row_date(pub_str: str) -> date | None:
    m = re.search(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(20\d{2})", pub_str)
    if not m:
        return None
    d, mo, y = map(int, m.groups())
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def is_earnings_release_candidate(candidate: DateCandidate) -> bool:
    """Return True if a candidate date looks like an earnings/financial report release."""
    context_norm = normalize_text(f"{candidate.label_context} {candidate.raw_line}")
    if any(excl in context_norm for excl in _EARNINGS_EXCLUDE_KEYWORDS):
        return False
    return any(incl in context_norm for incl in _EARNINGS_INCLUDE_KEYWORDS)


def extract_ljse_earnings_events(
    ticker: str,
    announcement_url: str,
    text: str,
    start_date: date,
    end_date: date,
) -> list[EarningsEvent]:
    """Filter extracted candidate dates into `EarningsEvent` objects within `[start_date, end_date]`."""
    candidates = extract_candidate_dates(text)
    events_by_date: dict[date, EarningsEvent] = {}

    for cand in candidates:
        if not (start_date <= cand.date <= end_date):
            continue
        if not is_earnings_release_candidate(cand):
            continue
        note = cand.label_context or cand.raw_line
        if announcement_url:
            note = f"{note} | Source: {announcement_url}"
        events_by_date[cand.date] = EarningsEvent(
            ticker=ticker,
            date=cand.date,
            note=note,
        )

    return sorted(events_by_date.values(), key=lambda e: e.date)


def _search_symbol_calendars(
    session: requests.Session,
    symbol_id: str,
    timeout: float = 20.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> list[tuple[str, str, str, str]]:
    """Search SEOnet for financial calendar announcements for `symbol_id`."""
    headers = {"User-Agent": USER_AGENT}
    for keyword in ("koledar", "calendar"):
        last_exc: Exception | None = None
        resp: requests.Response | None = None
        for attempt in range(2):
            try:
                resp = session.post(
                    SEONET_SEARCH_URL,
                    data={
                        "doc": "LATEST_PUBLIC_ANNOUNCEMENTS",
                        "field.symbol": symbol_id,
                        "field.words": keyword,
                        "field.sort_field": "published",
                        "field.sort_field_direction": "DESC",
                    },
                    headers=headers,
                    timeout=timeout,
                )
                resp.raise_for_status()
                break
            except requests.RequestException as exc:
                last_exc = exc
                resp = None
                if attempt == 0:
                    logger.warning("SEOnet search timed out/failed (%s); retrying once...", exc)
                    sleep_fn(2.0)
        if resp is None:
            raise last_exc or RuntimeError("SEOnet search failed")

        html_text = resp.content.decode(resp.encoding or "utf-8", errors="replace")
        rows = [
            row
            for row in parse_search_results(html_text)
            if is_financial_calendar_title(row[3])
        ]
        if rows:
            return rows
    return []


def _pick_recent_calendar_doc_ids(
    rows: Sequence[tuple[str, str, str, str]],
    today: date,
    max_age_days: int = 450,
    limit: int = 2,
) -> list[str]:
    """Pick the most recent matching announcement IDs (including recent amendments)."""
    cutoff = today - timedelta(days=max_age_days)
    selected: list[str] = []
    for doc_id, pub_str, _issuer, _title in rows:
        pub_date = _parse_seonet_row_date(pub_str)
        if pub_date is not None and pub_date < cutoff:
            continue
        if doc_id not in selected:
            selected.append(doc_id)
        if len(selected) >= limit:
            break
    return selected


def scrape_ljse_tickers(
    tickers: Sequence[str],
    today: date | None = None,
    lookahead_days: int = 365,
    session: requests.Session | None = None,
    timeout: float = 20.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> list[EarningsEvent]:
    """Scrape SEOnet automatically for all tickers in `tickers-ljse`."""
    normalized = [t.strip().upper() for t in tickers if t and t.strip()]
    if not normalized:
        return []

    ref_today = today or date.today()
    end_date = ref_today + timedelta(days=lookahead_days)
    http = session or requests.Session()

    home_resp = _http_get_with_retry(http, SEONET_HOME_URL, timeout=timeout, sleep_fn=sleep_fn)
    home_html = home_resp.content.decode(home_resp.encoding or "utf-8", errors="replace")
    symbol_map = parse_symbol_map(home_html)

    all_events: dict[tuple[str, date], EarningsEvent] = {}

    for idx, ticker in enumerate(normalized):
        if idx > 0:
            sleep_fn(0.5)  # polite pause between sequential issuer requests

        symbol_id = symbol_map.get(ticker)
        if not symbol_id:
            logger.warning("LJSE ticker %s not found in SEOnet symbol list; skipping.", ticker)
            continue

        try:
            rows = _search_symbol_calendars(http, symbol_id, timeout=timeout, sleep_fn=sleep_fn)
            doc_ids = _pick_recent_calendar_doc_ids(rows, today=ref_today)
            if not doc_ids:
                logger.warning("No recent financial calendar announcement found on SEOnet for %s.", ticker)
                continue

            # Process oldest selected first so newer amendment announcements override earlier dates
            for doc_id in reversed(doc_ids):
                doc_url = f"https://seonet.ljse.si/?doc_id={doc_id}"
                content = fetch_announcement_content(doc_url, session=http, timeout=timeout, sleep_fn=sleep_fn)
                combined_text = content.combined_text

                # If announcement has no attachments and no earnings dates in body, try external link
                events = extract_ljse_earnings_events(
                    ticker=ticker,
                    announcement_url=doc_url,
                    text=combined_text,
                    start_date=ref_today,
                    end_date=end_date,
                )
                if not events and not content.attachments and content.external_links:
                    for ext_url in content.external_links[:1]:
                        try:
                            ext_resp = _http_get_with_retry(http, ext_url, timeout=timeout, sleep_fn=sleep_fn)
                            ext_parser = _PlainHTMLTextParser()
                            ext_parser.feed(
                                ext_resp.content.decode(ext_resp.encoding or "utf-8", errors="replace")
                            )
                            ext_parser.close()
                            events = extract_ljse_earnings_events(
                                ticker=ticker,
                                announcement_url=ext_url,
                                text=ext_parser.text,
                                start_date=ref_today,
                                end_date=end_date,
                            )
                        except Exception as exc:
                            logger.warning("Failed external calendar link %s for %s: %s", ext_url, ticker, exc)

                for ev in events:
                    all_events[(ev.ticker, ev.date)] = ev

        except Exception as exc:
            logger.warning("Skipping LJSE ticker %s due to scrape error: %s", ticker, exc)

    return sorted(all_events.values(), key=lambda e: (e.date, e.ticker))


def load_ljse_cache(
    path: Path | str = DEFAULT_LJSE_CACHE_PATH,
    allowed_tickers: Iterable[str] | None = None,
) -> list[EarningsEvent]:
    """Load cached LJSE `EarningsEvent` items, optionally filtered to `allowed_tickers`."""
    cache_path = Path(path)
    if not cache_path.exists():
        return []

    raw = json.loads(cache_path.read_text(encoding="utf-8"))
    raw_events = raw.get("events", []) if isinstance(raw, dict) else []
    allowed_set = {t.strip().upper() for t in allowed_tickers} if allowed_tickers is not None else None

    events: list[EarningsEvent] = []
    for entry in raw_events:
        if not isinstance(entry, dict):
            continue
        ticker = str(entry.get("ticker", "")).strip().upper()
        raw_date = str(entry.get("date", "")).strip()
        if not ticker or not raw_date:
            continue
        if allowed_set is not None and ticker not in allowed_set:
            continue
        events.append(
            EarningsEvent(
                ticker=ticker,
                date=date.fromisoformat(raw_date),
                note=str(entry.get("note", "")).strip() or None,
            )
        )
    return sorted(events, key=lambda e: (e.date, e.ticker))


def save_ljse_cache(
    events: Sequence[EarningsEvent],
    path: Path | str = DEFAULT_LJSE_CACHE_PATH,
) -> bool:
    """Persist scraped LJSE events deterministically. Returns True if changed."""
    cache_path = Path(path)
    sorted_events = sorted(events, key=lambda e: (e.date, e.ticker, e.note or ""))
    payload = {
        "events": [
            {
                "ticker": ev.ticker,
                "date": ev.date.isoformat(),
                "note": ev.note or "",
            }
            for ev in sorted_events
        ]
    }
    rendered = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"

    if cache_path.exists() and cache_path.read_text(encoding="utf-8") == rendered:
        return False

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(rendered, encoding="utf-8")
    return True
