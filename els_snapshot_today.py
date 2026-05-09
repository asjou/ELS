"""
els_dashboard_refresh.py
═══════════════════════════════════════════════════════════════════════════════
Master orchestrator for the FCC ELS Grant-Time Dashboard.

What it does
────────────
1. Runs six FCC ELS scrapers in sequence (UAV STA, UAV CN, Space STA,
   Space CN, All STA, All CN) — each scraper is imported as a module, so no
   subprocess overhead and all shared Selenium logic is reused.
2. Computes all summary statistics (avg / min / max / count, bucket
   distributions, per-experiment-type breakdowns).
3. Serialises the results to a compact JSON payload.
4. Finds the single <script> block in ELS_snapshot_today.html that contains
   the sentinel comment  // @@LIVE_DATA_START  …  // @@LIVE_DATA_END  and
   replaces it wholesale with freshly-generated JavaScript constant
   declarations — so the page is immediately viewable with no server needed.
5. Patches the summary stats box at the top of the HTML with split averages:
   UAV STA avg / UAV CN avg, Space STA avg / Space CN avg,
   All STA avg / All CN avg.
6. Writes a timestamped backup of the previous HTML before overwriting.

Usage
─────
    python els_dashboard_refresh.py [--html PATH] [--csv-dir DIR]

Arguments
─────────
  --html      PATH to ELS_snapshot_today.html  (default: ELS_snapshot_today.html
              in the same directory as this script)
  --csv-dir   Directory where per-scraper CSVs are written  (default: ./csv_exports)
  --date-from Override the start of the grant-date window  (default: 01/01/2026)

Dependencies
────────────
    pip install selenium webdriver-manager

The six scraper modules (fcc_els_*.py) must live in the same directory as
this script, or on sys.path.

Exit codes
──────────
  0  All scrapers succeeded and the HTML was updated.
  1  One or more scrapers failed; the HTML is NOT modified (fail-safe).
  2  HTML file not found or the injection sentinel was missing.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import re
import shutil
import sys
import time
import traceback
from collections import defaultdict, OrderedDict
from datetime import date, datetime
from pathlib import Path
from typing import Any

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("els_refresh")

# ── Constants ─────────────────────────────────────────────────────────────────
DEFAULT_DATE_FROM    = "01/01/2026"
DEFAULT_HTML_NAME    = "ELS_snapshot_today.html"
DEFAULT_CSV_DIR      = "csv_exports"
SENTINEL_START       = "// @@LIVE_DATA_START"
SENTINEL_END         = "// @@LIVE_DATA_END"

DATE_FORMAT          = "%m/%d/%Y"
STA_PATTERN          = re.compile(r"\d{4}-EX-ST-\d{4}", re.IGNORECASE)
FILE_PATTERN         = re.compile(r"\d{4}-EX-[A-Z]+-\d{4}", re.IGNORECASE)

BUCKETS: list[tuple[str, Any]] = [
    ("<30d",     lambda d: d < 30),
    ("30-60d",   lambda d: 30 <= d < 60),
    ("60-90d",   lambda d: 60 <= d < 90),
    ("90-120d",  lambda d: 90 <= d < 120),
    ("120-180d", lambda d: 120 <= d < 180),
    ("180-365d", lambda d: 180 <= d < 365),
    (">365d",    lambda d: d >= 365),
]

SPACE_EXPERIMENT_TYPES = [
    ("Cubesats",                     ["cubesat"]),
    ("Inmarsat",                     ["inmarsat"]),
    ("Big LEO (Low Earth Orbit)",    ["big leo"]),
    ("Little LEO (Low Earth Orbit)", ["little leo"]),
    ("Rocket Launch",                ["rocket"]),
    ("Satellite, General",           ["satellite, general"]),
    ("SATCOM-on-the-move (SOTM)",    ["satcom-on-the-move", "sotm"]),
    ("Space (other than cubesats)",  ["space (other"]),
]


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 1 — Shared Selenium helpers (mirrors logic in the individual scripts)
# ═════════════════════════════════════════════════════════════════════════════

def _make_driver():
    """Return a headless Chrome WebDriver."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1920,1080")
    try:
        driver = webdriver.Chrome(options=opts)
    except Exception:
        _local_driver = (
            r"C:\Users\alice\.wdm\drivers\chromedriver\win64"
            r"\147.0.7727.57\chromedriver-win32\chromedriver.exe"
        )
        service = Service(_local_driver)
        driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(600)
    driver.set_script_timeout(600)
    # Increase urllib3 read timeout so slow FCC pages don't kill the session
    driver.command_executor._timeout = 600
    return driver


