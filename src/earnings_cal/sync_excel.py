"""Synchronize active holdings from a local Excel portfolio workbook into `tickers.yaml`."""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import unicodedata
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import openpyxl
import yaml

from earnings_cal.models import load_config

logger = logging.getLogger(__name__)

DEFAULT_EXCEL_PATH = Path(r"C:\Users\lukag\OneDrive\Namizje\Podatki\Finance\portfolio.xlsx")
DEFAULT_SYNC_CONFIG_PATH = Path("portfolio_sync.yaml")
DEFAULT_TICKERS_CONFIG_PATH = Path("tickers.yaml")
SUPPORTED_SYNC_MARKETS: tuple[str, ...] = ("us", "ljse", "eu")

DEFAULT_SUFFIX_MARKETS: dict[str, tuple[str, bool]] = {
    ".LJ": ("ljse", True),
    ".DE": ("eu", False),
    ".PA": ("eu", False),
    ".AS": ("eu", False),
    ".MI": ("eu", False),
    ".CO": ("eu", False),
    ".SW": ("eu", False),
    ".HE": ("eu", False),
    ".ST": ("eu", False),
    ".OL": ("eu", False),
    ".VI": ("eu", False),
    ".PR": ("eu", False),
    ".WA": ("eu", False),
    ".L": ("eu", False),
}


class ExcelSyncError(RuntimeError):
    """Raised when reading the portfolio workbook, sync config, or updating Git fails."""


@dataclass(frozen=True)
class TickerRoute:
    """Target market and normalized feed symbol for an Excel ticker."""

    market: str
    ticker: str


@dataclass(frozen=True)
class ExcelSyncConfig:
    """Configuration controlling how `portfolio.xlsx` maps to `tickers.yaml`."""

    excel_path: Path = DEFAULT_EXCEL_PATH
    sheet_name: str | None = None
    min_quantity: float = 0.0
    ticker_column: str = "Ticker"
    name_column: str = "Ime delnice"
    quantity_column: str = "Količina"
    stop_markers: tuple[str, ...] = ("SKUPAJ", "TOTAL")
    ignore_tickers: frozenset[str] = frozenset({"CASH"})
    ignore_name_substrings: tuple[str, ...] = ("etf", "denar")
    ignore_section_substrings: tuple[str, ...] = (
        "kripto",
        "crypto",
        "binance",
        "dividende",
        "nabave",
    )
    routing: dict[str, TickerRoute] = field(default_factory=dict)
    suffix_markets: dict[str, tuple[str, bool]] = field(
        default_factory=lambda: dict(DEFAULT_SUFFIX_MARKETS)
    )
    default_market: str = "us"
    extra_tickers: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class ExcelHolding:
    """Single parsed row from a portfolio table inside an Excel workbook."""

    ticker: str
    name: str
    quantity: float
    sheet_name: str
    section_title: str
    row_number: int


@dataclass(frozen=True)
class SyncResolution:
    """Resolved per-market ticker lists plus diagnostic skip/warning details."""

    tickers_by_market: dict[str, list[str]]
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    routed_aliases: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SyncDiff:
    """Before/after per-market ticker diff applied to `tickers.yaml`."""

    changed: bool
    added_by_market: dict[str, list[str]]
    removed_by_market: dict[str, list[str]]
    final_by_market: dict[str, list[str]]


