"""
Greater Richmond, VA — Motivated Seller Lead Scraper v3
=======================================================
Covers: Richmond City (#760), Henrico (#087), Chesterfield (#041),
        Hanover (#085), Goochland (#075)

Sources (in priority order):
  1. Richmond City GIS ArcGIS REST API — property transfers with distress markers
     (free, no captcha, uses services1.arcgis.com/k3vhq11XkBNeeOfM)
  2. ACT DataScout Playwright — land instruments recorded with circuit court clerks
     (actdatascout.com — Richmond City, reCAPTCHA v3 auto-handled by Playwright)
  3. OCIS Playwright — circuit court civil cases across all 5 jurisdictions
     (eapps.courts.state.va.us/ocis — Angular SPA navigated by Playwright)

Outputs: data/records.json, dashboard/records.json, data/leads.csv (GHL format)
"""

import asyncio
import csv
import io
import json
import os
import re
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeout

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", 30))
BASE_DIR      = Path(__file__).parent.parent

OUTPUT_JSON   = [
    BASE_DIR / "dashboard" / "records.json",
    BASE_DIR / "data"      / "records.json",
]
OUTPUT_CSV    = BASE_DIR / "data" / "leads.csv"

COURTS = [
    {"fips": "760", "ocis_id": "760C", "name": "Richmond City",       "state": "VA"},
    {"fips": "087", "ocis_id": "087C", "name": "Henrico County",       "state": "VA"},
    {"fips": "041", "ocis_id": "041C", "name": "Chesterfield County",  "state": "VA"},
    {"fips": "085", "ocis_id": "085C", "name": "Hanover County",       "state": "VA"},
    {"fips": "075", "ocis_id": "075C", "name": "Goochland County",     "state": "VA"},
]

# Richmond City GIS ArcGIS REST (no auth required)
RVA_GIS_BASE = "https://services1.arcgis.com/k3vhq11XkBNeeOfM/arcgis/rest/services"

# ACT DataScout — Virginia land records
ACTDATASCOUT_BASE = "https://www.actdatascout.com/RealProperty/Virginia"

# OCIS — Virginia court case search (Angular SPA)
OCIS_BASE = "https://eapps.courts.state.va.us/ocis"

PAGE_TIMEOUT = 30_000   # ms

# ---------------------------------------------------------------------------
# Lead scoring
# ---------------------------------------------------------------------------

SCORE_RULES = {
    # Source type
    "Foreclosure / Forced Sale":   85,
    "Lis Pendens":                  90,
    "Tax Deed":                     80,
    "Notice of Foreclosure":        82,
    "Judgment":                     70,
    "Mechanic Lien":                65,
    "HOA Lien":                     60,
    "Federal Tax Lien":             75,
    "IRS Tax Lien":                 78,
    "Medicaid Lien":                62,
    "Probate":                      68,
    "Special Financing":            50,
    "Surplus Property":             55,
    "Civil Case - Foreclosure":     75,
    "Civil Case":                   45,
}

def score_lead(lead: dict) -> int:
    base = SCORE_RULES.get(lead.get("doc_type"), 40)
    # Equity bonus: if sale price < 70% of AV
    ratio = lead.get("sale_ratio", 1.0)
    if ratio and ratio < 0.70:
        base = min(100, base + 10)
    elif ratio and ratio < 0.85:
        base = min(100, base + 5)
    return base


# ---------------------------------------------------------------------------
# Source 1: Richmond City GIS — Property Transfers
# ---------------------------------------------------------------------------

