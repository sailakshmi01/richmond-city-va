"""
Greater Richmond, VA — Motivated Seller Lead Scraper v4
=======================================================
Covers: Richmond City (#760), Henrico (#087), Chesterfield (#041),
        Hanover (#085), Goochland (#075)

Sources (in priority order):
  1. Richmond City GIS ArcGIS REST — property transfers with distress markers
     (free, no captcha, services1.arcgis.com/k3vhq11XkBNeeOfM)
  2. OCIS Playwright — circuit court civil cases (foreclosures, judgments)
     (eapps.courts.state.va.us/ocis — Angular SPA, accept terms → REST API)
  3. ACT DataScout Playwright — Richmond City land instruments
     (actdatascout.com — reCAPTCHA v3 auto-handled by Playwright)

Outputs: data/records.json, dashboard/records.json, data/leads.csv (GHL)
"""

import asyncio
import csv
import json
import os
import re
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from playwright.async_api import async_playwright, Page, BrowserContext, TimeoutError as PlaywrightTimeout

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", 30))
BASE_DIR      = Path(__file__).parent.parent

OUTPUT_JSON = [
    BASE_DIR / "dashboard" / "records.json",
    BASE_DIR / "data"      / "records.json",
]
OUTPUT_CSV = BASE_DIR / "data" / "leads.csv"

# All 5 Greater Richmond jurisdictions
COURTS = [
    {"fips": "760", "ocis_id": "760C", "name": "Richmond City",      "city": "Richmond"},
    {"fips": "087", "ocis_id": "087C", "name": "Henrico County",      "city": "Henrico"},
    {"fips": "041", "ocis_id": "041C", "name": "Chesterfield County", "city": "Chesterfield"},
    {"fips": "085", "ocis_id": "085C", "name": "Hanover County",      "city": "Hanover"},
    {"fips": "075", "ocis_id": "075C", "name": "Goochland County",    "city": "Goochland"},
]

RVA_GIS_BASE    = "https://services1.arcgis.com/k3vhq11XkBNeeOfM/arcgis/rest/services"
ACTDATASCOUT_BASE = "https://www.actdatascout.com/RealProperty/Virginia"
OCIS_BASE       = "https://eapps.courts.state.va.us/ocis"
OCIS_REST_BASE  = "https://eapps.courts.state.va.us/ocis-rest/api/public"

PAGE_TIMEOUT    = 45_000   # ms
API_TIMEOUT     = 30_000   # ms

# ---------------------------------------------------------------------------
# Lead scoring
# ---------------------------------------------------------------------------

SCORE_RULES = {
    "Lis Pendens":               90,
    "Foreclosure / Forced Sale": 85,
    "Foreclosure Civil Suit":    83,
    "Notice of Foreclosure":     82,
    "Tax Deed":                  80,
    "IRS Tax Lien":              78,
    "Federal Tax Lien":          75,
    "Judgment":                  72,
    "Probate":                   68,
    "Mechanic Lien":             65,
    "Medicaid Lien":             62,
    "HOA Lien":                  60,
    "Surplus Property":          55,
    "Special Financing":         50,
    "Civil Case":                45,
}

def score_lead(lead: dict) -> int:
    base = SCORE_RULES.get(lead.get("doc_type"), 40)
    ratio = lead.get("sale_ratio")
    if ratio and ratio < 0.70:
        base = min(100, base + 10)
    elif ratio and ratio < 0.85:
        base = min(100, base + 5)
    return base


# ---------------------------------------------------------------------------
# Source 1: Richmond City GIS — Property Transfers (REST, no auth)
# ---------------------------------------------------------------------------

