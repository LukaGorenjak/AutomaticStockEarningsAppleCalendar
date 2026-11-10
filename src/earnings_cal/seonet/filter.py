"""Filter SEOnet feed items by watched LJSE issuer and financial calendar title."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from earnings_cal.seonet.feed import FeedItem

# Matches "financial calendar" and Slovene inflections of "finančni koledar"
# (e.g. "finančni koledar", "finančnega koledarja", "finančnemu koledarju").
_CALENDAR_TITLE_RE = re.compile(
    r"\b(?:financial\s+calendar|financn\w*\s+koledar\w*)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MatchedItem:
    """A SEOnet RSS item matching a configured LJSE issuer and financial calendar title."""

    item: FeedItem
    ticker: str
    issuer_name: str


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


def filter_items(
    items: Sequence[FeedItem],
    ljse_issuers: Mapping[str, str],
) -> list[MatchedItem]:
    """Filter `items` to announcements whose issuer is in `ljse_issuers` and title matches."""
    if not ljse_issuers:
        return []

    issuer_to_ticker: dict[str, tuple[str, str]] = {}
    for ticker, issuer_name in ljse_issuers.items():
        cleaned_name = issuer_name.strip()
        issuer_to_ticker[cleaned_name] = (ticker.strip().upper(), cleaned_name)

    matched: list[MatchedItem] = []
    for item in items:
        issuer_key = item.issuer.strip()
        if issuer_key not in issuer_to_ticker:
            continue
        if not is_financial_calendar_title(item.title):
            continue
        ticker, canonical_name = issuer_to_ticker[issuer_key]
        matched.append(MatchedItem(item=item, ticker=ticker, issuer_name=canonical_name))

    return matched
