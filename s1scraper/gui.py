"""Desktop window: pick the window, cities and sources, press Run, Excel updates.

Built on Tkinter (included with Python), so nothing extra to install.
"""

from __future__ import annotations

import os
import platform
import queue
import subprocess
import sys
import threading
import time
import traceback
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import APP_NAME, __version__
from .cities import CITIES
from .config import CUSTOM, SETTINGS_FILE, WINDOW_PRESETS, Settings, SettingsError, compute_window
from .schedule import DAYS, FREQUENCIES, Schedule, ScheduleError, is_blocked_by_macos, mac_access_help
from .schedule import check_needed as schedule_check_needed
from .schedule import check_outcome as schedule_check_outcome
from .schedule import end_check as end_schedule_check
from .schedule import install as install_schedule
from .schedule import is_installed as schedule_is_installed
from .schedule import start_check as start_schedule_check
from .sources import ALL_SOURCES, GROUP_LABELS

SPEEDS = {"Gentle (safest, slowest)": "gentle", "Normal (recommended)": "normal", "Fast": "fast"}
# Whole run; when it is near, later dates keep what their listing showed (nearest dates are read first)
TIME_LIMITS = {"30 min": 30.0, "45 min": 45.0, "1 hour": 60.0, "1.5 hours": 90.0, "2 hours": 120.0,
               "No limit": 0.0}


def limit_label(minutes: float) -> str:
    m = float(minutes or 0)
    return next((label for label, v in TIME_LIMITS.items() if v == m), f"{m:g} min")


