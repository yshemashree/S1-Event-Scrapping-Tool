import json
from datetime import date

import pytest

from s1scraper.cli import apply_args, build_parser
from s1scraper.config import CUSTOM, Settings, SettingsError, compute_window
from s1scraper.sources import SOURCE_BY_KEY


def args(*argv):
    return build_parser().parse_args(list(argv))


def test_window_presets_and_custom():
    today = date(2026, 10, 8)
    assert compute_window("1 week", today=today) == (today, date(2026, 10, 15))
    assert compute_window("1 month", today=today) == (today, date(2026, 11, 8))
    assert compute_window("6 months", today=today) == (today, date(2027, 4, 8))
    assert compute_window("10 days", today=today) == (today, date(2026, 10, 18))
    assert compute_window(CUSTOM, "2026-10-10", "31/12/2026") == (date(2026, 10, 10), date(2026, 12, 31))
    for bad in [("fortnight",), (CUSTOM, "2026-12-01", "2026-11-01"), (CUSTOM, "", "2026-11-01"),
                (CUSTOM, "2026-01-01", "2028-01-01")]:
        with pytest.raises(SettingsError):
            compute_window(*bad, today=today) if len(bad) == 1 else compute_window(*bad)


def test_cli_arguments_map_onto_settings():
    s = apply_args(Settings(), args("--window", "2w", "--cities", "Mumbai", "Bangalore", "--sources", "bookmyshow",
                                    "district", "--speed", "gentle", "--no-browser"))
    assert s.window_preset == "2 weeks" and s.cities == ["Mumbai", "Bangalore"]
    assert [k for k, v in s.sources.items() if v] == ["bookmyshow", "district"]
    assert s.speed == "gentle" and not s.use_browser
    s = apply_args(Settings(), args("--from", "2026-10-10", "--to", "2026-12-31"))
    assert (s.window_preset, s.window_start, s.window_end) == (CUSTOM, "2026-10-10", "2026-12-31")
    with pytest.raises(SettingsError):
        apply_args(Settings(), args("--sources", "nosuchsite"))
    assert Settings().time_limit_minutes == 60
    a = args("--sources", "district", "--dry-run", "--save-pages", "--find", "Sunidhi Chauhan", "A R Rahman")
    assert a.dry_run and a.save_pages and a.find == ["Sunidhi Chauhan", "A R Rahman"]
    assert apply_args(Settings(), args("--time-limit", "90")).time_limit_minutes == 90
    assert apply_args(Settings(), args("--max-minutes", "0")).time_limit_minutes == 0
    with pytest.raises(SettingsError):
        apply_args(Settings(), args("--time-limit", "-5"))


def test_settings_round_trip_and_new_sources_default_on(tmp_path):
    path = tmp_path / "settings.json"
    s = Settings(window_preset="3 months", cities=["Pune"])
    s.sources["district"] = False
    s.save(path)
    data = json.loads(path.read_text())
    del data["sources"]["fever"]                     # as if Fever were added in a later version
    path.write_text(json.dumps(data))
    loaded = Settings.load(path)
    assert loaded.window_preset == "3 months" and loaded.cities == ["Pune"]
    assert loaded.sources["district"] is False and loaded.sources["fever"] is True
    assert set(loaded.sources) == set(SOURCE_BY_KEY)


def test_validation():
    with pytest.raises(SettingsError):
        Settings(cities=["Paris"]).validate()
    with pytest.raises(SettingsError):
        Settings(sources={k: False for k in SOURCE_BY_KEY}).validate()
    with pytest.raises(SettingsError):
        Settings(workbook_path="calendar.csv").validate()
    with pytest.raises(SettingsError):
        Settings(time_limit_minutes="soon").validate()
    Settings().validate()


def test_the_browser_never_shows_from_saved_settings(tmp_path):
    path = tmp_path / "settings.json"
    s = Settings()
    s.show_browser = True                       # what an older version could save from the app
    s.save(path)
    assert Settings.load(path).show_browser is False
    assert apply_args(Settings.load(path), args("--show-browser")).show_browser is True   # one run, on request
