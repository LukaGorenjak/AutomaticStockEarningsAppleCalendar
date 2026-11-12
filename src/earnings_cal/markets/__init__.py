"""Multi-market abstractions, built-in markets (`us`, `ljse`, `macro`, `eu`), and central registry."""

from __future__ import annotations

from earnings_cal.markets.base import Market
from earnings_cal.markets.eu import EUMarket
from earnings_cal.markets.ljse import LJSEMarket, load_ljse_events_yaml
from earnings_cal.markets.macro import MacroMarket, load_macro_events_yaml
from earnings_cal.markets.registry import (
    MARKET_REGISTRY,
    get_market_registry,
    get_registered_market_ids,
    instantiate_markets,
    register_market,
)
from earnings_cal.markets.us import USMarket
from earnings_cal.models import DateWindow

__all__ = [
    "DateWindow",
    "EUMarket",
    "LJSEMarket",
    "MARKET_REGISTRY",
    "MacroMarket",
    "Market",
    "USMarket",
    "get_market_registry",
    "get_registered_market_ids",
    "instantiate_markets",
    "load_ljse_events_yaml",
    "load_macro_events_yaml",
    "register_market",
]
