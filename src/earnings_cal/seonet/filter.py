"""Filter SEOnet feed items by watched LJSE issuer and calendar/dividend/AGM title."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from earnings_cal.seonet.feed import FeedItem

# Keyword set 1: Financial calendars in Slovene and English
# Matches "financial calendar" and inflections of "finančni koledar".
_CALENDAR_TITLE_RE = re.compile(
    r"\b(?:financial\s+calendar|financn\w*\s+koledar\w*)\b",
    re.IGNORECASE,
)

# Keyword set 2: Dividend and AGM corporate event announcements in Slovene and English
# Covers: "dividend*", "izplačilo dividend", "dan presečne liste", "presečni dan",
# "brez upravičenja do dividende", "skupščina" (and inflections),
# "ex-dividend", "record date", "dividend payment", "annual general meeting", "general meeting", "agm".
_DIVIDEND_AGM_TITLE_RE = re.compile(
    r"\b(?:"
    r"dividend\w*"
    r"|izplacil\w*\s+dividend\w*"
    r"|presecn\w*\s+(?:list\w*|dan|dnev\w*)"
    r"|dan\s+presecn\w*\s+list\w*"
    r"|brez\s+upravic\w*\s+do\s+dividend\w*"
    r"|skupscin\w*"
    r"|ex[\s-]+dividend"
    r"|record\s+date"
    r"|dividend\s+payment"
    r"|annual\s+general\s+meeting"
    r"|general\s+meeting(?:\s+of\s+shareholders)?"
    r"|agm"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MatchedItem:
    """A SEOnet RSS item matching a configured LJSE issuer and watcher title filter."""

    item: FeedItem
    ticker: str
    issuer_name: str
    category: str = "financial_calendar"


def normalize_text(text: str) -> str:
    """Lowercase text, strip diacritics (č/š/ž -> c/s/z), and collapse whitespace."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.lower().split())


def is_financial_calendar_title(title: str) -> bool:
    """Return True if `title` refers to a financial calendar in Slovene or English."""
    if not title or not title.strip():
        return False
    normalized = normalize_text(title)
    return bool(_CALENDAR_TITLE_RE.search(normalized))


def is_dividend_or_agm_title(title: str) -> bool:
    """Return True if `title` refers to dividends, record/ex-dates, or AGMs in Slovene or English."""
    if not title or not title.strip():
        return False
    normalized = normalize_text(title)
    return bool(_DIVIDEND_AGM_TITLE_RE.search(normalized))


def classify_announcement_title(title: str) -> str | None:
    """Classify a SEOnet announcement title into `'financial_calendar'`, `'dividend_agm'`, or `None`."""
    if is_financial_calendar_title(title):
        return "financial_calendar"
    if is_dividend_or_agm_title(title):
        return "dividend_agm"
    return None


def filter_items(
    items: Sequence[FeedItem],
    ljse_issuers: Mapping[str, str],
) -> list[MatchedItem]:
    """Filter `items` to announcements whose issuer/instrument is watched and title matches."""
    if not ljse_issuers:
        return []

    issuer_to_ticker: dict[str, tuple[str, str]] = {}
    ticker_to_issuer: dict[str, str] = {}
    for ticker, issuer_name in ljse_issuers.items():
        clean_ticker = ticker.strip().upper()
        cleaned_name = issuer_name.strip()
        issuer_to_ticker[cleaned_name] = (clean_ticker, cleaned_name)
        ticker_to_issuer[clean_ticker] = cleaned_name

    matched: list[MatchedItem] = []
    for item in items:
        category = classify_announcement_title(item.title)
        if category is None:
            continue

        issuer_key = item.issuer.strip()
        if issuer_key in issuer_to_ticker:
            ticker, canonical_name = issuer_to_ticker[issuer_key]
            matched.append(
                MatchedItem(
                    item=item,
                    ticker=ticker,
                    issuer_name=canonical_name,
                    category=category,
                )
            )
            continue

        # Also match official Ljubljana Stock Exchange (Ljubljanska borza) dividend notices
        # when `<instrument>` carries a watched ticker (e.g. ex-dividend notices for NLBR).
        inst = item.instrument.strip().upper()
        if (
            category == "dividend_agm"
            and inst in ticker_to_issuer
            and "ljubljanska borza" in normalize_text(issuer_key)
        ):
            matched.append(
                MatchedItem(
                    item=item,
                    ticker=inst,
                    issuer_name=ticker_to_issuer[inst],
                    category=category,
                )
            )

    return matched
