# S1 Event Scraper

Builds StepOne's rolling events calendar for **Mumbai, Pune, Delhi NCR, Bengaluru, Kolkata,
Ahmedabad and Chennai** from BookMyShow, District (Zomato), AllEvents and the other platforms
and venues in the workbook's Guide, and writes the results straight into the Excel workbook.

* Pick any window - next 1 week, 2 weeks, 1/2/3/6 months, or exact dates - and run it whenever needed.
* **Master Calander** is never edited: new events are only *added* below the last row.
* **Rolling Calendar** (the client's sheet) is rebuilt on every run for the chosen window: city by
  city, each city in date order.
* Every run finishes within the **Time limit** chosen in the window (1 hour by default). The nearest
  dates are read in full first.
* Every row has a **Register By** date beside the start date, and an **End Date** only when it can be
  trusted. The Master also keeps the **Organizer** (or `Source: <platform>` when none is published).
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
      **Options:** *Speed* (keep *Normal*) and *Time limit* (1 hour by default; *1.5 hours* or
      *2 hours* give far-off dates more detail on a 6-month run).
   4. Press **▶ Run scraper**. Progress and a live log are shown; **■ Stop** cancels safely
      (the workbook is only written at the very end, so a stopped run changes nothing).
4. When it finishes, click *Yes* to open the workbook.

### Automatic runs (optional)

Section **5. Automatic runs** in the window runs the scraper on its own - no window, no terminal:

1. Pick **Every week** (and the day) or **Every day**, and a time such as `07:00`.
2. Press **Save schedule**. The status next to the button shows what is active, e.g.
   *Active: Every Friday at 07:00*. Choose **Off** and *Save schedule* to stop it.

Each automatic run uses what was last chosen in the window (date window, cities, sources, workbook),
so a *Next 1 week* window gives a fresh week every Friday. The Master sheet only grows; the Rolling
Calendar is rebuilt; the hidden *Run Log* sheet gets a line per run.

* The computer has to be on. A run missed while it was asleep happens when it wakes up
  (a run missed while it was switched off is skipped until the next one).
* Keep the workbook closed in Excel at that time, otherwise the results go to a copy next to it.
* **Mac:** after *Save schedule* the app runs a quick background test. If macOS asks whether Python
  may access a folder, click *Allow*. If the test says macOS blocked it, the app folder or workbook is
  in Desktop, Documents or Downloads, which macOS guards from background jobs: move them into your
  home folder (or follow the Full Disk Access steps shown) and press *Save schedule* again.
  A notification appears when each automatic run finishes.
* Windows uses Task Scheduler (task *S1 Event Scraper*), macOS a launch agent, Linux cron.

> Close the workbook in Excel before a run finishes. If it is still open, the results are saved
> to a copy next to it (`… (updated 2026-10-08 1530).xlsx`) and the window tells you so.

**How long a run takes.** All sites are read at the same time, each at its own polite pace, and an
event page is only opened when the listing is missing something (venue, time, price, description).
Every run finishes in about the **Time limit** at most (1 hour by default); shorter windows usually
finish well before it. Event pages are read nearest dates first, so if time runs out it is the
far-off events that keep only what their listing showed (name, date, venue, link). Event pages read
in the last 10 days are reused, so the next run is quicker.

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
| **Master Calander** | Append-only. Each run adds the events it has not seen before under a band like `▌ SCRAPER UPDATE · 08 OCT 2026 · 132 NEW EVENTS`, styled like the hand-typed rows. Existing rows are never edited. Events already typed in by hand (same title, city and date) are not added again, and an event whose row you delete is not re-added on the next run. One-time layout change, as StepOne asked: the first run of this version removes the **Ticket Platform(s)** and **Link** columns and adds **Register By** beside Start Date (every other cell keeps its value and look, and the workbook is copied to `backups/` first). **Added On** is added at the end if missing. |
| **Rolling Calendar** | The client's sheet, rebuilt every run: all events in the window, city by city (Mumbai, Pune, Delhi NCR, Bengaluru, Kolkata, Ahmedabad, Chennai) under a band per city, each city in date order. No prices, organisers, ticket platforms or links. Frozen header, filter arrows on every column (filter by city, tier, category …), print-ready landscape layout. |
| **Run Log** (hidden) | One line per run: when, window, events found, new rows in Master, per-source counts, warnings, minutes taken and event pages read. Right-click a tab → *Unhide* to see it. |

So the visible tabs stay exactly as in StepOne's sheet: the Guide, the Master (StepOne's own record) and the
Rolling Calendar (the one shared with the client). One tab per city and a Summary sheet with live counts
can be switched on with `city_tabs` / `summary_tab` in `settings.json`.

