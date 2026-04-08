"""
Richmond City, VA — Automated Motivated Seller Lead Scraper
Clerk portal: https://va-richmond-cc.judicial.net/
Property appraiser bulk data: fetched from public GIS / DBF if available.
"""

import asyncio
import json
import os
import re
import csv
import io
import tempfile
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeout

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LOOKBACK_DAYS = 7
BASE_OUTPUT_DIR = Path(__file__).parent.parent

OUTPUT_FILES = [
    BASE_OUTPUT_DIR / "dashboard" / "records.json",
    BASE_OUTPUT_DIR / "data" / "records.json",
]

# Richmond City, VA Circuit Court Clerk portal
CLERK_BASE_URL = "https://va-richmond-cc.judicial.net/"
CLERK_SEARCH_URL = "https://va-richmond-cc.judicial.net/court-public-access"

# Document type codes → category mappings
DOC_TYPE_MAP = {
    # Lis Pendens / Foreclosure
    "LP":     ("LP",      "Lis Pendens"),
    "LIS":    ("LP",      "Lis Pendens"),
    "LPS":    ("LP",      "Lis Pendens"),
    "NOFC":   ("NOFC",    "Notice of Foreclosure"),
    "NOTS":   ("NOFC",    "Notice of Foreclosure"),
    # Tax Deed
    "TAXDEED":("TAXDEED", "Tax Deed"),
    "TD":     ("TAXDEED", "Tax Deed"),
    # Judgment
    "JUD":    ("JUD",     "Judgment"),
    "CCJ":    ("JUD",     "Certified Judgment"),
    "DRJUD":  ("JUD",     "Domestic Judgment"),
    "FJ":     ("JUD",     "Judgment"),
    "DJ":     ("JUD",     "Judgment"),
    # Tax / IRS / Federal Liens
    "LNCORPTX":("LNCORPTX","Corp Tax Lien"),
    "LNIRS":  ("LNIRS",   "IRS Lien"),
    "LNFED":  ("LNFED",   "Federal Lien"),
    # Liens
    "LN":     ("LN",      "Lien"),
    "LNMECH": ("LNMECH",  "Mechanic Lien"),
    "LNHOA":  ("LNHOA",   "HOA Lien"),
    # Medicaid
    "MEDLN":  ("MEDLN",   "Medicaid Lien"),
    # Probate
    "PRO":    ("PRO",     "Probate Document"),
    "WILL":   ("PRO",     "Probate Document"),
    "ADMIN":  ("PRO",     "Probate Document"),
    # Notice of Commencement
    "NOC":    ("NOC",     "Notice of Commencement"),
    # Release Lis Pendens
    "RELLP":  ("RELLP",   "Release Lis Pendens"),
    "RLP":    ("RELLP",   "Release Lis Pendens"),
}

TARGET_CATS = {"LP", "NOFC", "TAXDEED", "JUD", "LNCORPTX", "LNIRS", "LNFED",
               "LN", "LNMECH", "LNHOA", "MEDLN", "PRO", "NOC", "RELLP"}

# ---------------------------------------------------------------------------
# Property Appraiser lookup (DBF)
# ---------------------------------------------------------------------------