def open_path(path: Path) -> None:
    """Open a file or folder with the computer's default program."""
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class App:
    def __init__(self, root: tk.Tk, settings_path: Optional[Path] = None):
        self.root = root
        self.settings_path = settings_path
        try:
            self.settings = Settings.load(settings_path)
        except SettingsError as exc:
            messagebox.showwarning(APP_NAME, f"{exc}\nDefault settings will be used.")
            self.settings = Settings()
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self._check = None            # background test of the automatic run (macOS)
        self._check_job = None
        self.worker: Optional[threading.Thread] = None
        self.stop_event = threading.Event()

        root.title(f"{APP_NAME} · StepOne")
        root.minsize(860, 640)
        root.geometry("980x760")
        self._style()
        self._build()
        self._load_into_form()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll_job = root.after(100, self._poll)

    # --------------------------------------------------------------- layout
    def _style(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("Sub.TLabel", foreground="#666666")
        style.configure("Section.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("Run.TButton", font=("Segoe UI", 11, "bold"), padding=(18, 6))

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)

        head = ttk.Frame(outer)
        head.pack(fill="x")
        ttk.Label(head, text=APP_NAME, style="Title.TLabel").pack(side="left")
        ttk.Label(head, text=f"   StepOne rolling events calendar · v{__version__}", style="Sub.TLabel").pack(
            side="left", pady=(6, 0))

        # 1. workbook
        box = ttk.LabelFrame(outer, text=" 1. Excel workbook to update ", style="Section.TLabelframe", padding=8)
        box.pack(fill="x", pady=(10, 4))
        self.workbook_var = tk.StringVar()
        ttk.Entry(box, textvariable=self.workbook_var).pack(side="left", fill="x", expand=True)
        ttk.Button(box, text="Browse…", command=self.browse).pack(side="left", padx=(6, 0))
        ttk.Button(box, text="Open", command=self.open_workbook).pack(side="left", padx=(6, 0))

        # 2. window
        box = ttk.LabelFrame(outer, text=" 2. Date window ", style="Section.TLabelframe", padding=8)
        box.pack(fill="x", pady=4)
        self.mode_var = tk.StringVar(value="preset")
        self.preset_var = tk.StringVar(value="1 month")
        ttk.Radiobutton(box, text="Next", variable=self.mode_var, value="preset",
                        command=self._sync_window).grid(row=0, column=0, sticky="w")
        self.preset_box = ttk.Combobox(box, textvariable=self.preset_var, values=list(WINDOW_PRESETS),
                                       state="readonly", width=12)
        self.preset_box.grid(row=0, column=1, sticky="w", padx=(4, 18))
        self.preset_box.bind("<<ComboboxSelected>>", lambda e: self._sync_window())
        ttk.Radiobutton(box, text="From", variable=self.mode_var, value="custom",
                        command=self._sync_window).grid(row=0, column=2, sticky="w")
        self.from_var, self.to_var = tk.StringVar(), tk.StringVar()
        self.from_entry = ttk.Entry(box, textvariable=self.from_var, width=12)
        self.from_entry.grid(row=0, column=3, padx=4)
        ttk.Label(box, text="to").grid(row=0, column=4)
        self.to_entry = ttk.Entry(box, textvariable=self.to_var, width=12)
        self.to_entry.grid(row=0, column=5, padx=4)
        ttk.Label(box, text="(YYYY-MM-DD)", style="Sub.TLabel").grid(row=0, column=6, padx=(2, 12))
        self.window_label = ttk.Label(box, text="", style="Sub.TLabel")
        self.window_label.grid(row=0, column=7, sticky="w")
        for var in (self.from_var, self.to_var):
            var.trace_add("write", lambda *a: self._sync_window())

        # 3. cities
        box = ttk.LabelFrame(outer, text=" 3. Cities ", style="Section.TLabelframe", padding=8)
        box.pack(fill="x", pady=4)
        self.city_vars: Dict[str, tk.BooleanVar] = {}
        for i, city in enumerate(CITIES):
            var = tk.BooleanVar(value=True)
            self.city_vars[city.name] = var
            ttk.Checkbutton(box, text=city.name, variable=var).grid(row=0, column=i, sticky="w", padx=(0, 10))
        links = ttk.Frame(box)
        links.grid(row=0, column=len(CITIES), padx=(10, 0))
        ttk.Button(links, text="All", width=5, command=lambda: self._set_all(self.city_vars, True)).pack(side="left")
        ttk.Button(links, text="None", width=5, command=lambda: self._set_all(self.city_vars, False)).pack(
            side="left", padx=(4, 0))

        # 4. sources
        box = ttk.LabelFrame(outer, text=" 4. Sources ", style="Section.TLabelframe", padding=8)
        box.pack(fill="x", pady=4)
        self.source_vars: Dict[str, tk.BooleanVar] = {}
        row = 0
        for group, label in GROUP_LABELS.items():
            members = [s for s in ALL_SOURCES if s.group == group]
            if not members:
                continue
            ttk.Label(box, text=label + ":", style="Sub.TLabel").grid(row=row, column=0, sticky="nw", padx=(0, 8))
            frame = ttk.Frame(box)
            frame.grid(row=row, column=1, sticky="w")
            for i, src in enumerate(members):
                var = tk.BooleanVar(value=src.enabled_by_default)
                self.source_vars[src.key] = var
                cb = ttk.Checkbutton(frame, text=src.name, variable=var)
                cb.grid(row=i // 4, column=i % 4, sticky="w", padx=(0, 14))
            row += 1

        # options
        box = ttk.LabelFrame(outer, text=" Options ", style="Section.TLabelframe", padding=8)
        box.pack(fill="x", pady=4)
        ttk.Label(box, text="Speed:").pack(side="left")
        self.speed_var = tk.StringVar()
        ttk.Combobox(box, textvariable=self.speed_var, values=list(SPEEDS), state="readonly",
                     width=22).pack(side="left", padx=(4, 14))
        ttk.Label(box, text="Time limit:").pack(side="left")
        self.limit_var = tk.StringVar()
        ttk.Combobox(box, textvariable=self.limit_var, values=list(TIME_LIMITS), state="readonly",
                     width=9).pack(side="left", padx=(4, 14))
        self.browser_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(box, text="Use Chrome/Edge when needed (works out of sight)",
                        variable=self.browser_var).pack(side="left")

        # actions
        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=(10, 4))
        self.run_btn = ttk.Button(bar, text="▶  Run scraper", style="Run.TButton", command=self.start_run)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(bar, text="■  Stop", command=self.stop_run, state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Automatic runs…", command=self.open_schedule).pack(side="left", padx=(24, 0))
        self.schedule_label = ttk.Label(bar, text="", style="Sub.TLabel")
        self.schedule_label.pack(side="left", padx=(8, 0))
        self.freq_var = tk.StringVar(value="Off")       # the Automatic runs window edits these
        self.day_var = tk.StringVar(value="Friday")
        self.time_var = tk.StringVar(value="07:00")
        self.schedule_win: Optional[tk.Toplevel] = None
        ttk.Button(bar, text="Logs folder", command=self.open_logs).pack(side="right")
        self.diag_btn = ttk.Button(bar, text="Health check", command=self.start_diagnostics)
        self.diag_btn.pack(side="right", padx=(0, 8))

        self.progress = ttk.Progressbar(outer, mode="determinate", maximum=1000)
        self.progress.pack(fill="x", pady=(6, 2))
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(outer, textvariable=self.status_var, style="Sub.TLabel").pack(fill="x")

        logbox = ttk.Frame(outer)
        logbox.pack(fill="both", expand=True, pady=(6, 0))
        # Colours are set in full: with only the background set, macOS Dark Mode draws white text on it
        mono = ("Menlo", 11) if sys.platform == "darwin" else ("Consolas", 9)
        self.log = tk.Text(logbox, height=12, wrap="word", font=mono, state="disabled",
                           background="#FBFAF7", foreground="#1D2420", insertbackground="#1D2420",
                           selectbackground="#CFE3DD", selectforeground="#1D2420", relief="solid", borderwidth=1)
        scroll = ttk.Scrollbar(logbox, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    # ------------------------------------------------------------- settings
    def _load_into_form(self) -> None:
        s = self.settings
        self.workbook_var.set(str(s.workbook_path))
        if s.window_preset == CUSTOM:
            self.mode_var.set("custom")
            self.from_var.set(s.window_start)
            self.to_var.set(s.window_end)
        else:
            self.mode_var.set("preset")
            self.preset_var.set(s.window_preset if s.window_preset in WINDOW_PRESETS else "1 month")
            today = date.today()
            self.from_var.set(today.isoformat())
            self.to_var.set((today + timedelta(days=30)).isoformat())
        chosen = {c.lower() for c in s.cities}
        for name, var in self.city_vars.items():
            var.set(name.lower() in chosen)
        for key, var in self.source_vars.items():
            var.set(bool(s.sources.get(key, var.get())))
        self.speed_var.set(next((k for k, v in SPEEDS.items() if v == s.speed), "Normal (recommended)"))
        self.limit_var.set(limit_label(s.time_limit_minutes))
        self.browser_var.set(bool(s.use_browser))
        self.freq_var.set(FREQUENCIES.get(s.schedule_frequency, "Off"))
        self.day_var.set(s.schedule_day if s.schedule_day in DAYS else "Friday")
        self.time_var.set(s.schedule_time or "07:00")
        self._sync_schedule()
        self._sync_window()

    def _read_form(self) -> Settings:
        s = self.settings
        s.workbook_path = self.workbook_var.get().strip()
        if self.mode_var.get() == "custom":
            s.window_preset = CUSTOM
            s.window_start, s.window_end = self.from_var.get().strip(), self.to_var.get().strip()
        else:
            s.window_preset = self.preset_var.get()
        s.cities = [name for name, var in self.city_vars.items() if var.get()]
        s.sources = {key: var.get() for key, var in self.source_vars.items()}
        s.speed = SPEEDS.get(self.speed_var.get(), "normal")
        s.time_limit_minutes = TIME_LIMITS.get(self.limit_var.get(), s.time_limit_minutes)
        s.use_browser = self.browser_var.get()
        s.schedule_frequency = next((k for k, v in FREQUENCIES.items() if v == self.freq_var.get()), "off")
        s.schedule_day = self.day_var.get()
        s.schedule_time = self.time_var.get().strip()
        return s

    def _sync_window(self) -> None:
        custom = self.mode_var.get() == "custom"
        self.preset_box.configure(state="disabled" if custom else "readonly")
        for entry in (self.from_entry, self.to_entry):
            entry.configure(state="normal" if custom else "disabled")
        try:
            if custom:
                start, end = compute_window(CUSTOM, self.from_var.get(), self.to_var.get())
            else:
                start, end = compute_window(self.preset_var.get())
            self.window_label.configure(text=f"→ {start:%d %b %Y} to {end:%d %b %Y}", foreground="#666666")
        except SettingsError as exc:
            self.window_label.configure(text=str(exc), foreground="#B03A2E")

    @staticmethod
    def _set_all(vars_: Dict[str, tk.BooleanVar], value: bool) -> None:
        for var in vars_.values():
            var.set(value)

    # -------------------------------------------------------------- actions
    def browse(self) -> None:
        current = Path(self.workbook_var.get() or ".")
        path = filedialog.askopenfilename(
            title="Choose the events workbook", filetypes=[("Excel workbook", "*.xlsx"), ("All files", "*.*")],
            initialdir=str(current.parent if current.parent.exists() else Path.home()))
        if path:
            self.workbook_var.set(path)

    def open_workbook(self) -> None:
        path = self.settings.resolve(self.workbook_var.get().strip() or self.settings.workbook_path)
        if path.exists():
            open_path(path)
        else:
            messagebox.showinfo(APP_NAME, f"{path.name} does not exist yet - it is created on the first run.")

    def open_logs(self) -> None:
        folder = self.settings.data_path / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        open_path(folder)

    def open_schedule(self) -> None:
        """Small window for automatic runs (kept out of the main form so it fits small screens)."""
        if self.schedule_win is not None and self.schedule_win.winfo_exists():
            self.schedule_win.deiconify()
            self.schedule_win.lift()
            return
        win = self.schedule_win = tk.Toplevel(self.root)
        win.title("Automatic runs")
        win.transient(self.root)
        win.resizable(False, False)
        frame = ttk.Frame(win, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, wraplength=440, justify="left",
                  text="Run the scraper on its own. " + self.SCHEDULE_USES).pack(anchor="w")
        row = ttk.Frame(frame)
        row.pack(anchor="w", pady=(12, 0))
        self.freq_box = ttk.Combobox(row, textvariable=self.freq_var, values=list(FREQUENCIES.values()),
                                     state="readonly", width=12)
        self.freq_box.pack(side="left")
        self.freq_box.bind("<<ComboboxSelected>>", lambda e: self._sync_schedule())
        ttk.Label(row, text="on").pack(side="left", padx=(8, 4))
        self.day_box = ttk.Combobox(row, textvariable=self.day_var, values=DAYS, state="readonly", width=11)
        self.day_box.pack(side="left")
        ttk.Label(row, text="at").pack(side="left", padx=(8, 4))
        self.time_entry = ttk.Entry(row, textvariable=self.time_var, width=7)
        self.time_entry.pack(side="left")
        ttk.Label(row, text="(24-hour, e.g. 07:00)", style="Sub.TLabel").pack(side="left", padx=(8, 0))
        self.schedule_status = ttk.Label(frame, text="", wraplength=440, justify="left")
        self.schedule_status.pack(anchor="w", pady=(12, 0))
        ttk.Label(frame, text=self.SCHEDULE_NOTE, wraplength=440, justify="left",
                  style="Sub.TLabel").pack(anchor="w", pady=(8, 0))
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="Close", command=win.destroy).pack(side="right")
        ttk.Button(buttons, text="Save schedule", command=self.save_schedule).pack(side="right", padx=(0, 8))
        self._sync_schedule()

    def _sync_schedule(self, status: Optional[str] = None) -> None:
        """Update the line next to the button and, if open, the Automatic runs window."""
        s = self.settings
        if status is None:
            if s.schedule_frequency == "off":
                status, short = "Off: runs only when you press Run scraper.", "Off"
            else:
                try:
                    short = Schedule(s.schedule_frequency, s.schedule_day, s.schedule_time).validate().describe()
                    status = f"Active: {short}."
                except ScheduleError:
                    status = short = "Settings invalid - pick them again and Save schedule."
                if not schedule_is_installed():
                    status += " Not set up on this computer yet - press Save schedule."
                    short = "not set up on this computer"
        else:
            short = status
        self.schedule_label.configure(text=short if len(short) <= 40 else short[:38] + "…")
        if self.schedule_win is not None and self.schedule_win.winfo_exists():
            freq = self.freq_var.get()
            self.day_box.configure(state="readonly" if freq == FREQUENCIES["weekly"] else "disabled")
            self.time_entry.configure(state="disabled" if freq == FREQUENCIES["off"] else "normal")
            self.schedule_status.configure(text=status)

    def save_schedule(self) -> None:
        """Register the automatic run with this computer, using the Automatic runs window's choices."""
        settings = self._read_form()
        try:
            sched = Schedule(settings.schedule_frequency, settings.schedule_day, settings.schedule_time).validate()
            if sched.frequency != "off":
                settings.validate()
        except (ScheduleError, SettingsError) as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self._dialog_parent())
            return
        settings.schedule_day, settings.schedule_time = sched.day, sched.time
        try:
            settings.save(self.settings_path)
            message = install_schedule(sched, self.settings_path or SETTINGS_FILE)
        except (ScheduleError, OSError) as exc:
            messagebox.showerror(APP_NAME, f"Could not set up the automatic run:\n\n{exc}", parent=self._dialog_parent())
            return
        self.time_var.set(sched.time)
        self._sync_schedule(message)
        self.append_log(message)
        if sched.frequency == "off":
            return
        if schedule_check_needed():
            self._start_background_check(message)
        else:
            messagebox.showinfo(APP_NAME, f"{message}\n\n{self.SCHEDULE_USES} {self.SCHEDULE_NOTE}",
                                parent=self._dialog_parent())

    SCHEDULE_USES = "It uses the date window, cities, sources and workbook chosen in the main window."
    SCHEDULE_NOTE = ("Keep the computer on (a run missed while asleep happens when it wakes) and the "
                     "workbook closed in Excel at that time.")
    CHECK_SECONDS = 120

    def _start_background_check(self, message: str) -> None:
        """macOS: start the automatic run's access test now, while someone is here to see it."""
        self._cancel_background_check()
        try:
            self._check = start_schedule_check(self.settings_path or SETTINGS_FILE)
        except (ScheduleError, OSError) as exc:
            messagebox.showwarning(APP_NAME, f"{message}\n\nThe background test could not start: {exc}",
                                   parent=self._dialog_parent())
            return
        self._sync_schedule("Testing in the background…")
        self.append_log("Testing the automatic run in the background. If macOS asks whether Python may "
                        "access a folder, click Allow.")
        deadline = time.time() + self.CHECK_SECONDS

        def poll() -> None:
            self._check_job = None
            outcome = schedule_check_outcome(self._check)
            if outcome is None and time.time() < deadline:
                self._check_job = self.root.after(1000, poll)
                return
            self._cancel_background_check()
            self._report_background_check(message, outcome)

        self._check_job = self.root.after(1000, poll)

    def _cancel_background_check(self) -> None:
        if self._check_job is not None:
            self.root.after_cancel(self._check_job)
            self._check_job = None
        if self._check is not None:
            try:
                end_schedule_check(self._check)
            except OSError:
                pass
            self._check = None

    def _report_background_check(self, message: str, outcome) -> None:
        if outcome is None:
            outcome = (False, f"The test did not report back within {self.CHECK_SECONDS // 60} minutes.")
        passed, details = outcome
        if passed:
            self._sync_schedule(message.replace("Scheduled: ", "").rstrip(".") + " (tested)")
            self.append_log("Background test passed: the automatic run can open the app and the workbook.")
            messagebox.showinfo(APP_NAME, message + "\n\nBackground test passed: the automatic run can open the "
                                f"app and the workbook.\n\n{self.SCHEDULE_USES} {self.SCHEDULE_NOTE}",
                                parent=self._dialog_parent())
            return
        self._sync_schedule("Saved, but the background test failed")
        self.append_log(f"Background test failed: {details}")
        text = mac_access_help(details) if is_blocked_by_macos(details) else (
            f"The automatic run is saved, but it would fail:\n\n{details}\n\nFix this, then press Save schedule again.")
        messagebox.showwarning(APP_NAME, text, parent=self._dialog_parent())

    def _dialog_parent(self) -> tk.Misc:
        win = self.schedule_win
        return win if win is not None and win.winfo_exists() else self.root

    def _busy(self, busy: bool) -> None:
        self.run_btn.configure(state="disabled" if busy else "normal")
        self.diag_btn.configure(state="disabled" if busy else "normal")
        self.stop_btn.configure(state="normal" if busy else "disabled")

    def _prepare(self) -> Optional[Settings]:
        if self.worker and self.worker.is_alive():
            return None
        settings = self._read_form()
        try:
            settings.validate()
        except SettingsError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return None
        try:
            settings.save(self.settings_path)
        except OSError as exc:
            self.append_log(f"Could not save settings: {exc}")
        from .excel import is_locked

        path = settings.resolve(settings.workbook_path)
        if is_locked(path) and not messagebox.askokcancel(
                APP_NAME, f"{path.name} seems to be open in Excel.\n\nClose it before the run finishes, or the "
                          "results will be saved to a new file next to it.\n\nContinue?"):
            return None
        self.stop_event = threading.Event()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.progress["value"] = 0
        self._busy(True)
        return settings

    def start_run(self) -> None:
        settings = self._prepare()
        if settings is None:
            return
        self.status_var.set("Starting…")
        self.worker = threading.Thread(target=self._run_worker, args=(settings,), daemon=True)
        self.worker.start()

    def start_diagnostics(self) -> None:
        settings = self._prepare()
        if settings is None:
            return
        self.status_var.set("Health check: reading one city per source…")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.worker = threading.Thread(target=self._diag_worker, args=(settings,), daemon=True)
        self.worker.start()

    def stop_run(self) -> None:
        self.stop_event.set()
        self.status_var.set("Stopping after the current page…")
        self.stop_btn.configure(state="disabled")

    # ------------------------------------------------------------- workers
    def _run_worker(self, settings: Settings) -> None:
        from .cli import setup_logging
        from .pipeline import Runner

        try:
            log_path = setup_logging(settings)
            self.events.put(("log", f"Log file: {log_path}"))
            runner = Runner(settings, log_fn=lambda m: self.events.put(("log", m)),
                            progress_fn=lambda f, t: self.events.put(("progress", f, t)),
                            stop_event=self.stop_event)
            report = runner.run()
            self.events.put(("done", report))
        except Exception as exc:  # noqa: BLE001 - show any failure in the window
            self.events.put(("error", f"{type(exc).__name__}: {exc}", traceback.format_exc()))

    def _diag_worker(self, settings: Settings) -> None:
        from .cli import setup_logging
        from .diagnose import run_diagnostics

        try:
            setup_logging(settings)
            run_diagnostics(settings, lambda line: self.events.put(("log", line)))
            self.events.put(("diag_done",))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("error", f"{type(exc).__name__}: {exc}", traceback.format_exc()))

    # ------------------------------------------------------------- updates
    def append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _poll(self) -> None:
        try:
            while True:
                item = self.events.get_nowait()
                kind = item[0]
                if kind == "log":
                    self.append_log(item[1])
                elif kind == "progress":
                    self.progress.configure(mode="determinate")
                    self.progress["value"] = int(item[1] * 1000)
                    self.status_var.set(item[2])
                elif kind == "done":
                    self._finish(item[1])
                elif kind == "diag_done":
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.progress["value"] = 1000
                    self.status_var.set("Health check finished. The report is in the box below, and saved as "
                                        "report.txt in the diagnostics folder.")
                    self._busy(False)
                elif kind == "error":
                    self.progress.stop()
                    self._busy(False)
                    self.append_log(item[2])
                    self.status_var.set("Failed - see the log.")
                    messagebox.showerror(APP_NAME, f"The run failed:\n\n{item[1]}")
        except queue.Empty:
            pass
        self._poll_job = self.root.after(100, self._poll)

    def _finish(self, report) -> None:
        self._busy(False)
        lines = report.summary_lines()
        for line in lines:
            self.append_log(line)
        if report.cancelled:
            self.status_var.set("Stopped - the workbook was not changed.")
            return
        self.progress["value"] = 1000
        self.status_var.set(f"Done · {len(report.events)} events · {report.new_in_master} new in Master")
        text = "\n".join(lines[:2] + [ln for ln in lines if ln.startswith(("Master", "Warning"))])
        if report.saved_to and messagebox.askyesno(APP_NAME, text + "\n\nOpen the workbook now?"):
            open_path(Path(report.saved_to))

    def on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP_NAME, "A run is in progress. Stop it and close?"):
                return
            self.stop_event.set()
            self.worker.join(timeout=15)
        self._cancel_background_check()
        self.root.destroy()


def _dpi_aware() -> None:
    if platform.system() == "Windows":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def _bring_to_front(root: tk.Tk) -> None:
    """Show the window in front. On macOS a window started from Terminal
    otherwise opens behind the Terminal window."""
    root.deiconify()
    root.lift()
    try:
        root.attributes("-topmost", True)
        root.after(800, lambda: root.attributes("-topmost", False))
    except tk.TclError:
        pass
    root.focus_force()


def main(settings_path: Optional[Path] = None) -> int:
    _dpi_aware()
    root = tk.Tk()
    App(root, settings_path)
    root.after(200, lambda: _bring_to_front(root))
    root.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
