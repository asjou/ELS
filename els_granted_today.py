"""
FCC ELS — Granted Applications Stats Builder
─────────────────────────────────────────────
1. Scrapes https://apps.fcc.gov/oetcf/els/reports/GenericSearch.cfm
   • Disposal Date Range: 01/01/2026 → today
   • Application Status:  Granted
   • Application Type:    All
2. Paginates through all result pages (100 records/page)
3. Classifies each record by Application Type via File Number code
4. Writes els_granted_today.html  (same design language as fcc_els_pending_stats.html)

Usage:
    python els_granted_today.py [--skip-scrape] [--debug]

    --skip-scrape   Re-render HTML from the most-recent CSV without hitting FCC.
    --debug         Dump all form input names/values and save a screenshot before
                    submitting; then exit without writing CSV or HTML.

Application Type codes in File Number  (YYYY-EX-<CODE>-NNNNNN):
    ST  → Special Temporary Authority (STA)
    CN  → New License
    MD  → Modification
    RN  → Renewal
    TC  → Transfer of Control
    AS  → Assignment
    (anything else → Other)
"""

import csv
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

# ── Paths & constants ─────────────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).parent
OUTPUT_HTML  = SCRIPT_DIR / "els_granted_today.html"
TODAY        = date.today()
TODAY_LABEL  = TODAY.strftime("%B %d, %Y").replace(" 0", " ")
DATE_FROM    = "01/01/2026"
DATE_TO      = TODAY.strftime("%m/%d/%Y")

BASE_URL         = "https://apps.fcc.gov/oetcf/els/reports/GenericSearch.cfm"
RECORDS_PER_PAGE = 100

# Application type display order + labels
APP_TYPES = [
    ("ST", "Special Temporary Authority"),
    ("CN", "New License"),
    ("MD", "Modification"),
    ("RN", "Renewal"),
    ("TC", "Transfer of Control"),
    ("AS", "Assignment"),
]
APP_TYPE_CODES  = [t[0] for t in APP_TYPES]
APP_TYPE_LABELS = {t[0]: t[1] for t in APP_TYPES}

CSV_HEADERS = ["File Number", "Call Sign", "Applicant Name", "Receipt Date",
               "Status", "Status Date"]

# Column indices in raw <td> list
# Row layout: ['', 'InitialCurrent', 'N/A', '', 'N/A', 'N/A',
#              FileNum(6), CallSign(7), Name(8), ReceiptDate(9), Status(10), StatusDate(11)]
COL_INDICES = [6, 7, 8, 9, 10, 11]


