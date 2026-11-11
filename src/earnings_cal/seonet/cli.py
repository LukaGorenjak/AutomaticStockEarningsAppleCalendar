"""CLI and pipeline orchestration for the SEOnet financial calendar watcher."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from earnings_cal.models import load_config
from earnings_cal.seonet.dates import extract_candidate_dates
from earnings_cal.seonet.documents import (
    AnnouncementContent,
    extract_from_announcement_html,
    fetch_announcement_content,
)
from earnings_cal.seonet.feed import SEONET_FEED_URL, FeedError, FeedItem, fetch_feed, parse_feed
from earnings_cal.seonet.filter import filter_items
from earnings_cal.seonet.report import render_announcement_report
from earnings_cal.seonet.state import DEFAULT_STATE_PATH, StateError, load_seen, save_seen
from earnings_cal.status import update_seonet_source_status

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("tickers.yaml")
DEFAULT_REPORTS_DIR = Path("reports")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="earnings_cal.seonet",
        description="Watch SEOnet RSS for LJSE financial calendar announcements (manual review only).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to tickers.yaml (default: tickers.yaml)",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help="Path to seonet_seen.json state file (default: state/seonet_seen.json)",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=DEFAULT_REPORTS_DIR,
        help="Directory to write generated Markdown reports (default: reports/)",
    )
    parser.add_argument(
        "--feed-url",
        default=SEONET_FEED_URL,
        help=f"SEOnet RSS feed URL (default: {SEONET_FEED_URL})",
    )
    parser.add_argument(
        "--feed-file",
        type=Path,
        default=None,
        help="Optional local RSS XML file to read instead of fetching over HTTP",
    )
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=None,
        help="Optional directory containing offline doc_<id>.html and att_<AttachmentID>.* fixtures",
    )
    parser.add_argument(
        "--no-write-state",
        action="store_true",
        help="Do not update state/seonet_seen.json (useful for dry-run fixture checks)",
    )
    parser.add_argument(
        "--status-file",
        type=Path,
        default=None,
        help="Optional status.json file to update the 'seonet' source health entry",
    )
    return parser.parse_args(argv)


def _load_fixture_document(announcement_url: str, fixtures_dir: Path) -> AnnouncementContent:
    """Load an announcement HTML page and its attachments from a local fixtures directory."""
    parsed = urlparse(announcement_url)
    doc_ids = parse_qs(parsed.query).get("doc_id")
    if not doc_ids:
        raise FileNotFoundError(f"Cannot resolve fixture doc_id from URL: {announcement_url}")
    doc_id = doc_ids[0].strip()
    html_path = fixtures_dir / f"doc_{doc_id}.html"
    if not html_path.exists():
        raise FileNotFoundError(f"Missing fixture announcement HTML: {html_path}")

    html_text = html_path.read_text(encoding="utf-8", errors="replace")

    # Map any attachment URLs referenced in the HTML to matching att_<AttachmentID>.* files
    local_map: dict[str, Path] = {}
    for att_file in fixtures_dir.glob("att_*"):
        if att_file.suffix.lower() in {".headers", ".tmp"}:
            continue
        stem = att_file.stem  # e.g. att_56105
        att_id = stem.removeprefix("att_")
        if att_id:
            local_map[f"https://seonet.ljse.si/file.aspx?AttachmentID={att_id}"] = att_file

    return extract_from_announcement_html(
        html_text=html_text,
        announcement_url=announcement_url,
        local_attachment_map=local_map,
    )


def run_seonet(
    config_path: Path = DEFAULT_CONFIG_PATH,
    state_path: Path = DEFAULT_STATE_PATH,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    feed_url: str = SEONET_FEED_URL,
    feed_file: Path | None = None,
    fixtures_dir: Path | None = None,
    write_state: bool = True,
    feed_fetcher: Callable[[], list[FeedItem]] | None = None,
    content_fetcher: Callable[[str], AnnouncementContent] | None = None,
    status_path: Path | None = None,
) -> int:
    """Run the SEOnet watcher pipeline. Returns 0 on success, non-zero on error."""
    try:
        config = load_config(config_path)
    except Exception as exc:
        logger.error("Failed to load config %s: %s", config_path, exc)
        return 1

    seonet_max_age = config.thresholds.get_source_max_age_days("seonet")

    if not config.ljse_issuers:
        logger.info("No 'ljse_issuers' configured in %s; nothing to watch.", config_path)
        return 0

    try:
        seen_ids = load_seen(state_path)
    except StateError as exc:
        logger.error("%s", exc)
        return 1

    try:
        if feed_fetcher is not None:
            feed_items = feed_fetcher()
        elif feed_file is not None:
            feed_items = parse_feed(feed_file.read_bytes())
        else:
            feed_items = fetch_feed(url=feed_url)
    except (FeedError, OSError) as exc:
        logger.error("SEOnet feed error (state preserved): %s", exc)
        if status_path is not None:
            update_seonet_source_status(
                status_path=status_path,
                succeeded=False,
                errors=[str(exc)],
                max_age_days=seonet_max_age,
            )
        return 1

    matched_items = filter_items(feed_items, config.ljse_issuers)
    new_matches = [m for m in matched_items if m.item.id not in seen_ids]

    if not new_matches:
        logger.info("No new SEOnet financial calendar announcements.")
        if status_path is not None:
            update_seonet_source_status(
                status_path=status_path,
                succeeded=True,
                max_age_days=seonet_max_age,
            )
        return 0

    if content_fetcher is not None:
        fetch_doc = content_fetcher
    elif fixtures_dir is not None:
        fetch_doc = lambda url: _load_fixture_document(url, fixtures_dir)  # noqa: E731
    else:
        fetch_doc = fetch_announcement_content

    rendered_reports: list[str] = []
    newly_seen: set[str] = set()
    failures: list[str] = []

    for match in new_matches:
        item = match.item
        try:
            content = fetch_doc(item.link)
            candidates = extract_candidate_dates(content.combined_text)
            report_md = render_announcement_report(match, content, candidates)

            reports_dir.mkdir(parents=True, exist_ok=True)
            report_path = reports_dir / f"{match.ticker}_{item.id}.md"
            report_path.write_text(report_md, encoding="utf-8")

            rendered_reports.append(report_md)
            newly_seen.add(item.id)
            logger.info("Generated report %s (%d candidate date(s))", report_path, len(candidates))
        except Exception as exc:
            msg = f"Failed processing announcement {item.id} ({match.ticker} - {item.title}): {exc}"
            logger.error("%s", msg)
            failures.append(msg)

    if write_state and newly_seen:
        updated_seen = seen_ids | newly_seen
        save_seen(updated_seen, state_path)

    if rendered_reports:
        sys.stdout.write("\n---\n\n".join(rendered_reports))

    if failures:
        if status_path is not None:
            update_seonet_source_status(
                status_path=status_path,
                succeeded=False,
                errors=failures,
                max_age_days=seonet_max_age,
            )
        return 1

    if status_path is not None:
        update_seonet_source_status(
            status_path=status_path,
            succeeded=True,
            max_age_days=seonet_max_age,
        )

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    args = parse_args(argv)
    return run_seonet(
        config_path=args.config,
        state_path=args.state,
        reports_dir=args.reports_dir,
        feed_url=args.feed_url,
        feed_file=args.feed_file,
        fixtures_dir=args.fixtures_dir,
        write_state=not args.no_write_state,
        status_path=args.status_file,
    )
