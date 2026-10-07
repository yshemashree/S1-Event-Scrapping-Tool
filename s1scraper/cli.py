"""Command line: ``python run_scraper.py [options]`` (``--gui`` opens the window)."""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import APP_NAME, __version__
from .config import CUSTOM, Settings, SettingsError
from .sources import ALL_SOURCES


def setup_logging(settings: Settings, verbose: bool = False) -> Path:
    folder = settings.data_path / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"scraper_{datetime.now():%Y%m%d_%H%M%S}.log"
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)s %(name)s: %(message)s"))
    root.addHandler(fh)
    if verbose:
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.DEBUG)
        sh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(sh)
    for noisy in ("urllib3", "asyncio", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return path


def build_parser() -> argparse.ArgumentParser:
    keys = ", ".join(s.key for s in ALL_SOURCES)
    p = argparse.ArgumentParser(
        prog="run_scraper.py",
        description=f"{APP_NAME} {__version__}: scrape events for the chosen cities and window into Excel.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python run_scraper.py                       # use settings.json\n"
            "  python run_scraper.py --window 1w           # next week\n"
            "  python run_scraper.py --window 6m --cities Mumbai Pune\n"
            "  python run_scraper.py --from 2026-10-10 --to 2026-12-31\n"
            "  python run_scraper.py --sources bookmyshow district\n"
            "  python run_scraper.py --diagnose            # quick health check of every source\n"
            "  python run_scraper.py --gui                 # open the desktop window\n\n"
            f"Source keys: {keys}"
        ),
    )
    p.add_argument("--gui", action="store_true", help="open the desktop window")
    p.add_argument("--settings", type=Path, help="settings file (default: settings.json next to the app)")
    p.add_argument("--workbook", help="Excel workbook to update (.xlsx)")
    p.add_argument("--window", help="1w, 2w, 1m, 2m, 3m, 6m or e.g. '10 days'")
    p.add_argument("--from", dest="date_from", help="custom window start, e.g. 2026-10-10")
    p.add_argument("--to", dest="date_to", help="custom window end, e.g. 2026-12-31")
    p.add_argument("--cities", nargs="+", help="cities to cover (default: all seven)")
    p.add_argument("--sources", nargs="+", help="only these sources (keys listed below)")
    p.add_argument("--speed", choices=["gentle", "normal", "fast"], help="request pacing per site")
    p.add_argument("--no-browser", action="store_true", help="never open pages in a real browser")
    p.add_argument("--show-browser", action="store_true", help="show the browser window while it works")
    p.add_argument("--diagnose", action="store_true",
                   help="read one city per source, save raw pages to diagnostics/ and print a health report")
    p.add_argument("--save-settings", action="store_true", help="store the given options in settings.json")
    p.add_argument("--verbose", "-v", action="store_true", help="print debug logging")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return p


_WINDOW_ALIASES = {"1w": "1 week", "2w": "2 weeks", "1m": "1 month", "2m": "2 months", "3m": "3 months",
                   "6m": "6 months"}


def apply_args(settings: Settings, args: argparse.Namespace) -> Settings:
    if args.workbook:
        settings.workbook_path = args.workbook
    if args.window:
        settings.window_preset = _WINDOW_ALIASES.get(args.window.lower().replace(" ", ""), args.window)
    if args.date_from or args.date_to:
        settings.window_preset = CUSTOM
        settings.window_start = args.date_from or ""
        settings.window_end = args.date_to or ""
    if args.cities:
        settings.cities = args.cities
    if args.sources:
        wanted = {s.lower() for s in args.sources}
        unknown = wanted - {s.key for s in ALL_SOURCES}
        if unknown:
            raise SettingsError(f"Unknown source(s): {', '.join(sorted(unknown))}")
        settings.sources = {s.key: s.key in wanted for s in ALL_SOURCES}
    if args.speed:
        settings.speed = args.speed
    if args.no_browser:
        settings.use_browser = False
    if args.show_browser:
        settings.show_browser = True
    return settings


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.gui:
        try:
            from .gui import main as gui_main
        except ImportError as exc:   # Python built without Tk (Homebrew, minimal Linux installs)
            print("The app window needs Tkinter, which this Python does not include "
                  f"({exc}).\n  Windows/macOS: install Python from https://www.python.org/downloads/\n"
                  "  Linux: sudo apt install python3-tk\n"
                  "You can still run the scraper from this terminal:  python run_scraper.py --help",
                  file=sys.stderr)
            return 3
        return gui_main(settings_path=args.settings)
    try:
        settings = apply_args(Settings.load(args.settings), args)
        settings.validate()
    except SettingsError as exc:
        print(f"Settings problem: {exc}", file=sys.stderr)
        return 2
    if args.save_settings:
        settings.save(args.settings)
    log_path = setup_logging(settings, args.verbose)

    if args.diagnose:
        from .diagnose import run_diagnostics

        return run_diagnostics(settings, print)

    from .pipeline import Runner

    stop = threading.Event()
    last = [""]

    def progress(frac: float, text: str) -> None:
        line = f"[{frac * 100:5.1f}%] {text}"
        if line != last[0]:
            last[0] = line
            print("\r" + line[:110].ljust(110), end="", flush=True)

    def log_line(msg: str) -> None:
        print("\r" + " " * 110 + "\r" + msg, flush=True)

    runner = Runner(settings, log_fn=log_line, progress_fn=progress, stop_event=stop)
    try:
        report = runner.run()
    except KeyboardInterrupt:
        stop.set()
        print("\nStopped.")
        return 130
    except SettingsError as exc:
        print(f"\nSettings problem: {exc}", file=sys.stderr)
        return 2
    print()
    for line in report.summary_lines():
        print(line)
    print(f"Log: {log_path}")
    if report.cancelled:
        return 130
    return 0 if report.events or not report.error else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