def scrape_richmond_gis(lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    """
    Richmond City ArcGIS FeatureServer — annual CAMA snapshot (Oct 2021–Oct 2024).
    Filters for distressed sale indicators: Foreclosure, Special Financing.
    """
    leads   = []
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 Chrome/120", "Accept": "application/json"})

    cutoff_date   = "2024-01-01"   # most recent full data year in this snapshot
    DISTRESS_CODES = ["Foreclosure%", "Special Financing%"]

    for code_filter in DISTRESS_CODES:
        try:
            params = {
                "where":            f"val_code_2 LIKE '{code_filter}' AND sale_date >= date '{cutoff_date}'",
                "outFields":        "OBJECTID2,parcel_id,prop_street,owner1,sale_date,sale_price,Land,Impr,tot,val_code_1,val_code_2,DocNum",
                "orderByFields":    "sale_date DESC",
                "resultRecordCount": 500,
                "f":                "json",
            }
            r = session.get(
                f"{RVA_GIS_BASE}/AssessorProValGPINRecTransPublish/FeatureServer/0/query",
                params=params, timeout=30,
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
                    sale_ts    = a.get("sale_date")
                    sale_date  = datetime.fromtimestamp(sale_ts / 1000).strftime("%Y-%m-%d") if sale_ts else None
                    assessed   = a.get("tot", 0) or 0
                    sale_price = a.get("sale_price", 0) or 0
                    ratio      = round(sale_price / assessed, 3) if assessed > 0 else None
                    address    = (a.get("prop_street") or "").strip()
                    vc2        = (a.get("val_code_2") or "").strip()
                    doc_type   = "Foreclosure / Forced Sale" if "foreclo" in vc2.lower() else "Special Financing"

                    lead = {
                        "id":             f"rva-gis-{a.get('OBJECTID2', a.get('parcel_id', ''))}",
                        "source":         "Richmond City GIS",
                        "jurisdiction":   "Richmond City",
                        "doc_type":       doc_type,
                        "recorded_date":  sale_date,
                        "address":        address,
                        "city":           "Richmond",
                        "state":          "VA",
                        "zip":            "",
                        "grantor":        "",
                        "grantee":        (a.get("owner1") or "").strip(),
                        "assessed_value": assessed,
                        "sale_price":     sale_price,
                        "sale_ratio":     ratio,
                        "doc_number":     (a.get("DocNum") or "").strip(),
                        "parcel_id":      (a.get("parcel_id") or "").strip(),
                        "val_code":       vc2,
                        "scraped_at":     datetime.utcnow().isoformat() + "Z",
                    }
                    lead["score"] = score_lead(lead)
                    leads.append(lead)
                except Exception:
                    continue

        except Exception as e:
            print(f"[RVA GIS] Error fetching {code_filter}: {e}")

    # Deduplicate by OBJECTID2-based ID
    seen, unique = set(), []
    for lead in leads:
        if lead["id"] not in seen:
            seen.add(lead["id"])
            unique.append(lead)

    print(f"[RVA GIS] Total unique leads: {len(unique)}")
    return unique


def scrape_richmond_surplus() -> list[dict]:
    """Richmond City surplus/tax-sale properties (free ArcGIS API)."""
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
                "id":             f"rva-surplus-{a.get('ObjectId', '')}",
                "source":         "Richmond City Surplus",
                "jurisdiction":   "Richmond City",
                "doc_type":       "Surplus Property",
                "recorded_date":  None,
                "address":        (a.get("Address") or "").strip(),
                "city":           "Richmond",
                "state":          "VA",
                "zip":            "",
                "grantor":        "City of Richmond",
                "grantee":        "",
                "assessed_value": 0,
                "sale_price":     0,
                "sale_ratio":     None,
                "doc_number":     "",
                "parcel_id":      (a.get("Parcel_ID") or "").strip(),
                "val_code":       "Surplus",
                "scraped_at":     datetime.utcnow().isoformat() + "Z",
            }
            lead["score"] = score_lead(lead)
            leads.append(lead)
    except Exception as e:
        print(f"[RVA Surplus] Error: {e}")
    print(f"[RVA Surplus] {len(leads)} properties")
    return leads


# ---------------------------------------------------------------------------
# Source 2: OCIS Playwright — Circuit Court Civil Cases (all 5 jurisdictions)
# ---------------------------------------------------------------------------

# Plaintiff keywords that indicate motivated-seller situations
OCIS_PLAINTIFF_SEARCHES = [
    # Mortgage servicers / trustees
    "TRUSTEE",
    "PENNYMAC",
    "NEWREZ",
    "FREEDOM MORTGAGE",
    "LOANCARE",
    "CARRINGTON MORTGAGE",
    "SPECIALIZED LOAN",
    "PHH MORTGAGE",
    "MR COOPER",
    "NATIONSTAR",
    "WELLS FARGO BANK",
    "BANK OF AMERICA",
    "US BANK",
    "DEUTSCHE BANK",
    # Federal/tax
    "INTERNAL REVENUE",
    "UNITED STATES",
    # Local government
    "CITY OF RICHMOND",
    "COUNTY OF HENRICO",
    "COUNTY OF CHESTERFIELD",
]

# Case type keywords we care about
CIVIL_KEYWORDS = {
    "FORECL":   "Foreclosure Civil Suit",
    "JUDGMENT":  "Judgment",
    "LIEN":     "Civil Case",
    "PROBATE":  "Probate",
    "ESTATE":   "Probate",
    "IRS":      "IRS Tax Lien",
    "REVENUE":  "IRS Tax Lien",
    "TAX":      "Federal Tax Lien",
    "HOA":      "HOA Lien",
    "MORTGAGE": "Foreclosure Civil Suit",
    "TRUSTEE":  "Foreclosure Civil Suit",
    "BANK":     "Foreclosure Civil Suit",
}


async def scrape_ocis(
    context: BrowserContext, courts: list[dict], lookback_days: int = LOOKBACK_DAYS
) -> list[dict]:
    """
    Navigate Virginia OCIS (Angular SPA), accept terms, then use the
    established session to call the REST API for each court × search term.

    Strategy:
      1. Load landing page → click #acceptTerms button
      2. After Angular completes navigation, inject fetch() calls to the REST API
         (same-origin, session cookie auto-included)
      3. Collect & deduplicate results across all courts and search terms
    """
    all_leads = []
    page = await context.new_page()

    try:
        # ── Step 1: Accept terms ──────────────────────────────────────────────
        landing_url = f"{OCIS_BASE}/landing"
        print(f"[OCIS] Loading landing page…")
        await page.goto(landing_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)

        # Wait for Angular to bootstrap (app-root has children)
        await page.wait_for_function(
            "document.querySelector('app-root') && document.querySelector('app-root').children.length > 0",
            timeout=PAGE_TIMEOUT,
        )
        await page.wait_for_timeout(2000)

        # Click the "Accept" button (id=acceptTerms from JS bundle analysis)
        terms_clicked = False
        for selector in ["#acceptTerms", "button:has-text('Accept')", "button:has-text('I Accept')",
                         ".btn-primary", "[id*='accept']"]:
            try:
                btn = await page.wait_for_selector(selector, timeout=6000, state="visible")
                if btn:
                    await btn.click()
                    print(f"[OCIS] Clicked terms via: {selector}")
                    terms_clicked = True
                    break
            except PlaywrightTimeout:
                continue

        if not terms_clicked:
            # Last resort — find any button with accept/agree text
            buttons = await page.query_selector_all("button")
            for btn in buttons:
                txt = (await btn.inner_text()).strip().lower()
                if any(w in txt for w in ("accept", "agree", "continue", "proceed")):
                    await btn.click()
                    print(f"[OCIS] Clicked terms via text scan: '{txt}'")
                    terms_clicked = True
                    break

        if not terms_clicked:
            print("[OCIS] WARNING: Could not click terms button — taking screenshot for debug")
            await page.screenshot(path="/tmp/ocis_terms.png")

        # Wait for Angular to navigate to search page
        await page.wait_for_timeout(3000)
        await page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT)
        print(f"[OCIS] After terms: {page.url}")

        # Take debug screenshot
        try:
            await page.screenshot(path="/tmp/ocis_after_terms.png")
        except Exception:
            pass

        # ── Step 2: Search each court via REST API from page context ──────────
        from_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

        for court in courts:
            court_leads = await _ocis_search_court_via_api(page, court, from_date)
            all_leads.extend(court_leads)
            await asyncio.sleep(1)   # polite delay between courts

    except PlaywrightTimeout:
        print("[OCIS] Timed out")
        try:
            await page.screenshot(path="/tmp/ocis_timeout.png")
        except Exception:
            pass
    except Exception as e:
        print(f"[OCIS] Error: {e}")
        traceback.print_exc()
    finally:
        await page.close()

    # Deduplicate
    seen, unique = set(), []
    for lead in all_leads:
        if lead["id"] not in seen:
            seen.add(lead["id"])
            unique.append(lead)

    print(f"[OCIS] Total unique leads across all courts: {len(unique)}")
    return unique