class ParcelLookup:
    """Loads parcel DBF file and builds owner-name → parcel mapping."""

    def __init__(self):
        self.by_owner: dict[str, list[dict]] = {}
        self.loaded = False

    def load(self, dbf_path: str):
        try:
            from dbfread import DBF
            table = DBF(dbf_path, encoding="latin-1", ignore_missing_memofile=True)
            for rec in table:
                rec_dict = {k.upper(): (v or "").strip() if isinstance(v, str) else v
                            for k, v in rec.items()}
                owner_raw = (
                    rec_dict.get("OWN1") or rec_dict.get("OWNER") or
                    rec_dict.get("OWNER1") or ""
                ).strip().upper()
                if not owner_raw:
                    continue
                parcel = self._normalize_parcel(rec_dict)
                for variant in self._name_variants(owner_raw):
                    self.by_owner.setdefault(variant, []).append(parcel)
            self.loaded = True
            print(f"[ParcelLookup] Loaded {sum(len(v) for v in self.by_owner.values())} parcel entries")
        except Exception as exc:
            print(f"[ParcelLookup] Could not load DBF: {exc}")

    @staticmethod
    def _normalize_parcel(rec: dict) -> dict:
        site_addr = (
            rec.get("SITEADDR") or rec.get("SITE_ADDR") or ""
        ).strip()
        site_city = (rec.get("SITE_CITY") or "").strip()
        site_zip  = str(rec.get("SITE_ZIP") or "").strip()
        mail_addr = (
            rec.get("MAILADR1") or rec.get("ADDR_1") or ""
        ).strip()
        mail_city = (rec.get("MAILCITY") or rec.get("CITY") or "").strip()
        mail_state= (rec.get("STATE") or "").strip()
        mail_zip  = str(rec.get("MAILZIP") or rec.get("ZIP") or "").strip()
        return {
            "prop_address": site_addr,
            "prop_city":    site_city,
            "prop_state":   "VA",
            "prop_zip":     site_zip,
            "mail_address": mail_addr,
            "mail_city":    mail_city,
            "mail_state":   mail_state,
            "mail_zip":     mail_zip,
        }

    @staticmethod
    def _name_variants(name: str) -> list[str]:
        """Produce FIRST LAST, LAST FIRST, LAST, FIRST variants."""
        name = name.strip().upper()
        variants = {name}
        # Remove trailing comma if any
        clean = re.sub(r",\s*", " ", name).strip()
        parts = clean.split()
        if len(parts) >= 2:
            # LAST FIRST → FIRST LAST
            flipped = " ".join(parts[1:]) + " " + parts[0]
            variants.add(flipped)
            # Comma form: LAST, FIRST
            variants.add(f"{parts[0]}, {' '.join(parts[1:])}")
        return [v for v in variants if v]

    def lookup(self, owner_name: str) -> Optional[dict]:
        if not owner_name:
            return None
        key = owner_name.strip().upper()
        for variant in self._name_variants(key):
            hits = self.by_owner.get(variant)
            if hits:
                return hits[0]
        return None


parcel_db = ParcelLookup()

# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

def with_retry(fn, attempts=3, delay=3):
    """Synchronous retry wrapper."""
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            print(f"  [retry {attempt}/{attempts}] {exc}")
            import time; time.sleep(delay)
    raise last_exc

# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

TODAY = datetime.utcnow().date()

def compute_flags(record: dict) -> list[str]:
    flags = []
    cat = record.get("cat", "")
    owner = (record.get("owner") or "").upper()
    filed_str = record.get("filed") or ""
    amount = record.get("amount") or 0

    if cat in ("LP",):
        flags.append("Lis pendens")
    if cat in ("NOFC",):
        flags.append("Pre-foreclosure")
    if cat in ("JUD",):
        flags.append("Judgment lien")
    if cat in ("LNCORPTX", "LNIRS", "LNFED"):
        flags.append("Tax lien")
    if cat in ("LNMECH",):
        flags.append("Mechanic lien")
    if cat in ("PRO",):
        flags.append("Probate / estate")
    if re.search(r"\b(LLC|CORP|INC|LTD|LP|TRUST|HOLDINGS|GROUP|PROPERTIES)\b", owner):
        flags.append("LLC / corp owner")

    # New this week
    try:
        filed_date = datetime.strptime(filed_str, "%Y-%m-%d").date()
        if (TODAY - filed_date).days <= 7:
            flags.append("New this week")
    except Exception:
        pass

    return list(dict.fromkeys(flags))  # deduplicate, preserve order


def compute_score(record: dict, flags: list[str]) -> int:
    score = 30  # base
    score += 10 * len(flags)

    cat = record.get("cat", "")
    amount = record.get("amount") or 0

    # LP + FC combo
    if "Lis pendens" in flags and "Pre-foreclosure" in flags:
        score += 20
    # Amount bonuses
    try:
        amt = float(amount)
        if amt > 100_000:
            score += 15
        elif amt > 50_000:
            score += 10
    except (TypeError, ValueError):
        pass

    if "New this week" in flags:
        score += 5
    if record.get("prop_address") or record.get("mail_address"):
        score += 5

    return min(score, 100)

# ---------------------------------------------------------------------------
# Playwright scraper — Richmond City Circuit Court
# ---------------------------------------------------------------------------

# The Virginia Judicial System's public access portal for Circuit Courts
# Richmond City Circuit Court is Court #760
# URL pattern: https://eapps.courts.state.va.us/ocis/landRecordSearch

