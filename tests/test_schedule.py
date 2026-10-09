import json
import os
import plistlib
import subprocess
import sys
import time
import xml.dom.minidom
from datetime import date
from pathlib import Path

import pytest

from s1scraper import schedule as sch
from s1scraper.config import Settings
from s1scraper.schedule import Schedule, ScheduleError, parse_time


class FakeRun:
    """Stands in for subprocess.run and records the system commands."""

    def __init__(self, crontab=""):
        self.calls = []
        self.crontab = crontab
        self.xml = None

    def __call__(self, args, input=None, **kw):
        self.calls.append(list(args))
        out, code = "", 0
        if args[:2] == ["crontab", "-l"]:
            out, code = self.crontab, (0 if self.crontab else 1)
        elif args[:2] == ["crontab", "-"]:
            self.crontab = input
        elif args[:2] == ["schtasks", "/Create"]:
            self.xml = Path(args[args.index("/XML") + 1]).read_text(encoding="utf-16")
        return subprocess.CompletedProcess(args, code, out, "")


@pytest.mark.parametrize("text, expected", [
    ("07:00", (7, 0)), ("7:00", (7, 0)), ("19:30", (19, 30)), ("7", (7, 0)), ("7 pm", (19, 0)),
    ("12am", (0, 0)), ("12:15 pm", (12, 15)), ("06.45", (6, 45)),
])
def test_parse_time(text, expected):
    assert parse_time(text) == expected


@pytest.mark.parametrize("text", ["", "25:00", "7:75", "noon", "13 pm"])
def test_parse_time_rejects(text):
    with pytest.raises(ScheduleError):
        parse_time(text)


def test_schedule_validation_and_description():
    assert Schedule("weekly", "fri", "7:00").validate() == Schedule("weekly", "Friday", "07:00")
    assert Schedule("weekly", "Friday", "07:00").describe() == "Every Friday at 07:00"
    assert Schedule("daily", "", "18:30").validate().describe() == "Every day at 18:30"
    assert Schedule("off").describe() == "Off"
    for bad in (Schedule("hourly"), Schedule("weekly", "Funday"), Schedule("daily", "Monday", "nope")):
        with pytest.raises(ScheduleError):
            bad.validate()


def test_launchd_plist_uses_launchd_weekday_numbers():
    cmd = ["/py", "run_scraper.py", "--notify"]
    plist = sch.launchd_plist(Schedule("weekly", "Friday", "07:05"), cmd, Path("/tmp/log"))
    assert plist["StartCalendarInterval"] == {"Hour": 7, "Minute": 5, "Weekday": 5}
    assert sch.launchd_plist(Schedule("weekly", "Sunday", "07:00"), cmd, Path("/l"))["StartCalendarInterval"]["Weekday"] == 0
    assert "Weekday" not in sch.launchd_plist(Schedule("daily", "Friday", "07:00"), cmd, Path("/l"))["StartCalendarInterval"]
    assert plist["ProgramArguments"] == cmd and plist["Label"] == sch.LABEL and plist["RunAtLoad"] is False


def test_macos_install_and_remove(tmp_path):
    run = FakeRun()
    msg = sch.install(Schedule("weekly", "Friday", "07:00"), platform="darwin", run=run, home=tmp_path)
    assert msg == "Scheduled: Every Friday at 07:00."
    path = tmp_path / "Library" / "LaunchAgents" / f"{sch.LABEL}.plist"
    data = plistlib.loads(path.read_bytes())
    assert data["StartCalendarInterval"] == {"Hour": 7, "Minute": 0, "Weekday": 5}
    assert data["ProgramArguments"][-1] == "--notify" and data["ProgramArguments"][1].endswith("run_scraper.py")
    assert any(c[:2] == ["launchctl", "bootstrap"] for c in run.calls)
    assert sch.is_installed("darwin", run, tmp_path)
    assert sch.install(Schedule("off"), platform="darwin", run=run, home=tmp_path) == "Automatic runs are off."
    assert not path.exists() and not sch.is_installed("darwin", run, tmp_path)