async def _ocis_search_court_via_api(
    page: Page, court: dict, from_date: str
) -> list[dict]:
    """
    For a given court, run all plaintiff searches via the OCIS REST API
    using the established browser session (same-origin fetch from page context).
    """
    leads      = []
    court_name = court["name"]
    ocis_id    = court["ocis_id"]
    city       = court.get("city", court_name)

    print(f"[OCIS:{court_name}] Searching…")

    for search_term in OCIS_PLAINTIFF_SEARCHES:
        try:
            # Call OCIS REST API from within the browser page context
            # The session cookie (set by clicking #acceptTerms) is auto-included
            payload = {
                "courtLevels":    ["C"],          # Circuit courts only
                "selectedCourts": [ocis_id],       # e.g. "760C"
                "searchBy":       "Name",
                "searchString":   [search_term],
                "divisions":      ["All"],
            }

            result = await page.evaluate(
                """
                async (args) => {
                    const [url, payload] = args;
                    try {
                        const resp = await fetch(url, {
                            method: "POST",
                            headers: {
                                "Content-Type": "application/json",
                                "Accept": "application/json",
                            },
                            body: JSON.stringify(payload),
                            credentials: "include",
                        });
                        const text = await resp.text();
                        try { return JSON.parse(text); }
                        catch { return { _raw: text }; }
                    } catch (e) {
                        return { _error: e.toString() };
                    }
                }
                """,
                [f"{OCIS_REST_BASE}/search", payload],
            )

            if result.get("_error"):
                print(f"[OCIS:{court_name}] Fetch error for '{search_term}': {result['_error']}")
                continue

            # Parse the OCIS response envelope
            entity = result.get("context", {}).get("entity", result.get("entity", result))
            if isinstance(entity, dict) and entity.get("status") == "FAILURE":
                msgs = [m.get("messageCode", "") for m in entity.get("messages", [])]
                if "terms.notAccepted" in msgs:
                    print(f"[OCIS:{court_name}] Terms not accepted — retrying terms click")
                    # Try to accept terms again if we can
                    for sel in ["#acceptTerms", "button:has-text('Accept')"]:
                        try:
                            btn = await page.query_selector(sel)
                            if btn:
                                await btn.click()
                                await page.wait_for_timeout(3000)
                                break
                        except Exception:
                            pass
                else:
                    print(f"[OCIS:{court_name}] Search failed for '{search_term}': {msgs}")
                continue

            # Extract payload — either at root or inside context.entity
            payload_data = entity.get("payload") if isinstance(entity, dict) else None
            if payload_data is None:
                # Try root-level payload
                payload_data = result.get("payload")
            if payload_data is None:
                # Try unwrapped result directly
                payload_data = result if isinstance(result, list) else None

            cases = _extract_cases(payload_data)
            if cases:
                print(f"[OCIS:{court_name}] '{search_term}': {len(cases)} cases")

            for case in cases:
                lead = _parse_ocis_case(case, court_name, city, from_date)
                if lead:
                    leads.append(lead)

        except Exception as e:
            print(f"[OCIS:{court_name}] Error for '{search_term}': {e}")
            continue

        await asyncio.sleep(0.5)   # brief pause between searches

    print(f"[OCIS:{court_name}] {len(leads)} leads total")
    return leads