# ── Scraper ───────────────────────────────────────────────────────────────────
def parse_rows(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if "FileNumber" in headers and "Applicant Name" in headers:
            rows = []
            for tr in table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(cells) > max(COL_INDICES):
                    row = [cells[i] for i in COL_INDICES]
                    if re.match(r'\d{4}-EX-', row[0]):
                        rows.append(row)
            return rows
    return []


def scrape() -> Path:
    csv_path = SCRIPT_DIR / f"fcc_els_granted_{TODAY.strftime('%Y%m%d')}.csv"
    all_rows = []

    with sync_playwright() as pw:
        browser = pw.firefox.launch(headless=True)
        page    = browser.new_page()

        print("▶ Loading FCC ELS search form …")
        try:
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30_000)
        except PWTimeout:
            pass

        # ── Status: uncheck all, check only Granted ──
        for name in ["all", "pending", "dismissed", "expired"]:
            el = page.query_selector(f'input[name="{name}"]')
            if el and el.is_checked():
                el.click()

        granted = page.query_selector('input[name="granted"]')
        if granted and not granted.is_checked():
            granted.click()

        # ── Grant Date Range ──
        page.fill('input[name="grant_date_from"]', DATE_FROM)
        page.fill('input[name="grant_date_to"]',   DATE_TO)

        # ── Records per page ──
        sr = page.query_selector('input[name="show_records"]')
        if sr:
            sr.fill(str(RECORDS_PER_PAGE))

        # ── Debug: dump inputs and screenshot before submitting ──
        if "--debug" in sys.argv:
            print("\n── DEBUG: All <input> elements on the page ──")
            inputs = page.query_selector_all("input")
            for inp in inputs:
                name  = inp.get_attribute("name")  or "(no name)"
                itype = inp.get_attribute("type")  or "text"
                val   = ""
                try:
                    val = inp.input_value()
                except Exception:
                    pass
                checked = ""
                if itype in ("checkbox", "radio"):
                    try:
                        checked = "  [CHECKED]" if inp.is_checked() else "  [unchecked]"
                    except Exception:
                        pass
                print(f"  name={name!r:35s}  type={itype:10s}  value={val!r}{checked}")

            print("\n── DEBUG: All <select> elements on the page ──")
            selects = page.query_selector_all("select")
            for sel in selects:
                name = sel.get_attribute("name") or "(no name)"
                try:
                    val = sel.input_value()
                except Exception:
                    val = "?"
                print(f"  name={name!r:35s}  selected={val!r}")

            shot = SCRIPT_DIR / "debug_screenshot.png"
            page.screenshot(path=str(shot), full_page=True)
            print(f"\n── DEBUG: Screenshot saved → {shot.name}")
            browser.close()
            print("\nExiting (--debug). No CSV or HTML written.")
            sys.exit(0)

        page.click('input[type="submit"]')
        try:
            page.wait_for_selector("table", timeout=25_000)
        except PWTimeout:
            pass

        body_text = page.inner_text("body")
        m = re.search(r'([\d,]+)\s+applications?\s+were\s+found', body_text, re.IGNORECASE)
        total = int(m.group(1).replace(",", "")) if m else 0
        print(f"  Found {total} granted applications. Paginating …\n")

        page_num = 1
        while True:
            rows = parse_rows(page.content())
            all_rows.extend(rows)
            print(f"  Page {page_num}: {len(rows)} rows  (running total: {len(all_rows)})")

            nxt = page.query_selector('form[name="next_result"] input[type="submit"]')
            if not nxt:
                break
            nxt.click()
            try:
                page.wait_for_selector("table", timeout=20_000)
            except PWTimeout:
                pass
            page_num += 1

        browser.close()

    # Deduplicate by File Number (keep last)
    seen = {}
    for row in all_rows:
        seen[row[0]] = row
    unique = list(seen.values())

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADERS)
        writer.writerows(unique)

    print(f"\n✓ CSV: {csv_path.name}  ({len(unique)} unique records)")
    return csv_path


