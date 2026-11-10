"""CLI entry point for generating the earnings iCalendar feed."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

from earnings_cal.build import merge_events, write_calendar
from earnings_cal.fetch import (
    EarningsProvider,
    FinnhubProvider,
    MockEarningsProvider,
    ProviderError,
)
from earnings_cal.models import load_config

logger = logging.getLogger(__name__)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="earnings_cal",
        description="Generate an iCalendar (.ics) feed of upcoming stock earnings dates.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("tickers.yaml"),
        help="Path to watchlist YAML file (default: tickers.yaml)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/earnings.ics"),
        help="Output path for the generated .ics file (default: docs/earnings.ics)",
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
    return parser.parse_args(argv)


def run(
    config_path: Path = Path("tickers.yaml"),
    output_path: Path = Path("docs/earnings.ics"),
    days: int = 90,
    provider: EarningsProvider | None = None,
    today: date | None = None,
    use_mock: bool = False,
) -> int:
    """Execute the earnings calendar generation pipeline.

    Returns 0 on success, or non-zero if configuration or API fetching fails.
    Never overwrites `output_path` on error.
    """
    try:
        config = load_config(config_path)
    except Exception as exc:
        logger.error("Failed to load config from %s: %s", config_path, exc)
        return 1

    if not config.tickers and not config.manual_events:
        logger.error("No tickers or manual_events found in %s", config_path)
        return 1

    start_date = today or date.today()
    end_date = start_date + timedelta(days=days)

    active_provider = provider
    if active_provider is None:
        env_key = os.environ.get("FINNHUB_API_KEY", "").strip()
        if use_mock or env_key.lower() == "mock":
            logger.info("Using MockEarningsProvider for earnings calendar generation.")
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
    except ProviderError as exc:
        logger.error("Aborting update to preserve existing .ics: %s", exc)
        return 1
    except Exception as exc:
        logger.error("Unexpected provider error: %s", exc)
        return 1

    merged = merge_events(api_events, config.manual_events)
    if not merged:
        logger.error("No events to write; preserving existing .ics file.")
        return 1

    changed = write_calendar(merged, output_path)
    if changed:
        logger.info("Wrote %d event(s) to %s", len(merged), output_path)
    else:
        logger.info("No changes detected in %s (%d event(s))", output_path, len(merged))

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    args = parse_args(argv)
    return run(
        config_path=args.config,
        output_path=args.output,
        days=args.days,
        use_mock=args.mock,
    )


if __name__ == "__main__":
    sys.exit(main())
