"""Allow running package via `python -m earnings_cal`."""

from __future__ import annotations

import sys

from earnings_cal.main import main

if __name__ == "__main__":
    sys.exit(main())