Rolling Calendar columns: **S.No. · Event Name · Activity Type · Tier · Register By · Start Date · End Date ·
City / Cities · Venue · Notes · Region · Added On**.
The Master has the same columns plus **Organizer** and **Price Range** after Venue (the tier is worked out
from the price).

* **Register By** is the booking or registration deadline the site itself publishes (end of ticket
  sales, "registrations close on …"), never later than the event's last day. When the site gives no
  separate deadline, the last day you can go is shown in grey italics: the date of a one-day event, or
  the end date of a run (a workshop held daily until 1 Nov shows 1 Nov, not a start date already past).
* **End Date** is only filled when it can be trusted. A late show that ends after midnight counts as
  one evening. An event running on many dates over more than a month says *Multiple dates* (grey
  italics), unless it is an exhibition or festival whose own page gives its closing date. A weekly
  series that began long before the window and lists no dates inside it is left out.

* **Tier** follows the Guide, using the lowest (entry) ticket price: Ultra Premium ₹5,000+,
  Luxury ₹1,550–5,000, Premium ₹500–1,549, Standard under ₹500 or free; *TBC* when no price is published.
  (`tier_basis` in `settings.json` can switch this to the highest or average ticket price.)
* **Activity Type** is one of the Guide's eight (Culture, Art, Music, F&B, Sports to Watch,
  Recreational Sports, Theatre, Comedy) plus *Workshops* and *Other*, decided from the platform's own
  category first, then the title and description.
* **Organizer** is what the event page publishes; if nothing is published it says
  `Source: BookMyShow` (grey italics). Ticketing platforms listed as "organiser" are not trusted.
* **Price Range** (Master only) like `₹800 – ₹2,500`, `₹499 onwards` or `Free`.
* **Notes** is one short line on what the event is, in plain words.
* The same show listed on several platforms becomes one row.
* Each event's page address is still kept in the hidden `_scraper_index` sheet, which is how an event
  is never added to the Master twice. When an event page is read, the address the site itself settles
  on is kept (after redirects, its canonical link), tracking tags are removed, and BookMyShow plays,
  sports and events each keep their own section.

---

## Staying unblocked

Weekly (or on-demand) runs only stay safe if the sites never see anything unusual. The scraper:

* uses **no accounts or logins** - there is nothing that can be banned;
* sends **one request at a time per site** with a random 1–2.5 s gap (Gentle: 2–4.5 s, Fast:
  0.6–1.4 s) and a longer pause every few dozen pages, so traffic looks like one person browsing;
  BookMyShow, which is quick to turn visitors away, always gets 2.5–5 s; different sites are read in
  parallel, each at its own pace;
* **backs off** on "slow down" answers (HTTP 429/503, honouring `Retry-After`) and, after three
  refusals in a row from a site, **stops contacting that site for the rest of the run** and says so
  in the log and Run Log - rather than pushing on and getting the IP flagged;
* **caches** event pages for 10 days, so next week's run only downloads events it has not seen;
* honours each site's **robots.txt** (can be switched off in `settings.json`, e.g. with the site's permission);
* opens pages in the **Edge/Chrome already installed** on the computer only when a page needs
  JavaScript (infinite-scroll listings), at the same gentle pace. It works out of sight; no browser
  window opens on your screen.
  BookMyShow's event pages are never opened in the browser (it turns automated browsers away), and a
  browser refusal no longer stops its normal page downloads.

Good practice: run it from the office connection rather than a VPN, keep the default *Normal* speed,
and if the log ever says a site "refused … stopped contacting it", wait a few hours before the next run.

---

## Command line