def _extract_cases(payload) -> list[dict]:
    """Extract case list from OCIS API payload (handles various response shapes)."""
    if not payload:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        # Common keys: cases, results, caseList, data
        for key in ("cases", "results", "caseList", "data", "caseInfoList"):
            val = payload.get(key)
            if isinstance(val, list):
                return val
        # If the dict itself looks like a single case
        if payload.get("caseNumber") or payload.get("caseNo"):
            return [payload]
    return []


def _parse_ocis_case(case: dict, jurisdiction: str, city: str, from_date: str) -> Optional[dict]:
    """Map an OCIS case record to a lead dict."""
    if not isinstance(case, dict):
        return None

    # Case number (various field names)
    case_num  = (
        case.get("caseNumber") or case.get("caseNo") or
        case.get("caseId")     or case.get("id") or ""
    ).strip()

    # Filed date
    filed_raw = (
        case.get("filedDate") or case.get("fileDate") or
        case.get("commencedDate") or case.get("caseFiledDate") or ""
    )
    filed_date = _parse_date(str(filed_raw)) if filed_raw else None

    # Filter by date window
    if filed_date and filed_date < from_date:
        return None

    # Case type
    case_type = (
        case.get("caseType") or case.get("type") or
        case.get("caseTypeName") or ""
    ).strip()

    # Parties
    plaintiff  = _get_party(case, "plaintiff")
    defendant  = _get_party(case, "defendant")
    all_text   = f"{case_type} {plaintiff} {defendant}".upper()

    # Classify
    doc_type = _classify_ocis_case(all_text, case_type, plaintiff)
    if not doc_type:
        return None   # not relevant to motivated sellers

    if not case_num:
        return None

    lead = {
        "id":             f"ocis-{case_num.replace(' ', '').replace('-', '')}",
        "source":         "OCIS Court Cases",
        "jurisdiction":   jurisdiction,
        "doc_type":       doc_type,
        "recorded_date":  filed_date,
        "address":        _get_property_address(case),
        "city":           city,
        "state":          "VA",
        "zip":            "",
        "grantor":        plaintiff,
        "grantee":        defendant,
        "assessed_value": 0,
        "sale_price":     _parse_amount(case),
        "sale_ratio":     None,
        "doc_number":     case_num,
        "parcel_id":      (case.get("parcelId") or case.get("gpin") or "").strip(),
        "val_code":       case_type,
        "case_status":    (case.get("caseStatus") or case.get("status") or "").strip(),
        "scraped_at":     datetime.utcnow().isoformat() + "Z",
    }
    lead["score"] = score_lead(lead)
    return lead


