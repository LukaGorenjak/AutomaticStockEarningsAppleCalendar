"""Allow running SEOnet watcher via `python -m earnings_cal.seonet`."""

from __future__ import annotations

import sys

from earnings_cal.seonet.cli import main

if __name__ == "__main__":
    sys.exit(main())