```
python run_scraper.py                         # use the settings saved by the window
python run_scraper.py --window 1w             # next week   (1w 2w 1m 2m 3m 6m, or "10 days")
python run_scraper.py --from 2026-10-10 --to 2026-12-31
python run_scraper.py --cities Mumbai Pune --sources bookmyshow district
python run_scraper.py --window 6m --time-limit 90   # six months, finish within about 1.5 hours
python run_scraper.py --sources district --window 6m --dry-run --find "Sunidhi Chauhan" "A R Rahman"
                                              # one site only, workbook untouched, and for each name:
                                              # was it on a listing page, is it in the sheet, or why not
python run_scraper.py --save-pages            # keep a copy of every page read in diagnostics/
python run_scraper.py --workbook "D:\StepOne\Rolling_Event_Calander_Friday_Updates.xlsx"
python run_scraper.py --diagnose              # health check
python run_scraper.py --gui                   # open the window
python run_scraper.py --help
```

**Scheduling:** use *5. Automatic runs* in the window (see above). It registers
`run_scraper.py --notify` with the computer's own scheduler and the last saved settings.

---

## Sources

| Group | Sources | Notes |
|---|---|---|
| Main | BookMyShow, District (Zomato, includes former Paytm Insider), AllEvents | Events, plays and sports per city; BookMyShow "activities" (water parks, gaming zones) are left out unless `include_activities` is on. District is read from its general page and its category lists (music, comedy shows, performances, sports events, nightlife and any other list it links to), because the general page only shows the next few days. |
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
| `time_limit_minutes` | `60` | Finish within about this many minutes (`0` = no limit). Nearest dates are read first. |
| `use_browser` | `true` | Real-browser fallback for JavaScript pages. The browser always works out of sight; `--show-browser` on the command line shows it for one run. |
| `browser_channel`, `browser_path` | empty | Force `msedge`/`chrome`, or a browser executable. |
| `respect_robots_txt` | `true` | Honour each site's robots.txt. |
| `tier_basis` | `min` | Tier from `min` (entry), `avg` or `max` ticket price. |
| `max_details_per_source` | `1500` | Event pages read per source per run (nearest dates first). |
| `detail_cache_days` | `10` | Re-use downloaded event pages this long. |
| `include_activities`, `include_online`, `include_undated` | `false` | Widen what is kept. |
| `city_tabs`, `summary_tab` | `false` | Extra sheets: one tab per city, and live counts by city, tier and category. |
| `backup_before_write`, `keep_backups` | `true`, `15` | Copy of the workbook in `backups/` before each write. |
| `schedule_frequency`, `schedule_day`, `schedule_time` | `off`, `Friday`, `07:00` | Automatic runs (`off`, `daily`, `weekly`). Change them in the window so the computer's scheduler is updated too. |

Logs (`logs/`), backups (`backups/`) and health-check output (`diagnostics/`) are created next to the
app. The page cache lives in the computer's own cache folder (Mac: `~/Library/Caches/S1 Event Scraper`),
away from a Desktop that iCloud or OneDrive may sync; a problem with it never stops a run. On a Mac the
console output of automatic runs also goes to `~/Library/Logs/S1 Event Scraper/scheduled_runs.log`.

---

## Troubleshooting

* **"… is open in another program"** - close the workbook in Excel; the results of that run are in the
  `(updated …)` copy next to it.
* **"No usable browser found"** - install Google Chrome or Microsoft Edge, or run
  `.venv\Scripts\python -m playwright install chromium` (Windows) / `.venv/bin/python -m playwright install chromium`.
* **An event you know of is missing** - run that site alone with `--dry-run --find "<name>"` (see
  [Command line](#command-line)). The last lines say whether it was on a listing page at all, whether it
  made the sheet, or why it was left out (outside the dates, other city, online-only ...).
* **A source finds 0 events** - run the Health check and look at that source's lines; send the
  `diagnostics/<date>/` folder if it is not obvious.
* **"refused … stopped contacting it"** - the site asked us to slow down; nothing else to do but wait
  a few hours. Consider the *Gentle* speed.
* **BookMyShow finds few events for a city** - tick only BookMyShow and that city, press *Health check*,
  and send the `diagnostics/<date>/` folder.
* **A run takes too long** - pick a shorter *Time limit*; the nearest dates still get full detail.
* **An automatic run did not happen** - check the status line in *5. Automatic runs* (it says if the
  schedule is not registered on this computer), that the computer was on, and the newest file in `logs/`
  (Mac: also `~/Library/Logs/S1 Event Scraper/`). On a Mac press *Save schedule* again to re-run the
  background test.
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
that refuses with 403, a two-city tour page, a JavaScript-only infinite-scroll listing), check that every
existing Master cell keeps its value and look across runs (and through the one-time column change),
and drive the Tk window.
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