def _get_party(case: dict, role: str) -> str:
    """Extract plaintiff or defendant name from case record."""
    # Try direct fields
    if role == "plaintiff":
        val = case.get("plaintiff") or case.get("plaintiffName") or case.get("complainant") or ""
        if val:
            return str(val).strip()
    else:
        val = case.get("defendant") or case.get("defendantName") or case.get("respondent") or ""
        if val:
            return str(val).strip()

    # Try party list
    parties = case.get("parties") or case.get("partyList") or []
    if isinstance(parties, list):
        role_upper = role.upper()
        for p in parties:
            if isinstance(p, dict):
                p_role = (p.get("partyType") or p.get("role") or "").upper()
                if role_upper in p_role:
                    return (p.get("name") or p.get("partyName") or "").strip()
        # Fallback: first/second party
        if parties:
            idx = 0 if role == "plaintiff" else 1
            p = parties[idx] if idx < len(parties) else parties[0]
            if isinstance(p, dict):
                return (p.get("name") or p.get("partyName") or "").strip()

    return ""


def _classify_ocis_case(text: str, case_type: str, plaintiff: str) -> Optional[str]:
    """Return canonical doc_type if case is relevant, else None."""
    for keyword, doc_type in CIVIL_KEYWORDS.items():
        if keyword in text:
            return doc_type
    # Circuit court civil case with no specific keyword — skip
    return None


def _get_property_address(case: dict) -> str:
    """Try to extract property address from case record."""
    for field in ("propertyAddress", "address", "propertyLocation", "siteAddress"):
        val = case.get(field)
        if val and isinstance(val, str) and len(val.strip()) > 3:
            return val.strip()
    return ""