def find_latest_csv() -> Path:
    candidates = sorted(
        SCRIPT_DIR.glob("fcc_els_granted_*.csv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError("No fcc_els_granted_*.csv found. Run without --skip-scrape.")
    return candidates[0]


# ── Data loading & classification ─────────────────────────────────────────────
def app_type_code(file_number: str) -> str:
    """Extract the two-letter type code from YYYY-EX-<CODE>-NNNNNN."""
    parts = file_number.split("-")
    code  = parts[2].upper() if len(parts) >= 3 else ""
    return code if code in APP_TYPE_CODES else "OT"


def parse_date(s: str):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def load_rows(csv_path: Path) -> list:
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            fn          = r.get("File Number",    "").strip()
            callsign    = r.get("Call Sign",       "").strip()
            applicant   = r.get("Applicant Name", "").strip()
            receipt_raw = r.get("Receipt Date",   "").strip()
            status_raw  = r.get("Status Date",    "").strip()

            receipt_dt  = parse_date(receipt_raw)
            status_dt   = parse_date(status_raw)

            rows.append({
                "file":        fn,
                "callsign":    callsign,
                "applicant":   applicant,
                "received":    receipt_raw,
                "status_date": status_raw,
                "status_dt":   status_dt,    # for sorting
                "type_code":   app_type_code(fn),
            })

    # Sort: most-recently granted first
    rows.sort(key=lambda x: x["status_dt"] or date.min, reverse=True)
    return rows


# ── Stats ─────────────────────────────────────────────────────────────────────
def compute_type_counts(rows: list) -> dict:
    """Return {type_code: count} for all known types."""
    counts = {code: 0 for code, _ in APP_TYPES}
    counts["OT"] = 0
    for r in rows:
        tc = r["type_code"]
        counts[tc] = counts.get(tc, 0) + 1
    return counts


# ── HTML builder ──────────────────────────────────────────────────────────────
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FCC ELS — Granted Applications 2026</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Figtree:ital,wght@0,300;0,400;0,500;0,600;0,700;0,800;0,900;1,300;1,400&display=swap" rel="stylesheet">
<style>
/* ── Reset & Base ── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { scroll-behavior: smooth; -webkit-font-smoothing: antialiased; }

:root {
  --white: #ffffff;
  --bg: #f5f5f7;
  --bg2: #e8e8ed;
  --surface: #ffffff;
  --border: rgba(0,0,0,0.08);
  --border-med: rgba(0,0,0,0.12);
  --text: #1d1d1f;
  --text-secondary: #6e6e73;
  --text-tertiary: #a1a1a6;

  --green-accent: #1a9b5e;
  --green-soft: #e3f6ee;
  --green-mid: #4dc790;

  --blue-accent: #0071e3;
  --blue-soft: #e5f0ff;
  --blue-mid: #5facf5;

  --amber: #bf5900;
  --amber-soft: #fff3e5;
  --amber-mid: #f5a623;

  --red-accent: #c1362a;
  --red-soft: #ffebea;
  --red-mid: #e87370;

  --purple-accent: #6e3ef5;
  --purple-soft: #f0ebff;
  --purple-mid: #b89ef9;

  --teal-accent: #0a7d6e;
  --teal-soft: #e0f5f2;

  --radius-sm: 10px;
  --radius: 16px;
  --radius-lg: 22px;

  --shadow-sm: 0 2px 8px rgba(0,0,0,0.06), 0 0 0 0.5px rgba(0,0,0,0.06);
  --shadow: 0 4px 20px rgba(0,0,0,0.08), 0 0 0 0.5px rgba(0,0,0,0.06);
  --shadow-hover: 0 8px 32px rgba(0,0,0,0.12), 0 0 0 0.5px rgba(0,0,0,0.08);
}

body {
  background: var(--bg);
  color: var(--text);
  font-family: 'Figtree', -apple-system, BlinkMacSystemFont, sans-serif;
  font-size: 15px;
  line-height: 1.5;
  min-height: 100vh;
}

/* ══ NAV ══ */
nav {
  position: sticky; top: 0; z-index: 200;
  background: rgba(255,255,255,0.85);
  backdrop-filter: saturate(180%) blur(20px);
  -webkit-backdrop-filter: saturate(180%) blur(20px);
  border-bottom: 0.5px solid rgba(0,0,0,0.1);
  height: 52px; display: flex; align-items: center;
  padding: 0 max(24px, calc(50% - 580px)); gap: 0;
}
.nav-brand { font-size: 17px; font-weight: 700; color: var(--text); letter-spacing: -0.02em; flex-shrink: 0; margin-right: auto; }
.nav-links { display: flex; gap: 4px; overflow-x: auto; scrollbar-width: none; }
.nav-links::-webkit-scrollbar { display: none; }
.nav-link { font-size: 13px; font-weight: 500; color: var(--text-secondary); text-decoration: none; padding: 6px 14px; border-radius: 20px; transition: background 0.15s, color 0.15s; white-space: nowrap; }
.nav-link:hover { background: var(--bg); color: var(--text); }
.nav-date { font-size: 12px; color: var(--text-tertiary); margin-left: 16px; white-space: nowrap; flex-shrink: 0; }

/* ══ HERO ══ */
.hero {
  background: var(--white);
  padding: 80px max(24px, calc(50% - 580px)) 72px;
  text-align: center;
}
.hero-eyebrow { font-size: 13px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; color: var(--green-accent); margin-bottom: 18px; }
.hero-title { font-size: clamp(38px, 5.5vw, 66px); font-weight: 800; letter-spacing: -0.03em; line-height: 1.05; color: var(--text); margin-bottom: 20px; }
.hero-title .thin { font-weight: 300; }
.hero-sub { font-size: 18px; font-weight: 400; color: var(--text-secondary); max-width: 560px; margin: 0 auto 52px; line-height: 1.6; }

/* ── Hero bucket bar ── */
.hero-buckets {
  max-width: 820px;
  margin: 0 auto 0;
  background: var(--white);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow);
  overflow: hidden;
  border: 0.5px solid var(--border-med);
}
.hero-buckets-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 16px 24px 12px;
  border-bottom: 0.5px solid var(--border);
}
.hero-buckets-title { font-size: 13px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; color: var(--text-tertiary); }
.hero-total-badge {
  font-size: 13px; font-weight: 700;
  background: var(--green-soft); color: var(--green-accent);
  padding: 4px 12px; border-radius: 20px;
}

