# S1 Event Scraper

Builds StepOne's rolling events calendar for **Mumbai, Pune, Delhi NCR, Bengaluru, Kolkata,
Ahmedabad and Chennai** from BookMyShow, District (Zomato), AllEvents and the other platforms
and venues in the workbook's Guide, and writes the results straight into the Excel workbook.

* Pick any window - next 1 week, 2 weeks, 1/2/3/6 months, or exact dates - and run it whenever needed.
* **Master Calander** is never edited: new events are only *added* below the last row.
* **Rolling Calendar** (plus one tab per city) is rebuilt on every run for the chosen window.
* Every row has the **Organizer** (or `Source: <platform>` when none is published) and a
  clickable **Link** to the ticket page.
* No accounts, no logins, no paid third-party scraping services: the scraper reads public pages
  at a gentle, human pace so the office IP is not flagged (see [Staying unblocked](#staying-unblocked)).

---

## Quick start (no terminal needed)

1. Install **Python 3.9 or newer** from <https://www.python.org/downloads/>
   (Windows: tick *"Add python.exe to PATH"* in the installer).
2. Double-click the launcher in this folder:
   * Windows: **`Run S1 Event Scraper.bat`**
   * macOS: **`Run S1 Event Scraper.command`** (first time: right-click → Open)

   The first start takes about a minute to set itself up (it needs internet). After that it opens
   straight away.
3. In the window:
   1. **Excel workbook** - *Browse…* to the calendar workbook (e.g. `Roling_Event_Calander_Friday_Updates.xlsx`).
      If the file does not exist yet, a new one is created in the same layout.
   2. **Date window** - *Next 1 month* (or any preset), or *From … to …*.
   3. **Cities** and **Sources** - all main ones are ticked by default.
   4. Press **▶ Run scraper**. Progress and a live log are shown; **■ Stop** cancels safely
      (the workbook is only written at the very end, so a stopped run changes nothing).
4. When it finishes, click *Yes* to open the workbook.

> Close the workbook in Excel before a run finishes. If it is still open, the results are saved
> to a copy next to it (`… (updated 2026-10-08 1530).xlsx`) and the window tells you so.

**How long a run takes.** Pages are read slowly on purpose. A 1-week window usually takes 10–30 minutes;
the first 6-month run can take 1–2 hours (BookMyShow and District are read in parallel). Later runs are
much faster because event pages already read in the last 10 days are reused.

**Safety nets.** A copy of the workbook is saved in `backups/` before every write. If a run finds
nothing because sites could not be reached (no internet, VPN, firewall), the workbook is left exactly
as it was. If only some sources fail, the run still completes and the Rolling Calendar's second row
says which sources are missing this time.

**Before the first real run, press _Health check_.** It reads one city per source, opens a few
event pages and prints what it could extract (dates, venues, prices, organisers) per source, and
saves the raw pages in `diagnostics/`. If a source shows 0 events or "BLOCKED", send that folder
over - the fix is usually a one-line URL change in `s1scraper/sources/`.

---

## What the workbook contains after a run

| Sheet | Behaviour |
|---|---|
| **Guide** | Untouched. |
| **Master Calander** | Append-only. Each run adds the events it has not seen before under a band like `▌ SCRAPER UPDATE · 08 OCT 2026 · 132 NEW EVENTS`, styled like the hand-typed rows. Existing rows and cells are never modified. Events already typed in by hand (same title, city and date) are not added again, and an event whose row you delete is not re-added on the next run. Two columns are added to the header if missing: **Link** and **Added On**. |
| **Rolling Calendar** | Rebuilt every run: all events in the window, grouped by month, sorted by date. Frozen header, filter arrows on every column (filter by city, tier, category, platform …), print-ready landscape layout. |
| **Mumbai … Chennai** | The same, one tab per city (switch off with *One tab per city*). |
| **Summary** | Live counts by city × tier, city × activity type and per platform (formulas over the Rolling Calendar), plus how tiers and categories are decided. |
| **Run Log** | One line per run: when, window, events found, new rows in Master, per-source counts, warnings. |

Columns (same order as the Master): **S.No. · Event Name · Activity Type · Tier · Start Date · End Date ·
City / Cities · Venue · Organizer · Price Range · Ticket Platform(s) · Notes · Region · Link · Added On**.

* **Tier** follows the Guide, using the lowest (entry) ticket price: Ultra Premium ₹5,000+,
  Luxury ₹1,550–5,000, Premium ₹500–1,549, Standard under ₹500 or free; *TBC* when no price is published.
  (`tier_basis` in `settings.json` can switch this to the highest or average ticket price.)
* **Activity Type** is one of the Guide's eight (Culture, Art, Music, F&B, Sports to Watch,
  Recreational Sports, Theatre, Comedy) plus *Workshops* and *Other*, decided from the platform's own
  category first, then the title and description.
* **Organizer** is what the event page publishes; if nothing is published it says
  `Source: BookMyShow` (grey italics). Ticketing platforms listed as "organiser" are not trusted.
* **Price Range** like `₹800 – ₹2,500`, `₹499 onwards` or `Free`. When a page lists ticket tiers
  (GA / Fan Pit / VIP …) they are spelled out in **Notes**, together with show time, multiple dates,
  language, age limit and duration.
* The same show listed on several platforms becomes one row; all platforms appear in
  **Ticket Platform(s)** and the Link points to the first one (BookMyShow, then District, …).

---

## Staying unblocked

Weekly (or on-demand) runs only stay safe if the sites never see anything unusual. The scraper:

* uses **no accounts or logins** - there is nothing that can be banned;
* sends **one request at a time per site** with a random 1.5–4 s gap (Gentle: 3–7 s) and a longer
  pause every few dozen pages, so traffic looks like one person browsing; different sites are
  read in parallel, each at its own pace;
* **backs off** on "slow down" answers (HTTP 429/503, honouring `Retry-After`) and, after three
  refusals in a row from a site, **stops contacting that site for the rest of the run** and says so
  in the log and Run Log - rather than pushing on and getting the IP flagged;
* **caches** event pages for 10 days, so next week's run only downloads events it has not seen;
* honours each site's **robots.txt** (can be switched off in `settings.json`, e.g. with the site's permission);
* opens pages in the **Edge/Chrome already installed** on the computer only when a page needs
  JavaScript (infinite-scroll listings), at the same gentle pace. *Show the browser* lets you watch it.

