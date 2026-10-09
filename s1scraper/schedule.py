"""Automatic runs: register the scraper with the computer's own scheduler.

* macOS   - a launchd agent (~/Library/LaunchAgents); a run missed while the
            Mac was asleep happens when it wakes. macOS keeps background jobs
            out of Desktop, Documents and Downloads unless allowed, so the app
            runs a quick background check right after saving (start_check).
* Windows - a Task Scheduler task for the current user; "run as soon as
            possible after a missed start" is switched on.
* Linux   - a crontab line.

The scheduled run uses whatever was last saved in the app (window, cities,
sources, workbook) and, on macOS, shows a notification when it finishes.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape

from .config import APP_DIR, SETTINGS_FILE

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
FREQUENCIES = {"off": "Off", "daily": "Every day", "weekly": "Every week"}
LABEL = "com.stepone.s1eventscraper"
CHECK_LABEL = LABEL + ".check"
TASK_NAME = "S1 Event Scraper"
CRON_MARK = "# S1 Event Scraper (managed by the app)"

Runner = Callable[..., "subprocess.CompletedProcess"]


class ScheduleError(Exception):
    pass


def parse_time(text: str):
    """"7:00", "07:00", "19:30", "7 pm", "7:30am" -> (hour, minute)."""
    s = (text or "").strip().lower().replace(".", ":")
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s)
    if not m:
        raise ScheduleError(f"Could not read the time {text!r}. Use 24-hour time like 07:00 or 19:30.")
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm:
        if not 1 <= hour <= 12:
            raise ScheduleError(f"Could not read the time {text!r}.")
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if hour > 23 or minute > 59:
        raise ScheduleError(f"Could not read the time {text!r}. Use 24-hour time like 07:00 or 19:30.")
    return hour, minute


@dataclass
class Schedule:
    frequency: str = "off"     # off / daily / weekly
    day: str = "Friday"        # used when weekly
    time: str = "07:00"

    def validate(self) -> "Schedule":
        freq = (self.frequency or "off").lower()
        if freq not in FREQUENCIES:
            raise ScheduleError(f"Unknown frequency {self.frequency!r}.")
        day = next((d for d in DAYS if d.lower().startswith((self.day or "").strip().lower()[:3])), None)
        if freq == "weekly" and (not self.day or day is None):
            raise ScheduleError(f"Unknown day {self.day!r}.")
        hour, minute = parse_time(self.time)
        return Schedule(freq, day or "Friday", f"{hour:02d}:{minute:02d}")

    @property
    def hour(self) -> int:
        return parse_time(self.time)[0]

    @property
    def minute(self) -> int:
        return parse_time(self.time)[1]

    def describe(self) -> str:
        if self.frequency == "off":
            return "Off"
        when = "Every day" if self.frequency == "daily" else f"Every {self.day}"
        return f"{when} at {self.time}"


# ------------------------------------------------------------------ command

def scraper_command(settings_path: Path = SETTINGS_FILE, platform: Optional[str] = None,
                    extra: Sequence[str] = ("--notify",)) -> List[str]:
    platform = platform or sys.platform
    python = Path(sys.executable)
    if platform.startswith("win"):
        windowless = python.with_name("pythonw.exe")   # no console window popping up
        if windowless.exists():
            python = windowless
    cmd = [str(python), str(APP_DIR / "run_scraper.py")]
    if Path(settings_path).resolve() != SETTINGS_FILE.resolve():
        cmd += ["--settings", str(Path(settings_path).resolve())]
    return cmd + list(extra)


def log_dir(settings_path: Path = SETTINGS_FILE, platform: Optional[str] = None,
            home: Optional[Path] = None) -> Path:
    """Where scheduled runs write their console output. On macOS this is
    ~/Library/Logs, which a background job can always write to."""
    if (platform or sys.platform) == "darwin":
        folder = (home or Path.home()) / "Library" / "Logs" / "S1 Event Scraper"
    else:
        try:
            from .config import Settings

            folder = Settings.load(settings_path).data_path / "logs"
        except Exception:  # noqa: BLE001 - fall back to the app folder
            folder = APP_DIR / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


# ------------------------------------------------------------------- macOS

def launchd_plist(s: Schedule, cmd: List[str], log_path: Path) -> dict:
    interval = {"Hour": s.hour, "Minute": s.minute}
    if s.frequency == "weekly":
        interval["Weekday"] = (DAYS.index(s.day) + 1) % 7      # launchd: 0 = Sunday
    return _job(LABEL, cmd, log_path, StartCalendarInterval=interval, RunAtLoad=False)


def _job(label: str, cmd: List[str], log_path: Path, **extra) -> dict:
    return {
        "Label": label,
        "ProgramArguments": cmd,
        # not the app folder: if that is on the Desktop, macOS may refuse to start the job there
        "WorkingDirectory": str(log_path.parent),
        "EnvironmentVariables": {"PYTHONIOENCODING": "utf-8"},
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        **extra,
    }


def _plist_path(home: Optional[Path], label: str = LABEL) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{label}.plist"


def _uid() -> int:
    return os.getuid() if hasattr(os, "getuid") else 0


def _load_launchd(job: dict, path: Path, run: Runner) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    run(["launchctl", "bootout", f"gui/{_uid()}/{job['Label']}"], capture_output=True, text=True)
    with open(path, "wb") as fh:
        plistlib.dump(job, fh)
    for attempt in range(3):      # right after a bootout launchd can briefly say "Input/output error"
        res = run(["launchctl", "bootstrap", f"gui/{_uid()}", str(path)], capture_output=True, text=True)
        if res.returncode == 0:
            break
        time.sleep(0.5 * (attempt + 1))
    if res.returncode != 0:   # older macOS
        res = run(["launchctl", "load", "-w", str(path)], capture_output=True, text=True)
    if res.returncode != 0:
        raise ScheduleError(f"macOS refused the schedule: {(res.stderr or res.stdout).strip()}")


def _unload_launchd(path: Path, label: str, run: Runner) -> None:
    run(["launchctl", "bootout", f"gui/{_uid()}/{label}"], capture_output=True, text=True)
    if path.exists():
        path.unlink()


# ----------------------------------------------------------------- Windows

def task_xml(s: Schedule, cmd: List[str], today: Optional[date] = None) -> str:
    start = (today or date.today()) + timedelta(days=1)
    boundary = f"{start.isoformat()}T{s.hour:02d}:{s.minute:02d}:00"
    if s.frequency == "weekly":
        schedule = (f"<ScheduleByWeek><DaysOfWeek><{s.day} /></DaysOfWeek>"
                    f"<WeeksInterval>1</WeeksInterval></ScheduleByWeek>")
    else:
        schedule = "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
    command = escape(cmd[0])
    arguments = escape(subprocess.list2cmdline(cmd[1:]))
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>S1 Event Scraper - automatic run</Description></RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{boundary}</StartBoundary>
      <Enabled>true</Enabled>
      {schedule}
    </CalendarTrigger>
  </Triggers>
  <Settings>
    <StartWhenAvailable>true</StartWhenAvailable>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <ExecutionTimeLimit>PT8H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{escape(str(APP_DIR))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _install_schtasks(s: Schedule, cmd: List[str], run: Runner) -> None:
    fd, path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        Path(path).write_text(task_xml(s, cmd), encoding="utf-16")
        res = run(["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/XML", path], capture_output=True, text=True)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if res.returncode != 0:
        raise ScheduleError(f"Windows refused the schedule: {(res.stderr or res.stdout).strip()}")


def _remove_schtasks(run: Runner) -> None:
    run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], capture_output=True, text=True)


# ------------------------------------------------------------------- Linux

def cron_line(s: Schedule, cmd: List[str], log_path: Path) -> str:
    dow = str((DAYS.index(s.day) + 1) % 7) if s.frequency == "weekly" else "*"
    command = " ".join(shlex.quote(c) for c in cmd)
    return (f"{s.minute} {s.hour} * * {dow} cd {shlex.quote(str(APP_DIR))} && {command} "
            f">> {shlex.quote(str(log_path))} 2>&1 {CRON_MARK}")


def _crontab_without_ours(run: Runner) -> List[str]:
    res = run(["crontab", "-l"], capture_output=True, text=True)
    existing = res.stdout.splitlines() if res.returncode == 0 else []
    return [line for line in existing if CRON_MARK not in line]


def _write_crontab(lines: List[str], run: Runner) -> None:
    res = run(["crontab", "-"], input="\n".join(lines) + "\n", capture_output=True, text=True)
    if res.returncode != 0:
        raise ScheduleError(f"Could not update the crontab: {(res.stderr or res.stdout).strip()}")


# --------------------------------------------------------------------- API

def install(s: Schedule, settings_path: Path = SETTINGS_FILE, platform: Optional[str] = None,
            run: Runner = subprocess.run, home: Optional[Path] = None) -> str:
    """Register (or with frequency "off", remove) the automatic run. Returns a status line."""
    s = s.validate()
    platform = platform or sys.platform
    if s.frequency == "off":
        remove(platform, run, home)
        return "Automatic runs are off."
    cmd = scraper_command(settings_path, platform)
    if platform.startswith("win"):
        _install_schtasks(s, cmd, run)        # each run still writes its own log file in logs/
        return f"Scheduled: {s.describe()}."
    log_path = log_dir(settings_path, platform, home) / "scheduled_runs.log"
    if platform == "darwin":
        _load_launchd(launchd_plist(s, cmd, log_path), _plist_path(home), run)
    else:
        _write_crontab(_crontab_without_ours(run) + [cron_line(s, cmd, log_path)], run)
    return f"Scheduled: {s.describe()}."


def remove(platform: Optional[str] = None, run: Runner = subprocess.run, home: Optional[Path] = None) -> None:
    platform = platform or sys.platform
    if platform == "darwin":
        _unload_launchd(_plist_path(home), LABEL, run)
    elif platform.startswith("win"):
        _remove_schtasks(run)
    else:
        lines = _crontab_without_ours(run)
        _write_crontab(lines, run)


def is_installed(platform: Optional[str] = None, run: Runner = subprocess.run, home: Optional[Path] = None) -> bool:
    platform = platform or sys.platform
    try:
        if platform == "darwin":
            return _plist_path(home).exists()
        if platform.startswith("win"):
            return run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True, text=True).returncode == 0
        res = run(["crontab", "-l"], capture_output=True, text=True)
        return res.returncode == 0 and CRON_MARK in res.stdout
    except OSError:
        return False


def notify(title: str, message: str, platform: Optional[str] = None, run: Runner = subprocess.run) -> None:
    """A desktop notification after a scheduled run (macOS; elsewhere the logs folder has it)."""
    if (platform or sys.platform) != "darwin":
        return
    def q(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')
    try:
        run(["osascript", "-e", f'display notification "{q(message)}" with title "{q(title)}"'],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


# -------------------------------------------------------- background check

def access_problems(settings_path: Path = SETTINGS_FILE) -> List[str]:
    """What stops a run started by the scheduler: unreadable settings, or folders
    it cannot open or write to (on macOS, Desktop/Documents/Downloads unless allowed)."""
    from .config import Settings, SettingsError

    try:
        settings = Settings.load(settings_path)
        settings.validate()
    except (SettingsError, OSError) as exc:
        return [f"Settings: {exc}"]
    workbook = settings.resolve(settings.workbook_path)
    problems: List[str] = []
    checked = set()
    for what, folder, write in (("app folder", APP_DIR, False), ("data folder", settings.data_path, True),
                                ("workbook folder", workbook.parent, True)):
        if (folder, write) in checked:
            continue
        checked.add((folder, write))
        try:
            if what == "data folder":
                folder.mkdir(parents=True, exist_ok=True)
            os.listdir(folder)
            if write:
                fd, tmp = tempfile.mkstemp(prefix=".s1-check-", dir=folder)
                os.close(fd)
                os.unlink(tmp)
        except OSError as exc:
            problems.append(f"Cannot {'write to' if write else 'open'} the {what} {folder}: {exc.strerror or exc}")
    if workbook.exists():
        try:
            with open(workbook, "rb") as fh:
                fh.read(1)
        except OSError as exc:
            problems.append(f"Cannot open the workbook {workbook}: {exc.strerror or exc}")
    return problems


def write_check_result(settings_path: Path, result_path: Path) -> int:
    """``run_scraper.py --check-access FILE``: run by the scheduler during the background check."""
    problems = access_problems(settings_path)
    result = {"ok": not problems, "problems": problems, "python": sys.executable,
              "time": datetime.now().isoformat(timespec="seconds")}
    tmp = Path(str(result_path) + ".tmp")
    tmp.write_text(json.dumps(result, indent=2), encoding="utf-8")
    os.replace(tmp, result_path)
    if check_needed():
        try:    # so the check never starts again at the next login, even if the app was closed meanwhile
            _plist_path(None, CHECK_LABEL).unlink()
        except OSError:
            pass
    return 0 if not problems else 1


def check_needed(platform: Optional[str] = None) -> bool:
    return (platform or sys.platform) == "darwin"


@dataclass
class BackgroundCheck:
    result_path: Path
    log_path: Path
    plist_path: Path


def start_check(settings_path: Path = SETTINGS_FILE, run: Runner = subprocess.run,
                home: Optional[Path] = None) -> BackgroundCheck:
    """macOS: start the access check as a launchd job now, the same way the
    scheduled run starts, so a blocked folder shows up (or macOS asks for
    permission) while someone is at the Mac rather than at 7 in the morning."""
    folder = log_dir(settings_path, "darwin", home)
    chk = BackgroundCheck(folder / "background_check.json", folder / "background_check.log",
                          _plist_path(home, CHECK_LABEL))
    for old in (chk.result_path, chk.log_path):
        if old.exists():
            old.unlink()
    cmd = scraper_command(settings_path, "darwin", ["--check-access", str(chk.result_path)])
    _load_launchd(_job(CHECK_LABEL, cmd, chk.log_path, RunAtLoad=True), chk.plist_path, run)
    return chk


def check_outcome(chk: BackgroundCheck) -> Optional[Tuple[bool, str]]:
    """None while the check is still running, else (passed, details)."""
    if chk.result_path.exists():
        try:
            data = json.loads(chk.result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return bool(data.get("ok")), "\n".join(data.get("problems") or [])
    try:
        log = chk.log_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        log = ""
    if any(sign in log for sign in ("Traceback", "Error", "not permitted", "can't open")):
        return False, log[-800:]      # Python stopped before it could report
    return None


def end_check(chk: BackgroundCheck, run: Runner = subprocess.run) -> None:
    _unload_launchd(chk.plist_path, CHECK_LABEL, run)


def is_blocked_by_macos(details: str) -> bool:
    return any(sign in details for sign in ("not permitted", "Permission", "did not report back"))


def mac_access_help(details: str) -> str:
    python_app = Path(sys.base_prefix) / "Resources" / "Python.app"
    target = python_app if python_app.exists() else Path(os.path.realpath(sys.executable))
    return ("The automatic run is saved, but macOS did not let it open the files it needs. This happens "
            "when the app folder or the workbook is in Desktop, Documents or Downloads.\n\n"
            "Fix it one of two ways, then press Save schedule again:\n\n"
            f"1. Move the app folder (and the workbook) into your home folder ({Path.home()}), "
            "open the app from there and choose the workbook again.\n\n"
            "2. Or allow Python: System Settings > Privacy & Security > Full Disk Access > +, "
            f"press Cmd+Shift+G, paste\n    {target}\nthen Open, and make sure it is switched on.\n\n"
            f"Details: {details}")