def test_macos_job_runs_and_logs_outside_the_app_folder(tmp_path):
    """The app folder may be on the Desktop, which macOS keeps background jobs out of."""
    sch.install(Schedule("daily", "Friday", "07:00"), platform="darwin", run=FakeRun(), home=tmp_path)
    data = plistlib.loads((tmp_path / "Library" / "LaunchAgents" / f"{sch.LABEL}.plist").read_bytes())
    logs = tmp_path / "Library" / "Logs" / "S1 Event Scraper"
    assert data["WorkingDirectory"] == str(logs) and data["StandardErrorPath"] == str(logs / "scheduled_runs.log")
    assert logs.is_dir() and data["EnvironmentVariables"]["PYTHONIOENCODING"] == "utf-8"


def test_windows_task_xml_is_valid_and_catches_up_missed_runs():
    xml_text = sch.task_xml(Schedule("weekly", "Friday", "07:00"), [r"C:\app\pythonw.exe", r"C:\my app\run.py", "--notify"],
                            today=date(2026, 10, 8))
    doc = xml.dom.minidom.parseString(xml_text.replace('encoding="UTF-16"', 'encoding="UTF-8"').encode("utf-8"))
    text = lambda tag: doc.getElementsByTagName(tag)[0].firstChild.data  # noqa: E731
    assert text("StartBoundary") == "2026-10-09T07:00:00"
    assert doc.getElementsByTagName("Friday") and text("StartWhenAvailable") == "true"
    assert text("Command") == r"C:\app\pythonw.exe" and text("Arguments") == r'"C:\my app\run.py" --notify'
    daily = sch.task_xml(Schedule("daily", "Friday", "18:30"), ["p", "r"], today=date(2026, 10, 8))
    assert "<DaysInterval>1</DaysInterval>" in daily and "T18:30:00" in daily


def test_windows_install_uses_task_scheduler():
    run = FakeRun()
    sch.install(Schedule("daily", "Monday", "06:00"), platform="win32", run=run)
    create = next(c for c in run.calls if c[:2] == ["schtasks", "/Create"])
    assert create[create.index("/TN") + 1] == sch.TASK_NAME and "ScheduleByDay" in run.xml
    sch.remove("win32", run)
    assert ["schtasks", "/Delete", "/F", "/TN", sch.TASK_NAME] in run.calls


def test_linux_cron_keeps_other_entries_and_replaces_ours(settings_path, tmp_path):
    run = FakeRun(crontab="0 1 * * * backup.sh\n")
    sch.install(Schedule("weekly", "Monday", "07:30"), settings_path, platform="linux", run=run)
    sch.install(Schedule("weekly", "Friday", "08:00"), settings_path, platform="linux", run=run)
    lines = run.crontab.strip().splitlines()
    assert lines[0] == "0 1 * * * backup.sh" and len(lines) == 2
    assert lines[1].startswith("0 8 * * 5 cd ") and lines[1].endswith(sch.CRON_MARK)
    assert str(tmp_path / "data" / "logs" / "scheduled_runs.log") in lines[1]
    assert sch.is_installed("linux", run)
    sch.remove("linux", run)
    assert run.crontab.strip() == "0 1 * * * backup.sh"


def test_notify_only_on_macos():
    run = FakeRun()
    sch.notify("S1", 'Done "12" new', platform="linux", run=run)
    assert run.calls == []
    sch.notify("S1", 'Done "12" new', platform="darwin", run=run)
    assert run.calls[0][0] == "osascript" and '\\"12\\"' in run.calls[0][2]


@pytest.fixture
def settings_path(tmp_path):
    (tmp_path / "work").mkdir()
    path = tmp_path / "settings.json"
    Settings(workbook_path=str(tmp_path / "work" / "cal.xlsx"), data_dir=str(tmp_path / "data")).save(path)
    return path


def test_access_check_passes_and_reports(settings_path, tmp_path):
    result = tmp_path / "result.json"
    assert sch.write_check_result(settings_path, result) == 0
    data = json.loads(result.read_text(encoding="utf-8"))
    assert data["ok"] is True and data["problems"] == [] and data["python"]
    assert not list((tmp_path / "work").iterdir())          # the write test leaves nothing behind


def test_access_check_names_blocked_folders(settings_path, tmp_path, monkeypatch):
    real_listdir = os.listdir

    def listdir(folder):
        if str(folder) == str(tmp_path / "work"):
            raise PermissionError(1, "Operation not permitted")
        return real_listdir(folder)

    monkeypatch.setattr(sch.os, "listdir", listdir)
    problems = sch.access_problems(settings_path)
    assert len(problems) == 1 and "workbook folder" in problems[0] and "not permitted" in problems[0]
    assert sch.is_blocked_by_macos(problems[0])
    Settings(workbook_path=str(tmp_path / "missing" / "cal.xlsx")).save(settings_path)
    monkeypatch.undo()
    assert "workbook folder" in sch.access_problems(settings_path)[0]


