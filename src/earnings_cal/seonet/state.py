"""Deterministic state persistence for processed SEOnet announcement IDs."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

DEFAULT_STATE_PATH = Path("state/seonet_seen.json")


class StateError(RuntimeError):
    """Raised when the SEOnet seen-state file is malformed."""


def load_seen(path: Path | str = DEFAULT_STATE_PATH) -> set[str]:
    """Load processed SEOnet announcement IDs from `path`."""
    state_path = Path(path)
    if not state_path.exists():
        return set()

    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"Failed to read SEOnet state file {state_path}: {exc}") from exc

    if isinstance(raw, list):
        return {str(item).strip() for item in raw if str(item).strip()}

    if isinstance(raw, dict):
        items = raw.get("seen_ids")
        if items is None:
            return set()
        if not isinstance(items, list):
            raise StateError(f"'seen_ids' in {state_path} must be a list")
        return {str(item).strip() for item in items if str(item).strip()}

    raise StateError(f"Unexpected JSON structure in {state_path}: expected object or list")


def render_seen_json(seen_ids: Iterable[str]) -> str:
    """Serialize `seen_ids` deterministically with sorted keys and sorted IDs."""
    sorted_ids = sorted({str(item).strip() for item in seen_ids if str(item).strip()})
    payload = {"seen_ids": sorted_ids}
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def save_seen(seen_ids: Iterable[str], path: Path | str = DEFAULT_STATE_PATH) -> bool:
    """Persist `seen_ids` deterministically. Returns True if file contents changed."""
    state_path = Path(path)
    new_content = render_seen_json(seen_ids)

    if state_path.exists():
        existing = state_path.read_text(encoding="utf-8")
        if existing == new_content:
            return False

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(new_content, encoding="utf-8")
    return True