Good practice: run it from the office connection rather than a VPN, keep the default *Normal* speed,
and if the log ever says a site "refused … stopped contacting it", wait a few hours before the next run.

---

## Command line

```
python run_scraper.py                         # use the settings saved by the window
python run_scraper.py --window 1w             # next week   (1w 2w 1m 2m 3m 6m, or "10 days")
python run_scraper.py --from 2026-10-10 --to 2026-12-31
python run_scraper.py --cities Mumbai Pune --sources bookmyshow district
python run_scraper.py --workbook "D:\StepOne\Rolling_Event_Calander_Friday_Updates.xlsx"
python run_scraper.py --diagnose              # health check
python run_scraper.py --gui                   # open the window
python run_scraper.py --help
```

**Scheduling (optional):** on Windows, double-click `Schedule Weekly Run (Windows).bat` to run it
every Friday at 07:00 with the last saved settings. On macOS/Linux add a cron line such as
`0 7 * * 5 cd /path/to/S1-Event-Scrapping-Tool && .venv/bin/python run_scraper.py`.

---

## Sources

| Group | Sources | Notes |
|---|---|---|
| Main | BookMyShow, District (Zomato, includes former Paytm Insider), AllEvents | Events, plays and sports per city; BookMyShow "activities" (water parks, gaming zones) are left out unless `include_activities` is on. |
| More | Skillbox, SortMyScene, Eventbrite, Fever, Meetup | Online-only events are skipped. |
| Venues | NCPA, NMACC, Jio World Centre, Prithvi Theatre, Ranga Shankara, India Habitat Centre, India International Centre, Bangalore International Centre | Each venue's own programme pages. |
| Optional (off) | Explara, LBB, DreamSetGo, Platinumlist | Tick them in the window to include. |

