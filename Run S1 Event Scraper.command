#!/bin/bash
# Double-click (macOS) or run with bash (Linux) to open the S1 Event Scraper window.
# The first run creates a private Python environment in the .venv folder.
cd "$(dirname "$0")" || exit 1

if [ ! -x ".venv/bin/python" ]; then
  echo "Setting up S1 Event Scraper for the first time (needs internet, about a minute)..."
  if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 is not installed. Install it from https://www.python.org/downloads/ and run this again."
    read -n 1 -s -r -p "Press any key to close"
    exit 1
  fi
  if ! { python3 -m venv .venv \
         && .venv/bin/python -m pip install --upgrade pip \
         && .venv/bin/python -m pip install -r requirements.txt; }; then
    echo "Setup did not finish - check the internet connection and try again."
    rm -rf .venv
    read -n 1 -s -r -p "Press any key to close"
    exit 1
  fi
fi

exec .venv/bin/python run_scraper.py --gui
