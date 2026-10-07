"""Drives the real Tk window (needs a display; on Linux run under xvfb-run)."""

import os
import sys
import time
from datetime import datetime

import pytest

tk = pytest.importorskip("tkinter")


def _display_available():
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY"))


pytestmark = pytest.mark.skipif(not _display_available(), reason="no display (use xvfb-run)")


@pytest.fixture(scope="module")
def tk_root():
    """One Tk interpreter for the whole module: creating and destroying several
    in one process is unreliable on macOS."""
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def app(tk_root, tmp_path, monkeypatch):
    from s1scraper import gui
    from s1scraper.config import Settings

    settings_path = tmp_path / "settings.json"
    Settings(workbook_path=str(tmp_path / "cal.xlsx"), data_dir=str(tmp_path / "data")).save(settings_path)
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: False)
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **k: None)
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda *a, **k: None)
    window = tk.Toplevel(tk_root)
    window.withdraw()
    application = gui.App(window, settings_path)
    yield application
    window.after_cancel(application._poll_job)
    window.destroy()


def pump(app, until, timeout=10):
    """Run the normal Tk event loop (as the real app does) until ``until()`` or timeout."""
    window = app.root
    deadline = time.time() + timeout

    def check():
        if until() or time.time() > deadline:
            window.quit()
        else:
            window.after(50, check)

    window.after(50, check)
    window.mainloop()
    return until()


def test_form_round_trips_settings(app):
    assert len(app.city_vars) == 7 and "bookmyshow" in app.source_vars
    app.preset_var.set("2 weeks")
    app.city_vars["Pune"].set(False)
    app.source_vars["meetup"].set(False)
    app.speed_var.set("Gentle (safest, slowest)")
    s = app._read_form()
    assert s.window_preset == "2 weeks" and "Pune" not in s.cities and len(s.cities) == 6
    assert s.sources["meetup"] is False and s.speed == "gentle"


def test_custom_window_shows_validation(app):
    app.mode_var.set("custom")
    app.from_var.set("2026-12-01")
    app.to_var.set("2026-11-01")
    app._sync_window()
    assert "before" in app.window_label.cget("text")
    app.to_var.set("2026-12-31")
    assert "01 Dec 2026 to 31 Dec 2026" in app.window_label.cget("text")


def test_run_button_runs_in_background_and_reports(app, monkeypatch):
    from s1scraper import pipeline
    from s1scraper.pipeline import RunReport

    class FakeRunner:
        def __init__(self, settings, log_fn=None, progress_fn=None, stop_event=None, **kw):
            self.log_fn, self.progress_fn = log_fn, progress_fn

        def run(self):
            self.log_fn("BookMyShow · Mumbai: 12 listings")
            self.progress_fn(0.5, "Reading event pages")
            rep = RunReport(started=datetime(2026, 10, 8, 10), cities=["Mumbai"], sources=["BookMyShow"])
            rep.window_start = rep.window_end = datetime(2026, 10, 8).date()
            rep.saved_to, rep.new_in_master = "cal.xlsx", 12
            return rep

    monkeypatch.setattr(pipeline, "Runner", FakeRunner)
    app.start_run()
    assert pump(app, lambda: "Done" in app.status_var.get())
    log = app.log.get("1.0", "end")
    assert "12 listings" in log and "12 new events appended" in log
    assert str(app.run_btn.cget("state")) == "normal"


def test_validation_error_does_not_start_a_run(app, monkeypatch):
    for var in app.city_vars.values():
        var.set(False)
    app.start_run()
    assert app.worker is None