.hero-bucket-grid {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 1px;
  background: var(--border);
}
.hero-bucket-cell {
  background: var(--white);
  padding: 20px 12px 18px;
  text-align: center;
  transition: background 0.15s;
  cursor: default;
}
.hero-bucket-cell:hover { background: #fafafa; }

.hbc-count {
  font-size: 32px; font-weight: 700; letter-spacing: -0.04em; line-height: 1;
  margin-bottom: 6px; font-variant-numeric: tabular-nums;
}
.hbc-label { font-size: 11px; font-weight: 600; color: var(--text-secondary); margin-bottom: 6px; }
.hbc-bar-track { background: var(--bg); border-radius: 99px; height: 4px; overflow: hidden; margin: 0 4px; }
.hbc-bar-fill  { height: 100%; border-radius: 99px; width: 0; transition: width 1.1s cubic-bezier(0.4,0,0.2,1); }

/* Per-type colours */
.hbc-ST .hbc-count { color: var(--blue-accent); }
.hbc-ST .hbc-bar-fill { background: linear-gradient(90deg, var(--blue-accent), var(--blue-mid)); }
.hbc-CN .hbc-count { color: var(--green-accent); }
.hbc-CN .hbc-bar-fill { background: linear-gradient(90deg, var(--green-accent), var(--green-mid)); }
.hbc-MD .hbc-count { color: var(--amber); }
.hbc-MD .hbc-bar-fill { background: linear-gradient(90deg, var(--amber), var(--amber-mid)); }
.hbc-RN .hbc-count { color: var(--teal-accent); }
.hbc-RN .hbc-bar-fill { background: linear-gradient(90deg, var(--teal-accent), #2bbda0); }
.hbc-TC .hbc-count { color: var(--purple-accent); }
.hbc-TC .hbc-bar-fill { background: linear-gradient(90deg, var(--purple-accent), var(--purple-mid)); }
.hbc-AS .hbc-count { color: var(--red-accent); }
.hbc-AS .hbc-bar-fill { background: linear-gradient(90deg, var(--red-accent), var(--red-mid)); }

/* ── Hero stat row ── */
.hero-stats-row {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 1px; background: var(--border);
  border-top: 0.5px solid var(--border);
}
.hero-stat { background: var(--white); padding: 16px 12px; text-align: center; }
.hero-stat-val { font-size: 26px; font-weight: 700; letter-spacing: -0.03em; color: var(--green-accent); font-variant-numeric: tabular-nums; }
.hero-stat-lbl { font-size: 11px; font-weight: 500; color: var(--text-tertiary); margin-top: 2px; }

/* ══ PAGE ══ */
.page { padding: 0 max(24px, calc(50% - 580px)) 100px; }
.section { padding-top: 80px; }

.section-eyebrow { display: inline-flex; align-items: center; gap: 6px; font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; margin-bottom: 12px; }
.dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
.dot.granted { background: var(--green-accent); }
.eyebrow-text.granted { color: var(--green-accent); }

.section-title { font-size: clamp(26px, 4vw, 36px); font-weight: 700; letter-spacing: -0.025em; color: var(--text); margin-bottom: 6px; line-height: 1.1; }
.section-desc { font-size: 15px; color: var(--text-secondary); margin-bottom: 32px; max-width: 560px; }

/* ══ TYPE SECTIONS ══ */
.type-sections-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
@media (max-width: 800px) { .type-sections-grid { grid-template-columns: 1fr; } }

/* ══ TABLE CARD ══ */
.table-card {
  background: var(--white);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  overflow: hidden;
  transition: box-shadow 0.2s;
  margin-bottom: 0;
}
.table-card:hover { box-shadow: var(--shadow); }
.table-card.full-width { grid-column: 1 / -1; }

.table-header { padding: 16px 24px; border-bottom: 0.5px solid var(--border); display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.table-header-title { font-size: 14px; font-weight: 600; color: var(--text); }
.type-pill { font-size: 10px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; padding: 3px 9px; border-radius: 20px; flex-shrink: 0; }
.count-badge { font-size: 12px; font-weight: 600; color: var(--text-secondary); margin-left: auto; }

/* Pill colours per type */
.pill-ST { background: var(--blue-soft);   color: var(--blue-accent); }
.pill-CN { background: var(--green-soft);  color: var(--green-accent); }
.pill-MD { background: var(--amber-soft);  color: var(--amber); }
.pill-RN { background: var(--teal-soft);   color: var(--teal-accent); }
.pill-TC { background: var(--purple-soft); color: var(--purple-accent); }
.pill-AS { background: var(--red-soft);    color: var(--red-accent); }

table { width: 100%; border-collapse: collapse; }
thead th { font-size: 11px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; color: var(--text-tertiary); padding: 10px 20px; text-align: left; background: #fafafa; border-bottom: 0.5px solid var(--border); }
thead th.r { text-align: right; }
tbody tr { transition: background 0.12s; }
tbody tr:hover { background: #f9f9fb; }
tbody td { padding: 11px 20px; border-bottom: 0.5px solid var(--border); font-size: 13px; vertical-align: middle; }
tbody tr:last-child td { border-bottom: none; }

.td-file { font-family: 'SF Mono','Fira Code',monospace; font-size: 11.5px; color: var(--blue-accent); white-space: nowrap; font-weight: 500; }
.td-applicant { color: var(--text); font-weight: 500; max-width: 260px; font-size: 13px; }
.td-date { font-family: 'SF Mono','Fira Code',monospace; font-size: 11px; color: var(--text-tertiary); white-space: nowrap; }
.td-callsign { font-family: 'SF Mono','Fira Code',monospace; font-size: 11px; color: var(--text-secondary); white-space: nowrap; }

/* Scrollable app list wrapper */
.app-list-wrap { overflow-y: auto; max-height: 420px; }

/* ══ FOOTER ══ */
.divider { height: 0.5px; background: var(--border-med); margin-top: 80px; }
footer { padding: 40px max(24px, calc(50% - 580px)); display: flex; justify-content: space-between; align-items: flex-start; gap: 24px; flex-wrap: wrap; }
footer p { font-size: 12px; color: var(--text-tertiary); line-height: 1.7; }
footer a { color: var(--blue-accent); text-decoration: none; }
footer a:hover { text-decoration: underline; }

/* ══ SCROLL FADE ══ */
.fade-up { opacity: 0; transform: translateY(20px); transition: opacity 0.55s ease, transform 0.55s ease; }
.fade-up.visible { opacity: 1; transform: translateY(0); }

/* ══ RESPONSIVE ══ */
@media (max-width: 820px) {
  .hero-bucket-grid { grid-template-columns: repeat(3, 1fr); }
  .hero-stats-row   { grid-template-columns: repeat(3, 1fr); }
}
@media (max-width: 560px) {
  .hero-bucket-grid { grid-template-columns: repeat(2, 1fr); }
  .hero-stats-row   { grid-template-columns: repeat(2, 1fr); }
  .hero-title { font-size: 34px; }
  .nav-date { display: none; }
  thead th, tbody td { padding-left: 14px; padding-right: 14px; }
}

[id] { scroll-margin-top: 66px; }
</style>
</head>
<body>

<!-- NAV -->
<nav>
  <span class="nav-brand">FCC ELS</span>
  <div class="nav-links">
    <a class="nav-link" href="#granted-types">By Type</a>
    <a class="nav-link" href="#sta">STA</a>
    <a class="nav-link" href="#cn">New License</a>
    <a class="nav-link" href="#md">Modification</a>
    <a class="nav-link" href="#rn">Renewal</a>
    <a class="nav-link" href="#tc">Transfer</a>
    <a class="nav-link" href="#as">Assignment</a>
  </div>
  <span class="nav-date">@@TODAY_LABEL@@</span>
</nav>

<!-- HERO -->
<section class="hero">
  <p class="hero-eyebrow">Experimental Licensing System · Granted 2026</p>
  <h1 class="hero-title">License grants<br><span class="thin">since January 1, 2026</span></h1>
  <p class="hero-sub">All ELS applications granted from 01/01/2026 through @@TODAY_LABEL@@, broken down by Application Type.</p>

  <div class="hero-buckets" id="granted-types">
    <div class="hero-buckets-header">
      <span class="hero-buckets-title">Grants by Application Type</span>
      <span class="hero-total-badge" id="hero-total-badge">— total</span>
    </div>
    <div class="hero-bucket-grid" id="hero-bucket-grid">
      <!-- injected by JS -->
    </div>
    <div class="hero-stats-row">
      <div class="hero-stat">
        <div class="hero-stat-val" id="hs-total">—</div>
        <div class="hero-stat-lbl">Total granted</div>
      </div>
      <div class="hero-stat">
        <div class="hero-stat-val" id="hs-sta">—</div>
        <div class="hero-stat-lbl">Special Temp Auth</div>
      </div>
      <div class="hero-stat">
        <div class="hero-stat-val" id="hs-cn">—</div>
        <div class="hero-stat-lbl">New Licenses</div>
      </div>
    </div>
  </div>
</section>

<!-- PAGE -->
<div class="page">

  <!-- PER-TYPE APPLICATION LISTS -->
  <section class="section fade-up" id="sta-cn">
    <div class="section-eyebrow"><span class="dot granted"></span><span class="eyebrow-text granted">Granted</span></div>
    <h2 class="section-title">Applications by Type</h2>
    <p class="section-desc">Each panel lists applications of that type, sorted by grant date — most recent on top.</p>

    <div class="type-sections-grid">

      <!-- STA -->
      <div class="table-card fade-up" id="sta">
        <div class="table-header">
          <span class="type-pill pill-ST">STA</span>
          <span class="table-header-title">Special Temporary Authority</span>
          <span class="count-badge" id="count-ST">—</span>
        </div>
        <div class="app-list-wrap">
          <table id="apps-ST">
            <thead><tr><th>File Number</th><th>Applicant</th><th>Call Sign</th><th class="r">Grant Date</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </div>

      <!-- New License -->
      <div class="table-card fade-up" id="cn">
        <div class="table-header">
          <span class="type-pill pill-CN">New License</span>
          <span class="table-header-title">New License</span>
          <span class="count-badge" id="count-CN">—</span>
        </div>
        <div class="app-list-wrap">
          <table id="apps-CN">
            <thead><tr><th>File Number</th><th>Applicant</th><th>Call Sign</th><th class="r">Grant Date</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </div>

      <!-- Modification -->
      <div class="table-card fade-up" id="md">
        <div class="table-header">
          <span class="type-pill pill-MD">Modification</span>
          <span class="table-header-title">Modification</span>
          <span class="count-badge" id="count-MD">—</span>
        </div>
        <div class="app-list-wrap">
          <table id="apps-MD">
            <thead><tr><th>File Number</th><th>Applicant</th><th>Call Sign</th><th class="r">Grant Date</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </div>

      <!-- Renewal -->
      <div class="table-card fade-up" id="rn">
        <div class="table-header">
          <span class="type-pill pill-RN">Renewal</span>
          <span class="table-header-title">Renewal</span>
          <span class="count-badge" id="count-RN">—</span>
        </div>
        <div class="app-list-wrap">
          <table id="apps-RN">
            <thead><tr><th>File Number</th><th>Applicant</th><th>Call Sign</th><th class="r">Grant Date</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </div>

      <!-- Transfer of Control -->
      <div class="table-card fade-up" id="tc">
        <div class="table-header">
          <span class="type-pill pill-TC">Transfer of Control</span>
          <span class="table-header-title">Transfer of Control</span>
          <span class="count-badge" id="count-TC">—</span>
        </div>
        <div class="app-list-wrap">
          <table id="apps-TC">
            <thead><tr><th>File Number</th><th>Applicant</th><th>Call Sign</th><th class="r">Grant Date</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </div>

      <!-- Assignment -->
      <div class="table-card fade-up" id="as">
        <div class="table-header">
          <span class="type-pill pill-AS">Assignment</span>
          <span class="table-header-title">Assignment</span>
          <span class="count-badge" id="count-AS">—</span>
        </div>
        <div class="app-list-wrap">
          <table id="apps-AS">
            <thead><tr><th>File Number</th><th>Applicant</th><th>Call Sign</th><th class="r">Grant Date</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </div>

    </div><!-- /type-sections-grid -->
  </section>

  <div class="divider"></div>
</div>

<footer>
  <p>Data as of @@TODAY_LABEL@@<br>Source: <a href="https://apps.fcc.gov/oetcf/els/reports/GenericSearch.cfm" target="_blank">FCC ELS Generic Search</a></p>
  <p>Disposal Date Range: 01/01/2026 – @@DATE_TO@@<br>Generated by els_granted_today.py</p>
</footer>

<script>
const SNAPSHOT_DATE = "@@TODAY_LABEL@@";
const TYPE_CODES    = ["ST","CN","MD","RN","TC","AS"];
const TYPE_LABELS   = {ST:"STA",CN:"New License",MD:"Modification",RN:"Renewal",TC:"Transfer of Control",AS:"Assignment"};
const TYPE_COUNTS   = @@TYPE_COUNTS_JSON@@;
const GRANTED_DATA  = @@ROWS_JSON@@;

/* ── Hero bucket grid ── */
function renderHero() {
  const total = GRANTED_DATA.length;
  document.getElementById('hero-total-badge').textContent = total.toLocaleString() + ' total';
  document.getElementById('hs-total').textContent = total.toLocaleString();
  document.getElementById('hs-sta').textContent   = (TYPE_COUNTS['ST'] || 0).toLocaleString();
  document.getElementById('hs-cn').textContent    = (TYPE_COUNTS['CN'] || 0).toLocaleString();

  const grid     = document.getElementById('hero-bucket-grid');
  const maxCount = Math.max(...TYPE_CODES.map(c => TYPE_COUNTS[c] || 0));

  TYPE_CODES.forEach(code => {
    const c = TYPE_COUNTS[code] || 0;
    const w = maxCount > 0 ? (c / maxCount * 100).toFixed(1) : 0;
    const cell = document.createElement('div');
    cell.className = `hero-bucket-cell hbc-${code}`;
    cell.innerHTML = `
      <div class="hbc-count" data-target="${c}">0</div>
      <div class="hbc-label">${TYPE_LABELS[code]}</div>
      <div class="hbc-bar-track"><div class="hbc-bar-fill" data-w="${w}%"></div></div>`;
    grid.appendChild(cell);
  });

  requestAnimationFrame(() => {
    grid.querySelectorAll('.hbc-bar-fill').forEach(b => b.style.width = b.dataset.w);
  });
}

/* ── Per-type application tables ── */
function renderTypeTable(code) {
  const rows = GRANTED_DATA.filter(r => r.type_code === code);
  // already sorted newest-first from Python, but enforce here too
  rows.sort((a, b) => (b.status_date || '').localeCompare(a.status_date || ''));

  document.getElementById(`count-${code}`).textContent =
    rows.length.toLocaleString() + ' application' + (rows.length !== 1 ? 's' : '');

  const tbody = document.querySelector(`#apps-${code} tbody`);
  rows.forEach(r => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="td-file">${r.file}</td>
      <td class="td-applicant">${r.applicant}</td>
      <td class="td-callsign">${r.callsign || '—'}</td>
      <td class="td-date" style="text-align:right">${r.status_date}</td>`;
    tbody.appendChild(tr);
  });
}

/* ── Counter animation ── */
function countUp(el, target, dur) {
  const t0 = performance.now();
  (function step(now) {
    const p = Math.min((now - t0) / dur, 1);
    const e = 1 - Math.pow(1 - p, 3);
    el.textContent = Math.round(target * e).toLocaleString();
    if (p < 1) requestAnimationFrame(step);
    else el.textContent = target.toLocaleString();
  })(performance.now());
}

/* ── Scroll observer ── */
const obs = new IntersectionObserver(entries => {
  entries.forEach(e => {
    if (e.isIntersecting) {
      e.target.classList.add('visible');
      obs.unobserve(e.target);
    }
  });
}, { threshold: 0.08 });

/* ── Init ── */
document.addEventListener('DOMContentLoaded', () => {
  renderHero();
  TYPE_CODES.forEach(renderTypeTable);

  document.querySelectorAll('.fade-up, .table-card').forEach(el => obs.observe(el));

  setTimeout(() => {
    document.querySelectorAll('.hbc-count[data-target]').forEach(el => {
      countUp(el, parseInt(el.dataset.target, 10), 900);
    });
    countUp(document.getElementById('hs-total'), GRANTED_DATA.length, 900);
    countUp(document.getElementById('hs-sta'),   TYPE_COUNTS['ST'] || 0, 900);
    countUp(document.getElementById('hs-cn'),    TYPE_COUNTS['CN'] || 0, 900);
  }, 200);
});
</script>
</body>
</html>
"""


def rows_to_js(rows: list) -> str:
    js_rows = []
    for r in rows:
        js_rows.append({
            "file":        r["file"],
            "callsign":    r["callsign"],
            "applicant":   r["applicant"],
            "received":    r["received"],
            "status_date": r["status_date"],
            "type_code":   r["type_code"],
        })
    return json.dumps(js_rows, ensure_ascii=False)


def build_html(rows: list, type_counts: dict) -> str:
    html = HTML_TEMPLATE
    html = html.replace("@@TODAY_LABEL@@",      TODAY_LABEL)
    html = html.replace("@@DATE_TO@@",          DATE_TO)
    html = html.replace("@@TYPE_COUNTS_JSON@@", json.dumps(type_counts))
    html = html.replace("@@ROWS_JSON@@",        rows_to_js(rows))
    return html


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    skip = "--skip-scrape" in sys.argv

    if skip:
        csv_path = find_latest_csv()
        print(f"✓ --skip-scrape: using {csv_path.name}")
    else:
        csv_path = scrape()

    rows        = load_rows(csv_path)
    type_counts = compute_type_counts(rows)

    print(f"\nTotal granted: {len(rows)}")
    for code, label in APP_TYPES:
        print(f"  {code} ({label}): {type_counts.get(code, 0)}")

    html = build_html(rows, type_counts)
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    print(f"\n✓ HTML written to: {OUTPUT_HTML}")


if __name__ == "__main__":
    main()
