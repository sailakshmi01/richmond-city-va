"""
Greater Richmond, VA — Automated Motivated Seller Lead Scraper
Covers: Richmond City, Henrico, Chesterfield, Hanover, Goochland
Virginia OCIS portal: https://eapps.courts.state.va.us/ocis/landRecordSearch

Strategy: one search per court (date-range only, no doc-type filter),
filter records locally — 5 searches total instead of 100+.
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

# All known Virginia land records entry points (tried in order)
VA_LAND_RECORD_URLS = [
    "https://eapps.courts.state.va.us/ocis/landRecordSearch",
    "https://eapps.courts.state.va.us/landRecordSearch",
    "https://lrims.courts.state.va.us/",
    "https://publicaccess.courts.state.va.us/",
    "https://eapps.courts.state.va.us/caseSearch/landRecords",
]

# Per-search timeouts (seconds)
PAGE_LOAD_TIMEOUT   = 30_000   # 30s
NETWORK_TIMEOUT     = 15_000   # 15s
ELEMENT_TIMEOUT     = 5_000    # 5s
PER_COURT_TIMEOUT   = 120      # 2 min hard cap per court

# ---------------------------------------------------------------------------
# Target jurisdictions
# ---------------------------------------------------------------------------

COURTS = [
    {"id": "760", "name": "Richmond City",       "city": "Richmond",      "state": "VA"},
    {"id": "087", "name": "Henrico County",       "city": "Henrico",       "state": "VA"},
    {"id": "041", "name": "Chesterfield County",  "city": "Chesterfield",  "state": "VA"},
    {"id": "085", "name": "Hanover County",       "city": "Hanover",       "state": "VA"},
    {"id": "075", "name": "Goochland County",     "city": "Goochland",     "state": "VA"},
]

# ---------------------------------------------------------------------------
# Document type codes → category mappings
# ---------------------------------------------------------------------------

DOC_TYPE_MAP = {
    "LP":        ("LP",        "Lis Pendens"),
    "LIS":       ("LP",        "Lis Pendens"),
    "LPS":       ("LP",        "Lis Pendens"),
    "NOFC":      ("NOFC",      "Notice of Foreclosure"),
    "NOTS":      ("NOFC",      "Notice of Foreclosure"),
    "TAXDEED":   ("TAXDEED",   "Tax Deed"),
    "TD":        ("TAXDEED",   "Tax Deed"),
    "JUD":       ("JUD",       "Judgment"),
    "CCJ":       ("JUD",       "Certified Judgment"),
    "DRJUD":     ("JUD",       "Domestic Judgment"),
    "FJ":        ("JUD",       "Judgment"),
    "DJ":        ("JUD",       "Judgment"),
    "LNCORPTX":  ("LNCORPTX",  "Corp Tax Lien"),
    "LNIRS":     ("LNIRS",     "IRS Lien"),
    "LNFED":     ("LNFED",     "Federal Lien"),
    "LN":        ("LN",        "Lien"),
    "LNMECH":    ("LNMECH",    "Mechanic Lien"),
    "LNHOA":     ("LNHOA",     "HOA Lien"),
    "MEDLN":     ("MEDLN",     "Medicaid Lien"),
    "PRO":       ("PRO",       "Probate Document"),
    "WILL":      ("PRO",       "Probate Document"),
    "ADMIN":     ("PRO",       "Probate Document"),
    "NOC":       ("NOC",       "Notice of Commencement"),
    "RELLP":     ("RELLP",     "Release Lis Pendens"),
    "RLP":       ("RELLP",     "Release Lis Pendens"),
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
    def __init__(self):
        self.by_owner: dict[str, list[dict]] = {}
        self.loaded = False

    def load(self, dbf_path: str):
        try:
            from dbfread import DBF
            table = DBF(dbf_path, encoding="latin-1", ignore_missing_memofile=True)
            for rec in table:
                rd = {k.upper(): (v or "").strip() if isinstance(v, str) else v for k, v in rec.items()}
                owner_raw = (rd.get("OWN1") or rd.get("OWNER") or rd.get("OWNER1") or "").strip().upper()
                if not owner_raw:
                    continue
                parcel = self._norm(rd)
                for v in self._variants(owner_raw):
                    self.by_owner.setdefault(v, []).append(parcel)
            self.loaded = True
            print(f"[ParcelLookup] Loaded {sum(len(v) for v in self.by_owner.values())} entries")
        except Exception as exc:
            print(f"[ParcelLookup] Could not load DBF: {exc}")

    @staticmethod
    def _norm(rd: dict) -> dict:
        return {
            "prop_address": (rd.get("SITEADDR") or rd.get("SITE_ADDR") or "").strip(),
            "prop_city":    (rd.get("SITE_CITY") or "").strip(),
            "prop_state":   "VA",
            "prop_zip":     str(rd.get("SITE_ZIP") or "").strip(),
            "mail_address": (rd.get("MAILADR1") or rd.get("ADDR_1") or "").strip(),
            "mail_city":    (rd.get("MAILCITY") or rd.get("CITY") or "").strip(),
            "mail_state":   (rd.get("STATE") or "").strip(),
            "mail_zip":     str(rd.get("MAILZIP") or rd.get("ZIP") or "").strip(),
        }

    @staticmethod
    def _variants(name: str) -> list[str]:
        name = name.strip().upper()
        variants = {name}
        clean = re.sub(r",\s*", " ", name).strip()
        parts = clean.split()
        if len(parts) >= 2:
            variants.add(" ".join(parts[1:]) + " " + parts[0])
            variants.add(f"{parts[0]}, {' '.join(parts[1:])}")
        return [v for v in variants if v]

    def lookup(self, owner_name: str) -> Optional[dict]:
        if not owner_name:
            return None
        for v in self._variants(owner_name.strip().upper()):
            hits = self.by_owner.get(v)
            if hits:
                return hits[0]
        return None


parcel_db = ParcelLookup()

# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

TODAY = datetime.utcnow().date()


def compute_flags(rec: dict) -> list[str]:
    flags = []
    cat   = rec.get("cat", "")
    owner = (rec.get("owner") or "").upper()
    filed_str = rec.get("filed") or ""

    flag_map = {
        "LP":       "Lis pendens",
        "NOFC":     "Pre-foreclosure",
        "JUD":      "Judgment lien",
        "LNMECH":   "Mechanic lien",
        "PRO":      "Probate / estate",
    }
    if cat in flag_map:
        flags.append(flag_map[cat])
    if cat in ("LNCORPTX", "LNIRS", "LNFED"):
        flags.append("Tax lien")
    if re.search(r"\b(LLC|CORP|INC|LTD|LP|TRUST|HOLDINGS|GROUP|PROPERTIES)\b", owner):
        flags.append("LLC / corp owner")
    try:
        if (TODAY - datetime.strptime(filed_str, "%Y-%m-%d").date()).days <= 7:
            flags.append("New this week")
    except Exception:
        pass

    return list(dict.fromkeys(flags))


def compute_score(rec: dict, flags: list[str]) -> int:
    score = 30 + 10 * len(flags)
    if "Lis pendens" in flags and "Pre-foreclosure" in flags:
        score += 20
    try:
        amt = float(rec.get("amount") or 0)
        if amt > 100_000:
            score += 15
        elif amt > 50_000:
            score += 10
    except (TypeError, ValueError):
        pass
    if "New this week" in flags:
        score += 5
    if rec.get("prop_address") or rec.get("mail_address"):
        score += 5
    return min(score, 100)

# ---------------------------------------------------------------------------
# Playwright — one search per court, filter locally
# ---------------------------------------------------------------------------

async def probe_land_records_url(page: Page) -> str:
    """Find the first working Virginia land records URL. Saves debug HTML."""
    debug_dir = BASE_OUTPUT_DIR / "data"
    debug_dir.mkdir(parents=True, exist_ok=True)

    for url in VA_LAND_RECORD_URLS:
        try:
            print(f"  [probe] Trying: {url}")
            resp = await page.goto(url, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
            if resp and resp.status < 400:
                content = await page.content()
                soup    = BeautifulSoup(content, "lxml")

                # Save first successful page for debugging
                debug_path = debug_dir / "debug_portal.html"
                try:
                    debug_path.write_text(content, encoding="utf-8")
                except Exception:
                    pass

                # Log all form elements so we know the real field names
                forms = soup.find_all("form")
                for fi, form in enumerate(forms):
                    print(f"  [probe] Form {fi}: action={form.get('action')} method={form.get('method')}")
                    for el in form.find_all(["input", "select", "textarea"]):
                        name = el.get("name") or el.get("id") or "(no name)"
                        etype = el.get("type") or el.name
                        print(f"           field: {etype} name={name}")

                # Also log any selects with options
                for sel in soup.find_all("select"):
                    opts = [o.get("value","") + "=" + o.get_text(strip=True) for o in sel.find_all("option")[:5]]
                    print(f"  [probe] <select name={sel.get('name')} id={sel.get('id')}> options: {opts}")

                if forms or soup.find("table"):
                    print(f"  [probe] SUCCESS: {url}")
                    return url
        except Exception as exc:
            print(f"  [probe] Failed {url}: {exc}")

    return VA_LAND_RECORD_URLS[0]  # fall back to first URL


async def search_court(page: Page, court: dict, start_date: str, end_date: str,
                       base_url: str) -> list[dict]:
    """
    Search the Virginia land records portal for all records in the date range
    for one court. Auto-detects form field names from the live page.
    """
    court_id   = court["id"]
    court_name = court["name"]
    records    = []

    try:
        await page.goto(base_url, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("load", timeout=PAGE_LOAD_TIMEOUT)
        except PlaywrightTimeout:
            pass

        content = await page.content()
        soup    = BeautifulSoup(content, "lxml")

        # --- Auto-detect field names from the live HTML ---
        court_sel_name  = None
        start_field     = None
        end_field       = None

        for sel_tag in soup.find_all("select"):
            n = sel_tag.get("name") or sel_tag.get("id") or ""
            opts_vals = [o.get("value","") for o in sel_tag.find_all("option")]
            if court_id in opts_vals or any(c["id"] in opts_vals for c in COURTS):
                court_sel_name = n
                print(f"  [detect] Court select: name={n}")
                break
            if any(k in n.lower() for k in ["court", "jurisdiction", "fips"]):
                court_sel_name = n
                print(f"  [detect] Court select (by name): name={n}")

        for inp in soup.find_all("input"):
            n = (inp.get("name") or inp.get("id") or "").lower()
            if not start_field and any(k in n for k in ["startdate","start_date","datefrom","from","begindate","filed_from"]):
                start_field = inp.get("name") or inp.get("id")
                print(f"  [detect] Start date field: {start_field}")
            if not end_field and any(k in n for k in ["enddate","end_date","dateto","to","enddt","filed_to"]):
                end_field = inp.get("name") or inp.get("id")
                print(f"  [detect] End date field: {end_field}")

        # --- Fill the form ---
        if court_sel_name:
            try:
                await page.select_option(f'[name="{court_sel_name}"]', value=court_id, timeout=ELEMENT_TIMEOUT)
            except Exception:
                try:
                    await page.select_option(f'[name="{court_sel_name}"]', label=court_name, timeout=ELEMENT_TIMEOUT)
                except Exception as e:
                    print(f"  [warn] Could not select court: {e}")
        else:
            # Try common selectors
            for sel in ['select[name="courtId"]', 'select[name="court"]', '#courtSelect',
                        '#court', 'select[name="fipsCode"]', 'select[name="selectedCourt"]']:
                try:
                    await page.select_option(sel, value=court_id, timeout=ELEMENT_TIMEOUT)
                    print(f"  [fallback] Used court selector: {sel}")
                    break
                except Exception:
                    try:
                        await page.select_option(sel, label=court_name, timeout=ELEMENT_TIMEOUT)
                        print(f"  [fallback] Used court selector by label: {sel}")
                        break
                    except Exception:
                        continue

        date_fmt = "%m/%d/%Y"  # Most Virginia portals use MM/DD/YYYY
        start_str = datetime.strptime(start_date, "%Y-%m-%d").strftime(date_fmt)
        end_str   = datetime.strptime(end_date,   "%Y-%m-%d").strftime(date_fmt)

        if start_field:
            await page.fill(f'[name="{start_field}"], #{start_field}', start_str, timeout=ELEMENT_TIMEOUT)
        else:
            for sel in ['input[name="startDate"]', 'input[name="dateFrom"]', '#startDate',
                        '#dateFrom', 'input[name="instrumentDateFrom"]', 'input[name="fromDate"]']:
                try:
                    await page.fill(sel, start_str, timeout=ELEMENT_TIMEOUT)
                    break
                except Exception:
                    continue

        if end_field:
            await page.fill(f'[name="{end_field}"], #{end_field}', end_str, timeout=ELEMENT_TIMEOUT)
        else:
            for sel in ['input[name="endDate"]', 'input[name="dateTo"]', '#endDate',
                        '#dateTo', 'input[name="instrumentDateTo"]', 'input[name="toDate"]']:
                try:
                    await page.fill(sel, end_str, timeout=ELEMENT_TIMEOUT)
                    break
                except Exception:
                    continue

        # --- Submit ---
        for sel in ['button[type="submit"]', 'input[type="submit"]', '#searchBtn',
                    '#btnSearch', '#search', 'button:has-text("Search")', 'a:has-text("Search")']:
            try:
                await page.click(sel, timeout=ELEMENT_TIMEOUT)
                print(f"  [submit] Clicked: {sel}")
                break
            except Exception:
                continue

        try:
            await page.wait_for_load_state("load", timeout=NETWORK_TIMEOUT)
        except PlaywrightTimeout:
            pass

        # Save post-search HTML for debugging
        post_content = await page.content()
        debug_path   = BASE_OUTPUT_DIR / "data" / f"debug_{court_id}_results.html"
        try:
            debug_path.write_text(post_content, encoding="utf-8")
        except Exception:
            pass

        # --- Paginate and parse ---
        while True:
            recs = await parse_results_page(page, court)
            records.extend(recs)
            print(f"  [page] +{len(recs)} records (total {len(records)})")

            try:
                nxt = await page.query_selector('a:has-text("Next"), button:has-text("Next"), [aria-label="Next page"]')
                if nxt and await nxt.is_visible():
                    await nxt.click(timeout=ELEMENT_TIMEOUT)
                    try:
                        await page.wait_for_load_state("load", timeout=NETWORK_TIMEOUT)
                    except PlaywrightTimeout:
                        pass
                else:
                    break
            except Exception:
                break

    except PlaywrightTimeout as exc:
        print(f"  [timeout] {court_name}: {exc}")
    except Exception as exc:
        print(f"  [error]   {court_name}: {exc}")
        traceback.print_exc()

    return records


async def parse_results_page(page: Page, court: dict) -> list[dict]:
    """Parse every result table row on the current page, keep only target cats."""
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

            col_map: dict[str, int] = {}
            for i, h in enumerate(headers):
                if any(x in h for x in ["INST", "DOC", "NUMBER"]):
                    col_map.setdefault("doc_num", i)
                if "TYPE" in h:
                    col_map.setdefault("doc_type", i)
                if any(x in h for x in ["DATE", "FILED", "RECORD"]):
                    col_map.setdefault("filed", i)
                if any(x in h for x in ["GRANTOR", "OWNER", "PARTY1"]):
                    col_map.setdefault("owner", i)
                if any(x in h for x in ["GRANTEE", "PARTY2"]):
                    col_map.setdefault("grantee", i)
                if any(x in h for x in ["LEGAL", "DESCRIPTION"]):
                    col_map.setdefault("legal", i)
                if any(x in h for x in ["AMOUNT", "CONSIDER", "VALUE"]):
                    col_map.setdefault("amount", i)

            if "doc_num" not in col_map:
                continue

            for row in rows[1:]:
                cells = row.find_all("td")
                if not cells:
                    continue

                def ct(key):
                    idx = col_map.get(key)
                    return cells[idx].get_text(strip=True) if idx is not None and idx < len(cells) else ""

                doc_num  = ct("doc_num")
                if not doc_num:
                    continue

                doc_type = ct("doc_type")
                cat, cat_label = classify_doc_type(doc_type)
                if cat not in TARGET_CATS:
                    continue

                link_tag  = row.find("a", href=True)
                clerk_url = ""
                if link_tag:
                    href = link_tag["href"]
                    clerk_url = href if href.startswith("http") else VIRGINIA_OCIS_BASE + "/" + href.lstrip("/")

                records.append(make_record(
                    doc_num=doc_num,
                    doc_type=doc_type,
                    filed=normalize_date(ct("filed")),
                    cat=cat,
                    cat_label=cat_label,
                    owner=ct("owner"),
                    grantee=ct("grantee"),
                    amount=parse_amount(ct("amount")),
                    legal=ct("legal"),
                    clerk_url=clerk_url or build_clerk_url(doc_num, court["id"]),
                    court=court,
                ))

    except Exception as exc:
        print(f"  [parse] Error: {exc}")

    return records


async def scrape_all_courts(start_date: str, end_date: str) -> list[dict]:
    """One Playwright search per court — 5 total."""
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

        # Step 1: Probe for the correct URL (once)
        probe_page = await context.new_page()
        print("[probe] Finding working Virginia land records URL...")
        base_url = await probe_land_records_url(probe_page)
        await probe_page.close()
        print(f"[probe] Using: {base_url}\n")

        # Step 2: Search each court
        for court in COURTS:
            print(f"\n[court] {court['name']}  (ID: {court['id']})")

            page = await context.new_page()
            try:
                recs = await asyncio.wait_for(
                    search_court(page, court, start_date, end_date, base_url),
                    timeout=PER_COURT_TIMEOUT,
                )
            except asyncio.TimeoutError:
                print(f"  [timeout] {court['name']} exceeded {PER_COURT_TIMEOUT}s — skipping")
                recs = []
            except Exception as exc:
                print(f"  [error] {court['name']}: {exc}")
                recs = []
            finally:
                await page.close()

            new_count = 0
            for rec in recs:
                key = f"{court['id']}:{rec['doc_num']}"
                if key not in seen:
                    seen.add(key)
                    all_records.append(rec)
                    new_count += 1

            print(f"  → {new_count} new records")

            # HTTP fallback
            http_recs = fetch_court_http(court, start_date, end_date, seen)
            all_records.extend(http_recs)
            if http_recs:
                print(f"  → {len(http_recs)} additional via HTTP fallback")

        await context.close()
        await browser.close()

    return all_records

# ---------------------------------------------------------------------------
# HTTP fallback (requests + BeautifulSoup)
# ---------------------------------------------------------------------------

def fetch_court_http(court: dict, start_date: str, end_date: str, seen: set) -> list[dict]:
    records  = []
    court_id = court["id"]
    session  = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "application/json, text/html, */*",
    })

    # JSON API probe
    for url in [
        f"{VA_OCIS_API}/landRecords?courtId={court_id}&startDate={start_date}&endDate={end_date}",
        f"{VIRGINIA_OCIS_BASE}/api/landRecords?court={court_id}&fromDate={start_date}&toDate={end_date}",
    ]:
        try:
            resp = session.get(url, timeout=20)
            if resp.status_code == 200 and "application/json" in resp.headers.get("content-type", ""):
                data  = resp.json()
                items = data if isinstance(data, list) else data.get("records", data.get("results", []))
                for item in items:
                    rec = parse_api_item(item, court)
                    if rec:
                        key = f"{court_id}:{rec['doc_num']}"
                        if key not in seen:
                            seen.add(key)
                            records.append(rec)
                if records:
                    return records
        except Exception:
            pass

    # HTML fallback
    try:
        resp = session.get(
            f"{VIRGINIA_OCIS_BASE}/landRecordSearch"
            f"?courtId={court_id}&startDate={start_date}&endDate={end_date}",
            timeout=30,
        )
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
        print(f"  [http] HTML fallback failed {court['name']}: {exc}")

    return records


def parse_api_item(item: dict, court: dict) -> Optional[dict]:
    try:
        doc_type = (item.get("documentType") or item.get("instrType") or item.get("docType") or "").strip()
        cat, cat_label = classify_doc_type(doc_type)
        if cat not in TARGET_CATS:
            return None
        doc_num = str(item.get("instrumentNumber") or item.get("docNumber") or item.get("instrNum") or "").strip()
        if not doc_num:
            return None
        return make_record(
            doc_num=doc_num,
            doc_type=doc_type,
            filed=normalize_date(str(item.get("recordedDate") or item.get("filedDate") or item.get("date") or "")),
            cat=cat, cat_label=cat_label,
            owner=str(item.get("grantor") or item.get("owner") or "").strip(),
            grantee=str(item.get("grantee") or "").strip(),
            amount=parse_amount(str(item.get("consideration") or item.get("amount") or "")),
            legal=str(item.get("legalDescription") or item.get("legal") or "").strip(),
            clerk_url=str(item.get("url") or item.get("link") or build_clerk_url(doc_num, court["id"])),
            court=court,
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
        if not doc_num:
            return None
        doc_type = get(["TYPE"])
        cat, cat_label = classify_doc_type(doc_type)
        if cat not in TARGET_CATS:
            return None

        link_tag  = next((c.find("a", href=True) for c in cells if c.find("a", href=True)), None)
        href      = link_tag["href"] if link_tag else ""
        clerk_url = href if href.startswith("http") else (VIRGINIA_OCIS_BASE + "/" + href.lstrip("/") if href else "")

        return make_record(
            doc_num=doc_num, doc_type=doc_type,
            filed=normalize_date(get(["DATE", "FILED", "RECORD"])),
            cat=cat, cat_label=cat_label,
            owner=get(["GRANTOR", "OWNER"]),
            grantee=get(["GRANTEE"]),
            amount=parse_amount(get(["AMOUNT", "CONSIDER"])),
            legal=get(["LEGAL", "DESC"]),
            clerk_url=clerk_url or build_clerk_url(doc_num, court["id"]),
            court=court,
        )
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Property Appraiser DBF download
# ---------------------------------------------------------------------------

PARCEL_DBF_URLS = [
    "https://www.rva.gov/sites/default/files/2024-01/ParcelData.zip",
    "https://opendata.rva.gov/datasets/parcels/data.zip",
    "https://gis.rva.gov/download/parcels.zip",
]


def download_parcel_dbf() -> Optional[str]:
    import zipfile
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (compatible; MotivatedSellerScraper/1.0)"
    for url in PARCEL_DBF_URLS:
        try:
            print(f"[parcel] Trying: {url}")
            resp    = session.get(url, timeout=60, stream=True)
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
                        path = os.path.join(root, fname)
                        print(f"[parcel] Found DBF: {path}")
                        return path
        except Exception as exc:
            print(f"[parcel] Failed {url}: {exc}")
    print("[parcel] Could not download parcel DBF — address enrichment skipped")
    return None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_record(*, doc_num, doc_type, filed, cat, cat_label,
                owner, grantee, amount, legal, clerk_url, court: dict) -> dict:
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
    for fmt in ["%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%Y%m%d",
                "%d-%b-%Y", "%B %d, %Y", "%b %d, %Y", "%m/%d/%y"]:
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
    safe = requests.utils.quote(doc_num, safe="")
    return f"{VIRGINIA_OCIS_BASE}/landRecordSearch?courtId={court_id}&instrumentNumber={safe}"

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
        if not parts:  return ("", "")
        if len(parts) == 1: return (parts[0], "")
        return (" ".join(parts[:-1]), parts[-1])

    out = io.StringIO()
    w   = csv.DictWriter(out, fieldnames=columns)
    w.writeheader()
    for rec in records:
        first, last = split_name(rec.get("owner") or "")
        w.writerow({
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
    return out.getvalue()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    print("=" * 60)
    print("Greater Richmond, VA — Motivated Seller Lead Scraper")
    print(f"Courts: {', '.join(c['name'] for c in COURTS)}")
    print(f"Run time: {datetime.utcnow().isoformat()}Z")
    print("=" * 60)

    end_dt   = datetime.utcnow().date()
    start_dt = end_dt - timedelta(days=LOOKBACK_DAYS)
    start_date, end_date = start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d")
    print(f"Date range: {start_date} → {end_date}  ({LOOKBACK_DAYS} days)\n")

    # 1. Parcel data
    dbf_path = download_parcel_dbf()
    if dbf_path:
        parcel_db.load(dbf_path)

    # 2. Scrape
    print("[phase 1] Scraping all courts (one search per court)...")
    records = await scrape_all_courts(start_date, end_date)
    print(f"\nTotal raw records: {len(records)}")

    # 3. Enrich
    print("[phase 2] Enriching with parcel data...")
    enriched = 0
    for rec in records:
        p = parcel_db.lookup(rec.get("owner") or "")
        if p:
            rec.update(p)
            enriched += 1
    print(f"Enriched {enriched}/{len(records)} records")

    # 4. Score
    print("[phase 3] Scoring...")
    for rec in records:
        rec["flags"] = compute_flags(rec)
        rec["score"] = compute_score(rec, rec["flags"])

    records.sort(key=lambda r: r["score"], reverse=True)
    with_address = sum(1 for r in records if r.get("prop_address") or r.get("mail_address"))

    by_court: dict[str, int] = {}
    for rec in records:
        j = rec.get("jurisdiction") or "Unknown"
        by_court[j] = by_court.get(j, 0) + 1

    # 5. Save
    payload = {
        "fetched_at":       datetime.utcnow().isoformat() + "Z",
        "source":           "Virginia OCIS — Greater Richmond Area",
        "courts":           [c["name"] for c in COURTS],
        "date_range":       f"{start_date} to {end_date}",
        "total":            len(records),
        "with_address":     with_address,
        "by_jurisdiction":  by_court,
        "records":          records,
    }

    for path in OUTPUT_FILES:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            print(f"[output] → {path}  ({len(records)} records)")
        except Exception as exc:
            print(f"[output] Error {path}: {exc}")

    csv_path = BASE_OUTPUT_DIR / "data" / "ghl_export.csv"
    try:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.write_text(generate_ghl_csv(records), encoding="utf-8")
        print(f"[output] → {csv_path}")
    except Exception as exc:
        print(f"[output] CSV error: {exc}")

    # 6. Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print(f"  Date range:       {start_date} → {end_date}")
    print(f"  Total leads:      {len(records)}")
    print(f"  With address:     {with_address}")
    print(f"  High score ≥70:   {sum(1 for r in records if r.get('score', 0) >= 70)}")
    print(f"\n  By jurisdiction:")
    for name, cnt in sorted(by_court.items(), key=lambda x: -x[1]):
        print(f"    {name:<26} {cnt}")
    if records:
        t = records[0]
        print(f"\n  Top lead: {t.get('owner')} | score={t.get('score')} | {t.get('cat_label')} | {t.get('jurisdiction')}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
