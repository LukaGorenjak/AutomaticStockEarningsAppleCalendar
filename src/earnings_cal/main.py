"""CLI entry point for generating multi-market iCalendar (.ics) feeds."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

from earnings_cal.build import merge_events, write_calendar, write_index_html
from earnings_cal.fetch import (
    EarningsProvider,
    FinnhubProvider,
    MockEarningsProvider,
    ProviderError,
)
from earnings_cal.markets.registry import instantiate_markets
from earnings_cal.models import DateWindow, WatchlistConfig, load_config, validate_timezone
from earnings_cal.seonet.ljse import (
    DEFAULT_LJSE_CACHE_PATH,
    load_ljse_cache,
    save_ljse_cache,
    scrape_ljse_tickers,
)

logger = logging.getLogger(__name__)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="earnings_cal",
        description="Generate iCalendar (.ics) feeds for all registered markets.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("tickers.yaml"),
        help="Path to watchlist YAML file (default: tickers.yaml)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("docs"),
        help="Output directory for per-market .ics feeds and index.html (default: docs)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional single-file output path for legacy compatibility",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="Lookahead window in days from today (default: 90)",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use deterministic mocked API responses instead of calling Finnhub",
    )
    parser.add_argument(
        "--scrape-ljse",
        action="store_true",
        help="Scrape SEOnet for LJSE tickers and update state/ljse_cache.json",
    )
    parser.add_argument(
        "--ljse-cache",
        type=Path,
        default=DEFAULT_LJSE_CACHE_PATH,
        help="Path to cached LJSE events JSON (default: state/ljse_cache.json)",
    )
    parser.add_argument(
        "--ljse-days",
        type=int,
        default=365,
        help="Lookahead window in days for LJSE events (default: 365)",
    )
    parser.add_argument(
        "--display-timezone",
        type=str,
        default=None,
        help="Override display_timezone from config (default: Europe/Ljubljana)",
    )
    return parser.parse_args(argv)


def _run_legacy_single_feed(
    config: WatchlistConfig,
    output_path: Path,
    start_date: date,
    end_date: date,
    provider: EarningsProvider | None,
    use_mock: bool,
    scrape_ljse: bool,
    ljse_cache_path: Path,
    ljse_days: int,
) -> int:
    """Legacy single-feed pipeline for backward compatibility with single-file callers."""
    if not config.tickers and not config.tickers_ljse and not config.manual_events:
        logger.error("No tickers, tickers-ljse, or manual_events found in configuration")
        return 1

    api_events = []
    if config.tickers:
        active_provider = provider
        if active_provider is None:
            env_key = os.environ.get("FINNHUB_API_KEY", "").strip()
            if use_mock or env_key.lower() == "mock":
                active_provider = MockEarningsProvider()
            else:
                try:
                    active_provider = FinnhubProvider(api_key=env_key)
                except ProviderError as exc:
                    logger.error("%s", exc)
                    return 1

        try:
            api_events = active_provider.fetch_earnings(
                config.tickers,
                start_date=start_date,
                end_date=end_date,
            )
        except Exception as exc:
            logger.error("Aborting update to preserve existing .ics: %s", exc)
            return 1

    if scrape_ljse and config.tickers_ljse:
        scraped = scrape_ljse_tickers(
            config.tickers_ljse,
            today=start_date,
            lookahead_days=ljse_days,
        )
        if scraped:
            scraped_tickers = {ev.ticker for ev in scraped}
            existing_cached = load_ljse_cache(ljse_cache_path, allowed_tickers=config.tickers_ljse)
            preserved = [ev for ev in existing_cached if ev.ticker not in scraped_tickers]
            save_ljse_cache(preserved + scraped, ljse_cache_path)

    ljse_events = load_ljse_cache(ljse_cache_path, allowed_tickers=config.tickers_ljse)
    merged = merge_events(api_events, ljse_events, config.manual_events)
    if not merged:
        logger.error("No events to write; preserving existing .ics file.")
        return 1

    write_calendar(merged, output_path, display_timezone=config.display_timezone)
    return 0


def run(
    config_path: Path = Path("tickers.yaml"),
    output_path: Path | None = None,
    days: int = 90,
    provider: EarningsProvider | None = None,
    today: date | None = None,
    use_mock: bool = False,
    scrape_ljse: bool = False,
    ljse_cache_path: Path = DEFAULT_LJSE_CACHE_PATH,
    ljse_days: int = 365,
    output_dir: Path | None = None,
    display_timezone: str | None = None,
) -> int:
    """Execute the multi-market calendar generation pipeline.

    Builds one `.ics` feed per registered market in `output_dir` (default `docs/`),
    plus `docs/earnings.ics` as an alias of `docs/us.ics`, and `docs/index.html`.
    One failing market never blocks the others: its error is logged, its existing
    `.ics` file is preserved, and `run()` exits non-zero at the end.
    """
    try:
        config = load_config(config_path)
        active_display_tz = validate_timezone(
            display_timezone or config.display_timezone,
            field_name="display_timezone",
        )
    except Exception as exc:
        logger.error("Failed to load config from %s: %s", config_path, exc)
        return 1

    start_date = today or date.today()
    end_date = start_date + timedelta(days=days)
    window = DateWindow(start=start_date, end=end_date)

    # Preserve backward compatibility when legacy-only config + explicit output_path are passed
    if config.is_legacy_only and output_path is not None and output_dir is None:
        return _run_legacy_single_feed(
            config=config,
            output_path=output_path,
            start_date=start_date,
            end_date=end_date,
            provider=provider,
            use_mock=use_mock,
            scrape_ljse=scrape_ljse,
            ljse_cache_path=ljse_cache_path,
            ljse_days=ljse_days,
        )

    target_dir = output_dir or (output_path.parent if output_path is not None else Path("docs"))
    target_dir.mkdir(parents=True, exist_ok=True)

    markets = instantiate_markets(
        config=config,
        provider=provider,
        use_mock=use_mock,
    )

    had_failure = False

    for market in markets:
        feed_path = target_dir / market.feed_filename
        try:
            events = market.fetch_events(window)
            if not events:
                raise ProviderError(f"Market '{market.id}' returned zero events.")
        except Exception as exc:
            logger.error(
                "Market '%s' (%s) failed: %s. Keeping existing %s untouched.",
                market.id,
                market.display_name,
                exc,
                feed_path,
            )
            had_failure = True
            continue

        changed = write_calendar(
            events,
            feed_path,
            calendar_name=market.calendar_name,
            timezone_name=market.timezone,
            display_timezone=active_display_tz,
        )
        if changed:
            logger.info("Wrote %d event(s) for market '%s' to %s", len(events), market.id, feed_path)
        else:
            logger.info("No changes for market '%s' in %s (%d event(s))", market.id, feed_path, len(events))

        # Write any legacy alias feeds (e.g. docs/earnings.ics as an alias of docs/us.ics)
        for alias_name in getattr(market, "legacy_aliases", ()) or ():
            alias_path = target_dir / alias_name
            write_calendar(
                events,
                alias_path,
                calendar_name=market.calendar_name,
                timezone_name=market.timezone,
                display_timezone=active_display_tz,
            )

    write_index_html(
        markets,
        target_dir / "index.html",
        display_timezone=active_display_tz,
    )

    return 1 if had_failure else 0


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    args = parse_args(argv)
    return run(
        config_path=args.config,
        output_path=args.output,
        output_dir=args.output_dir if args.output is None else None,
        days=args.days,
        use_mock=args.mock,
        scrape_ljse=args.scrape_ljse,
        ljse_cache_path=args.ljse_cache,
        ljse_days=args.ljse_days,
        display_timezone=args.display_timezone,
    )


if __name__ == "__main__":
    sys.exit(main())
