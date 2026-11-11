"""CLI entry point for generating multi-market iCalendar (.ics) feeds, change logs, and status."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from earnings_cal.build import merge_events, write_calendar, write_index_html
from earnings_cal.changes import (
    DEFAULT_CHANGES_PATH,
    CalendarChange,
    append_changes,
    diff_feed,
    get_recent_date_moves,
    render_alert_issue_payload,
    render_change_summary,
    render_commit_message,
)
from earnings_cal.fetch import (
    EarningsProvider,
    FinnhubProvider,
    MockEarningsProvider,
    ProviderError,
)
from earnings_cal.markets.registry import instantiate_markets
from earnings_cal.models import DateWindow, WatchlistConfig, load_config, validate_timezone
from earnings_cal.results import (
    DEFAULT_RESULTS_LOOKBACK_DAYS,
    DEFAULT_RESULTS_PATH,
    EarningsSurpriseProvider,
    PriceProvider,
    update_results,
)
from earnings_cal.seonet.ljse import (
    DEFAULT_LJSE_CACHE_PATH,
    load_ljse_cache,
    save_ljse_cache,
    scrape_ljse_tickers,
)
from earnings_cal.status import (
    DEFAULT_STATUS_PATH,
    build_status_snapshot,
    evaluate_market_status,
    evaluate_source_status,
    inject_stale_warning_ics,
    load_status,
    write_status,
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
    parser.add_argument(
        "--changes-file",
        type=Path,
        default=None,
        help="Path to append-only change log JSONL (default: <output-dir>/../data/changes.jsonl)",
    )
    parser.add_argument(
        "--status-file",
        type=Path,
        default=None,
        help="Path to health status JSON (default: <output-dir>/status.json)",
    )
    parser.add_argument(
        "--commit-msg-file",
        type=Path,
        default=None,
        help="Optional output path to write a formatted git commit message summary",
    )
    parser.add_argument(
        "--alert-file",
        type=Path,
        default=None,
        help="Optional output path to write JSON issue payload for date changes or removals",
    )
    parser.add_argument(
        "--results-file",
        type=Path,
        default=None,
        help="Path to post-earnings results CSV (default: <output-dir>/../data/results.csv)",
    )
    parser.add_argument(
        "--results-lookback-days",
        type=int,
        default=DEFAULT_RESULTS_LOOKBACK_DAYS,
        help="Lookback window in days for recording past earnings results (default: 30)",
    )
    parser.add_argument(
        "--results-correction",
        action="store_true",
        help="Allow overwriting existing rows in data/results.csv",
    )
    return parser.parse_args(argv)


def _resolve_now_utc(now: datetime | None, today: date | None) -> datetime:
    if now is not None:
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)
    if today is not None:
        return datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


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
    changes_path: Path | None = None,
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

    resolved_changes_path = changes_path or (output_path.parent.parent / DEFAULT_CHANGES_PATH)
    changes = diff_feed(
        previous_ics_path=output_path,
        new_events=merged,
        window=DateWindow(start=start_date, end=end_date),
        run_date=start_date,
        market_id="us",
        display_timezone=config.display_timezone,
    )
    if changes:
        append_changes(changes, resolved_changes_path)
    move_notes = get_recent_date_moves(
        resolved_changes_path,
        run_date=start_date,
        events=merged,
        display_timezone=config.display_timezone,
    )

    write_calendar(
        merged,
        output_path,
        display_timezone=config.display_timezone,
        move_notes=move_notes,
    )
    return 0


def _write_workflow_outputs(
    all_changes: Sequence[CalendarChange],
    commit_msg_file: Path | None,
    alert_file: Path | None,
) -> None:
    if commit_msg_file is not None:
        commit_msg_file.parent.mkdir(parents=True, exist_ok=True)
        commit_msg_file.write_text(render_commit_message(all_changes), encoding="utf-8")

    if alert_file is not None:
        payload = render_alert_issue_payload(all_changes)
        if payload is not None:
            alert_file.parent.mkdir(parents=True, exist_ok=True)
            alert_file.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        elif alert_file.exists():
            alert_file.unlink()


def run(
    config_path: Path = Path("tickers.yaml"),
    output_path: Path | None = None,
    days: int = 90,
    provider: EarningsProvider | None = None,
    today: date | None = None,
    now: datetime | None = None,
    use_mock: bool = False,
    scrape_ljse: bool = False,
    ljse_cache_path: Path = DEFAULT_LJSE_CACHE_PATH,
    ljse_days: int = 365,
    output_dir: Path | None = None,
    display_timezone: str | None = None,
    changes_path: Path | None = None,
    status_path: Path | None = None,
    commit_msg_file: Path | None = None,
    alert_file: Path | None = None,
    results_path: Path | None = None,
    results_lookback_days: int = DEFAULT_RESULTS_LOOKBACK_DAYS,
    results_correction: bool = False,
    surprise_provider: EarningsSurpriseProvider | None = None,
    price_provider: PriceProvider | None = None,
) -> int:
    """Execute the multi-market calendar generation, change-reporting, and health-status pipeline."""
    now_utc = _resolve_now_utc(now, today)
    start_date = today or now_utc.date()

    try:
        config = load_config(config_path)
        active_display_tz = validate_timezone(
            display_timezone or config.display_timezone,
            field_name="display_timezone",
        )
    except Exception as exc:
        logger.error("Failed to load config from %s: %s", config_path, exc)
        return 1

    end_date = start_date + timedelta(days=days)
    window = DateWindow(start=start_date, end=end_date)

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
            changes_path=changes_path,
        )

    target_dir = output_dir or (output_path.parent if output_path is not None else Path("docs"))
    target_dir.mkdir(parents=True, exist_ok=True)
    resolved_changes_path = changes_path or (target_dir.parent / DEFAULT_CHANGES_PATH)
    resolved_status_path = status_path or (target_dir / DEFAULT_STATUS_PATH.name)
    resolved_results_path = results_path or (target_dir.parent / DEFAULT_RESULTS_PATH)

    prev_status = load_status(resolved_status_path)
    prev_markets = dict(prev_status.get("markets") or {})
    prev_sources = dict(prev_status.get("sources") or {})
    thresholds = config.thresholds

    # Optional SEOnet scrape when `--scrape-ljse` is requested
    seonet_succeeded: bool | None = None
    seonet_errors: list[str] = []
    if scrape_ljse and config.tickers_ljse:
        try:
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
                seonet_succeeded = True
            else:
                seonet_succeeded = False
                seonet_errors.append("SEOnet scrape returned 0 upcoming LJSE events.")
        except Exception as exc:
            seonet_succeeded = False
            seonet_errors.append(str(exc))

    markets = instantiate_markets(
        config=config,
        provider=provider,
        use_mock=use_mock,
    )

    had_failure = False
    all_changes: list[CalendarChange] = []
    market_status_entries: dict[str, dict] = {}
    api_succeeded: bool | None = None
    api_errors: list[str] = []

    for market in markets:
        feed_path = target_dir / market.feed_filename
        max_age = thresholds.get_market_max_age_days(market.id)
        min_upcoming = thresholds.get_market_min_upcoming_events(market.id)
        prev_market_entry = prev_markets.get(market.id)

        fetched_events = None
        fetch_errors: list[str] = []
        try:
            fetched_events = market.fetch_events(window)
        except Exception as exc:
            fetch_errors.append(str(exc))

        if fetch_errors:
            upcoming_count: int | None = None
        else:
            fetched_events = fetched_events or []
            upcoming_count = sum(
                1
                for ev in fetched_events
                if ev.resolve_display(active_display_tz).local_date >= start_date
            )

        market_entry = evaluate_market_status(
            market_id=market.id,
            prev_entry=prev_market_entry,
            now_utc=now_utc,
            max_age_days=max_age,
            min_upcoming_events=min_upcoming,
            upcoming_events=upcoming_count,
            errors=fetch_errors,
        )
        market_status_entries[market.id] = market_entry

        if market.id == "us":
            if market_entry["errors"]:
                api_succeeded = False
                api_errors = list(market_entry["errors"])
            else:
                api_succeeded = True
                api_errors = []

        if market_entry["errors"]:
            had_failure = True
            logger.error(
                "Market '%s' (%s) unhealthy: %s.",
                market.id,
                market.display_name,
                "; ".join(market_entry["errors"]),
            )
            # Only inject calendar stale warning when stale AND we are not on an uninitialized sentinel
            has_prior_good = (prev_market_entry or {}).get("last_success_at") is not None
            if market_entry["stale"] and (has_prior_good or not feed_path.exists()):
                inject_stale_warning_ics(
                    feed_path=feed_path,
                    market_id=market.id,
                    warning_date=start_date,
                    last_success_date=market_entry.get("last_success_date"),
                    errors=market_entry["errors"],
                    calendar_name=market.calendar_name,
                    display_timezone=active_display_tz,
                )
                for alias_name in getattr(market, "legacy_aliases", ()) or ():
                    inject_stale_warning_ics(
                        feed_path=target_dir / alias_name,
                        market_id=market.id,
                        warning_date=start_date,
                        last_success_date=market_entry.get("last_success_date"),
                        errors=market_entry["errors"],
                        calendar_name=market.calendar_name,
                        display_timezone=active_display_tz,
                    )
            else:
                logger.info("Preserving existing %s untouched.", feed_path)
            continue

        # Healthy run: diff, append changes, write clean calendar (removes any stale warning VEVENT)
        assert fetched_events is not None
        market_window = market.effective_window(window)
        market_changes = diff_feed(
            previous_ics_path=feed_path,
            new_events=fetched_events,
            window=market_window,
            run_date=start_date,
            market_id=market.id,
            display_timezone=active_display_tz,
        )
        all_changes.extend(market_changes)

        if market_changes:
            append_changes(market_changes, resolved_changes_path)

        move_notes = get_recent_date_moves(
            resolved_changes_path,
            run_date=start_date,
            events=fetched_events,
            display_timezone=active_display_tz,
        )

        changed = write_calendar(
            fetched_events,
            feed_path,
            calendar_name=market.calendar_name,
            timezone_name=market.timezone,
            display_timezone=active_display_tz,
            move_notes=move_notes,
        )
        if changed:
            logger.info("Wrote %d event(s) for market '%s' to %s", len(fetched_events), market.id, feed_path)
        else:
            logger.info("No changes for market '%s' in %s (%d event(s))", market.id, feed_path, len(fetched_events))

        for alias_name in getattr(market, "legacy_aliases", ()) or ():
            alias_path = target_dir / alias_name
            write_calendar(
                fetched_events,
                alias_path,
                calendar_name=market.calendar_name,
                timezone_name=market.timezone,
                display_timezone=active_display_tz,
                move_notes=move_notes,
            )

    sources_entries = {
        "api": evaluate_source_status(
            source_name="api",
            prev_entry=prev_sources.get("api"),
            now_utc=now_utc,
            max_age_days=thresholds.get_source_max_age_days("api"),
            succeeded=api_succeeded,
            errors=api_errors,
        ),
        "seonet": evaluate_source_status(
            source_name="seonet",
            prev_entry=prev_sources.get("seonet"),
            now_utc=now_utc,
            max_age_days=thresholds.get_source_max_age_days("seonet"),
            succeeded=seonet_succeeded,
            errors=seonet_errors,
        ),
    }

    status_snapshot = build_status_snapshot(
        prev_status=prev_status,
        markets_entries=market_status_entries,
        sources_entries=sources_entries,
        thresholds=thresholds,
        now_utc=now_utc,
    )
    write_status(status_snapshot, resolved_status_path)

    write_index_html(
        markets,
        target_dir / "index.html",
        display_timezone=active_display_tz,
    )

    if all_changes:
        logger.info("Calendar change summary:\n%s", render_change_summary(all_changes))
    else:
        logger.info("No calendar changes detected across markets.")

    _write_workflow_outputs(
        all_changes=all_changes,
        commit_msg_file=commit_msg_file,
        alert_file=alert_file,
    )

    # Record post-earnings results for past watchlist events (missing data is never an error)
    if provider is None or use_mock or results_path is not None or surprise_provider is not None or price_provider is not None:
        try:
            update_results(
                config=config,
                results_path=resolved_results_path,
                today=start_date,
                now=now_utc,
                lookback_days=results_lookback_days,
                correction=results_correction,
                earnings_provider=provider,
                surprise_provider=surprise_provider,
                price_provider=price_provider,
                use_mock=use_mock,
            )
        except Exception as exc:
            logger.warning("Post-earnings results update skipped: %s", exc)

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
        changes_path=args.changes_file,
        status_path=args.status_file,
        commit_msg_file=args.commit_msg_file,
        alert_file=args.alert_file,
        results_path=args.results_file,
        results_lookback_days=args.results_lookback_days,
        results_correction=args.results_correction,
    )


if __name__ == "__main__":
    sys.exit(main())