def _norm_label(text: Any) -> str:
    """Lowercase, strip accents, and collapse spaces for resilient column/header matching."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    decomposed = unicodedata.normalize("NFKD", raw)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.lower().split())


def _parse_quantity(value: Any) -> float:
    """Parse numeric holding quantity from Excel cells, treating blank/non-numeric as 0.0."""
    if value is None or isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().replace("\u00a0", "").replace(" ", "")
    if not text:
        return 0.0

    # Handle European decimal comma when no dot or when comma follows dot thousands separators
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")

    try:
        return float(text)
    except ValueError:
        return 0.0


def load_sync_config(path: Path | str = DEFAULT_SYNC_CONFIG_PATH) -> ExcelSyncConfig:
    """Load `portfolio_sync.yaml`, or return defaults if the file does not exist."""
    cfg_path = Path(path)
    if not cfg_path.exists():
        return ExcelSyncConfig()

    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise ExcelSyncError(f"Failed to parse {cfg_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ExcelSyncError(f"Expected YAML mapping in {cfg_path}, got {type(raw).__name__}")

    raw_excel = raw.get("excel_path")
    excel_path = Path(str(raw_excel).strip()) if raw_excel else DEFAULT_EXCEL_PATH

    raw_sheet = raw.get("sheet_name")
    sheet_name = str(raw_sheet).strip() if raw_sheet not in (None, "") else None

    min_quantity = float(raw.get("min_quantity", 0.0))
    ticker_col = str(raw.get("ticker_column") or "Ticker").strip()
    name_col = str(raw.get("name_column") or "Ime delnice").strip()
    qty_col = str(raw.get("quantity_column") or "Količina").strip()

    stop_markers = tuple(
        str(x).strip().upper()
        for x in (raw.get("stop_markers") or ["SKUPAJ", "TOTAL"])
        if str(x).strip()
    )
    ignore_tickers = frozenset(
        str(x).strip().upper() for x in (raw.get("ignore_tickers") or ["CASH"]) if str(x).strip()
    )
    ignore_name_substrings = tuple(
        _norm_label(x)
        for x in (raw.get("ignore_name_substrings") or ["ETF", "Denar"])
        if _norm_label(x)
    )
    ignore_section_substrings = tuple(
        _norm_label(x)
        for x in (
            raw.get("ignore_sections")
            or ["KRIPTO", "CRYPTO", "BINANCE", "DIVIDENDE", "NABAVE"]
        )
        if _norm_label(x)
    )

    routing: dict[str, TickerRoute] = {}
    for raw_key, raw_target in (raw.get("routing") or {}).items():
        src_ticker = str(raw_key).strip().upper()
        if not src_ticker or not isinstance(raw_target, Mapping):
            continue
        mkt = str(raw_target.get("market") or "").strip().lower()
        dst_ticker = str(raw_target.get("ticker") or src_ticker).strip().upper()
        if mkt not in SUPPORTED_SYNC_MARKETS:
            raise ExcelSyncError(
                f"Unsupported market {mkt!r} in {cfg_path} routing for {src_ticker}"
            )
        routing[src_ticker] = TickerRoute(market=mkt, ticker=dst_ticker)

    suffix_markets = dict(DEFAULT_SUFFIX_MARKETS)
    if "suffix_markets" in raw and isinstance(raw["suffix_markets"], Mapping):
        suffix_markets = {}
        for raw_sfx, cfg_sfx in raw["suffix_markets"].items():
            sfx = str(raw_sfx).strip().upper()
            if not sfx or not isinstance(cfg_sfx, Mapping):
                continue
            mkt = str(cfg_sfx.get("market") or "").strip().lower()
            strip_sfx = bool(cfg_sfx.get("strip_suffix", False))
            if mkt not in SUPPORTED_SYNC_MARKETS:
                raise ExcelSyncError(
                    f"Unsupported market {mkt!r} in {cfg_path} suffix_markets for {sfx}"
                )
            suffix_markets[sfx] = (mkt, strip_sfx)

    default_market = str(raw.get("default_market") or "us").strip().lower()
    if default_market not in SUPPORTED_SYNC_MARKETS:
        raise ExcelSyncError(f"Unsupported default_market {default_market!r} in {cfg_path}")

    extra_tickers: dict[str, tuple[str, ...]] = {}
    for mkt in SUPPORTED_SYNC_MARKETS:
        items = (raw.get("extra_tickers") or {}).get(mkt) or []
        extra_tickers[mkt] = tuple(
            str(t).strip().upper() for t in items if t is not None and str(t).strip()
        )

    return ExcelSyncConfig(
        excel_path=excel_path,
        sheet_name=sheet_name,
        min_quantity=min_quantity,
        ticker_column=ticker_col,
        name_column=name_col,
        quantity_column=qty_col,
        stop_markers=stop_markers,
        ignore_tickers=ignore_tickers,
        ignore_name_substrings=ignore_name_substrings,
        ignore_section_substrings=ignore_section_substrings,
        routing=routing,
        suffix_markets=suffix_markets,
        default_market=default_market,
        extra_tickers=extra_tickers,
    )


def _find_header_indices(
    row_values: Sequence[Any],
    sync_config: ExcelSyncConfig,
) -> tuple[int, int | None, int | None] | None:
    """Return `(ticker_idx, name_idx, qty_idx)` if `row_values` starts a holdings table."""
    norm_cells = [_norm_label(v) for v in row_values]
    ticker_label = _norm_label(sync_config.ticker_column)
    qty_label = _norm_label(sync_config.quantity_column)
    if ticker_label not in norm_cells or qty_label not in norm_cells:
        return None

    ticker_idx = norm_cells.index(ticker_label)
    name_label = _norm_label(sync_config.name_column)
    name_idx = norm_cells.index(name_label) if name_label in norm_cells else None
    qty_idx = norm_cells.index(qty_label)
    return ticker_idx, name_idx, qty_idx


def _parse_sheet_holdings(
    sheet: Any,
    sync_config: ExcelSyncConfig,
) -> list[ExcelHolding]:
    """Scan one worksheet top-to-bottom across any number of stacked holdings tables."""
    holdings: list[ExcelHolding] = []
    current_section = sheet.title
    active_cols: tuple[int, int | None, int | None] | None = None
    ticker_label = _norm_label(sync_config.ticker_column)

    for row_idx, raw_row in enumerate(sheet.iter_rows(values_only=True), start=1):
        row = list(raw_row)
        non_empty = [str(v).strip() for v in row if v is not None and str(v).strip()]

        if not non_empty:
            active_cols = None
            continue

        header_cols = _find_header_indices(row, sync_config)
        if header_cols is not None:
            active_cols = header_cols
            continue

        if active_cols is None:
            # Capture banner/section title lines such as "PORTFELJ – DELNICE (HOLDINGS) IBKR"
            if len(non_empty) == 1:
                current_section = non_empty[0]
            continue

        t_idx, n_idx, q_idx = active_cols
        raw_ticker = row[t_idx] if t_idx < len(row) else None
        ticker = str(raw_ticker or "").strip().upper()
        if not ticker:
            active_cols = None
            continue

        if ticker in sync_config.stop_markers or _norm_label(ticker) == ticker_label:
            active_cols = None
            continue

        name = (
            str(row[n_idx] or "").strip()
            if n_idx is not None and n_idx < len(row)
            else ""
        )
        quantity = (
            _parse_quantity(row[q_idx])
            if q_idx is not None and q_idx < len(row)
            else 1.0
        )

        holdings.append(
            ExcelHolding(
                ticker=ticker,
                name=name,
                quantity=quantity,
                sheet_name=sheet.title,
                section_title=current_section,
                row_number=row_idx,
            )
        )

    return holdings


def extract_holdings_from_excel(
    excel_path: Path | str,
    sync_config: ExcelSyncConfig,
) -> list[ExcelHolding]:
    """Read all stacked portfolio holdings tables from `excel_path` (`data_only=True`)."""
    wb_path = Path(excel_path)
    if not wb_path.exists():
        raise ExcelSyncError(f"Portfolio Excel file not found: {wb_path}")

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")
        try:
            wb = openpyxl.load_workbook(wb_path, data_only=True, read_only=True)
        except PermissionError as exc:
            raise ExcelSyncError(
                f"Cannot open {wb_path} (Permission denied). "
                "Close Microsoft Excel if the workbook is currently open, or wait for OneDrive sync, then retry."
            ) from exc
        except Exception as exc:
            raise ExcelSyncError(f"Failed to read Excel workbook {wb_path}: {exc}") from exc

        try:
            if sync_config.sheet_name:
                if sync_config.sheet_name not in wb.sheetnames:
                    raise ExcelSyncError(
                        f"Sheet {sync_config.sheet_name!r} not found in {wb_path}. "
                        f"Available sheets: {', '.join(wb.sheetnames)}"
                    )
                sheets = [wb[sync_config.sheet_name]]
            else:
                sheets = list(wb.worksheets)

            all_holdings: list[ExcelHolding] = []
            for sheet in sheets:
                all_holdings.extend(_parse_sheet_holdings(sheet, sync_config))
            return all_holdings
        finally:
            wb.close()


def _route_single_ticker(
    holding: ExcelHolding,
    sync_config: ExcelSyncConfig,
    known_ljse_issuers: set[str],
) -> tuple[TickerRoute, bool, str | None]:
    """Resolve `(TickerRoute, used_alias, warning)` for one Excel holding."""
    sym = holding.ticker.strip().upper()
    if sym in sync_config.routing:
        route = sync_config.routing[sym]
        return route, (route.ticker != sym), None

    for suffix in sorted(sync_config.suffix_markets.keys(), key=len, reverse=True):
        if sym.endswith(suffix) and len(sym) > len(suffix):
            mkt, strip_sfx = sync_config.suffix_markets[suffix]
            target_sym = sym[: -len(suffix)] if strip_sfx else sym
            return TickerRoute(market=mkt, ticker=target_sym), (target_sym != sym), None

    if sym in known_ljse_issuers:
        return TickerRoute(market="ljse", ticker=sym), False, None

    warning = None
    if "ilirika" in _norm_label(holding.section_title):
        warning = (
            f"Ticker {sym!r} under '{holding.section_title}' has no explicit routing rule; "
            f"routed to '{sync_config.default_market}'. Add it to portfolio_sync.yaml if it belongs to 'ljse' or 'ignore_tickers'."
        )
    else:
        warning = (
            f"Unmapped ticker {sym!r} ({holding.name or 'no name'}) routed to default market "
            f"'{sync_config.default_market}'. Verify it is not a colliding symbol or ETF."
        )
    return TickerRoute(market=sync_config.default_market, ticker=sym), False, warning


def resolve_portfolio_tickers(
    holdings: Sequence[ExcelHolding],
    sync_config: ExcelSyncConfig,
    tickers_config: Mapping[str, Any] | None = None,
) -> SyncResolution:
    """Filter, deduplicate, and route Excel holdings into per-market ticker lists."""
    by_market: dict[str, set[str]] = {mkt: set() for mkt in SUPPORTED_SYNC_MARKETS}
    skipped: list[str] = []
    warnings: list[str] = []
    routed_aliases: list[str] = []

    ljse_cfg = (tickers_config or {}).get("ljse") or {}
    known_ljse_issuers = {
        str(k).strip().upper()
        for k in (ljse_cfg.get("issuers") or {}).keys()
        if str(k).strip()
    }

    for holding in holdings:
        sym = holding.ticker.strip().upper()
        norm_section = _norm_label(holding.section_title)
        norm_sheet = _norm_label(holding.sheet_name)
        if any(
            sub in norm_section or sub in norm_sheet
            for sub in sync_config.ignore_section_substrings
        ):
            skipped.append(f"{sym} ({holding.section_title} - ignored section)")
            continue

        if holding.quantity <= sync_config.min_quantity:
            skipped.append(f"{sym} (qty={holding.quantity:g} <= {sync_config.min_quantity:g})")
            continue

        if sym in sync_config.ignore_tickers:
            skipped.append(f"{sym} (ignored ticker)")
            continue

        norm_name = _norm_label(holding.name)
        if norm_name and any(sub in norm_name for sub in sync_config.ignore_name_substrings):
            skipped.append(f"{sym} ({holding.name} - ignored instrument)")
            continue

        route, aliased, warning = _route_single_ticker(holding, sync_config, known_ljse_issuers)
        by_market[route.market].add(route.ticker)
        if aliased:
            routed_aliases.append(f"{sym} -> {route.market}:{route.ticker}")
        if warning:
            warnings.append(warning)

    for mkt in SUPPORTED_SYNC_MARKETS:
        by_market[mkt].update(sync_config.extra_tickers.get(mkt, ()))

    # Helpful diagnostic if an LJSE ticker lacks issuer metadata for SEOnet RSS watcher
    for ljse_sym in sorted(by_market["ljse"]):
        if ljse_sym not in known_ljse_issuers:
            warnings.append(
                f"LJSE ticker {ljse_sym} has no entry in tickers.yaml -> ljse.issuers yet "
                "(SEOnet RSS watcher filters by issuer name; weekly --scrape-ljse still matches by ticker)."
            )

    sorted_by_market = {mkt: sorted(vals) for mkt, vals in by_market.items()}
    return SyncResolution(
        tickers_by_market=sorted_by_market,
        skipped=skipped,
        warnings=warnings,
        routed_aliases=sorted(set(routed_aliases)),
    )


def apply_sync_to_tickers_yaml(
    tickers_path: Path | str,
    resolution: SyncResolution,
    dry_run: bool = False,
) -> SyncDiff:
    """Update `us.tickers`, `ljse.tickers`, and `eu.tickers` in `tickers.yaml` preserving all other settings."""
    cfg_path = Path(tickers_path)
    if not cfg_path.exists():
        raise ExcelSyncError(f"Config file not found: {cfg_path}")

    raw_text = cfg_path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw_text) or {}
    if not isinstance(data, dict):
        raise ExcelSyncError(f"Expected YAML mapping in {cfg_path}")

    added_by_market: dict[str, list[str]] = {}
    removed_by_market: dict[str, list[str]] = {}
    final_by_market: dict[str, list[str]] = {}
    any_changed = False

    for mkt in SUPPORTED_SYNC_MARKETS:
        new_list = list(resolution.tickers_by_market.get(mkt, []))
        section = data.get(mkt)
        if section is None:
            if not new_list:
                added_by_market[mkt] = []
                removed_by_market[mkt] = []
                final_by_market[mkt] = []
                continue
            section = {}
            data[mkt] = section
            any_changed = True
        elif not isinstance(section, dict):
            raise ExcelSyncError(f"Expected mapping for section {mkt!r} in {cfg_path}")

        old_list = [str(t).strip().upper() for t in (section.get("tickers") or []) if str(t).strip()]
        if old_list != new_list:
            any_changed = True
        added_by_market[mkt] = sorted(set(new_list) - set(old_list))
        removed_by_market[mkt] = sorted(set(old_list) - set(new_list))
        final_by_market[mkt] = new_list
        section["tickers"] = new_list

    # Ensure eu thresholds default safely so EU symbols without Finnhub free-tier coverage stay healthy
    if "eu" in data and isinstance(data.get("thresholds"), dict):
        mkt_thresholds = data["thresholds"].setdefault("markets", {})
        if isinstance(mkt_thresholds, dict) and "eu" not in mkt_thresholds:
            mkt_thresholds["eu"] = {"max_age_days": 3, "min_upcoming_events": 0}
            any_changed = True

    if any_changed and not dry_run:
        rendered = yaml.safe_dump(
            data,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        )
        cfg_path.write_text(rendered, encoding="utf-8")
        # Validate resulting config against the repo's strict schema loader
        load_config(cfg_path)

    return SyncDiff(
        changed=any_changed,
        added_by_market=added_by_market,
        removed_by_market=removed_by_market,
        final_by_market=final_by_market,
    )


def _run_git(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def git_push_tickers_yaml(
    tickers_path: Path | str,
    repo_root: Path | None = None,
    dry_run: bool = False,
    include_build_outputs: bool = False,
) -> bool:
    """Commit and push `tickers.yaml` (and optional calendar build outputs) when modified."""
    cfg_path = Path(tickers_path).resolve()
    root = (repo_root or cfg_path.parent).resolve()
    rel_cfg = cfg_path.relative_to(root).as_posix()

    allowed_paths = [rel_cfg]
    if include_build_outputs:
        for candidate in (
            "docs/us.ics",
            "docs/earnings.ics",
            "docs/ljse.ics",
            "docs/eu.ics",
            "docs/macro.ics",
            "docs/index.html",
            "docs/status.json",
            "data/changes.jsonl",
            "data/results.csv",
        ):
            if (root / candidate).exists():
                allowed_paths.append(candidate)

    staged = _run_git(["diff", "--cached", "--name-only"], root)
    if staged.returncode != 0:
        raise ExcelSyncError(f"git diff --cached failed: {staged.stderr.strip()}")
    allowed_set = set(allowed_paths)
    unrelated_staged = [
        line.strip()
        for line in staged.stdout.splitlines()
        if line.strip() and line.strip() not in allowed_set
    ]
    if unrelated_staged:
        raise ExcelSyncError(
            "Refusing to commit because unrelated files are already staged in git: "
            + ", ".join(unrelated_staged)
        )

    diff_check = _run_git(["diff", "--quiet", "--", *allowed_paths], root)
    staged_check = _run_git(["diff", "--cached", "--quiet", "--", *allowed_paths], root)
    untracked_build = [
        p
        for p in allowed_paths
        if _run_git(["ls-files", "--error-unmatch", p], root).returncode != 0
    ]
    if diff_check.returncode == 0 and staged_check.returncode == 0 and not untracked_build:
        logger.info("No git changes in %s; skipping commit and push.", ", ".join(allowed_paths))
        return False

    if dry_run:
        logger.info("[dry-run] Would commit and push %s.", ", ".join(allowed_paths))
        return True

    add_res = _run_git(["add", *allowed_paths], root)
    if add_res.returncode != 0:
        raise ExcelSyncError(f"git add failed: {add_res.stderr.strip()}")

    commit_msg = (
        "chore: sync portfolio.xlsx and rebuild market calendars"
        if include_build_outputs
        else "chore: sync tickers from portfolio.xlsx"
    )
    commit_res = _run_git(["commit", "-m", commit_msg], root)
    if commit_res.returncode != 0:
        raise ExcelSyncError(
            f"git commit failed: {(commit_res.stderr or commit_res.stdout).strip()}"
        )

    pull_res = _run_git(["pull", "--rebase", "--autostash"], root)
    if pull_res.returncode != 0:
        raise ExcelSyncError(
            f"git pull --rebase failed: {(pull_res.stderr or pull_res.stdout).strip()}"
        )

    push_res = _run_git(["push"], root)
    if push_res.returncode != 0:
        raise ExcelSyncError(
            f"git push failed: {(push_res.stderr or push_res.stdout).strip()}"
        )
    return True


def sync_portfolio(
    excel_path: Path | str | None = None,
    config_path: Path | str = DEFAULT_TICKERS_CONFIG_PATH,
    sync_config_path: Path | str = DEFAULT_SYNC_CONFIG_PATH,
    dry_run: bool = False,
    push: bool = False,
    build: bool = False,
) -> tuple[SyncResolution, SyncDiff, bool]:
    """Run the full Excel -> `tickers.yaml` sync (plus optional local calendar rebuild and git push)."""
    sync_cfg = load_sync_config(sync_config_path)
    target_excel = Path(excel_path) if excel_path is not None else sync_cfg.excel_path
    cfg_path = Path(config_path)
    raw_tickers_cfg = (
        yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        if cfg_path.exists()
        else {}
    )

    holdings = extract_holdings_from_excel(target_excel, sync_cfg)
    resolution = resolve_portfolio_tickers(holdings, sync_cfg, raw_tickers_cfg)
    diff = apply_sync_to_tickers_yaml(cfg_path, resolution, dry_run=dry_run)

    if build and not dry_run:
        from earnings_cal.main import run as run_calendar

        logger.info("Rebuilding market calendars (.ics) and docs/index.html...")
        run_calendar(config_path=cfg_path)

    pushed = False
    if push:
        pushed = git_push_tickers_yaml(
            cfg_path,
            dry_run=dry_run,
            include_build_outputs=build,
        )

    return resolution, diff, pushed


def _format_summary(
    excel_path: Path,
    resolution: SyncResolution,
    diff: SyncDiff,
    pushed: bool,
    dry_run: bool,
    built: bool = False,
) -> str:
    lines = [f"Portfolio sync from: {excel_path}"]
    if dry_run:
        lines.append("(DRY RUN - tickers.yaml not modified)")

    for mkt in SUPPORTED_SYNC_MARKETS:
        tickers = diff.final_by_market.get(mkt, [])
        added = diff.added_by_market.get(mkt, [])
        removed = diff.removed_by_market.get(mkt, [])
        lines.append(f"  [{mkt}] tickers ({len(tickers)}): {', '.join(tickers) or '(none)'}")
        if added:
            lines.append(f"    + added:   {', '.join(added)}")
        if removed:
            lines.append(f"    - removed: {', '.join(removed)}")

    if resolution.routed_aliases:
        lines.append(f"  Aliased: {', '.join(resolution.routed_aliases)}")
    if resolution.skipped:
        lines.append(f"  Skipped: {', '.join(resolution.skipped)}")
    for warn in resolution.warnings:
        lines.append(f"  WARNING: {warn}")

    if dry_run:
        if diff.changed:
            lines.append("Result: changes detected (re-run without --dry-run to apply).")
        else:
            lines.append("Result: tickers.yaml is already up to date.")
    elif pushed:
        if built:
            lines.append(
                "Result: calendars & static dashboard rebuilt, committed, and pushed to GitHub."
            )
        else:
            lines.append("Result: tickers.yaml updated, committed, and pushed to GitHub.")
    elif built:
        lines.append("Result: tickers.yaml and local calendars/dashboard are up to date.")
    elif not diff.changed:
        lines.append("Result: tickers.yaml is already up to date.")
    else:
        lines.append("Result: tickers.yaml updated locally (run with --push to trigger GitHub Actions).")

    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m earnings_cal.sync_excel",
        description="Sync active portfolio tickers from Excel into tickers.yaml.",
    )
    parser.add_argument(
        "--excel",
        type=Path,
        default=None,
        help="Path to Excel portfolio workbook (overrides excel_path in portfolio_sync.yaml)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_TICKERS_CONFIG_PATH,
        help="Path to tickers.yaml (default: tickers.yaml)",
    )
    parser.add_argument(
        "--sync-config",
        type=Path,
        default=DEFAULT_SYNC_CONFIG_PATH,
        help="Path to portfolio_sync.yaml (default: portfolio_sync.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview ticker changes without modifying tickers.yaml or pushing",
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="Rebuild docs/*.ics and docs/index.html locally after syncing tickers.yaml",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Commit and push changes to GitHub",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args(argv)
    try:
        sync_cfg = load_sync_config(args.sync_config)
        target_excel = args.excel if args.excel is not None else sync_cfg.excel_path
        resolution, diff, pushed = sync_portfolio(
            excel_path=target_excel,
            config_path=args.config,
            sync_config_path=args.sync_config,
            dry_run=args.dry_run,
            push=args.push,
            build=args.build,
        )
    except ExcelSyncError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(
        _format_summary(
            target_excel,
            resolution,
            diff,
            pushed,
            args.dry_run,
            built=args.build and not args.dry_run,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
