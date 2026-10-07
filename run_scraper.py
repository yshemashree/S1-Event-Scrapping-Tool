#!/usr/bin/env python3
"""S1 Event Scraper - run from a terminal, or double-click the launcher for the window.

    python run_scraper.py --help
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from s1scraper.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