def _parse_amount(case: dict) -> int:
    """Extract monetary amount from case."""
    for field in ("amount", "judgment", "claimAmount", "totalAmount", "amountInControversy"):
        val = case.get(field)
        if val:
            try:
                return int(re.sub(r"[^\d]", "", str(val)))
            except Exception:
                pass
    return 0


def _parse_date(s: str) -> Optional[str]:
    """Parse various date formats → YYYY-MM-DD."""
    s = (s or "").strip()
    # Handle epoch milliseconds
    if s.isdigit() and len(s) > 8:
        try:
            return datetime.fromtimestamp(int(s) / 1000).strftime("%Y-%m-%d")
        except Exception:
            pass
    for fmt in ["%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%Y%m%d"]:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s or None


def _map_doc_type(raw: str) -> str:
    raw_upper = raw.upper()
    mapping = {
        "LP": "Lis Pendens",      "LIS PENDENS": "Lis Pendens",
        "DT": "Deed of Trust",    "DEED OF TRUST": "Deed of Trust",
        "ML": "Mechanic Lien",    "MECHANIC LIEN": "Mechanic Lien",
        "HOA": "HOA Lien",
        "FL": "Federal Tax Lien", "FEDERAL": "Federal Tax Lien",
        "IRS": "IRS Tax Lien",
        "TD": "Tax Deed",         "TAX DEED": "Tax Deed",
        "JUD": "Judgment",        "JUDGMENT": "Judgment",
        "PROB": "Probate",        "PROBATE": "Probate",
        "MED": "Medicaid Lien",   "MEDICAID": "Medicaid Lien",
    }
    for key, val in mapping.items():
        if key in raw_upper:
            return val
    return raw.strip() or "Unknown"


# ---------------------------------------------------------------------------
# Source 3: ACT DataScout Playwright — Richmond City Land Instruments
# ---------------------------------------------------------------------------

async def scrape_actdatascout(
    page: Page, lookback_days: int = LOOKBACK_DAYS
) -> list[dict]:
    """
    Navigate ACT DataScout for Richmond City land records (Lis Pendens, Liens, etc.).
    reCAPTCHA v3 is invisible — Playwright typically passes automatically.
    """
    leads    = []
    url      = f"{ACTDATASCOUT_BASE}/Richmond"
    from_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%m/%d/%Y")
    to_date   = datetime.now().strftime("%m/%d/%Y")

    try:
        print(f"[ACT DataScout] Navigating to {url}")
        await page.goto(url, wait_until="networkidle", timeout=PAGE_TIMEOUT)
        await page.wait_for_timeout(2000)

        page_url = page.url
        title    = await page.title()
        print(f"[ACT DataScout] URL={page_url}, Title={title[:60]}")

        if "actdatascout.com/RealProperty" not in page_url:
            print("[ACT DataScout] Redirected — not available for this jurisdiction")
            return leads

        await page.screenshot(path="/tmp/actdatascout.png")

        # Date range fields (try multiple selector patterns)
        for sel in ["#FromDate", "#StartDate", "[name='FromDate']", "[name='StartDate']",
                    "input[placeholder*='from' i]", "input[placeholder*='start' i]"]:
            if await page.query_selector(sel):
                await page.fill(sel, from_date)
                print(f"[ACT DataScout] Filled from_date ({from_date}) via {sel}")
                break

        for sel in ["#ToDate", "#EndDate", "[name='ToDate']", "[name='EndDate']",
                    "input[placeholder*='to' i]", "input[placeholder*='end' i]"]:
            if await page.query_selector(sel):
                await page.fill(sel, to_date)
                print(f"[ACT DataScout] Filled to_date ({to_date}) via {sel}")
                break

        # Submit
        for sel in ["#btnSearch", "input[type='submit']", "button[type='submit']",
                    "button:has-text('Search')"]:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                print(f"[ACT DataScout] Clicked search via {sel}")
                await page.wait_for_load_state("networkidle", timeout=20000)
                break

        await page.wait_for_timeout(2000)
        await page.screenshot(path="/tmp/actdatascout_results.png")

        # Extract results table
        rows = await page.query_selector_all("table tr")
        print(f"[ACT DataScout] Found {len(rows)} rows")
        headers = []
        for i, row in enumerate(rows):
            cells = await row.query_selector_all("td, th")
            cell_texts = [(await c.inner_text()).strip() for c in cells]
            if i == 0:
                headers = cell_texts
                continue
            if not any(cell_texts):
                continue
            rec  = dict(zip(headers, cell_texts)) if headers else {}
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
    address    = rec.get("Property Address") or rec.get("Address") or rec.get("Situs") or ""
    grantor    = rec.get("Grantor") or rec.get("Seller") or ""
    grantee    = rec.get("Grantee") or rec.get("Buyer") or ""
    doc_type   = rec.get("Instrument Type") or rec.get("Doc Type") or rec.get("Type") or ""
    rec_date   = rec.get("Recording Date") or rec.get("Recorded") or rec.get("Date") or ""
    doc_num    = rec.get("Instrument Number") or rec.get("Doc #") or rec.get("Book/Page") or ""
    amount_str = rec.get("Consideration") or rec.get("Amount") or rec.get("Price") or "0"

    if not address and not grantor:
        return None

    amount = 0
    try:
        amount = int(re.sub(r"[^\d]", "", amount_str))
    except Exception:
        pass

    doc_type_canonical = _map_doc_type(doc_type)
    lead = {
        "id":             f"actscout-{doc_num.replace('/', '-').replace(' ', '')}",
        "source":         "ACT DataScout",
        "jurisdiction":   "Richmond City",
        "doc_type":       doc_type_canonical,
        "recorded_date":  _parse_date(rec_date),
        "address":        address.strip(),
        "city":           "Richmond",
        "state":          "VA",
        "zip":            "",
        "grantor":        grantor.strip(),
        "grantee":        grantee.strip(),
        "assessed_value": 0,
        "sale_price":     amount,
        "sale_ratio":     None,
        "doc_number":     doc_num.strip(),
        "parcel_id":      "",
        "val_code":       doc_type,
        "scraped_at":     datetime.utcnow().isoformat() + "Z",
    }
    lead["score"] = score_lead(lead)
    return lead


