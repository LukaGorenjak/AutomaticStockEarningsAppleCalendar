"""Central registry of supported markets.

Adding a new market requires only:
1. Creating its module in `src/earnings_cal/markets/<market_id>.py`
2. Registering it in `MARKET_REGISTRY` below
3. Adding its section `<market_id>:` to `tickers.yaml`
"""

from __future__ import annotations

from typing import Any

from earnings_cal.markets.base import Market
from earnings_cal.markets.ljse import LJSEMarket
from earnings_cal.markets.macro import MacroMarket
from earnings_cal.markets.us import USMarket
from earnings_cal.models import WatchlistConfig

# Single source of truth for registered markets
MARKET_REGISTRY: dict[str, type[Market] | Market] = {
    USMarket.id: USMarket,
    LJSEMarket.id: LJSEMarket,
    MacroMarket.id: MacroMarket,
}


def register_market(market: type[Market] | Market) -> None:
    """Register a `Market` class or instance in `MARKET_REGISTRY`."""
    market_id = getattr(market, "id", "").strip().lower()
    if not market_id:
        raise ValueError(f"Market {market!r} must define a non-empty 'id' attribute")
    MARKET_REGISTRY[market_id] = market


def get_market_registry() -> dict[str, type[Market] | Market]:
    """Return the active market registry mapping (`market_id -> Market class or instance`)."""
    return MARKET_REGISTRY


def get_registered_market_ids() -> list[str]:
    """Return registered market IDs in registration order."""
    return list(MARKET_REGISTRY.keys())


def instantiate_markets(
    config: WatchlistConfig | None = None,
    **options: Any,
) -> list[Market]:
    """Instantiate all registered markets in `MARKET_REGISTRY` with their config sections."""
    instances: list[Market] = []
    for market_id, entry in MARKET_REGISTRY.items():
        if isinstance(entry, Market):
            instances.append(entry)
            continue

        section = config.get_market_section(market_id) if config is not None else {}
        config_dir = config.config_dir if config is not None else None
        try:
            instance = entry(config=section, config_dir=config_dir, **options)
        except TypeError:
            # Support minimal custom Market subclasses that define a zero-arg __init__
            instance = entry()
            instance.config = section
            if config_dir is not None:
                instance.config_dir = config_dir
            instance.options = options
        instances.append(instance)
    return instances