VIRGINIA_OCIS_BASE = "https://eapps.courts.state.va.us/ocis"
RICHMOND_COURT_ID = "760"  # Richmond City Circuit Court FIPS


async def search_clerk_portal(page: Page, doc_type_code: str, start_date: str, end_date: str) -> list[dict]:
    """
    Search the Virginia OCIS land records portal for a given document type and date range.
    Falls back to alternate URL patterns if the primary fails.
    """
    records = []

    try:
        # Navigate to the land record search page
        await page.goto(f"{VIRGINIA_OCIS_BASE}/landRecordSearch", timeout=30000)
        await page.wait_for_load_state("networkidle", timeout=20000)

        # Try to select Richmond City court
        court_selectors = [
            'select[name="courtId"]',
            'select[name="court"]',
            '#courtSelect',
            '#court',
        ]
        for sel in court_selectors:
            try:
                await page.select_option(sel, value=RICHMOND_COURT_ID, timeout=3000)
                break
            except Exception:
                try:
                    await page.select_option(sel, label="Richmond City", timeout=3000)
                    break
                except Exception:
                    continue

        # Fill document type
        doc_type_selectors = [
            'input[name="documentType"]',
            'input[name="docType"]',
            'input[name="instrType"]',
            '#documentType',
            '#instrType',
        ]
        for sel in doc_type_selectors:
            try:
                await page.fill(sel, doc_type_code, timeout=3000)
                break
            except Exception:
                continue

        # Fill date range
        date_from_selectors = ['input[name="startDate"]', 'input[name="dateFrom"]', '#startDate', '#dateFrom']
        date_to_selectors   = ['input[name="endDate"]',   'input[name="dateTo"]',   '#endDate',   '#dateTo']

        for sel in date_from_selectors:
            try:
                await page.fill(sel, start_date, timeout=3000)
                break
            except Exception:
                continue

        for sel in date_to_selectors:
            try:
                await page.fill(sel, end_date, timeout=3000)
                break
            except Exception:
                continue

        # Submit search
        submit_selectors = ['button[type="submit"]', 'input[type="submit"]', '#searchBtn', '#search']
        for sel in submit_selectors:
            try:
                await page.click(sel, timeout=5000)
                break
            except Exception:
                continue

        await page.wait_for_load_state("networkidle", timeout=20000)
        records = await extract_results_from_page(page, doc_type_code)

    except PlaywrightTimeout as exc:
        print(f"  [clerk] Timeout searching {doc_type_code}: {exc}")
    except Exception as exc:
        print(f"  [clerk] Error searching {doc_type_code}: {exc}")
        traceback.print_exc()

    return records


async def extract_results_from_page(page: Page, doc_type_code: str) -> list[dict]:
    """Parse search results table from the current page state."""
    records = []
    try:
        content = await page.content()
        soup = BeautifulSoup(content, "lxml")

        # Try multiple table patterns
        tables = soup.find_all("table")
        for table in tables:
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue

            headers = [th.get_text(strip=True).upper() for th in rows[0].find_all(["th", "td"])]
            if not headers:
                continue

            # Try to identify relevant columns
            col_map = {}
            for i, h in enumerate(headers):
                if "INST" in h or "DOC" in h or "NUMBER" in h:
                    col_map.setdefault("doc_num", i)
                if "TYPE" in h:
                    col_map.setdefault("doc_type", i)
                if "DATE" in h or "FILED" in h or "RECORD" in h:
                    col_map.setdefault("filed", i)
                if "GRANTOR" in h or "OWNER" in h or "PARTY1" in h:
                    col_map.setdefault("owner", i)
                if "GRANTEE" in h or "PARTY2" in h:
                    col_map.setdefault("grantee", i)
                if "LEGAL" in h or "DESCRIPTION" in h:
                    col_map.setdefault("legal", i)
                if "AMOUNT" in h or "CONSIDER" in h or "VALUE" in h:
                    col_map.setdefault("amount", i)

            if not col_map.get("doc_num"):
                continue

            for row in rows[1:]:
                cells = row.find_all("td")
                if not cells:
                    continue

                def cell_text(key):
                    idx = col_map.get(key)
                    if idx is not None and idx < len(cells):
                        return cells[idx].get_text(strip=True)
                    return ""

                doc_num  = cell_text("doc_num")
                doc_type = cell_text("doc_type") or doc_type_code
                filed    = normalize_date(cell_text("filed"))
                owner    = cell_text("owner")
                grantee  = cell_text("grantee")
                legal    = cell_text("legal")
                amount   = parse_amount(cell_text("amount"))

                # Try to extract direct URL from a link in the row
                link_tag = row.find("a", href=True)
                clerk_url = ""
                if link_tag:
                    href = link_tag["href"]
                    if href.startswith("http"):
                        clerk_url = href
                    else:
                        clerk_url = VIRGINIA_OCIS_BASE + "/" + href.lstrip("/")

                if not doc_num:
                    continue

                cat, cat_label = classify_doc_type(doc_type)
                if cat not in TARGET_CATS:
                    continue

                rec = {
                    "doc_num":      doc_num,
                    "doc_type":     doc_type,
                    "filed":        filed,
                    "cat":          cat,
                    "cat_label":    cat_label,
                    "owner":        owner,
                    "grantee":      grantee,
                    "amount":       amount,
                    "legal":        legal,
                    "prop_address": "",
                    "prop_city":    "Richmond",
                    "prop_state":   "VA",
                    "prop_zip":     "",
                    "mail_address": "",
                    "mail_city":    "",
                    "mail_state":   "",
                    "mail_zip":     "",
                    "clerk_url":    clerk_url or build_clerk_url(doc_num),
                    "flags":        [],
                    "score":        0,
                }
                records.append(rec)

    except Exception as exc:
        print(f"  [extract] Error parsing page results: {exc}")

    return records