def scrape_richmond_gis(lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    """
    Query Richmond City's ArcGIS FeatureServer for property transfer records.
    Filters for distressed sale indicators (Foreclosure, Special Financing, etc.).
    Data covers ~Oct 2021 – Oct 2024 (annual CAMA snapshot).
    """
    leads = []
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 Chrome/120", "Accept": "application/json"})

    # Richmond City GIS data is an annual CAMA snapshot (Oct 2021 – Oct 2024).
    # Filter to the most recent 12 months of available data to surface
    # recent distress transactions; fall back to all records if nothing found.
    # The GIS is refreshed annually so this gives the most relevant leads.
    cutoff_date = "2024-01-01"  # most recent annual data year

    DISTRESS_CODES = [
        "Foreclosure%",
        "Special Financing%",
    ]

    for code_filter in DISTRESS_CODES:
        try:
            params = {
                "where": f"val_code_2 LIKE '{code_filter}' AND sale_date >= date '{cutoff_date}'",
                "outFields": (
                    "OBJECTID2,parcel_id,prop_street,owner1,sale_date,sale_price,"
                    "Land,Impr,tot,val_code_1,val_code_2,DocNum"
                ),
                "orderByFields": "sale_date DESC",
                "resultRecordCount": 500,
                "f": "json",
            }
            r = session.get(
                f"{RVA_GIS_BASE}/AssessorProValGPINRecTransPublish/FeatureServer/0/query",
                params=params,
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                print(f"[RVA GIS] Error for {code_filter}: {data['error']}")
                continue

            features = data.get("features", [])
            print(f"[RVA GIS] {code_filter}: {len(features)} records")

            for feat in features:
                a = feat.get("attributes", {})
                try:
                    sale_ts = a.get("sale_date")
                    sale_date = (
                        datetime.fromtimestamp(sale_ts / 1000).strftime("%Y-%m-%d")
                        if sale_ts else None
                    )
                    assessed   = a.get("tot", 0) or 0
                    sale_price = a.get("sale_price", 0) or 0
                    ratio      = round(sale_price / assessed, 3) if assessed > 0 else None
                    address    = (a.get("prop_street") or "").strip()
                    vc2        = (a.get("val_code_2") or "").strip()
                    doc_type   = (
                        "Foreclosure / Forced Sale" if "foreclo" in vc2.lower()
                        else "Special Financing"
                    )
                    lead = {
                        "id":            f"rva-gis-{a.get('OBJECTID2', a.get('parcel_id',''))}",
                        "source":        "Richmond City GIS",
                        "jurisdiction":  "Richmond City",
                        "doc_type":      doc_type,
                        "recorded_date": sale_date,
                        "address":       address,
                        "city":          "Richmond",
                        "state":         "VA",
                        "zip":           "",
                        "grantor":       "",
                        "grantee":       (a.get("owner1") or "").strip(),
                        "assessed_value": assessed,
                        "sale_price":    sale_price,
                        "sale_ratio":    ratio,
                        "doc_number":    (a.get("DocNum") or "").strip(),
                        "parcel_id":     (a.get("parcel_id") or "").strip(),
                        "val_code":      vc2,
                        "scraped_at":    datetime.utcnow().isoformat() + "Z",
                    }
                    lead["score"] = score_lead(lead)
                    leads.append(lead)
                except Exception:
                    continue

        except Exception as e:
            print(f"[RVA GIS] Error fetching {code_filter}: {e}")

    # Deduplicate by unique OBJECTID2-based id
    seen = set()
    unique = []
    for lead in leads:
        lid = lead["id"]
        if lid not in seen:
            seen.add(lid)
            unique.append(lead)

    print(f"[RVA GIS] Total unique leads: {len(unique)}")
    return unique


def scrape_richmond_surplus() -> list[dict]:
    """Richmond City surplus / tax-sale properties (free ArcGIS API)."""
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 Chrome/120", "Accept": "application/json"})
    leads = []
    try:
        r = session.get(
            f"{RVA_GIS_BASE}/2020_Surplus_Properties___New2/FeatureServer/0/query",
            params={"where": "1=1", "outFields": "*", "resultRecordCount": 200, "f": "json"},
            timeout=20,
        )
        data = r.json()
        if "error" in data:
            return []
        for feat in data.get("features", []):
            a = feat.get("attributes", {})
            lead = {
                "id":            f"rva-surplus-{a.get('ObjectId','')}",
                "source":        "Richmond City Surplus",
                "jurisdiction":  "Richmond City",
                "doc_type":      "Surplus Property",
                "recorded_date": None,
                "address":       (a.get("Address") or "").strip(),
                "city":          "Richmond",
                "state":         "VA",
                "zip":           "",
                "grantor":       "City of Richmond",
                "grantee":       "",
                "assessed_value": 0,
                "sale_price":    0,
                "sale_ratio":    None,
                "doc_number":    "",
                "parcel_id":     (a.get("Parcel_ID") or "").strip(),
                "val_code":      "Surplus",
                "scraped_at":    datetime.utcnow().isoformat() + "Z",
            }
            lead["score"] = score_lead(lead)
            leads.append(lead)
    except Exception as e:
        print(f"[RVA Surplus] Error: {e}")
    print(f"[RVA Surplus] {len(leads)} properties")
    return leads


# ---------------------------------------------------------------------------
# Source 2: ACT DataScout (Playwright) — Richmond City Land Instruments
# ---------------------------------------------------------------------------

async def scrape_actdatascout(
    page: Page, lookback_days: int = LOOKBACK_DAYS
) -> list[dict]:
    """
    Navigate ACT DataScout for Richmond City land records.
    Searches for Lis Pendens, Deeds of Trust, Mechanic Liens, etc.
    reCAPTCHA v3 is invisible — Playwright typically passes it.
    """
    leads = []
    url = f"{ACTDATASCOUT_BASE}/Richmond"

    DOC_TYPE_TARGETS = [
        ("Lis Pendens",         "LP"),
        ("Deed of Trust",       "DT"),
        ("Mechanic Lien",       "ML"),
        ("Release",             "REL"),
    ]

    try:
        print(f"[ACT DataScout] Navigating to {url}")
        await page.goto(url, wait_until="networkidle", timeout=PAGE_TIMEOUT)
        await page.wait_for_timeout(2000)

        # Take screenshot for debug
        await page.screenshot(path="/tmp/actdatascout.png")

        # Detect if we're on a search page or got redirected
        page_url = page.url
        title    = await page.title()
        print(f"[ACT DataScout] URL={page_url}, Title={title[:60]}")

        if "actdatascout.com/RealProperty" not in page_url:
            print("[ACT DataScout] Redirected away — not available for this county")
            return leads

        # Look for date-range search fields
        from_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%m/%d/%Y")
        to_date   = datetime.now().strftime("%m/%d/%Y")

        # Common ACT DataScout field selectors
        selectors = {
            "from_date": ["#FromDate", "#StartDate", "[name='FromDate']", "[name='StartDate']",
                          "input[placeholder*='from']", "input[placeholder*='From']"],
            "to_date":   ["#ToDate",   "#EndDate",   "[name='ToDate']",   "[name='EndDate']",
                          "input[placeholder*='to']",   "input[placeholder*='To']"],
            "doc_type":  ["#InstrumentType", "#DocType", "select[name='InstrumentType']"],
            "submit":    ["#btnSearch", "input[type='submit']", "button[type='submit']",
                          "button:has-text('Search')"],
        }

        # Try to fill the from_date
        for sel in selectors["from_date"]:
            if await page.query_selector(sel):
                await page.fill(sel, from_date)
                print(f"[ACT DataScout] Filled from_date via {sel}")
                break

        # Try to fill the to_date
        for sel in selectors["to_date"]:
            if await page.query_selector(sel):
                await page.fill(sel, to_date)
                print(f"[ACT DataScout] Filled to_date via {sel}")
                break

        # Click search
        for sel in selectors["submit"]:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                print(f"[ACT DataScout] Clicked search via {sel}")
                await page.wait_for_load_state("networkidle", timeout=15000)
                break

        await page.wait_for_timeout(2000)
        await page.screenshot(path="/tmp/actdatascout_results.png")

        # Extract results table
        rows = await page.query_selector_all("table tr")
        print(f"[ACT DataScout] Found {len(rows)} rows")
        headers = []
        for i, row in enumerate(rows):
            cells = await row.query_selector_all("td, th")
            cell_texts = [((await c.inner_text()).strip()) for c in cells]
            if i == 0:
                headers = cell_texts
                continue
            if not any(cell_texts):
                continue
            rec = dict(zip(headers, cell_texts)) if headers else {"row": cell_texts}
            lead = _parse_actdatascout_row(rec)
            if lead:
                leads.append(lead)

    except PlaywrightTimeout:
        print("[ACT DataScout] Timed out")
    except Exception as e:
        print(f"[ACT DataScout] Error: {e}")
        traceback.print_exc()

    print(f"[ACT DataScout] {len(leads)} leads")
    return leads


def _parse_actdatascout_row(rec: dict) -> Optional[dict]:
    """Parse a single ACT DataScout result row."""
    # Try to find common field names (vary by county configuration)
    address    = rec.get("Property Address") or rec.get("Address") or rec.get("Situs") or ""
    grantor    = rec.get("Grantor") or rec.get("Seller") or ""
    grantee    = rec.get("Grantee") or rec.get("Buyer") or ""
    doc_type   = rec.get("Instrument Type") or rec.get("Doc Type") or rec.get("Type") or ""
    rec_date   = rec.get("Recording Date") or rec.get("Recorded") or rec.get("Date") or ""
    doc_num    = rec.get("Instrument Number") or rec.get("Doc #") or rec.get("Book/Page") or ""
    amount_str = rec.get("Consideration") or rec.get("Amount") or rec.get("Price") or "0"

    if not address and not grantor:
        return None

    # Parse amount
    amount = 0
    try:
        amount = int(re.sub(r"[^\d]", "", amount_str))
    except Exception:
        pass

    # Map doc type to canonical type
    doc_type_canonical = _map_doc_type(doc_type)

    lead = {
        "id":            f"actscout-{doc_num.replace('/', '-').replace(' ', '')}",
        "source":        "ACT DataScout",
        "jurisdiction":  "Richmond City",
        "doc_type":      doc_type_canonical,
        "recorded_date": _parse_date(rec_date),
        "address":       address.strip(),
        "city":          "Richmond",
        "state":         "VA",
        "zip":           "",
        "grantor":       grantor.strip(),
        "grantee":       grantee.strip(),
        "assessed_value": 0,
        "sale_price":    amount,
        "sale_ratio":    None,
        "doc_number":    doc_num.strip(),
        "parcel_id":     "",
        "val_code":      doc_type,
        "scraped_at":    datetime.utcnow().isoformat() + "Z",
    }
    lead["score"] = score_lead(lead)
    return lead


def _map_doc_type(raw: str) -> str:
    raw_upper = raw.upper()
    mapping = {
        "LP": "Lis Pendens",   "LIS PENDENS": "Lis Pendens",
        "DT": "Deed of Trust", "DEED OF TRUST": "Deed of Trust",
        "ML": "Mechanic Lien", "MECHANIC LIEN": "Mechanic Lien",
        "HOA": "HOA Lien",
        "FL": "Federal Tax Lien", "FEDERAL": "Federal Tax Lien",
        "IRS": "IRS Tax Lien",
        "TD": "Tax Deed",      "TAX DEED": "Tax Deed",
        "JUD": "Judgment",     "JUDGMENT": "Judgment",
        "PROB": "Probate",     "PROBATE": "Probate",
        "MED": "Medicaid Lien","MEDICAID": "Medicaid Lien",
    }
    for key, val in mapping.items():
        if key in raw_upper:
            return val
    return raw.strip() or "Unknown"


def _parse_date(s: str) -> Optional[str]:
    """Parse various date formats → YYYY-MM-DD."""
    s = (s or "").strip()
    for fmt in ["%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%B %d, %Y"]:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s or None


# ---------------------------------------------------------------------------
# Source 3: OCIS Playwright — Circuit Court Civil Cases
# ---------------------------------------------------------------------------

async def scrape_ocis(
    page: Page, courts: list[dict], lookback_days: int = LOOKBACK_DAYS
) -> list[dict]:
    """
    Navigate Virginia OCIS (Angular SPA) to find civil court cases.
    - Accepts terms of service by clicking the UI button
    - Searches each circuit court for recent civil cases
    - Filters for foreclosure/judgment/lien-related case types
    """
    all_leads = []
    landing_url = f"{OCIS_BASE}/landing"

    # Plaintiff names that indicate foreclosure/liens
    FORECLOSURE_PLAINTIFFS = [
        "TRUSTEE",
        "BANK OF AMERICA",
        "WELLS FARGO",
        "PENNYMAC",
        "MR COOPER",
        "NATIONSTAR",
        "SPECIALIZED LOAN",
        "PHH MORTGAGE",
        "NEWREZ",
        "FREEDOM MORTGAGE",
        "LOANCARE",
        "CARRINGTON",
        "COMMONWEALTH OF VIRGINIA",
        "SECRETARY OF VETERANS",
        "INTERNAL REVENUE",
    ]

    try:
        print(f"[OCIS] Loading landing page: {landing_url}")
        await page.goto(landing_url, wait_until="networkidle", timeout=PAGE_TIMEOUT)
        await page.wait_for_timeout(2000)

        # Accept terms — click "I Accept" button
        terms_selectors = [
            "button:has-text('I Accept')",
            "button:has-text('Accept')",
            "button:has-text('Agree')",
            "#btnAccept",
            ".accept-btn",
            "[data-test='accept']",
        ]
        terms_clicked = False
        for sel in terms_selectors:
            try:
                btn = await page.wait_for_selector(sel, timeout=5000)
                if btn:
                    await btn.click()
                    print(f"[OCIS] Accepted terms via {sel}")
                    terms_clicked = True
                    break
            except PlaywrightTimeout:
                continue

        if not terms_clicked:
            # Try to click any visible button in the terms dialog
            visible_buttons = await page.query_selector_all("button:visible, .btn:visible")
            for btn in visible_buttons:
                text = (await btn.inner_text()).strip().lower()
                if "accept" in text or "agree" in text or "continue" in text:
                    await btn.click()
                    terms_clicked = True
                    print(f"[OCIS] Accepted terms via text match: '{text}'")
                    break

        await page.wait_for_timeout(2000)

        # Navigate to search page
        search_url = f"{OCIS_BASE}/search"
        print(f"[OCIS] Navigating to search: {search_url}")
        await page.goto(search_url, wait_until="networkidle", timeout=PAGE_TIMEOUT)
        await page.wait_for_timeout(3000)
        await page.screenshot(path="/tmp/ocis_search.png")
        print(f"[OCIS] Search page loaded: {page.url}")

        # For each court, search for civil cases
        for court in courts:
            court_leads = await _ocis_search_court(page, court, FORECLOSURE_PLAINTIFFS, lookback_days)
            all_leads.extend(court_leads)
            await asyncio.sleep(2)

    except PlaywrightTimeout:
        print("[OCIS] Timed out on landing page")
    except Exception as e:
        print(f"[OCIS] Error: {e}")
        traceback.print_exc()

    print(f"[OCIS] Total leads: {len(all_leads)}")
    return all_leads


async def _ocis_search_court(
    page: Page, court: dict, plaintiffs: list[str], lookback_days: int
) -> list[dict]:
    """Search OCIS for a specific court's civil cases."""
    leads = []
    court_name = court["name"]

    try:
        # Select court from dropdown
        court_sel = f"option:has-text('{court_name.upper()}')"
        court_dropdown = await page.query_selector("select[name*='court'], select[id*='court'], .court-select")
        if court_dropdown:
            await court_dropdown.select_option(label=court_name.upper())
            print(f"[OCIS:{court_name}] Selected court")
        else:
            # Try clicking on a multi-select or checkbox for this court
            court_checkbox = await page.query_selector(f"label:has-text('{court_name.upper()}')")
            if court_checkbox:
                await court_checkbox.click()
                print(f"[OCIS:{court_name}] Clicked court checkbox")

        # Search for each common foreclosure plaintiff
        for plaintiff in plaintiffs[:5]:  # Limit to avoid too many requests
            await asyncio.sleep(1)
            # Find search input
            search_input = await page.query_selector(
                "input[name*='search'], input[placeholder*='name'], input[id*='search'], input[type='text']"
            )
            if search_input:
                await search_input.fill(plaintiff)
                # Submit
                submit = await page.query_selector("button[type='submit'], input[type='submit'], button:has-text('Search')")
                if submit:
                    await submit.click()
                    await page.wait_for_timeout(3000)
                    # Extract results
                    rows = await page.query_selector_all("table.results tr, .case-row, .result-row")
                    for row in rows[1:]:  # Skip header
                        cells = await row.query_selector_all("td")
                        cell_texts = [(await c.inner_text()).strip() for c in cells]
                        if cell_texts:
                            lead = _parse_ocis_row(cell_texts, court_name)
                            if lead:
                                leads.append(lead)

    except Exception as e:
        print(f"[OCIS:{court_name}] Error: {e}")

    return leads


def _parse_ocis_row(cells: list[str], jurisdiction: str) -> Optional[dict]:
    """Parse an OCIS result row into a lead."""
    if len(cells) < 3:
        return None
    # Typical OCIS columns: Case Number, Filed Date, Case Type, Parties
    case_num   = cells[0] if len(cells) > 0 else ""
    filed_date = cells[1] if len(cells) > 1 else ""
    case_type  = cells[2] if len(cells) > 2 else ""
    parties    = cells[3] if len(cells) > 3 else ""

    # Only keep civil cases likely related to motivated sellers
    if not any(k in (case_type + parties).upper() for k in
               ["FORECLOSURE", "JUDGMENT", "LIEN", "TRUST", "BANK", "PROBATE",
                "ESTATE", "MORTGAGE", "LEVY", "IRS", "TAX"]):
        return None

    doc_type = "Civil Case - Foreclosure" if "FORECLO" in case_type.upper() else "Civil Case"
    if "PROB" in case_type.upper() or "ESTATE" in (parties + case_type).upper():
        doc_type = "Probate"
    elif "JUDG" in case_type.upper():
        doc_type = "Judgment"

    lead = {
        "id":            f"ocis-{case_num.replace(' ', '')}",
        "source":        "OCIS Court Cases",
        "jurisdiction":  jurisdiction,
        "doc_type":      doc_type,
        "recorded_date": _parse_date(filed_date),
        "address":       "",
        "city":          "",
        "state":         "VA",
        "zip":           "",
        "grantor":       parties,
        "grantee":       "",
        "assessed_value": 0,
        "sale_price":    0,
        "sale_ratio":    None,
        "doc_number":    case_num,
        "parcel_id":     "",
        "val_code":      case_type,
        "scraped_at":    datetime.utcnow().isoformat() + "Z",
    }
    lead["score"] = score_lead(lead)
    return lead


# ---------------------------------------------------------------------------
# Enrichment — Richmond City GIS parcel lookup
# ---------------------------------------------------------------------------

def enrich_with_parcel_data(leads: list[dict]) -> list[dict]:
    """
    Enrich leads that have a parcel_id with Richmond City GIS parcel data.
    Adds address, assessed value, owner name where missing.
    """
    rva_leads = [l for l in leads if not l.get("assessed_value") and l.get("parcel_id")
                 and l.get("jurisdiction") == "Richmond City"]
    if not rva_leads:
        return leads

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 Chrome/120", "Accept": "application/json"})

    # Batch lookup (max 100 at a time)
    ids = list({l["parcel_id"] for l in rva_leads})[:100]
    parcel_map = {}

    try:
        ids_escaped = "','".join(ids)
        r = session.get(
            f"{RVA_GIS_BASE}/Parcels/FeatureServer/0/query",
            params={
                "where": f"PIN IN ('{ids_escaped}')",
                "outFields": "PIN,OwnerName,AsrLocationBldgNo,LandValue,DwellingValue,TotalValue,LandUse",
                "f": "json",
            },
            timeout=30,
        )
        data = r.json()
        for feat in data.get("features", []):
            a = feat["attributes"]
            parcel_map[a.get("PIN", "").strip()] = a
    except Exception as e:
        print(f"[Enrich] Parcel lookup error: {e}")

    # Apply enrichment
    for lead in leads:
        pid = lead.get("parcel_id", "").strip()
        if pid in parcel_map:
            p = parcel_map[pid]
            if not lead.get("address"):
                lead["address"] = p.get("AsrLocationBldgNo", "").strip()
            if not lead.get("assessed_value"):
                lead["assessed_value"] = p.get("TotalValue", 0)
            if not lead.get("grantee"):
                lead["grantee"] = p.get("OwnerName", "").strip()
            lead["land_use"] = p.get("LandUse", "").strip()

    return leads


# ---------------------------------------------------------------------------
# Export functions
# ---------------------------------------------------------------------------

def export_json(leads: list[dict]) -> dict:
    """Write leads to all JSON output paths."""
    now = datetime.utcnow().isoformat() + "Z"

    # Build summary stats
    by_jurisdiction = {}
    by_type = {}
    for l in leads:
        j = l.get("jurisdiction", "Unknown")
        t = l.get("doc_type", "Unknown")
        by_jurisdiction[j] = by_jurisdiction.get(j, 0) + 1
        by_type[t] = by_type.get(t, 0) + 1

    output = {
        "metadata": {
            "generated": now,
            "lookback_days": LOOKBACK_DAYS,
            "total_records": len(leads),
            "by_jurisdiction": by_jurisdiction,
            "by_type": by_type,
            "sources": list({l.get("source", "") for l in leads}),
        },
        "records": leads,
    }

    for path in OUTPUT_JSON:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(output, indent=2, default=str))
        print(f"[Export] JSON → {path}")

    return output


def export_csv_ghl(leads: list[dict]) -> None:
    """Export leads in Go High Level CRM format."""
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)

    GHL_FIELDS = [
        ("firstName",      lambda l: (l.get("grantee") or "").split()[0] if l.get("grantee") else ""),
        ("lastName",       lambda l: " ".join((l.get("grantee") or "").split()[1:]) if l.get("grantee") else ""),
        ("email",          lambda l: ""),
        ("phone",          lambda l: ""),
        ("address1",       lambda l: l.get("address", "")),
        ("city",           lambda l: l.get("city", "")),
        ("state",          lambda l: l.get("state", "VA")),
        ("postalCode",     lambda l: l.get("zip", "")),
        ("companyName",    lambda l: l.get("grantor", "")),
        ("tags",           lambda l: l.get("doc_type", "")),
        ("source",         lambda l: l.get("source", "")),
        ("jurisdiction",   lambda l: l.get("jurisdiction", "")),
        ("docType",        lambda l: l.get("doc_type", "")),
        ("recordedDate",   lambda l: l.get("recorded_date", "")),
        ("docNumber",      lambda l: l.get("doc_number", "")),
        ("parcelId",       lambda l: l.get("parcel_id", "")),
        ("assessedValue",  lambda l: l.get("assessed_value", "")),
        ("salePrice",      lambda l: l.get("sale_price", "")),
        ("saleRatio",      lambda l: l.get("sale_ratio", "")),
        ("score",          lambda l: l.get("score", "")),
        ("scrapedAt",      lambda l: l.get("scraped_at", "")),
    ]

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[h for h, _ in GHL_FIELDS])
        writer.writeheader()
        for lead in leads:
            writer.writerow({h: fn(lead) for h, fn in GHL_FIELDS})

    print(f"[Export] GHL CSV → {OUTPUT_CSV} ({len(leads)} rows)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n{'='*60}")
    print(f"  Richmond VA Lead Scraper — {now_str}")
    print(f"  Lookback: {LOOKBACK_DAYS} days")
    print(f"{'='*60}\n")

    all_leads: list[dict] = []

    # ── Source 1: Richmond City GIS (free REST API, no captcha) ──
    print("── Source 1: Richmond City GIS ──")
    gis_leads = scrape_richmond_gis(LOOKBACK_DAYS)
    all_leads.extend(gis_leads)

    surplus_leads = scrape_richmond_surplus()
    all_leads.extend(surplus_leads)

    # ── Playwright sources ──
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
        )
        page = await context.new_page()

        # ── Source 2: ACT DataScout (Richmond City land instruments) ──
        print("\n── Source 2: ACT DataScout (Richmond City) ──")
        try:
            actscout_leads = await scrape_actdatascout(page, LOOKBACK_DAYS)
            all_leads.extend(actscout_leads)
        except Exception as e:
            print(f"[ACT DataScout] Failed: {e}")

        # ── Source 3: OCIS Circuit Court Civil Cases ──
        print("\n── Source 3: OCIS Circuit Court Cases ──")
        try:
            await page.goto("about:blank")
            ocis_leads = await scrape_ocis(page, COURTS, LOOKBACK_DAYS)
            all_leads.extend(ocis_leads)
        except Exception as e:
            print(f"[OCIS] Failed: {e}")

        await context.close()
        await browser.close()

    # ── Enrich ──
    print(f"\n── Enriching {len(all_leads)} leads ──")
    all_leads = enrich_with_parcel_data(all_leads)

    # ── Deduplicate ──
    seen_ids = set()
    unique_leads = []
    for lead in all_leads:
        lid = lead.get("id", "")
        if lid and lid not in seen_ids:
            seen_ids.add(lid)
            unique_leads.append(lead)

    # ── Sort by score desc, then date desc ──
    unique_leads.sort(
        key=lambda l: (-(l.get("score") or 0), l.get("recorded_date") or ""),
        reverse=False,
    )

    # ── Export ──
    print(f"\n── Exporting {len(unique_leads)} unique leads ──")
    export_json(unique_leads)
    export_csv_ghl(unique_leads)

    print(f"\n{'='*60}")
    print(f"  Done. {len(unique_leads)} total leads exported.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(main())