def _set_checkboxes(driver):
    from selenium.webdriver.common.by import By
    for name in ["all", "pending", "dismissed"]:
        try:
            cb = driver.find_element(By.NAME, name)
            if cb.is_selected():
                cb.click()
        except Exception:
            pass
    try:
        cb = driver.find_element(By.NAME, "granted")
        if not cb.is_selected():
            cb.click()
    except Exception:
        log.warning("'granted' checkbox not found")
    try:
        cb = driver.find_element(By.NAME, "expired")
        if not cb.is_selected():
            cb.click()
    except Exception:
        log.warning("'expired' checkbox not found")


def _set_field(driver, name: str, value: str) -> bool:
    from selenium.webdriver.common.by import By
    els = driver.find_elements(By.NAME, name)
    if not els:
        return False
    els[0].clear()
    els[0].send_keys(value)
    return True


def _wait_for_table(driver, first_page: bool, timeout: int = 45):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    RESULTS_FRAG = "GenericSearchResult.cfm"
    TH_XPATH = (
        "//th[contains(normalize-space(.),'File') and "
        "contains(normalize-space(.),'Number')]"
        " | //th[normalize-space(.)='Applicant Name']"
    )
    if first_page:
        WebDriverWait(driver, timeout).until(EC.url_contains(RESULTS_FRAG))
    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.XPATH, TH_XPATH))
    )


def _has_next(driver) -> bool:
    from selenium.webdriver.common.by import By
    try:
        btn = driver.find_element(
            By.XPATH, "//form[@name='next_result']//input[@type='submit']"
        )
        return btn.is_displayed() and btn.is_enabled()
    except Exception:
        return False


def _click_next(driver):
    from selenium.webdriver.common.by import By
    driver.find_element(
        By.XPATH, "//form[@name='next_result']//input[@type='submit']"
    ).click()


def _extract_simple_rows(driver) -> list[dict]:
    """
    Extract rows from the simple (non-dynamic-column) results table.
    Column indices are confirmed from the individual scraper scripts.
    """
    from selenium.webdriver.common.by import By

    COL_FILE   = 6
    COL_APP    = 8
    COL_REC    = 9
    COL_GRANT  = 11

    rows = []
    for tr in driver.find_elements(By.XPATH, "//table//tr"):
        tds = tr.find_elements(By.TAG_NAME, "td")
        if len(tds) <= COL_GRANT:
            continue
        fn = tds[COL_FILE].text.strip()
        if not FILE_PATTERN.match(fn):
            continue
        rows.append({
            "file_number":  fn,
            "applicant":    tds[COL_APP].text.strip(),
            "receipt_date": tds[COL_REC].text.strip(),
            "grant_date":   tds[COL_GRANT].text.strip(),
        })
    return rows


def _extract_dynamic_rows(driver, col_map: dict) -> list[dict]:
    """Extract rows using a dynamic column map (used for Space scrapers)."""
    from selenium.webdriver.common.by import By

    def _norm(t): return re.sub(r"\s+", " ", t.strip())

    rows = []
    for table in driver.find_elements(By.TAG_NAME, "table"):
        ths = table.find_elements(By.TAG_NAME, "th")
        if not any("File" in _norm(th.text) and "Number" in _norm(th.text)
                   for th in ths):
            continue
        for tr in table.find_elements(By.TAG_NAME, "tr"):
            tds = tr.find_elements(By.TAG_NAME, "td")
            if not tds:
                continue
            row = {col: (tds[idx].text.strip() if idx < len(tds) else "")
                   for col, idx in col_map.items()}
            if FILE_PATTERN.match(row.get("File Number", "")):
                rows.append(row)
        break
    return rows


def _build_col_map(driver) -> dict:
    """
    Detect columns dynamically from the first results page (Space scrapers).
    'Status Date' is renamed to 'Grant Date'.
    """
    from selenium.webdriver.common.by import By

    RENAME = {"Status Date": "Grant Date"}

    def _norm(t): return re.sub(r"\s+", " ", t.strip())

    for table in driver.find_elements(By.TAG_NAME, "table"):
        ths = table.find_elements(By.TAG_NAME, "th")
        headers = [_norm(th.text) for th in ths]
        if not any("File" in h and "Number" in h for h in headers):
            continue
        col_map = {}
        for idx, raw in enumerate(headers):
            name = RENAME.get(raw, raw)
            if not name or name.lower().startswith("view "):
                continue
            col_map[name] = idx
        return col_map
    return {}


