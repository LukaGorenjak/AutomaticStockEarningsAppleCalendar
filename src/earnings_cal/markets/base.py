"""Market abstraction for multi-market calendar generation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from earnings_cal.models import DateWindow, Event


class Market(ABC):
    """Abstract base class representing a financial market calendar feed.

    Subclasses define:
    - `id`: short market slug (e.g. `"us"`, `"ljse"`) used in `{id}.ics` and event UIDs
    - `display_name`: human-readable market name (e.g. `"United States"`)
    - `timezone`: IANA timezone identifier (e.g. `"America/New_York"`, `"Europe/Ljubljana"`)
    - `calendar_name`: `X-WR-CALNAME` header value (e.g. `"Earnings US"`, `"Events LJSE"`)
    - `fetch_events(window) -> list[Event]`: returns events within `[window.start, window.end]`
    """

    id: str
    display_name: str
    timezone: str
    calendar_name: str
    legacy_aliases: tuple[str, ...] = ()

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        config_dir: Path | None = None,
        **options: Any,
    ) -> None:
        self.config: dict[str, Any] = dict(config or {})
        self.config_dir: Path = config_dir or Path(".")
        self.options: dict[str, Any] = options

    @property
    def feed_filename(self) -> str:
        """Output `.ics` filename for this market (e.g. `us.ics`)."""
        return f"{self.id}.ics"

    def effective_window(self, window: DateWindow) -> DateWindow:
        """Return the effective `[start, end]` date window used when fetching this market."""
        return window

    @abstractmethod
    def fetch_events(self, window: DateWindow) -> list[Event]:
        """Fetch events for this market within `[window.start, window.end]`."""