async def scrape_richmond_clerk(start_date: str, end_date: str) -> list[dict]:
    """
    Main async scraping function.
    Iterates over all target document types and paginates results.
    """
    all_records: list[dict] = []
    seen_doc_nums: set[str] = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )

        # Process each document type
        for code, (cat, cat_label) in DOC_TYPE_MAP.items():
            if cat not in TARGET_CATS:
                continue
            print(f"[scraper] Searching: {code} ({cat_label})  {start_date} → {end_date}")
            page = await context.new_page()
            try:
                records = await search_with_retry(page, code, start_date, end_date)
                for rec in records:
                    if rec["doc_num"] not in seen_doc_nums:
                        seen_doc_nums.add(rec["doc_num"])
                        all_records.append(rec)
                print(f"         → {len(records)} records found")
            except Exception as exc:
                print(f"  [scraper] Failed for {code}: {exc}")
            finally:
                await page.close()

        # Also try the fallback static search via requests+BS4
        static_records = fetch_static_clerk_records(start_date, end_date, seen_doc_nums)
        for rec in static_records:
            if rec["doc_num"] not in seen_doc_nums:
                seen_doc_nums.add(rec["doc_num"])
                all_records.append(rec)

        await context.close()
        await browser.close()

    return all_records


async def search_with_retry(page: Page, doc_type_code: str, start_date: str, end_date: str, attempts=3) -> list[dict]:
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return await search_clerk_portal(page, doc_type_code, start_date, end_date)
        except Exception as exc:
            last_exc = exc
            print(f"  [retry {attempt}/{attempts}] {exc}")
            await asyncio.sleep(3)
    return []

# ---------------------------------------------------------------------------
# Static fallback: requests + BeautifulSoup
# Tries the Virginia OCIS REST-like endpoints that return JSON or XML
# ---------------------------------------------------------------------------

VA_OCIS_API = "https://eapps.courts.state.va.us/api"

