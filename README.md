# FCC ELS Dashboard Tools

Two Python scripts that scrape the [FCC Experimental Licensing System (ELS)](https://apps.fcc.gov/oetcf/els/reports/GenericSearch.cfm) and generate self-contained HTML dashboards — no server or database required.

---

## Scripts at a Glance

| Script | Output | Purpose |
|--------|--------|---------|
| `els_granted_today.py` | `els_granted_today.html` | **What was granted** — full list of all granted applications this year, broken down by application type |
| `els_snapshot_today.py` | `ELS_snapshot_today.html` | **How long grants take** — grant processing time statistics across UAV, Space, and all experiment types |

---

## `els_granted_today.py`

Scrapes all FCC ELS applications with status **Granted** from `01/01/2026` through today, classifies them by application type, and writes a polished HTML dashboard.

### What it does

1. Launches a headless Firefox browser (via Playwright), navigates the FCC ELS search form, and paginates through all results (100 records/page).
2. Parses each result row and extracts: file number, call sign, applicant name, receipt date, and grant date.
3. Classifies each record by the two-letter code embedded in its file number (`YYYY-EX-<CODE>-NNNNNN`):

   | Code | Type |
   |------|------|
   | `ST` | Special Temporary Authority (STA) |
   | `CN` | New License |
   | `MD` | Modification |
   | `RN` | Renewal |
   | `TC` | Transfer of Control |
   | `AS` | Assignment |
   | _(other)_ | Other |

4. Deduplicates by file number, saves raw data to a timestamped CSV, then generates `els_granted_today.html` — a fully self-contained page with animated count-up hero stats, per-type application tables, and a sticky nav bar.

### Usage

```bash
python els_granted_today.py [--skip-scrape] [--debug]
```

| Flag | Description |
|------|-------------|
| _(none)_ | Full scrape + HTML generation |
| `--skip-scrape` | Skip scraping; re-render HTML from the most recent `fcc_els_granted_*.csv` |
| `--debug` | Dump all form fields to stdout and save a screenshot (`debug_screenshot.png`) before submitting; exits without writing CSV or HTML |

### Output files

| File | Description |
|------|-------------|
| `els_granted_today.html` | Dashboard HTML (overwritten each run) |
| `fcc_els_granted_YYYYMMDD.csv` | Raw scraped records for that date |
| `debug_screenshot.png` | _(only with `--debug`)_ Full-page screenshot of the form before submission |

### Dependencies

```bash
pip install playwright beautifulsoup4
playwright install firefox
```

---

## `els_snapshot_today.py`

Runs **six** targeted FCC ELS scrapes in sequence and injects computed grant-time statistics directly into an existing HTML dashboard.

### What it does

1. Runs six scrapes covering every combination of experiment category and license type:

   | # | Category | License type |
   |---|----------|-------------|
   | 1 | UAV / Unmanned Aerial | STA (`-EX-ST-`) |
   | 2 | UAV / Unmanned Aerial | New License (CN) |
   | 3 | Space experiments | STA |
   | 4 | Space experiments | New License (CN) |
   | 5 | All experiment types | STA |
   | 6 | All experiment types | New License (CN) |

2. For each scrape, computes **avg / min / max / count** of days from receipt → grant date, plus bucket distributions (`<30d`, `30–60d`, `60–90d`, `90–120d`, `120–180d`, `180–365d`, `>365d`) and per-experiment-type breakdowns for Space.
3. Serialises all results into a JavaScript payload and injects it into `ELS_snapshot_today.html` between sentinel comments:
   ```
   // @@LIVE_DATA_START
   // ... generated JS constants ...
   // @@LIVE_DATA_END
   ```
4. Patches the visible metric cards in the HTML with the latest split averages (UAV STA / CN, Space STA / CN, All STA / CN).
5. Writes a timestamped backup of the previous HTML before overwriting.

**Fail-safe:** if any of the six scrapers fail, the HTML is left completely untouched and the script exits with code `1`.

### Usage

```bash
python els_snapshot_today.py [--html PATH] [--csv-dir DIR] [--date-from MM/DD/YYYY] [--reuse-csv]
```

| Argument | Default | Description |
|----------|---------|-------------|
| `--html` | `ELS_snapshot_today.html` (same dir as script) | Path to the dashboard HTML file to update |
| `--csv-dir` | `./csv_exports` | Directory for per-scraper CSV output |
| `--date-from` | `01/01/2026` | Start of the grant-date window |
| `--reuse-csv` | _(flag, off)_ | Skip re-scraping; recompute stats from today's existing CSVs |

### Output files

| File | Description |
|------|-------------|
| `ELS_snapshot_today.html` | Dashboard HTML, updated in-place |
| `ELS_snapshot_today_YYYYMMDD_HHMMSS.html` | Timestamped backup of the previous HTML |
| `csv_exports/uav_sta_YYYYMMDD.csv` | Raw UAV STA rows |
| `csv_exports/uav_cn_YYYYMMDD.csv` | Raw UAV CN rows |
| `csv_exports/space_sta_YYYYMMDD.csv` | Raw Space STA rows |
| `csv_exports/space_cn_YYYYMMDD.csv` | Raw Space CN rows |
| `csv_exports/all_sta_YYYYMMDD.csv` | Raw all-types STA rows |
| `csv_exports/all_cn_YYYYMMDD.csv` | Raw all-types CN rows |

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | All scrapers succeeded; HTML updated |
| `1` | One or more scrapers failed; HTML **not** modified |
| `2` | HTML file not found or injection sentinels missing |

### Dependencies

```bash
pip install selenium webdriver-manager
```

Google Chrome must be installed. ChromeDriver is auto-managed; a hardcoded fallback path for Windows exists in `_make_driver()` — update or remove it as needed.

---

## Project Structure

```
.
├── els_granted_today.py           # Granted applications dashboard
├── els_snapshot_today.py          # Grant-time statistics dashboard
├── els_granted_today.html         # Output: granted apps (overwritten each run)
├── ELS_snapshot_today.html        # Output: grant-time stats (updated in-place)
└── csv_exports/                   # Per-scraper CSVs (auto-created by snapshot script)
```

---

## Key Differences Between the Two Scripts

| | `els_granted_today.py` | `els_snapshot_today.py` |
|-|------------------------|------------------------|
| **Browser** | Playwright (Firefox) | Selenium (Chrome) |
| **Scrapes** | 1 (all granted, all types) | 6 (UAV/Space/All × STA/CN) |
| **Output focus** | Who was granted what | How long grants took |
| **HTML strategy** | Generates HTML from scratch | Injects into an existing HTML file |
| **Fail behavior** | Partial output on error | Full abort if any scrape fails |
