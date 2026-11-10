"""SEOnet RSS feed fetching and parsing."""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import requests

logger = logging.getLogger(__name__)

# Unfiltered SEOnet RSS feed returns both Slovene and English announcements in one request.
SEONET_FEED_URL = "https://seonet.ljse.si/rss/"
USER_AGENT = "earnings-cal-seonet-watcher/0.1 (+https://github.com/; personal watchlist)"
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class FeedError(RuntimeError):
    """Raised when the SEOnet RSS feed is unreachable or has an unexpected structure."""


@dataclass(frozen=True)
class FeedItem:
    """Typed representation of a single SEOnet RSS announcement item."""

    id: str
    issuer: str
    title: str
    published: str
    link: str
    description: str = ""
    instrument: str = ""


def extract_announcement_id(link: str, guid: str = "") -> str:
    """Extract the SEOnet `doc_id` from item link/guid, falling back to guid/link."""
    for candidate in (link, guid):
        cleaned = (candidate or "").strip()
        if not cleaned:
            continue
        parsed = urlparse(cleaned)
        doc_ids = parse_qs(parsed.query).get("doc_id")
        if doc_ids and doc_ids[0].strip():
            return doc_ids[0].strip()
    fallback = (guid or link or "").strip()
    if not fallback:
        raise FeedError("RSS item is missing both <link> and <guid>")
    return fallback


def parse_feed(xml_data: bytes | str) -> list[FeedItem]:
    """Parse SEOnet RSS 2.0 XML into a list of `FeedItem` objects."""
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as exc:
        raise FeedError(f"Malformed SEOnet RSS XML: {exc}") from exc

    if root.tag.lower() != "rss":
        raise FeedError(f"Unexpected RSS root element <{root.tag}>; expected <rss>")

    channel = root.find("channel")
    if channel is None:
        raise FeedError("Missing <channel> element in SEOnet RSS feed")

    items: list[FeedItem] = []
    for raw_item in channel.findall("item"):
        title = (raw_item.findtext("title") or "").strip()
        link = (raw_item.findtext("link") or "").strip()
        guid = (raw_item.findtext("guid") or "").strip()
        published = (raw_item.findtext("pubDate") or "").strip()
        author = (raw_item.findtext("author") or "").strip()
        category = (raw_item.findtext("category") or "").strip()
        description = (raw_item.findtext("description") or "").strip()
        instrument = (raw_item.findtext("instrument") or "").strip()

        if not title or not (link or guid):
            raise FeedError("Malformed SEOnet RSS <item>: missing required <title> or <link>")

        item_url = link or guid
        item_id = extract_announcement_id(link, guid)
        issuer = author or category

        items.append(
            FeedItem(
                id=item_id,
                issuer=issuer,
                title=title,
                published=published,
                link=item_url,
                description=description,
                instrument=instrument,
            )
        )

    return items


def fetch_feed(
    url: str = SEONET_FEED_URL,
    session: requests.Session | None = None,
    timeout: float = 10.0,
    backoff_seconds: float = 2.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> list[FeedItem]:
    """Fetch and parse the SEOnet RSS feed (10 s timeout, at most one retry)."""
    http = session or requests.Session()
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.1",
    }

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = http.get(url, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            last_error = exc
            if attempt == 0:
                logger.warning("SEOnet feed request failed (%s); retrying in %.1fs...", exc, backoff_seconds)
                sleep_fn(backoff_seconds)
                continue
            raise FeedError(f"Failed to fetch SEOnet RSS feed from {url}: {exc}") from exc

        if response.status_code in RETRYABLE_STATUS_CODES:
            last_error = FeedError(f"HTTP {response.status_code} fetching SEOnet RSS feed")
            if attempt == 0:
                logger.warning(
                    "HTTP %d fetching SEOnet RSS feed; retrying in %.1fs...",
                    response.status_code,
                    backoff_seconds,
                )
                sleep_fn(backoff_seconds)
                continue
            raise last_error

        if response.status_code >= 400:
            raise FeedError(f"HTTP {response.status_code} fetching SEOnet RSS feed from {url}")

        return parse_feed(response.content)

    raise FeedError(f"Failed to fetch SEOnet RSS feed from {url}: {last_error}")