# ---------------------------------------------------------------------------
# Enrichment — Richmond City GIS parcel lookup
# ---------------------------------------------------------------------------

def enrich_with_parcel_data(leads: list[dict]) -> list[dict]:
    """Enrich leads with address and AV from Richmond City GIS parcel data."""
    rva_leads = [
        l for l in leads
        if not l.get("assessed_value") and l.get("parcel_id")
        and l.get("jurisdiction") == "Richmond City"
    ]
    if not rva_leads:
        return leads

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 Chrome/120", "Accept": "application/json"})

    ids = list({l["parcel_id"] for l in rva_leads})[:100]
    parcel_map = {}
    try:
        ids_escaped = "','".join(ids)
        r = session.get(
            f"{RVA_GIS_BASE}/Parcels/FeatureServer/0/query",
            params={
                "where":     f"PIN IN ('{ids_escaped}')",
                "outFields": "PIN,OwnerName,AsrLocationBldgNo,TotalValue",
                "f":         "json",
            },
            timeout=30,
        )
        for feat in r.json().get("features", []):
            a = feat["attributes"]
            parcel_map[a.get("PIN", "").strip()] = a
    except Exception as e:
        print(f"[Enrich] Parcel lookup error: {e}")

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

    return leads


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_json(leads: list[dict]) -> dict:
    """Write leads to all JSON output paths and return the output dict."""
    now = datetime.utcnow().isoformat() + "Z"
    by_jurisdiction, by_type = {}, {}
    for l in leads:
        j = l.get("jurisdiction", "Unknown")
        t = l.get("doc_type", "Unknown")
        by_jurisdiction[j] = by_jurisdiction.get(j, 0) + 1
        by_type[t]         = by_type.get(t, 0) + 1

    output = {
        "metadata": {
            "generated":       now,
            "lookback_days":   LOOKBACK_DAYS,
            "total_records":   len(leads),
            "by_jurisdiction": by_jurisdiction,
            "by_type":         by_type,
            "sources":         sorted({l.get("source", "") for l in leads}),
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
        ("firstName",    lambda l: (l.get("grantee") or "").split()[0] if l.get("grantee") else ""),
        ("lastName",     lambda l: " ".join((l.get("grantee") or "").split()[1:]) if l.get("grantee") else ""),
        ("email",        lambda _: ""),
        ("phone",        lambda _: ""),
        ("address1",     lambda l: l.get("address", "")),
        ("city",         lambda l: l.get("city", "")),
        ("state",        lambda l: l.get("state", "VA")),
        ("postalCode",   lambda l: l.get("zip", "")),
        ("companyName",  lambda l: l.get("grantor", "")),
        ("tags",         lambda l: l.get("doc_type", "")),
        ("source",       lambda l: l.get("source", "")),
        ("jurisdiction", lambda l: l.get("jurisdiction", "")),
        ("docType",      lambda l: l.get("doc_type", "")),
        ("recordedDate", lambda l: l.get("recorded_date", "")),
        ("docNumber",    lambda l: l.get("doc_number", "")),
        ("parcelId",     lambda l: l.get("parcel_id", "")),
        ("assessedValue",lambda l: l.get("assessed_value", "")),
        ("salePrice",    lambda l: l.get("sale_price", "")),
        ("saleRatio",    lambda l: l.get("sale_ratio", "")),
        ("caseStatus",   lambda l: l.get("case_status", "")),
        ("score",        lambda l: l.get("score", "")),
        ("scrapedAt",    lambda l: l.get("scraped_at", "")),
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
    print(f"  Greater Richmond VA Lead Scraper v4 — {now_str}")
    print(f"  Lookback: {LOOKBACK_DAYS} days | Courts: {len(COURTS)}")
    print(f"{'='*60}\n")

    all_leads: list[dict] = []

    # ── Source 1: Richmond City GIS (free REST, no auth) ─────────────────────
    print("── Source 1: Richmond City GIS ──")
    all_leads.extend(scrape_richmond_gis(LOOKBACK_DAYS))
    all_leads.extend(scrape_richmond_surplus())

    # ── Playwright sources ────────────────────────────────────────────────────
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-web-security",      # allow same-origin API calls
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
            ignore_https_errors=True,
        )

        # ── Source 2: OCIS Circuit Court Civil Cases (all 5 courts) ──────────
        print("\n── Source 2: OCIS Circuit Court Cases (all 5 jurisdictions) ──")
        try:
            ocis_leads = await scrape_ocis(context, COURTS, LOOKBACK_DAYS)
            all_leads.extend(ocis_leads)
        except Exception as e:
            print(f"[OCIS] Failed: {e}")
            traceback.print_exc()

        # ── Source 3: ACT DataScout (Richmond City land instruments) ──────────
        print("\n── Source 3: ACT DataScout (Richmond City) ──")
        try:
            act_page = await context.new_page()
            actscout_leads = await scrape_actdatascout(act_page, LOOKBACK_DAYS)
            all_leads.extend(actscout_leads)
            await act_page.close()
        except Exception as e:
            print(f"[ACT DataScout] Failed: {e}")

        await context.close()
        await browser.close()

    # ── Enrich with parcel data ───────────────────────────────────────────────
    print(f"\n── Enriching {len(all_leads)} leads ──")
    all_leads = enrich_with_parcel_data(all_leads)

    # ── Deduplicate ───────────────────────────────────────────────────────────
    seen_ids, unique_leads = set(), []
    for lead in all_leads:
        lid = lead.get("id", "")
        if lid and lid not in seen_ids:
            seen_ids.add(lid)
            unique_leads.append(lead)

    # ── Sort: score DESC, then date DESC ─────────────────────────────────────
    unique_leads.sort(key=lambda l: (-(l.get("score") or 0), l.get("recorded_date") or ""))

    # ── Export ────────────────────────────────────────────────────────────────
    print(f"\n── Exporting {len(unique_leads)} unique leads ──")
    export_json(unique_leads)
    export_csv_ghl(unique_leads)

    print(f"\n{'='*60}")
    print(f"  Done. {len(unique_leads)} total unique leads exported.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(main())