def fetch_static_clerk_records(start_date: str, end_date: str, seen: set) -> list[dict]:
    """
    Attempt to fetch records via the Virginia Judicial system's public API
    or HTML search pages using requests + BeautifulSoup.
    """
    records = []
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "application/json, text/html, */*",
    })

    # Try JSON API endpoint first
    api_urls = [
        f"{VA_OCIS_API}/landRecords?courtId={RICHMOND_COURT_ID}&startDate={start_date}&endDate={end_date}",
        f"https://eapps.courts.state.va.us/ocis/api/landRecords?court={RICHMOND_COURT_ID}&fromDate={start_date}&toDate={end_date}",
    ]
    for url in api_urls:
        try:
            resp = session.get(url, timeout=20)
            if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("application/json"):
                data = resp.json()
                items = data if isinstance(data, list) else data.get("records", data.get("results", []))
                for item in items:
                    rec = parse_api_record(item)
                    if rec and rec["doc_num"] not in seen:
                        records.append(rec)
                if records:
                    print(f"  [static] Fetched {len(records)} records via API")
                    return records
        except Exception as exc:
            print(f"  [static] API attempt failed: {exc}")

    # Fallback: scrape the HTML search form
    html_search_url = f"https://eapps.courts.state.va.us/ocis/landRecordSearch?courtId={RICHMOND_COURT_ID}&startDate={start_date}&endDate={end_date}"
    try:
        resp = session.get(html_search_url, timeout=30)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "lxml")
            table = soup.find("table")
            if table:
                rows = table.find_all("tr")
                headers = [th.get_text(strip=True).upper() for th in rows[0].find_all(["th", "td"])]
                for row in rows[1:]:
                    cells = row.find_all("td")
                    if not cells:
                        continue
                    rec = parse_html_row(cells, headers)
                    if rec and rec["doc_num"] not in seen:
                        records.append(rec)
    except Exception as exc:
        print(f"  [static] HTML fallback failed: {exc}")

    return records


def parse_api_record(item: dict) -> Optional[dict]:
    try:
        doc_type = (item.get("documentType") or item.get("instrType") or item.get("docType") or "").strip()
        cat, cat_label = classify_doc_type(doc_type)
        if cat not in TARGET_CATS:
            return None
        doc_num  = str(item.get("instrumentNumber") or item.get("docNumber") or item.get("instrNum") or "").strip()
        if not doc_num:
            return None
        filed    = normalize_date(str(item.get("recordedDate") or item.get("filedDate") or item.get("date") or ""))
        owner    = str(item.get("grantor") or item.get("owner") or "").strip()
        grantee  = str(item.get("grantee") or "").strip()
        legal    = str(item.get("legalDescription") or item.get("legal") or "").strip()
        amount   = parse_amount(str(item.get("consideration") or item.get("amount") or ""))
        clerk_url = str(item.get("url") or item.get("link") or build_clerk_url(doc_num))
        return {
            "doc_num":      doc_num,
            "doc_type":     doc_type,
            "filed":        filed,
            "cat":          cat,
            "cat_label":    cat_label,
            "owner":        owner,
            "grantee":      grantee,
            "amount":       amount,
            "legal":        legal,
            "prop_address": "",
            "prop_city":    "Richmond",
            "prop_state":   "VA",
            "prop_zip":     "",
            "mail_address": "",
            "mail_city":    "",
            "mail_state":   "",
            "mail_zip":     "",
            "clerk_url":    clerk_url,
            "flags":        [],
            "score":        0,
        }
    except Exception:
        return None


def parse_html_row(cells, headers: list[str]) -> Optional[dict]:
    try:
        def get(key_fragments):
            for i, h in enumerate(headers):
                for frag in key_fragments:
                    if frag in h and i < len(cells):
                        return cells[i].get_text(strip=True)
            return ""

        doc_num  = get(["INST", "DOC", "NUM"])
        doc_type = get(["TYPE"])
        filed    = normalize_date(get(["DATE", "FILED", "RECORD"]))
        owner    = get(["GRANTOR", "OWNER"])
        grantee  = get(["GRANTEE"])
        legal    = get(["LEGAL", "DESC"])
        amount   = parse_amount(get(["AMOUNT", "CONSIDER"]))

        if not doc_num:
            return None

        cat, cat_label = classify_doc_type(doc_type)
        if cat not in TARGET_CATS:
            return None

        link_tag = None
        for cell in cells:
            link_tag = cell.find("a", href=True)
            if link_tag:
                break
        clerk_url = ""
        if link_tag:
            href = link_tag["href"]
            clerk_url = href if href.startswith("http") else VIRGINIA_OCIS_BASE + "/" + href.lstrip("/")

        return {
            "doc_num":      doc_num,
            "doc_type":     doc_type,
            "filed":        filed,
            "cat":          cat,
            "cat_label":    cat_label,
            "owner":        owner,
            "grantee":      grantee,
            "amount":       amount,
            "legal":        legal,
            "prop_address": "",
            "prop_city":    "Richmond",
            "prop_state":   "VA",
            "prop_zip":     "",
            "mail_address": "",
            "mail_city":    "",
            "mail_state":   "",
            "mail_zip":     "",
            "clerk_url":    clerk_url or build_clerk_url(doc_num),
            "flags":        [],
            "score":        0,
        }
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Property Appraiser DBF download
# ---------------------------------------------------------------------------