def _find_select_option(driver, field_name: str, keywords: list[str]) -> str | None:
    """Return the value of the first matching <option> in a named <select>."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import Select

    els = driver.find_elements(By.NAME, field_name)
    if not els:
        return None
    for opt in Select(els[0]).options:
        if any(kw in opt.text.strip().lower() for kw in keywords):
            return opt.get_attribute("value")
    return None


def _select_by_value(driver, field_name: str, value: str):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import Select
    Select(driver.find_element(By.NAME, field_name)).select_by_value(value)


def _submit(driver):
    from selenium.webdriver.common.by import By
    driver.find_element(
        By.XPATH,
        "//input[@type='submit' and not(ancestor::form[@name='next_result'])]"
    ).click()


def _paginate(driver, extract_fn, first_page_wait=True, tag="") -> list[dict]:
    """Generic paginator. extract_fn(driver) -> list[dict]."""
    all_rows = []
    page = 1
    while True:
        log.info("  %s page %d …", tag, page)
        try:
            _wait_for_table(driver, first_page=(page == 1 and first_page_wait))
        except Exception as exc:
            log.warning("  Table wait timed out on page %d: %s — stopping", page, exc)
            break
        rows = extract_fn(driver)
        log.info("    → %d rows", len(rows))
        all_rows.extend(rows)
        if not _has_next(driver):
            break
        _click_next(driver)
        page += 1
        time.sleep(1)
    return all_rows


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 2 — Statistics helpers
# ═════════════════════════════════════════════════════════════════════════════

def _parse_date(text: str) -> date | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, DATE_FORMAT).date()
    except ValueError:
        return None


def _annotate_days(rows: list[dict],
                   receipt_key="receipt_date",
                   grant_key="grant_date") -> list[int]:
    """
    Add 'days_to_grant' to each row dict; return list of valid day values.
    Works for both snake_case (simple scrapers) and Title Case (space scrapers).
    """
    valid = []
    for row in rows:
        rd = _parse_date(row.get(receipt_key) or row.get("Receipt Date", ""))
        gd = _parse_date(row.get(grant_key)   or row.get("Grant Date",   ""))
        if rd and gd:
            days = (gd - rd).days
            row["days_to_grant"] = days
            valid.append(days)
        else:
            row["days_to_grant"] = None
    return valid


def _bucket_counts(valid_days: list[int]) -> list[int]:
    return [sum(1 for d in valid_days if test(d)) for _, test in BUCKETS]


def _summary(valid_days: list[int], total_rows: int) -> dict:
    n = len(valid_days)
    if n == 0:
        return {"avg": 0.0, "min": 0, "max": 0, "count": total_rows,
                "valid": 0, "counts": [0]*7}
    return {
        "avg":    round(sum(valid_days) / n, 1),
        "min":    min(valid_days),
        "max":    max(valid_days),
        "count":  total_rows,
        "valid":  n,
        "counts": _bucket_counts(valid_days),
    }


def _stats_from_simple_csv(csv_path: Path) -> dict:
    """Load stats from an already-written simple-scraper CSV (skip re-scraping)."""
    import csv as csv_mod
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv_mod.DictReader(fh))
    valid_days = []
    for r in rows:
        raw = r.get("days_to_grant", "")
        if raw not in (None, "", "None"):
            try:
                valid_days.append(int(raw))
            except ValueError:
                pass
    return _summary(valid_days, len(rows))


def _stats_from_space_csv(csv_path: Path) -> dict:
    """Load stats from an already-written space-scraper CSV (skip re-scraping)."""
    import csv as csv_mod
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv_mod.DictReader(fh))
    valid_days = []
    for r in rows:
        raw = r.get("Days to Grant", "")
        if raw not in (None, "", "None"):
            try:
                valid_days.append(int(raw))
            except ValueError:
                pass
    stats = _summary(valid_days, len(rows))
    stats["per_type"] = []  # per-type breakdown not stored in CSV
    return stats


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 3 — Individual scraper runners
#   Each returns a dict of statistics; raw rows are also written to CSV.
# ═════════════════════════════════════════════════════════════════════════════

BASE_URL = "https://apps.fcc.gov/oetcf/els/reports/GenericSearch.cfm"


def _run_simple_scraper(
    tag: str,
    exp_type_field: str | None,
    exp_keywords: list[str] | None,
    is_sta: bool,
    date_from: str,
    date_to: str,
    csv_path: Path,
) -> dict:
    """
    Generic runner for the four "simple" scrapers:
      UAV STA, UAV CN, All STA, All CN.

    exp_type_field / exp_keywords: if set, selects an Experiment Type dropdown.
    is_sta: if True, filter rows post-scrape to -EX-ST- pattern;
            if False, exclude -EX-ST- (keeps CN rows).
    """
    log.info("[%s] Starting scrape …", tag)
    driver = _make_driver()
    all_rows: list[dict] = []

    try:
        driver.get(BASE_URL)
        time.sleep(2)

        # Experiment Type
        if exp_type_field and exp_keywords:
            val = _find_select_option(driver, exp_type_field, exp_keywords)
            if val:
                _select_by_value(driver, exp_type_field, val)
                log.info("  [%s] Experiment type set → %s", tag, val)
                time.sleep(0.5)
            else:
                log.warning("  [%s] Experiment type not found in dropdown", tag)

        # Status: Granted only
        _set_checkboxes(driver)

        # Date range — try both field-name conventions
        for from_name in ["grant_date_from", "disposalDateFrom", "disposal_date_from",
                          "dateFrom", "date_from"]:
            if _set_field(driver, from_name, date_from):
                log.info("  [%s] Date-from set via '%s'", tag, from_name)
                break
        for to_name in ["grant_date_to", "disposalDateTo", "disposal_date_to",
                        "dateTo", "date_to"]:
            if _set_field(driver, to_name, date_to):
                log.info("  [%s] Date-to   set via '%s'", tag, to_name)
                break

        # Records per page
        _set_field(driver, "show_records", "999")

        # Submit & paginate
        _submit(driver)
        all_rows = _paginate(driver, _extract_simple_rows, tag=tag)

    finally:
        driver.quit()

    # Post-scrape license-type filter
    before = len(all_rows)
    if is_sta:
        all_rows = [r for r in all_rows if STA_PATTERN.match(r["file_number"])]
    else:
        all_rows = [r for r in all_rows if not STA_PATTERN.match(r["file_number"])]
    log.info("[%s] Filter: %d → %d rows", tag, before, len(all_rows))

    # Compute days
    valid_days = _annotate_days(all_rows)
    stats = _summary(valid_days, len(all_rows))
    log.info("[%s] avg=%.1f  min=%s  max=%s  n=%d",
             tag, stats["avg"], stats["min"], stats["max"], stats["count"])

    # CSV export
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    import csv
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "file_number", "applicant", "receipt_date", "grant_date", "days_to_grant"
        ])
        w.writeheader()
        w.writerows(all_rows)
    log.info("[%s] CSV → %s", tag, csv_path)

    return stats


def _run_space_scraper(
    tag: str,
    is_sta: bool,
    date_from: str,
    date_to: str,
    csv_path: Path,
) -> dict:
    """
    Runs the space/satellite scraper across all 8 experiment types.
    Returns aggregate stats PLUS per_type breakdown list.
    """
    log.info("[%s] Starting scrape across %d experiment types …",
             tag, len(SPACE_EXPERIMENT_TYPES))

    driver = _make_driver()
    master: dict[str, dict] = {}
    exp_types_seen: dict[str, list[str]] = defaultdict(list)
    per_type: list[dict] = []
    col_map: dict = {}

    try:
        for exp_label, keywords in SPACE_EXPERIMENT_TYPES:
            log.info("  [%s] → %s", tag, exp_label)
            driver.get(BASE_URL)
            time.sleep(1.5)

            val = _find_select_option(driver, "experiment_type", keywords)
            if val is None:
                log.warning("    Experiment type '%s' not found — skipping", exp_label)
                per_type.append({"label": exp_label, "count": 0, "avg": 0.0,
                                  "skipped": True})
                continue

            _select_by_value(driver, "experiment_type", val)
            time.sleep(0.3)
            _set_checkboxes(driver)

            # Receipt date range (Space scrapers use receipt_date_from/to)
            _set_field(driver, "receipt_date_from", date_from)
            _set_field(driver, "receipt_date_to",   date_to)
            _set_field(driver, "show_records", "100")
            _submit(driver)

            # Paginate
            page = 1
            type_rows_valid = []
            while True:
                log.info("    page %d …", page)
                try:
                    _wait_for_table(driver, first_page=(page == 1))
                except Exception:
                    log.info("    (no results)")
                    break

                if not col_map:
                    col_map = _build_col_map(driver)
                    if col_map:
                        log.info("    Dynamic columns: %s", list(col_map.keys()))

                rows = _extract_dynamic_rows(driver, col_map)

                # Filter to STA or CN
                if is_sta:
                    rows = [r for r in rows
                            if STA_PATTERN.match(r.get("File Number", ""))]
                else:
                    rows = [r for r in rows
                            if not STA_PATTERN.match(r.get("File Number", ""))]

                log.info("    → %d matching rows", len(rows))

                for row in rows:
                    fn = row["File Number"]
                    if fn not in master:
                        master[fn] = dict(row)
                    exp_types_seen[fn].append(exp_label)
                    rd = _parse_date(row.get("Receipt Date", ""))
                    gd = _parse_date(row.get("Grant Date", ""))
                    if rd and gd:
                        type_rows_valid.append((gd - rd).days)

                if not _has_next(driver):
                    break
                _click_next(driver)
                page += 1
                time.sleep(1)

            avg = round(sum(type_rows_valid)/len(type_rows_valid), 1) \
                  if type_rows_valid else 0.0
            per_type.append({
                "label": exp_label,
                "count": len(type_rows_valid),
                "avg":   avg,
            })

    finally:
        driver.quit()

    # Deduplicated aggregate
    all_rows = list(master.values())
    for row in all_rows:
        fn = row["File Number"]
        seen_set: set[str] = set()
        uniq: list[str] = []
        for t in exp_types_seen[fn]:
            if t not in seen_set:
                seen_set.add(t)
                uniq.append(t)
        row["Experiment Types"] = "|".join(uniq)

    valid_days = _annotate_days(all_rows,
                                receipt_key="Receipt Date",
                                grant_key="Grant Date")
    stats = _summary(valid_days, len(all_rows))
    stats["per_type"] = [t for t in per_type if not t.get("skipped")]

    log.info("[%s] Aggregate: avg=%.1f  n=%d  types=%d",
             tag, stats["avg"], stats["count"], len(stats["per_type"]))

    # CSV export
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    import csv
    fixed = ["File Number", "Applicant Name", "Receipt Date",
             "Grant Date", "Days to Grant", "Experiment Types"]
    extra = [c for c in (col_map or {}) if c not in set(fixed)]
    fieldnames = fixed + extra

    # Normalise key names to match fieldnames
    for row in all_rows:
        row.setdefault("Applicant Name", row.get("Applicant Name", ""))
        row["Days to Grant"] = row.get("days_to_grant", "")

    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_rows)
    log.info("[%s] CSV → %s", tag, csv_path)

    return stats



def _infer_exp_type(file_number: str) -> str:
    """
    Best-effort experiment-type label from file number alone.
    The FCC ELS form doesn't always expose this in the list view.
    """
    fn = file_number.upper()
    if "-EX-ST-" in fn:
        return "Special Temporary Authority"
    if "-EX-CN-" in fn:
        return "New License"
    return "Experimental"


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 5 — HTML injection
# ═════════════════════════════════════════════════════════════════════════════

def _build_js_payload(
    uav_sta: dict,
    uav_cn:  dict,
    space_sta: dict,
    space_cn:  dict,
    all_sta: dict,
    all_cn:  dict,
    today_str: str,
) -> str:
    """
    Produce the JavaScript constant block that replaces the sentinel region
    in the HTML file.  All JS constants used by the page are declared here.
    """

    def _bucket_js(d: dict) -> str:
        return json.dumps(d["counts"])

    def _per_type_js(types: list[dict]) -> str:
        items = []
        for t in types:
            items.append(
                "  {{label:{}, count:{}, avg:{}}}".format(
                    json.dumps(t["label"]),
                    t["count"],
                    t["avg"],
                )
            )
        return "[\n" + ",\n".join(items) + "\n]"

    lines = [
        SENTINEL_START,
        f"// Auto-generated by els_dashboard_refresh.py — {today_str}",
        "",
        f"const SNAPSHOT_DATE = {json.dumps(today_str)};",
        "",
        "// ── UAV STA ──────────────────────────────────────────────────",
        f"const UAV_STA = {{",
        f"  counts: {_bucket_js(uav_sta)},",
        f"  total:  {uav_sta['count']},",
        f"  avg:    {uav_sta['avg']},",
        f"  min:    {uav_sta['min']},",
        f"  max:    {uav_sta['max']},",
        f"}};",
        "",
        "// ── UAV CN ──────────────────────────────────────────────────",
        f"const UAV_CN = {{",
        f"  counts: {_bucket_js(uav_cn)},",
        f"  total:  {uav_cn['count']},",
        f"  avg:    {uav_cn['avg']},",
        f"  min:    {uav_cn['min']},",
        f"  max:    {uav_cn['max']},",
        f"}};",
        "",
        "// ── Space STA by experiment type ─────────────────────────────",
        f"const SPACE_STA_TYPES = {_per_type_js(space_sta.get('per_type', []))};",
        "",
        f"const SPACE_STA = {{",
        f"  total: {space_sta['count']},",
        f"  avg:   {space_sta['avg']},",
        f"  min:   {space_sta['min']},",
        f"  max:   {space_sta['max']},",
        f"}};",
        "",
        "// ── Space CN by experiment type ──────────────────────────────",
        f"const SPACE_CN_TYPES = {_per_type_js(space_cn.get('per_type', []))};",
        "",
        f"const SPACE_CN = {{",
        f"  total: {space_cn['count']},",
        f"  avg:   {space_cn['avg']},",
        f"  min:   {space_cn['min']},",
        f"  max:   {space_cn['max']},",
        f"}};",
        "",
        "// ── All Types STA ─────────────────────────────────────────────",
        f"const ALL_STA = {{",
        f"  counts: {_bucket_js(all_sta)},",
        f"  total:  {all_sta['count']},",
        f"  avg:    {all_sta['avg']},",
        f"}};",
        "",
        "// ── All Types CN ──────────────────────────────────────────────",
        f"const ALL_CN = {{",
        f"  counts: {_bucket_js(all_cn)},",
        f"  total:  {all_cn['count']},",
        f"  avg:    {all_cn['avg']},",
        f"}};",
        "",
        SENTINEL_END,
    ]
    return "\n".join(lines)


def _inject_into_html(html_path: Path, js_block: str) -> None:
    """
    Replace the sentinel region inside the HTML file with js_block.
    Backs up the existing file first.
    """
    html = html_path.read_text(encoding="utf-8")

    # Verify sentinels exist
    if SENTINEL_START not in html:
        raise ValueError(
            f"Sentinel '{SENTINEL_START}' not found in {html_path}.\n"
            "Add  // @@LIVE_DATA_START  and  // @@LIVE_DATA_END  comments\n"
            "inside the <script> block to mark the data region."
        )
    if SENTINEL_END not in html:
        raise ValueError(
            f"Sentinel '{SENTINEL_END}' not found in {html_path}."
        )

    # Backup
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = html_path.with_name(f"{html_path.stem}_backup_{ts}{html_path.suffix}")
    shutil.copy2(html_path, backup)
    log.info("Backup written → %s", backup)

    # Replace region
    pattern = re.compile(
        re.escape(SENTINEL_START) + r".*?" + re.escape(SENTINEL_END),
        re.DOTALL,
    )
    new_html, n = pattern.subn(js_block, html)
    if n != 1:
        raise RuntimeError(
            f"Expected exactly 1 sentinel region, found {n}. Aborting injection."
        )

    html_path.write_text(new_html, encoding="utf-8")
    log.info("HTML updated → %s", html_path)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 6 — Metric-card & hero updater
#   The HTML also has hard-coded numbers inside metric-card divs.
#   We patch those too using targeted regex replacements.
# ═════════════════════════════════════════════════════════════════════════════

def _patch_metric_cards(html: str,
                        uav_sta, uav_cn,
                        space_sta, space_cn,
                        all_sta, all_cn,
                        today_label: str) -> str:
    """
    Update the inline metric values already present in the HTML markup.
    This is purely cosmetic — the <script> data block is the authoritative
    source; these are the visible numbers rendered before JS runs.
    Each metric-value div has a unique class+context we can key off.
    """
    # Rather than fragile regex on every number, we update the JS data block
    # and let the page render from there.  The static HTML metric cards are
    # intentionally left for JS to populate on DOMContentLoaded.
    # We do, however, update the nav date.
    html = re.sub(
        r'(<span class="nav-date">)[^<]*(</span>)',
        rf'\g<1>{today_label}\g<2>',
        html,
    )
    # Hero sub-text date range
    html = re.sub(
        r'(January 1\s*[–—-]\s*)[A-Za-z]+ \d+, \d{4}',
        rf'\g<1>{today_label}',
        html,
    )
    # ── Summary-box metric cards (split STA / CN) ─────────────────────────────
    # Each card is keyed by a unique data-metric attribute or id in the HTML.
    # We patch the visible avg value and count for each of the six scrapers.
    replacements = {
        "uav-sta-avg":   f"{uav_sta['avg']:.1f}",
        "uav-cn-avg":    f"{uav_cn['avg']:.1f}",
        "space-sta-avg": f"{space_sta['avg']:.1f}",
        "space-cn-avg":  f"{space_cn['avg']:.1f}",
        "all-sta-avg":   f"{all_sta['avg']:.1f}",
        "all-cn-avg":    f"{all_cn['avg']:.1f}",
        "uav-sta-count":   str(uav_sta['count']),
        "uav-cn-count":    str(uav_cn['count']),
        "space-sta-count": str(space_sta['count']),
        "space-cn-count":  str(space_cn['count']),
        "all-sta-count":   str(all_sta['count']),
        "all-cn-count":    str(all_cn['count']),
    }
    for metric_id, value in replacements.items():
        html = re.sub(
            rf'(data-metric="{re.escape(metric_id)}"[^>]*>)[^<]*(<)',
            rf'\g<1>{value}\g<2>',
            html,
        )
    return html


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 7 — Main entry point
# ═════════════════════════════════════════════════════════════════════════════

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Refresh the FCC ELS Grant-Time Dashboard HTML with live data."
    )
    p.add_argument(
        "--html",
        default=None,
        help=f"Path to {DEFAULT_HTML_NAME}  (default: same directory as this script)",
    )
    p.add_argument(
        "--csv-dir",
        default=DEFAULT_CSV_DIR,
        help=f"Directory for CSV exports  (default: {DEFAULT_CSV_DIR})",
    )
    p.add_argument(
        "--date-from",
        default=DEFAULT_DATE_FROM,
        help=f"Start of grant-date window  (default: {DEFAULT_DATE_FROM})",
    )
    p.add_argument(
        "--reuse-csv",
        action="store_true",
        help="If today's CSV already exists for a scraper, load it instead of re-scraping.",
    )
    return p.parse_args()


def main() -> int:
    args  = _parse_args()
    today = date.today()
    today_str   = today.strftime(DATE_FORMAT)       # 04/17/2026
    today_label = today.strftime("%B %#d, %Y")      # April 17, 2026
    csv_dir     = Path(args.csv_dir)
    ds          = today.strftime("%Y%m%d")

    # Resolve HTML path
    if args.html:
        html_path = Path(args.html).expanduser().resolve()
    else:
        html_path = Path(__file__).parent / DEFAULT_HTML_NAME

    if not html_path.exists():
        log.error("HTML file not found: %s", html_path)
        log.error("Pass --html PATH or place %s alongside this script.", DEFAULT_HTML_NAME)
        return 2

    log.info("═" * 68)
    log.info("FCC ELS Dashboard Refresh")
    log.info("Date range : %s → %s", args.date_from, today_str)
    log.info("HTML target: %s", html_path)
    log.info("CSV output : %s/", csv_dir)
    log.info("═" * 68)

    results: dict[str, dict | list] = {}
    failures: list[str] = []

    # ── 1. UAV STA ──────────────────────────────────────────────────────────
    log.info("")
    log.info("── [1/6] UAV Special Temporary Authority ──────────────────────")
    _csv_uav_sta = csv_dir / f"uav_sta_{ds}.csv"
    try:
        if args.reuse_csv and _csv_uav_sta.exists():
            log.info("[UAV-STA] Reusing existing CSV: %s", _csv_uav_sta.name)
            results["uav_sta"] = _stats_from_simple_csv(_csv_uav_sta)
        else:
            results["uav_sta"] = _run_simple_scraper(
                tag            = "UAV-STA",
                exp_type_field = "experiment_type",
                exp_keywords   = ["uav", "unmanned", "aerial"],
                is_sta         = True,
                date_from      = args.date_from,
                date_to        = today_str,
                csv_path       = _csv_uav_sta,
            )
    except Exception:
        log.error("UAV STA scrape FAILED:\n%s", traceback.format_exc())
        failures.append("UAV STA")

    # ── 2. UAV CN ───────────────────────────────────────────────────────────
    log.info("")
    log.info("── [2/6] UAV New License (CN) ──────────────────────────────────")
    _csv_uav_cn = csv_dir / f"uav_cn_{ds}.csv"
    try:
        if args.reuse_csv and _csv_uav_cn.exists():
            log.info("[UAV-CN] Reusing existing CSV: %s", _csv_uav_cn.name)
            results["uav_cn"] = _stats_from_simple_csv(_csv_uav_cn)
        else:
            results["uav_cn"] = _run_simple_scraper(
                tag            = "UAV-CN",
                exp_type_field = "experiment_type",
                exp_keywords   = ["uav", "unmanned", "aerial"],
                is_sta         = False,
                date_from      = args.date_from,
                date_to        = today_str,
                csv_path       = _csv_uav_cn,
            )
    except Exception:
        log.error("UAV CN scrape FAILED:\n%s", traceback.format_exc())
        failures.append("UAV CN")

    # ── 3. Space STA ────────────────────────────────────────────────────────
    log.info("")
    log.info("── [3/6] Space STA ─────────────────────────────────────────────")
    _csv_space_sta = csv_dir / f"space_sta_{ds}.csv"
    try:
        if args.reuse_csv and _csv_space_sta.exists():
            log.info("[SPACE-STA] Reusing existing CSV: %s", _csv_space_sta.name)
            results["space_sta"] = _stats_from_space_csv(_csv_space_sta)
        else:
            results["space_sta"] = _run_space_scraper(
                tag       = "SPACE-STA",
                is_sta    = True,
                date_from = args.date_from,
                date_to   = today_str,
                csv_path  = _csv_space_sta,
            )
    except Exception:
        log.error("Space STA scrape FAILED:\n%s", traceback.format_exc())
        failures.append("Space STA")

    # ── 4. Space CN ─────────────────────────────────────────────────────────
    log.info("")
    log.info("── [4/6] Space CN ──────────────────────────────────────────────")
    _csv_space_cn = csv_dir / f"space_cn_{ds}.csv"
    try:
        if args.reuse_csv and _csv_space_cn.exists():
            log.info("[SPACE-CN] Reusing existing CSV: %s", _csv_space_cn.name)
            results["space_cn"] = _stats_from_space_csv(_csv_space_cn)
        else:
            results["space_cn"] = _run_space_scraper(
                tag       = "SPACE-CN",
                is_sta    = False,
                date_from = args.date_from,
                date_to   = today_str,
                csv_path  = _csv_space_cn,
            )
    except Exception:
        log.error("Space CN scrape FAILED:\n%s", traceback.format_exc())
        failures.append("Space CN")

    # ── 5. All Types STA ────────────────────────────────────────────────────
    log.info("")
    log.info("── [5/6] All Experiment Types — STA ────────────────────────────")
    _csv_all_sta = csv_dir / f"all_sta_{ds}.csv"
    try:
        if args.reuse_csv and _csv_all_sta.exists():
            log.info("[ALL-STA] Reusing existing CSV: %s", _csv_all_sta.name)
            results["all_sta"] = _stats_from_simple_csv(_csv_all_sta)
        else:
            results["all_sta"] = _run_simple_scraper(
                tag            = "ALL-STA",
                exp_type_field = None,
                exp_keywords   = None,
                is_sta         = True,
                date_from      = args.date_from,
                date_to        = today_str,
                csv_path       = _csv_all_sta,
            )
    except Exception:
        log.error("All STA scrape FAILED:\n%s", traceback.format_exc())
        failures.append("All STA")

    # ── 6. All Types CN ─────────────────────────────────────────────────────
    log.info("")
    log.info("── [6/6] All Experiment Types — CN ─────────────────────────────")
    _csv_all_cn = csv_dir / f"all_cn_{ds}.csv"
    try:
        if args.reuse_csv and _csv_all_cn.exists():
            log.info("[ALL-CN] Reusing existing CSV: %s", _csv_all_cn.name)
            results["all_cn"] = _stats_from_simple_csv(_csv_all_cn)
        else:
            results["all_cn"] = _run_simple_scraper(
                tag            = "ALL-CN",
                exp_type_field = None,
                exp_keywords   = None,
                is_sta         = False,
                date_from      = args.date_from,
                date_to        = today_str,
                csv_path       = _csv_all_cn,
            )
    except Exception:
        log.error("All CN scrape FAILED:\n%s", traceback.format_exc())
        failures.append("All CN")

    
    # ── Fail-safe: abort HTML update if any core scraper failed ──────────────
    if failures:
        log.error("")
        log.error("═" * 68)
        log.error("FAILED scrapers: %s", ", ".join(failures))
        log.error("HTML file has NOT been modified (fail-safe).")
        log.error("═" * 68)
        return 1

    # ── Build JS payload ──────────────────────────────────────────────────────
    log.info("")
    log.info("── Building JS payload ─────────────────────────────────────────")
    js_block = _build_js_payload(
        uav_sta   = results["uav_sta"],
        uav_cn    = results["uav_cn"],
        space_sta = results["space_sta"],
        space_cn  = results["space_cn"],
        all_sta   = results["all_sta"],
        all_cn    = results["all_cn"],
        today_str = today_label,
    )

    # ── Inject into HTML ─────────────────────────────────────────────────────
    log.info("── Injecting into HTML ─────────────────────────────────────────")
    try:
        _inject_into_html(html_path, js_block)
    except (ValueError, RuntimeError) as exc:
        log.error("HTML injection failed: %s", exc)
        return 2

    # ── Patch visible metric cards & labels ──────────────────────────────────
    html = html_path.read_text(encoding="utf-8")
    html = _patch_metric_cards(
        html,
        uav_sta   = results["uav_sta"],
        uav_cn    = results["uav_cn"],
        space_sta = results["space_sta"],
        space_cn  = results["space_cn"],
        all_sta   = results["all_sta"],
        all_cn    = results["all_cn"],
        today_label   = today_label,
    )
    html_path.write_text(html, encoding="utf-8")

    # ── Summary ──────────────────────────────────────────────────────────────
    log.info("")
    log.info("═" * 68)
    log.info("✓  Dashboard refresh complete — %s", today_label)
    log.info("")
    log.info("  UAV  STA  avg %5.1f d  n=%d",
             results["uav_sta"]["avg"],  results["uav_sta"]["count"])
    log.info("  UAV  CN   avg %5.1f d  n=%d",
             results["uav_cn"]["avg"],   results["uav_cn"]["count"])
    log.info("  Space STA avg %5.1f d  n=%d",
             results["space_sta"]["avg"], results["space_sta"]["count"])
    log.info("  Space CN  avg %5.1f d  n=%d",
             results["space_cn"]["avg"],  results["space_cn"]["count"])
    log.info("  All  STA  avg %5.1f d  n=%d",
             results["all_sta"]["avg"],  results["all_sta"]["count"])
    log.info("  All  CN   avg %5.1f d  n=%d",
             results["all_cn"]["avg"],   results["all_cn"]["count"])
    log.info("")
    log.info("  HTML → %s", html_path)
    log.info("  CSVs → %s/", csv_dir)
    log.info("═" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