def test_cli_check_access(settings_path, tmp_path):
    from s1scraper.cli import main

    result = tmp_path / "result.json"
    assert main(["--settings", str(settings_path), "--check-access", str(result)]) == 0
    assert json.loads(result.read_text(encoding="utf-8"))["ok"] is True


def test_background_check_runs_as_a_one_off_launchd_job(settings_path, tmp_path):
    run, home = FakeRun(), tmp_path / "home"
    chk = sch.start_check(settings_path, run=run, home=home)
    job = plistlib.loads(chk.plist_path.read_bytes())
    assert job["Label"] == sch.CHECK_LABEL and job["RunAtLoad"] is True
    assert job["ProgramArguments"][-2:] == ["--check-access", str(chk.result_path)]
    assert "--settings" in job["ProgramArguments"] and "--notify" not in job["ProgramArguments"]
    assert any(c[:2] == ["launchctl", "bootstrap"] for c in run.calls)
    assert sch.check_outcome(chk) is None                      # still running
    sch.write_check_result(settings_path, chk.result_path)     # what the job does
    assert sch.check_outcome(chk) == (True, "")
    sch.end_check(chk, run)
    assert not chk.plist_path.exists() and run.calls[-1] == ["launchctl", "bootout", f"gui/{sch._uid()}/{sch.CHECK_LABEL}"]


def test_background_check_spots_python_refused_by_macos(settings_path, tmp_path):
    chk = sch.start_check(settings_path, run=FakeRun(), home=tmp_path)
    chk.log_path.write_text("Python: can't open file '/Users/x/Desktop/S1/run_scraper.py': "
                            "[Errno 1] Operation not permitted\n", encoding="utf-8")
    passed, details = sch.check_outcome(chk)
    assert not passed and sch.is_blocked_by_macos(details)
    help_text = sch.mac_access_help(details)
    assert "Full Disk Access" in help_text and "home folder" in help_text and "Operation not permitted" in help_text


def test_harmless_warnings_do_not_fail_the_check(settings_path, tmp_path):
    chk = sch.start_check(settings_path, run=FakeRun(), home=tmp_path)
    chk.log_path.write_text("NotOpenSSLWarning: urllib3 v2 only supports OpenSSL 1.1.1+\n", encoding="utf-8")
    assert sch.check_outcome(chk) is None


def _launchd_or_skip(fn):
    try:
        return fn()
    except ScheduleError as exc:      # no GUI login session on this machine
        pytest.skip(f"launchd not available here: {exc}")


@pytest.mark.skipif(not (sys.platform == "darwin" and os.environ.get("GITHUB_ACTIONS")),
                    reason="real launchd check runs on the macOS CI machine only")
def test_real_macos_launchd(settings_path):
    chk = _launchd_or_skip(lambda: sch.start_check(settings_path))
    try:
        deadline = time.time() + 60
        while sch.check_outcome(chk) is None and time.time() < deadline:
            time.sleep(1)
        outcome = sch.check_outcome(chk)
        log = chk.log_path.read_text(errors="replace") if chk.log_path.exists() else ""
        assert outcome == (True, ""), f"outcome={outcome!r} log={log!r}"
    finally:
        sch.end_check(chk)
    try:
        assert _launchd_or_skip(lambda: sch.install(Schedule("weekly", "Friday", "07:00"), settings_path))
        assert sch.is_installed()
        assert subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{sch.LABEL}"],
                              capture_output=True).returncode == 0
    finally:
        sch.remove()
    assert not sch.is_installed()


@pytest.mark.skipif(not (sys.platform.startswith("win") and os.environ.get("GITHUB_ACTIONS")),
                    reason="real Task Scheduler check runs on the Windows CI machine only")
def test_real_windows_task_scheduler():
    try:
        assert sch.install(Schedule("weekly", "Friday", "07:00")).startswith("Scheduled")
        assert sch.is_installed()
    finally:
        sch.remove()
    assert not sch.is_installed()
