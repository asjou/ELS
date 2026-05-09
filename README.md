# FCC ELS Grant-Time Dashboard Refresher

Scrapes the [FCC Experimental Licensing System (ELS)](https://apps.fcc.gov/oetcf/els/reports/GenericSearch.cfm), computes grant-time statistics, and injects live data directly into a self-contained HTML dashboard — no server required.

---

## What It Does

Runs six targeted scrapes against the FCC ELS search in sequence:

| # | Scraper | License type |
|---|---------|-------------|
| 1 | UAV / Unmanned Aerial | STA (`-EX-ST-`) |
| 2 | UAV / Unmanned Aerial | New License (CN) |
| 3 | Space experiments | STA |
| 4 | Space experiments | New License (CN) |
| 5 | All experiment types | STA |
| 6 | All experiment types | New License (CN) |

For each scraper it:

1. Navigates the FCC ELS search form with headless Chrome, paginates through all result pages, and saves raw rows to a timestamped CSV.
2. Computes **avg / min / max / count** of days from receipt → grant, plus bucket distributions (`<30d`, `30–60d`, …, `>365d`) and per-experiment-type breakdowns for Space.
3. Serialises results to a compact JavaScript payload and **injects it into `ELS_snapshot_today.html`** between sentinel comments (`// @@LIVE_DATA_START` … `// @@LIVE_DATA_END`).
4. Patches the visible metric cards in the HTML with the latest split averages.
5. Writes a timestamped backup of the previous HTML before overwriting.

If **any** scraper fails, the HTML is left untouched (fail-safe, exit code 1).

---

## Requirements

- Python 3.10+
- Google Chrome installed
- ChromeDriver (auto-managed, or specify a local path in `_make_driver()`)

```bash
pip install selenium webdriver-manager
```

---

## Usage

```bash
python els_snapshot_today.py [--html PATH] [--csv-dir DIR] [--date-from MM/DD/YYYY] [--reuse-csv]
```

| Argument | Default | Description |
|----------|---------|-------------|
| `--html` | `ELS_snapshot_today.html` (same dir as script) | Path to the dashboard HTML file |
| `--csv-dir` | `./csv_exports` | Directory for per-scraper CSV output |
| `--date-from` | `01/01/2026` | Start of the grant-date window |
| `--reuse-csv` | _(flag, off by default)_ | Skip re-scraping and recompute stats from existing CSVs |

### Examples

```bash
# Standard refresh with defaults
python els_snapshot_today.py

# Custom date window and output locations
python els_snapshot_today.py --date-from 06/01/2025 --html ../dashboard/ELS_snapshot_today.html --csv-dir ../data

# Fast re-run using already-downloaded CSVs (no browser launched)
python els_snapshot_today.py --reuse-csv
```

---

## Output

| Output | Location | Description |
|--------|----------|-------------|
| `ELS_snapshot_today.html` | `--html` path | Updated dashboard, viewable directly in any browser |
| `ELS_snapshot_today_YYYYMMDD_HHMMSS.html` | Same dir as HTML | Timestamped backup of previous HTML |
| `uav_sta_YYYYMMDD.csv` | `--csv-dir` | Raw UAV STA rows |
| `uav_cn_YYYYMMDD.csv` | `--csv-dir` | Raw UAV CN rows |
| `space_sta_YYYYMMDD.csv` | `--csv-dir` | Raw Space STA rows |
| `space_cn_YYYYMMDD.csv` | `--csv-dir` | Raw Space CN rows |
| `all_sta_YYYYMMDD.csv` | `--csv-dir` | Raw all-types STA rows |
| `all_cn_YYYYMMDD.csv` | `--csv-dir` | Raw all-types CN rows |

---

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | All scrapers succeeded; HTML updated |
| `1` | One or more scrapers failed; HTML **not** modified |
| `2` | HTML file not found or injection sentinels missing |

---

## HTML Injection Format

The dashboard HTML must contain exactly one `<script>` block with the sentinels:

```html
<script>
// @@LIVE_DATA_START
// ... generated JS constants go here ...
// @@LIVE_DATA_END
</script>
```

The script replaces everything between those comments with freshly generated `const` declarations on each run.

---

## Project Structure

```
.
├── els_snapshot_today.py      # This script (master orchestrator)
├── ELS_snapshot_today.html    # Dashboard HTML (updated in-place)
└── csv_exports/               # Per-scraper CSVs (auto-created)
```

The six `fcc_els_*.py` scraper modules referenced in older versions of this codebase have been consolidated into this single file.

---

## Notes

- The FCC ELS site can be slow; page load and script timeouts are set to **600 seconds**.
- Space scrapers use dynamic column detection since the FCC results table headers vary.
- The `--reuse-csv` flag is useful for iterating on stats or HTML injection logic without re-hitting the FCC site.
- A hardcoded fallback ChromeDriver path exists in `_make_driver()` for Windows environments — update or remove it as needed for your setup.