RICHMOND_GIS_DBF_URLS = [
    # Richmond City open data / GIS portals
    "https://www.rva.gov/sites/default/files/2024-01/ParcelData.zip",
    "https://opendata.rva.gov/datasets/parcels/data.zip",
    "https://gis.rva.gov/download/parcels.zip",
    # Fallback: Virginia GIS clearinghouse
    "https://vgin.vdem.virginia.gov/datasets/richmond-city-parcels/download",
]

def download_parcel_dbf() -> Optional[str]:
    """
    Try to download the Richmond City parcel DBF.
    Returns path to extracted DBF file or None.
    """
    import zipfile

    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (compatible; MotivatedSellerScraper/1.0)"

    for url in RICHMOND_GIS_DBF_URLS:
        try:
            print(f"[parcel] Trying: {url}")
            resp = session.get(url, timeout=60, stream=True)
            if resp.status_code != 200:
                continue

            content_type = resp.headers.get("content-type", "")
            if "zip" not in content_type and not url.endswith(".zip"):
                # Might be a redirect to the actual file
                if resp.url != url:
                    resp = session.get(resp.url, timeout=60, stream=True)

            tmpdir = tempfile.mkdtemp()
            zip_path = os.path.join(tmpdir, "parcels.zip")
            with open(zip_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)

            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(tmpdir)

            # Find a DBF file
            for root, dirs, files in os.walk(tmpdir):
                for fname in files:
                    if fname.lower().endswith(".dbf"):
                        dbf_path = os.path.join(root, fname)
                        print(f"[parcel] Found DBF: {dbf_path}")
                        return dbf_path

        except Exception as exc:
            print(f"[parcel] Failed {url}: {exc}")

    print("[parcel] Could not download parcel DBF — address enrichment skipped")
    return None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def classify_doc_type(doc_type: str) -> tuple[str, str]:
    """Map a raw document type string to a (cat, cat_label) pair."""
    upper = doc_type.strip().upper()
    # Direct lookup
    if upper in DOC_TYPE_MAP:
        return DOC_TYPE_MAP[upper]
    # Partial match
    for key, (cat, label) in DOC_TYPE_MAP.items():
        if key in upper or upper in key:
            return (cat, label)
    return ("OTHER", doc_type)