Every source uses the same layered reader, so a site redesign rarely breaks it: schema.org event data
(what sites publish for Google), then the data the page ships for its own JavaScript, then the visible
listing cards and labelled facts ("Organised by …", "Language"). Site specifics (listing URLs, city
spellings, event-link patterns) are a few lines per site in `s1scraper/sources/`.

---

## Settings (`settings.json`)

Created and updated by the window; editable by hand. Notable keys:

| Key | Default | Meaning |
|---|---|---|
| `workbook_path` | `Rolling_Event_Calendar.xlsx` | Workbook to update (relative paths are inside this folder). |
| `master_sheet` / `rolling_sheet` | `Master Calander ` / `Rolling Calendar` | Sheet names. |
| `window_preset` | `1 month` | `1 week` … `6 months` or `Custom dates` (+ `window_start`, `window_end`). |
| `speed` | `normal` | `gentle`, `normal` or `fast`. |
| `use_browser`, `show_browser` | `true`, `false` | Real-browser fallback for JavaScript pages. |
| `browser_channel`, `browser_path` | empty | Force `msedge`/`chrome`, or a browser executable. |
| `respect_robots_txt` | `true` | Honour each site's robots.txt. |
| `tier_basis` | `min` | Tier from `min` (entry), `avg` or `max` ticket price. |
| `max_details_per_source` | `1500` | Event pages read per source per run (nearest dates first). |
| `detail_cache_days` | `10` | Re-use downloaded event pages this long. |
| `include_activities`, `include_online`, `include_undated` | `false` | Widen what is kept. |
| `per_city_tabs`, `summary_sheet` | `true` | Extra sheets. |
| `backup_before_write`, `keep_backups` | `true`, `15` | Copy of the workbook in `backups/` before each write. |

Logs (`logs/`), backups (`backups/`), the page cache (`cache/`) and health-check output
(`diagnostics/`) are created next to the app.

---

## Troubleshooting

* **"… is open in another program"** - close the workbook in Excel; the results of that run are in the
  `(updated …)` copy next to it.
* **"No usable browser found"** - install Google Chrome or Microsoft Edge, or run
  `.venv\Scripts\python -m playwright install chromium` (Windows) / `.venv/bin/python -m playwright install chromium`.
* **A source finds 0 events** - run the Health check and look at that source's lines; send the
  `diagnostics/<date>/` folder if it is not obvious.
* **"refused … stopped contacting it"** - the site asked us to slow down; nothing else to do but wait
  a few hours. Consider the *Gentle* speed.
* **The window does not open on Mac/Linux** - the Python in use lacks Tk; install Python from python.org
  (Linux: `sudo apt install python3-tk`). The command line works regardless.

---

## For developers

```
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q           # Linux: xvfb-run -a .venv/bin/python -m pytest -q
```

The tests run whole scrapes against local stand-ins for BookMyShow, District and AllEvents
(`tests/fakesites.py`: embedded JSON state, Next.js data, React server payloads, JSON-LD, a page
that refuses with 403, a two-city tour page, a JavaScript-only infinite-scroll listing), check that the
existing Master cells stay byte-for-byte identical across runs, and drive the Tk window.
CI (`.github/workflows/tests.yml`) runs them on Windows, macOS and Linux.

```
s1scraper/
  sources/        one small profile per site (+ the shared crawler in base.py)
  extract.py      schema.org / embedded JSON / listing-card / labelled-fact extraction
  fetcher.py      polite HTTP: pacing, backoff, circuit breaker, robots.txt, cache
  browser.py      Edge/Chrome via Playwright for JavaScript pages
  pipeline.py     one run: discover → de-duplicate → read event pages → classify → write
  excel.py        Master append, Rolling/city tabs, Summary, Run Log
  classify.py     Activity Type and Tier rules (Guide thresholds)
  gui.py, cli.py, diagnose.py
```
