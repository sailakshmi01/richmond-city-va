"""
Greater Richmond, VA — Automated Motivated Seller Lead Scraper
Covers: Richmond City, Henrico, Chesterfield, Hanover, Goochland
Virginia OCIS portal: https://eapps.courts.state.va.us/ocis/landRecordSearch
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

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", 7))
BASE_OUTPUT_DIR = Path(__file__).parent.parent

OUTPUT_FILES = [
    BASE_OUTPUT_DIR / "dashboard" / "records.json",
    BASE_OUTPUT_DIR / "data" / "records.json",
]

VIRGINIA_OCIS_BASE = "https://eapps.courts.state.va.us/ocis"
VA_OCIS_API        = "https://eapps.courts.state.va.us/api"

# ---------------------------------------------------------------------------
# Target jurisdictions — all 5 Greater Richmond area courts
# ---------------------------------------------------------------------------

COURTS = [
    {"id": "760", "name": "Richmond City",    "city": "Richmond",       "state": "VA"},
    {"id": "087", "name": "Henrico County",   "city": "Henrico",        "state": "VA"},
    {"id": "041", "name": "Chesterfield County", "city": "Chesterfield","state": "VA"},
    {"id": "085", "name": "Hanover County",   "city": "Hanover",        "state": "VA"},
    {"id": "075", "name": "Goochland County", "city": "Goochland",      "state": "VA"},
]

# ---------------------------------------------------------------------------
# Document type codes → category mappings
# ---------------------------------------------------------------------------

DOC_TYPE_MAP = {
    # Lis Pendens / Foreclosure
    "LP":       ("LP",       "Lis Pendens"),
    "LIS":      ("LP",       "Lis Pendens"),
    "LPS":      ("LP",       "Lis Pendens"),
    "NOFC":     ("NOFC",     "Notice of Foreclosure"),
    "NOTS":     ("NOFC",     "Notice of Foreclosure"),
    # Tax Deed
    "TAXDEED":  ("TAXDEED",  "Tax Deed"),
    "TD":       ("TAXDEED",  "Tax Deed"),
    # Judgment
    "JUD":      ("JUD",      "Judgment"),
    "CCJ":      ("JUD",      "Certified Judgment"),
    "DRJUD":    ("JUD",      "Domestic Judgment"),
    "FJ":       ("JUD",      "Judgment"),
    "DJ":       ("JUD",      "Judgment"),
    # Tax / IRS / Federal Liens
    "LNCORPTX": ("LNCORPTX", "Corp Tax Lien"),
    "LNIRS":    ("LNIRS",    "IRS Lien"),
    "LNFED":    ("LNFED",    "Federal Lien"),
    # Liens
    "LN":       ("LN",       "Lien"),
    "LNMECH":   ("LNMECH",   "Mechanic Lien"),
    "LNHOA":    ("LNHOA",    "HOA Lien"),
    # Medicaid
    "MEDLN":    ("MEDLN",    "Medicaid Lien"),
    # Probate
    "PRO":      ("PRO",      "Probate Document"),
    "WILL":     ("PRO",      "Probate Document"),
    "ADMIN":    ("PRO",      "Probate Document"),
    # Notice of Commencement
    "NOC":      ("NOC",      "Notice of Commencement"),
    # Release Lis Pendens
    "RELLP":    ("RELLP",    "Release Lis Pendens"),
    "RLP":      ("RELLP",    "Release Lis Pendens"),
}

TARGET_CATS = {
    "LP", "NOFC", "TAXDEED", "JUD",
    "LNCORPTX", "LNIRS", "LNFED",
    "LN", "LNMECH", "LNHOA", "MEDLN",
    "PRO", "NOC", "RELLP",
}

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
                rec_dict = {
                    k.upper(): (v or "").strip() if isinstance(v, str) else v
                    for k, v in rec.items()
                }
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
        site_addr  = (rec.get("SITEADDR") or rec.get("SITE_ADDR") or "").strip()
        site_city  = (rec.get("SITE_CITY") or "").strip()
        site_zip   = str(rec.get("SITE_ZIP") or "").strip()
        mail_addr  = (rec.get("MAILADR1") or rec.get("ADDR_1") or "").strip()
        mail_city  = (rec.get("MAILCITY") or rec.get("CITY") or "").strip()
        mail_state = (rec.get("STATE") or "").strip()
        mail_zip   = str(rec.get("MAILZIP") or rec.get("ZIP") or "").strip()
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
        name = name.strip().upper()
        variants = {name}
        clean = re.sub(r",\s*", " ", name).strip()
        parts = clean.split()
        if len(parts) >= 2:
            flipped = " ".join(parts[1:]) + " " + parts[0]
            variants.add(flipped)
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
    cat   = record.get("cat", "")
    owner = (record.get("owner") or "").upper()
    filed_str = record.get("filed") or ""

    if cat == "LP":
        flags.append("Lis pendens")
    if cat == "NOFC":
        flags.append("Pre-foreclosure")
    if cat == "JUD":
        flags.append("Judgment lien")
    if cat in ("LNCORPTX", "LNIRS", "LNFED"):
        flags.append("Tax lien")
    if cat == "LNMECH":
        flags.append("Mechanic lien")
    if cat == "PRO":
        flags.append("Probate / estate")
    if re.search(r"\b(LLC|CORP|INC|LTD|LP|TRUST|HOLDINGS|GROUP|PROPERTIES)\b", owner):
        flags.append("LLC / corp owner")

    try:
        filed_date = datetime.strptime(filed_str, "%Y-%m-%d").date()
        if (TODAY - filed_date).days <= 7:
            flags.append("New this week")
    except Exception:
        pass

    return list(dict.fromkeys(flags))


def compute_score(record: dict, flags: list[str]) -> int:
    score = 30
    score += 10 * len(flags)

    amount = record.get("amount") or 0
    if "Lis pendens" in flags and "Pre-foreclosure" in flags:
        score += 20
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
# Playwright scraper — Virginia OCIS land records
# ---------------------------------------------------------------------------

async def search_clerk_portal(
    page: Page,
    court: dict,
    doc_type_code: str,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """Search the Virginia OCIS land records portal for one court + doc type."""
    records = []
    court_id   = court["id"]
    court_name = court["name"]

    try:
        await page.goto(f"{VIRGINIA_OCIS_BASE}/landRecordSearch", timeout=30000)
        await page.wait_for_load_state("networkidle", timeout=20000)

        # Select court
        court_selectors = [
            'select[name="courtId"]', 'select[name="court"]',
            '#courtSelect', '#court',
        ]
        for sel in court_selectors:
            try:
                await page.select_option(sel, value=court_id, timeout=3000)
                break
            except Exception:
                try:
                    await page.select_option(sel, label=court_name, timeout=3000)
                    break
                except Exception:
                    continue

        # Fill document type
        for sel in ['input[name="documentType"]', 'input[name="docType"]',
                    'input[name="instrType"]', '#documentType', '#instrType']:
            try:
                await page.fill(sel, doc_type_code, timeout=3000)
                break
            except Exception:
                continue

        # Fill date range
        for sel in ['input[name="startDate"]', 'input[name="dateFrom"]', '#startDate', '#dateFrom']:
            try:
                await page.fill(sel, start_date, timeout=3000)
                break
            except Exception:
                continue

        for sel in ['input[name="endDate"]', 'input[name="dateTo"]', '#endDate', '#dateTo']:
            try:
                await page.fill(sel, end_date, timeout=3000)
                break
            except Exception:
                continue

        # Submit
        for sel in ['button[type="submit"]', 'input[type="submit"]', '#searchBtn', '#search']:
            try:
                await page.click(sel, timeout=5000)
                break
            except Exception:
                continue

        await page.wait_for_load_state("networkidle", timeout=20000)
        records = await extract_results_from_page(page, court, doc_type_code)

    except PlaywrightTimeout as exc:
        print(f"  [clerk] Timeout {court_name}/{doc_type_code}: {exc}")
    except Exception as exc:
        print(f"  [clerk] Error {court_name}/{doc_type_code}: {exc}")
        traceback.print_exc()

    return records


async def extract_results_from_page(page: Page, court: dict, doc_type_code: str) -> list[dict]:
    """Parse search results table from the current page."""
    records = []
    try:
        content = await page.content()
        soup    = BeautifulSoup(content, "lxml")

        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue

            headers = [th.get_text(strip=True).upper() for th in rows[0].find_all(["th", "td"])]
            if not headers:
                continue

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

                link_tag  = row.find("a", href=True)
                clerk_url = ""
                if link_tag:
                    href = link_tag["href"]
                    clerk_url = href if href.startswith("http") else VIRGINIA_OCIS_BASE + "/" + href.lstrip("/")

                if not doc_num:
                    continue

                cat, cat_label = classify_doc_type(doc_type)
                if cat not in TARGET_CATS:
                    continue

                records.append(make_record(
                    doc_num=doc_num, doc_type=doc_type, filed=filed,
                    cat=cat, cat_label=cat_label, owner=owner, grantee=grantee,
                    amount=amount, legal=legal,
                    clerk_url=clerk_url or build_clerk_url(doc_num, court["id"]),
                    court=court,
                ))

    except Exception as exc:
        print(f"  [extract] Error parsing page: {exc}")

    return records


async def scrape_all_courts(start_date: str, end_date: str) -> list[dict]:
    """Main async scraping loop — all courts × all doc types."""
    all_records: list[dict] = []
    seen: set[str] = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )

        for court in COURTS:
            print(f"\n{'='*50}")
            print(f"[court] {court['name']}  (ID: {court['id']})")
            print(f"{'='*50}")

            for code, (cat, cat_label) in DOC_TYPE_MAP.items():
                if cat not in TARGET_CATS:
                    continue
                print(f"  [search] {code} ({cat_label})  {start_date} → {end_date}")
                page = await context.new_page()
                try:
                    recs = await search_with_retry(page, court, code, start_date, end_date)
                    new_recs = 0
                    for rec in recs:
                        key = f"{court['id']}:{rec['doc_num']}"
                        if key not in seen:
                            seen.add(key)
                            all_records.append(rec)
                            new_recs += 1
                    print(f"         → {new_recs} new records")
                except Exception as exc:
                    print(f"  [scraper] Failed {court['name']}/{code}: {exc}")
                finally:
                    await page.close()

            # Static fallback for this court
            static_recs = fetch_static_court_records(court, start_date, end_date, seen)
            all_records.extend(static_recs)

        await context.close()
        await browser.close()

    return all_records


async def search_with_retry(
    page: Page, court: dict, doc_type_code: str,
    start_date: str, end_date: str, attempts: int = 3,
) -> list[dict]:
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return await search_clerk_portal(page, court, doc_type_code, start_date, end_date)
        except Exception as exc:
            last_exc = exc
            print(f"    [retry {attempt}/{attempts}] {exc}")
            await asyncio.sleep(3)
    return []

# ---------------------------------------------------------------------------
# Static fallback — requests + BeautifulSoup (per court)
# ---------------------------------------------------------------------------

def fetch_static_court_records(court: dict, start_date: str, end_date: str, seen: set) -> list[dict]:
    """Fallback HTTP fetch for one court using requests + BeautifulSoup."""
    records = []
    court_id = court["id"]
    session  = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "application/json, text/html, */*",
    })

    # JSON API probe
    api_urls = [
        f"{VA_OCIS_API}/landRecords?courtId={court_id}&startDate={start_date}&endDate={end_date}",
        f"{VIRGINIA_OCIS_BASE}/api/landRecords?court={court_id}&fromDate={start_date}&toDate={end_date}",
    ]
    for url in api_urls:
        try:
            resp = session.get(url, timeout=20)
            if resp.status_code == 200 and "application/json" in resp.headers.get("content-type", ""):
                data  = resp.json()
                items = data if isinstance(data, list) else data.get("records", data.get("results", []))
                for item in items:
                    rec = parse_api_record(item, court)
                    if rec:
                        key = f"{court_id}:{rec['doc_num']}"
                        if key not in seen:
                            seen.add(key)
                            records.append(rec)
                if records:
                    print(f"  [static] {court['name']}: {len(records)} records via API")
                    return records
        except Exception as exc:
            print(f"  [static] API failed {court['name']}: {exc}")

    # HTML search fallback
    html_url = (
        f"{VIRGINIA_OCIS_BASE}/landRecordSearch"
        f"?courtId={court_id}&startDate={start_date}&endDate={end_date}"
    )
    try:
        resp = session.get(html_url, timeout=30)
        if resp.status_code == 200:
            soup  = BeautifulSoup(resp.text, "lxml")
            table = soup.find("table")
            if table:
                rows    = table.find_all("tr")
                headers = [th.get_text(strip=True).upper() for th in rows[0].find_all(["th", "td"])]
                for row in rows[1:]:
                    cells = row.find_all("td")
                    if not cells:
                        continue
                    rec = parse_html_row(cells, headers, court)
                    if rec:
                        key = f"{court_id}:{rec['doc_num']}"
                        if key not in seen:
                            seen.add(key)
                            records.append(rec)
    except Exception as exc:
        print(f"  [static] HTML fallback failed {court['name']}: {exc}")

    return records


def parse_api_record(item: dict, court: dict) -> Optional[dict]:
    try:
        doc_type = (
            item.get("documentType") or item.get("instrType") or item.get("docType") or ""
        ).strip()
        cat, cat_label = classify_doc_type(doc_type)
        if cat not in TARGET_CATS:
            return None
        doc_num = str(
            item.get("instrumentNumber") or item.get("docNumber") or item.get("instrNum") or ""
        ).strip()
        if not doc_num:
            return None
        filed    = normalize_date(str(item.get("recordedDate") or item.get("filedDate") or item.get("date") or ""))
        owner    = str(item.get("grantor") or item.get("owner") or "").strip()
        grantee  = str(item.get("grantee") or "").strip()
        legal    = str(item.get("legalDescription") or item.get("legal") or "").strip()
        amount   = parse_amount(str(item.get("consideration") or item.get("amount") or ""))
        clerk_url = str(item.get("url") or item.get("link") or build_clerk_url(doc_num, court["id"]))
        return make_record(
            doc_num=doc_num, doc_type=doc_type, filed=filed,
            cat=cat, cat_label=cat_label, owner=owner, grantee=grantee,
            amount=amount, legal=legal, clerk_url=clerk_url, court=court,
        )
    except Exception:
        return None


def parse_html_row(cells, headers: list[str], court: dict) -> Optional[dict]:
    try:
        def get(frags):
            for i, h in enumerate(headers):
                if any(f in h for f in frags) and i < len(cells):
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

        link_tag  = next((cell.find("a", href=True) for cell in cells if cell.find("a", href=True)), None)
        clerk_url = ""
        if link_tag:
            href = link_tag["href"]
            clerk_url = href if href.startswith("http") else VIRGINIA_OCIS_BASE + "/" + href.lstrip("/")

        return make_record(
            doc_num=doc_num, doc_type=doc_type, filed=filed,
            cat=cat, cat_label=cat_label, owner=owner, grantee=grantee,
            amount=amount, legal=legal,
            clerk_url=clerk_url or build_clerk_url(doc_num, court["id"]),
            court=court,
        )
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Property Appraiser DBF download
# ---------------------------------------------------------------------------

# Richmond metro GIS / open data parcel download URLs
PARCEL_DBF_URLS = [
    "https://www.rva.gov/sites/default/files/2024-01/ParcelData.zip",
    "https://opendata.rva.gov/datasets/parcels/data.zip",
    "https://gis.rva.gov/download/parcels.zip",
    "https://vgin.vdem.virginia.gov/datasets/richmond-city-parcels/download",
]


def download_parcel_dbf() -> Optional[str]:
    import zipfile

    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (compatible; MotivatedSellerScraper/1.0)"

    for url in PARCEL_DBF_URLS:
        try:
            print(f"[parcel] Trying: {url}")
            resp = session.get(url, timeout=60, stream=True)
            if resp.status_code != 200:
                continue
            tmpdir   = tempfile.mkdtemp()
            zip_path = os.path.join(tmpdir, "parcels.zip")
            with open(zip_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(tmpdir)
            for root, _, files in os.walk(tmpdir):
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

def make_record(
    *, doc_num, doc_type, filed, cat, cat_label,
    owner, grantee, amount, legal, clerk_url, court: dict,
) -> dict:
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
        "jurisdiction": court["name"],
        "court_id":     court["id"],
        "prop_address": "",
        "prop_city":    court["city"],
        "prop_state":   court["state"],
        "prop_zip":     "",
        "mail_address": "",
        "mail_city":    "",
        "mail_state":   "",
        "mail_zip":     "",
        "clerk_url":    clerk_url,
        "flags":        [],
        "score":        0,
    }


def classify_doc_type(doc_type: str) -> tuple[str, str]:
    upper = doc_type.strip().upper()
    if upper in DOC_TYPE_MAP:
        return DOC_TYPE_MAP[upper]
    for key, (cat, label) in DOC_TYPE_MAP.items():
        if key in upper or upper in key:
            return (cat, label)
    return ("OTHER", doc_type)


def normalize_date(raw: str) -> str:
    if not raw:
        return ""
    raw = raw.strip()
    for fmt in [
        "%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y",
        "%Y%m%d", "%d-%b-%Y", "%B %d, %Y", "%b %d, %Y", "%m/%d/%y",
    ]:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def parse_amount(raw: str) -> Optional[float]:
    if not raw:
        return None
    clean = re.sub(r"[,$\s]", "", raw)
    try:
        v = float(clean)
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None


def build_clerk_url(doc_num: str, court_id: str) -> str:
    safe_num = requests.utils.quote(doc_num, safe="")
    return (
        f"{VIRGINIA_OCIS_BASE}/landRecordSearch"
        f"?courtId={court_id}&instrumentNumber={safe_num}"
    )

# ---------------------------------------------------------------------------
# GHL CSV Export
# ---------------------------------------------------------------------------

def generate_ghl_csv(records: list[dict]) -> str:
    columns = [
        "First Name", "Last Name",
        "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
        "Property Address", "Property City", "Property State", "Property Zip",
        "Lead Type", "Document Type", "Date Filed", "Document Number",
        "Amount/Debt Owed", "Seller Score", "Motivated Seller Flags",
        "Jurisdiction", "Source", "Public Records URL",
    ]

    def split_name(full: str) -> tuple[str, str]:
        parts = full.strip().split()
        if not parts:
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
            "Jurisdiction":           rec.get("jurisdiction") or "",
            "Source":                 "Virginia OCIS - " + (rec.get("jurisdiction") or "Greater Richmond, VA"),
            "Public Records URL":     rec.get("clerk_url") or "",
        })

    return output.getvalue()

# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

async def main():
    print("=" * 60)
    print("Greater Richmond, VA — Motivated Seller Lead Scraper")
    print(f"Courts: {', '.join(c['name'] for c in COURTS)}")
    print(f"Run time: {datetime.utcnow().isoformat()}Z")
    print("=" * 60)

    end_date_obj   = datetime.utcnow().date()
    start_date_obj = end_date_obj - timedelta(days=LOOKBACK_DAYS)
    start_date = start_date_obj.strftime("%Y-%m-%d")
    end_date   = end_date_obj.strftime("%Y-%m-%d")
    print(f"Date range: {start_date} → {end_date}  ({LOOKBACK_DAYS} days)")

    # 1. Load parcel data
    dbf_path = download_parcel_dbf()
    if dbf_path:
        parcel_db.load(dbf_path)

    # 2. Scrape all courts
    print("\n[phase 1] Scraping all courts...")
    records = await scrape_all_courts(start_date, end_date)
    print(f"\nTotal raw records across all courts: {len(records)}")

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

    # Sort by score descending
    records.sort(key=lambda r: r["score"], reverse=True)
    with_address = sum(1 for r in records if r.get("prop_address") or r.get("mail_address"))

    # Per-court breakdown
    by_court: dict[str, int] = {}
    for rec in records:
        j = rec.get("jurisdiction") or "Unknown"
        by_court[j] = by_court.get(j, 0) + 1

    # 5. Build output payload
    payload = {
        "fetched_at":    datetime.utcnow().isoformat() + "Z",
        "source":        "Virginia OCIS — Greater Richmond Area",
        "courts":        [c["name"] for c in COURTS],
        "date_range":    f"{start_date} to {end_date}",
        "total":         len(records),
        "with_address":  with_address,
        "by_jurisdiction": by_court,
        "records":       records,
    }

    # 6. Save JSON
    for output_path in OUTPUT_FILES:
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            print(f"[output] Saved {len(records)} records → {output_path}")
        except Exception as exc:
            print(f"[output] Error saving {output_path}: {exc}")

    # 7. GHL CSV
    csv_path = BASE_OUTPUT_DIR / "data" / "ghl_export.csv"
    try:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.write_text(generate_ghl_csv(records), encoding="utf-8")
        print(f"[output] GHL CSV → {csv_path}")
    except Exception as exc:
        print(f"[output] Error saving GHL CSV: {exc}")

    # 8. Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print(f"  Date range:        {start_date} → {end_date}")
    print(f"  Total leads:       {len(records)}")
    print(f"  With address:      {with_address}")
    print(f"  High score (≥70):  {sum(1 for r in records if r.get('score', 0) >= 70)}")
    print(f"\n  By jurisdiction:")
    for court_name, count in sorted(by_court.items(), key=lambda x: -x[1]):
        print(f"    {court_name:<25} {count}")
    if records:
        top = records[0]
        print(f"\n  Top lead: {top.get('owner')} | score={top.get('score')} | {top.get('cat_label')} | {top.get('jurisdiction')}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