def normalize_date(raw: str) -> str:
    """Try to parse various date formats and return YYYY-MM-DD."""
    if not raw:
        return ""
    raw = raw.strip()
    formats = [
        "%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y",
        "%Y%m%d", "%d-%b-%Y", "%B %d, %Y", "%b %d, %Y",
        "%m/%d/%y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def parse_amount(raw: str) -> Optional[float]:
    """Parse a dollar amount string to float."""
    if not raw:
        return None
    clean = re.sub(r"[,$\s]", "", raw)
    try:
        v = float(clean)
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None


def build_clerk_url(doc_num: str) -> str:
    """Build a best-guess direct URL for a document in the clerk portal."""
    safe_num = requests.utils.quote(doc_num, safe="")
    return (
        f"https://eapps.courts.state.va.us/ocis/landRecordSearch"
        f"?courtId={RICHMOND_COURT_ID}&instrumentNumber={safe_num}"
    )

# ---------------------------------------------------------------------------
# GHL CSV Export
# ---------------------------------------------------------------------------

def generate_ghl_csv(records: list[dict]) -> str:
    """
    Generate a GHL (GoHighLevel) compatible CSV string.
    """
    columns = [
        "First Name", "Last Name",
        "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
        "Property Address", "Property City", "Property State", "Property Zip",
        "Lead Type", "Document Type", "Date Filed", "Document Number",
        "Amount/Debt Owed", "Seller Score", "Motivated Seller Flags",
        "Source", "Public Records URL",
    ]

    def split_name(full_name: str) -> tuple[str, str]:
        parts = full_name.strip().split()
        if len(parts) == 0:
            return ("", "")
        if len(parts) == 1:
            return (parts[0], "")
        return (" ".join(parts[:-1]), parts[-1])

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()

    for rec in records:
        first, last = split_name(rec.get("owner") or "")
        writer.writerow({
            "First Name":             first,
            "Last Name":              last,
            "Mailing Address":        rec.get("mail_address") or "",
            "Mailing City":           rec.get("mail_city") or "",
            "Mailing State":          rec.get("mail_state") or "",
            "Mailing Zip":            rec.get("mail_zip") or "",
            "Property Address":       rec.get("prop_address") or "",
            "Property City":          rec.get("prop_city") or "",
            "Property State":         rec.get("prop_state") or "",
            "Property Zip":           rec.get("prop_zip") or "",
            "Lead Type":              rec.get("cat_label") or rec.get("cat") or "",
            "Document Type":          rec.get("doc_type") or "",
            "Date Filed":             rec.get("filed") or "",
            "Document Number":        rec.get("doc_num") or "",
            "Amount/Debt Owed":       str(rec.get("amount") or ""),
            "Seller Score":           str(rec.get("score") or 0),
            "Motivated Seller Flags": "; ".join(rec.get("flags") or []),
            "Source":                 "Richmond City, VA - Circuit Court",
            "Public Records URL":     rec.get("clerk_url") or "",
        })

    return output.getvalue()

# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

async def main():
    print("=" * 60)
    print("Richmond City, VA — Motivated Seller Lead Scraper")
    print(f"Run time: {datetime.utcnow().isoformat()}Z")
    print("=" * 60)

    end_date_obj   = datetime.utcnow().date()
    start_date_obj = end_date_obj - timedelta(days=LOOKBACK_DAYS)
    start_date = start_date_obj.strftime("%Y-%m-%d")
    end_date   = end_date_obj.strftime("%Y-%m-%d")
    print(f"Date range: {start_date} → {end_date}")

    # 1. Load parcel data
    dbf_path = download_parcel_dbf()
    if dbf_path:
        parcel_db.load(dbf_path)

    # 2. Scrape clerk portal
    print("\n[phase 1] Scraping clerk portal...")
    records = await scrape_richmond_clerk(start_date, end_date)
    print(f"\nTotal raw records: {len(records)}")

    # 3. Enrich with parcel data
    print("\n[phase 2] Enriching with parcel data...")
    enriched = 0
    for rec in records:
        parcel = parcel_db.lookup(rec.get("owner") or "")
        if parcel:
            rec.update(parcel)
            enriched += 1

    print(f"Enriched {enriched}/{len(records)} records with address data")

    # 4. Compute flags and scores
    print("\n[phase 3] Computing seller scores...")
    for rec in records:
        rec["flags"] = compute_flags(rec)
        rec["score"] = compute_score(rec, rec["flags"])

    # 5. Sort by score descending
    records.sort(key=lambda r: r["score"], reverse=True)

    with_address = sum(1 for r in records if r.get("prop_address") or r.get("mail_address"))

    # 6. Build output payload
    payload = {
        "fetched_at":    datetime.utcnow().isoformat() + "Z",
        "source":        "Richmond City, VA - Circuit Court Clerk",
        "date_range":    f"{start_date} to {end_date}",
        "total":         len(records),
        "with_address":  with_address,
        "records":       records,
    }

    # 7. Save JSON to output files
    for output_path in OUTPUT_FILES:
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            print(f"[output] Saved {len(records)} records → {output_path}")
        except Exception as exc:
            print(f"[output] Error saving {output_path}: {exc}")

    # 8. Generate GHL CSV
    csv_path = BASE_OUTPUT_DIR / "data" / "ghl_export.csv"
    try:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.write_text(generate_ghl_csv(records), encoding="utf-8")
        print(f"[output] GHL CSV saved → {csv_path}")
    except Exception as exc:
        print(f"[output] Error saving GHL CSV: {exc}")

    # 9. Summary
    print("\n" + "=" * 60)
    print(f"SUMMARY")
    print(f"  Total leads:       {len(records)}")
    print(f"  With address:      {with_address}")
    print(f"  High score (≥70):  {sum(1 for r in records if r.get('score', 0) >= 70)}")
    print(f"  Date range:        {start_date} → {end_date}")
    if records:
        top = records[0]
        print(f"  Top lead:          {top.get('owner')} | score={top.get('score')} | {top.get('cat_label')}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
