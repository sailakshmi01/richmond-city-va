"""
Greater Richmond, VA — Motivated Seller Lead Scraper v5
=======================================================
Covers: Richmond City (#760), Henrico (#087), Chesterfield (#041),
        Hanover (#085), Goochland (#075)

Working Sources (free, no auth, pure REST):
  1. Richmond City GIS (ArcGIS REST)
     • Foreclosure / Forced Sales  — AssessorProValGPINRecTransPublish
     • Special Financing           — AssessorProValGPINRecTransPublish
     • Surplus Properties          — 2020_Surplus_Properties___New2
  2. Richmond City Open Data (Socrata)
     • Delinquent Real Estate Taxes (6 months+) — dataset 83t5-hbac
  3. Henrico County GIS (ArcGIS FeatureServer)
     • CAMA Data: recent low-price sales filtered for distress

Outputs: data/records.json, dashboard/records.json, data/leads.csv
"""

import csv
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

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

RVA_GIS_BASE  = "https://services1.arcgis.com/k3vhq11XkBNeeOfM/arcgis/rest/services"
SOCRATA_BASE  = "https://data.richmondgov.com/resource"
HENRICO_CAMA  = ("https://portal.henrico.gov/mapping/rest/services/"
                 "Layers/Tax_Parcels_and_CAMA_Data_External/FeatureServer/0")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
})

# ---------------------------------------------------------------------------
# Lead scoring
# ---------------------------------------------------------------------------

SCORE_RULES = {
    "Lis Pendens":               90,
    "Foreclosure / Forced Sale": 85,
    "Tax Deed":                  80,
    "IRS Tax Lien":              78,
    "Judgment":                  72,
    "Delinquent Tax (6+ yrs)":   88,
    "Delinquent Tax (3-5 yrs)":  82,
    "Delinquent Tax (1-2 yrs)":  72,
    "Probate":                   68,
    "Mechanic Lien":             65,
    "Medicaid Lien":             62,
    "HOA Lien":                  60,
    "Surplus Property":          55,
    "Special Financing":         50,
    "Henrico Distressed Sale":   65,
}

def score_lead(lead: dict) -> int:
    base = SCORE_RULES.get(lead.get("doc_type"), 40)
    ratio = lead.get("sale_ratio")
    if ratio and ratio < 0.60:
        base = min(100, base + 12)
    elif ratio and ratio < 0.70:
        base = min(100, base + 8)
    elif ratio and ratio < 0.85:
        base = min(100, base + 4)
    return base


# ---------------------------------------------------------------------------
# Source 1: Richmond City GIS — Property Transfers (ArcGIS REST)
# ---------------------------------------------------------------------------

def scrape_richmond_gis(lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    """
    Richmond City ArcGIS — CAMA snapshot with Foreclosure & Special Financing codes.
    Date window: 2024-01-01 → now (data is annual snapshot, not live).
    """
    leads        = []
    cutoff_date  = "2024-01-01"
    DISTRESS_CODES = ["Foreclosure%", "Special Financing%"]

    for code_filter in DISTRESS_CODES:
        try:
            params = {
                "where":             f"val_code_2 LIKE '{code_filter}' AND sale_date >= date '{cutoff_date}'",
                "outFields":         "OBJECTID2,parcel_id,prop_street,owner1,sale_date,sale_price,Land,Impr,tot,val_code_1,val_code_2,DocNum",
                "orderByFields":     "sale_date DESC",
                "resultRecordCount": 500,
                "f":                 "json",
            }
            r = SESSION.get(
                f"{RVA_GIS_BASE}/AssessorProValGPINRecTransPublish/FeatureServer/0/query",
                params=params, timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                print(f"[RVA GIS] API error for {code_filter}: {data['error']}")
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

    print(f"[RVA GIS] Total unique property transfer leads: {len(unique)}")
    return unique


def scrape_richmond_surplus() -> list[dict]:
    """Richmond City surplus / tax-sale properties (free ArcGIS API)."""
    leads = []
    try:
        r = SESSION.get(
            f"{RVA_GIS_BASE}/2020_Surplus_Properties___New2/FeatureServer/0/query",
            params={"where": "1=1", "outFields": "*", "resultRecordCount": 200, "f": "json"},
            timeout=20,
        )
        data = r.json()
        if "error" in data:
            print(f"[RVA Surplus] API error: {data['error']}")
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
    print(f"[RVA Surplus] {len(leads)} surplus properties")
    return leads


# ---------------------------------------------------------------------------
# Source 2: Richmond City Open Data — Delinquent Real Estate Taxes
# ---------------------------------------------------------------------------

def scrape_richmond_delinquent() -> list[dict]:
    """
    Richmond City Socrata dataset 83t5-hbac — properties delinquent 6+ months.
    Fields: property_code, current_owner_name_1, physical_address,
            total_due, total_years_del, gis_location (coordinates)
    """
    leads = []
    try:
        # Fetch all records — paginate in blocks of 1000
        offset = 0
        PAGE   = 1000
        all_records = []
        while True:
            r = SESSION.get(
                f"{SOCRATA_BASE}/83t5-hbac.json",
                params={
                    "$limit":  PAGE,
                    "$offset": offset,
                    "$order":  "total_years_del DESC",
                },
                timeout=30,
            )
            r.raise_for_status()
            page_records = r.json()
            if not isinstance(page_records, list) or not page_records:
                break
            all_records.extend(page_records)
            if len(page_records) < PAGE:
                break
            offset += PAGE

        print(f"[RVA Delinquent] {len(all_records)} delinquent tax records fetched")

        for rec in all_records:
            try:
                prop_code = (rec.get("property_code") or "").strip()
                address   = (rec.get("physical_address") or "").strip()
                owner     = (rec.get("current_owner_name_1") or "").strip()
                total_due = float(rec.get("total_due", 0) or 0)
                years_del = int(float(rec.get("total_years_del", 0) or 0))

                # Skip if no address or very small amount
                if not address or address == "0" or total_due < 100:
                    continue

                # Classify by years delinquent
                if years_del >= 6:
                    doc_type = "Delinquent Tax (6+ yrs)"
                elif years_del >= 3:
                    doc_type = "Delinquent Tax (3-5 yrs)"
                else:
                    doc_type = "Delinquent Tax (1-2 yrs)"

                # Extract coordinates if available
                geo = rec.get("gis_location") or {}
                lat = lon = None
                if isinstance(geo, dict) and geo.get("type") == "Point":
                    coords = geo.get("coordinates", [])
                    if len(coords) == 2:
                        lon, lat = coords[0], coords[1]

                lead = {
                    "id":             f"rva-del-{prop_code}",
                    "source":         "Richmond Delinquent Tax",
                    "jurisdiction":   "Richmond City",
                    "doc_type":       doc_type,
                    "recorded_date":  None,
                    "address":        address,
                    "city":           "Richmond",
                    "state":          "VA",
                    "zip":            "",
                    "grantor":        "",
                    "grantee":        owner,
                    "assessed_value": 0,
                    "sale_price":     0,
                    "sale_ratio":     None,
                    "doc_number":     prop_code,
                    "parcel_id":      prop_code,
                    "val_code":       f"Delinquent {years_del} yr(s)",
                    "total_due":      total_due,
                    "years_delinquent": years_del,
                    "lat":            lat,
                    "lon":            lon,
                    "scraped_at":     datetime.utcnow().isoformat() + "Z",
                }
                lead["score"] = score_lead(lead)
                leads.append(lead)
            except Exception:
                continue

    except Exception as e:
        print(f"[RVA Delinquent] Error: {e}")

    print(f"[RVA Delinquent] {len(leads)} delinquent tax leads (after address filter)")
    return leads


# ---------------------------------------------------------------------------
# Source 3: Henrico County CAMA — Distressed / Low-Price Recent Sales
# ---------------------------------------------------------------------------

def scrape_henrico_cama(lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    """
    Henrico County Tax_Parcels_and_CAMA_Data_External (public ArcGIS FeatureServer).
    Strategy: fetch recent sales, filter in Python for low price:value ratio.
    Low ratio (< 0.70) = likely foreclosure, estate sale, or distressed transaction.
    """
    leads     = []
    # Use 3× lookback to catch more distressed events; min 365 days
    window_days = max(lookback_days * 3, 365)
    cutoff_ms   = int((datetime.now() - timedelta(days=window_days)).timestamp() * 1000)

    FIELDS = (
        "OBJECTID,PID,GPIN,FULL_ADDRESS,CITY,ZIP_CODE,"
        "LAST_SALE_DATE,LAST_SALE_PRICE,"
        "LAND_VALUE_CURRENT,IMPROVEMENTS_VALUE_CURRENT,"
        "USE_DESCRIPTION,USE_CODE,DEED_BOOK,DEED_PAGE"
    )

    try:
        # Try multiple query approaches — Henrico's ArcGIS is restrictive
        all_feats = []
        # ISO date approach works for ArcGIS FeatureServer date fields
        cutoff_iso = (datetime.now() - timedelta(days=window_days)).strftime("%Y-%m-%d")

        # Attempt simple date-only filter first, then LAST_SALE_PRICE > 0
        for where_clause in [
            f"LAST_SALE_DATE > date '{cutoff_iso}'",
            f"LAST_SALE_DATE > {cutoff_ms}",
            "LAST_SALE_PRICE > 0 AND LAST_SALE_PRICE < 500000",
        ]:
            r = SESSION.get(
                f"{HENRICO_CAMA}/query",
                params={
                    "where":             where_clause,
                    "outFields":         FIELDS,
                    "orderByFields":     "LAST_SALE_DATE DESC",
                    "resultRecordCount": 1,
                    "f":                 "json",
                },
                timeout=20,
            )
            probe = r.json()
            if "error" not in probe:
                print(f"[Henrico CAMA] Working WHERE clause: {where_clause}")
                # Paginate with the working clause
                offset = 0
                PAGE   = 1000
                while True:
                    r2 = SESSION.get(
                        f"{HENRICO_CAMA}/query",
                        params={
                            "where":             where_clause,
                            "outFields":         FIELDS,
                            "orderByFields":     "LAST_SALE_DATE DESC",
                            "resultOffset":      offset,
                            "resultRecordCount": PAGE,
                            "f":                 "json",
                        },
                        timeout=30,
                    )
                    data2 = r2.json()
                    if "error" in data2:
                        print(f"[Henrico CAMA] Pagination error: {data2['error']}")
                        break
                    feats2 = data2.get("features", [])
                    all_feats.extend(feats2)
                    if len(feats2) < PAGE:
                        break
                    offset += PAGE
                break
            else:
                print(f"[Henrico CAMA] WHERE failed ({where_clause}): {probe.get('error',{}).get('message','?')}")

        print(f"[Henrico CAMA] {len(all_feats)} recent sales fetched (window: {window_days} days)")

        for feat in all_feats:
            a = feat.get("attributes", {})
            try:
                land_val  = a.get("LAND_VALUE_CURRENT", 0) or 0
                impr_val  = a.get("IMPROVEMENTS_VALUE_CURRENT", 0) or 0
                total_val = land_val + impr_val
                sale_price = a.get("LAST_SALE_PRICE", 0) or 0

                if total_val <= 0 or sale_price <= 0:
                    continue

                ratio = round(sale_price / total_val, 3)
                if ratio >= 0.75:
                    continue   # not distressed enough

                sale_ts   = a.get("LAST_SALE_DATE")
                sale_date = datetime.fromtimestamp(sale_ts / 1000).strftime("%Y-%m-%d") if sale_ts else None

                gpin    = (a.get("GPIN") or a.get("PID") or "").strip()
                address = (a.get("FULL_ADDRESS") or "").strip()
                city    = (a.get("CITY") or "Henrico").strip()
                zip_code = (a.get("ZIP_CODE") or "").strip()
                deed_ref = f"{a.get('DEED_BOOK','')}/{a.get('DEED_PAGE','')}"

                lead = {
                    "id":             f"henrico-cama-{a.get('OBJECTID', gpin)}",
                    "source":         "Henrico CAMA",
                    "jurisdiction":   "Henrico County",
                    "doc_type":       "Henrico Distressed Sale",
                    "recorded_date":  sale_date,
                    "address":        address,
                    "city":           city.replace(" VA", "").strip(),
                    "state":          "VA",
                    "zip":            zip_code,
                    "grantor":        "",
                    "grantee":        "",
                    "assessed_value": total_val,
                    "sale_price":     sale_price,
                    "sale_ratio":     ratio,
                    "doc_number":     deed_ref,
                    "parcel_id":      gpin,
                    "val_code":       (a.get("USE_DESCRIPTION") or "").strip(),
                    "scraped_at":     datetime.utcnow().isoformat() + "Z",
                }
                lead["score"] = score_lead(lead)
                leads.append(lead)
            except Exception:
                continue

    except Exception as e:
        print(f"[Henrico CAMA] Error: {e}")

    # Deduplicate
    seen, unique = set(), []
    for lead in leads:
        if lead["id"] not in seen:
            seen.add(lead["id"])
            unique.append(lead)

    print(f"[Henrico CAMA] {len(unique)} distressed sale leads (ratio < 0.75)")
    return unique


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_json(leads: list[dict]) -> dict:
    """Write leads to all JSON output paths."""
    now           = datetime.utcnow().isoformat() + "Z"
    by_jurisdiction: dict[str, int] = {}
    by_type:        dict[str, int] = {}
    for l in leads:
        j = l.get("jurisdiction", "Unknown")
        t = l.get("doc_type",     "Unknown")
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
        ("firstName",     lambda l: (l.get("grantee") or "").split()[0] if l.get("grantee") else ""),
        ("lastName",      lambda l: " ".join((l.get("grantee") or "").split()[1:]) if l.get("grantee") else ""),
        ("email",         lambda _: ""),
        ("phone",         lambda _: ""),
        ("address1",      lambda l: l.get("address", "")),
        ("city",          lambda l: l.get("city", "")),
        ("state",         lambda l: l.get("state", "VA")),
        ("postalCode",    lambda l: l.get("zip", "")),
        ("companyName",   lambda l: l.get("grantor", "")),
        ("tags",          lambda l: l.get("doc_type", "")),
        ("source",        lambda l: l.get("source", "")),
        ("jurisdiction",  lambda l: l.get("jurisdiction", "")),
        ("docType",       lambda l: l.get("doc_type", "")),
        ("recordedDate",  lambda l: l.get("recorded_date", "")),
        ("docNumber",     lambda l: l.get("doc_number", "")),
        ("parcelId",      lambda l: l.get("parcel_id", "")),
        ("assessedValue", lambda l: l.get("assessed_value", "")),
        ("salePrice",     lambda l: l.get("sale_price", "")),
        ("saleRatio",     lambda l: l.get("sale_ratio", "")),
        ("totalDue",      lambda l: l.get("total_due", "")),
        ("yearsDelinquent",lambda l: l.get("years_delinquent", "")),
        ("score",         lambda l: l.get("score", "")),
        ("scrapedAt",     lambda l: l.get("scraped_at", "")),
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

def main() -> None:
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n{'='*60}")
    print(f"  Greater Richmond VA Lead Scraper v5 — {now_str}")
    print(f"  Lookback: {LOOKBACK_DAYS} days")
    print(f"{'='*60}\n")

    all_leads: list[dict] = []

    # ── Source 1: Richmond City GIS ──────────────────────────────────────────
    print("── Source 1: Richmond City GIS (Foreclosure + Special Financing + Surplus) ──")
    all_leads.extend(scrape_richmond_gis(LOOKBACK_DAYS))
    all_leads.extend(scrape_richmond_surplus())

    # ── Source 2: Richmond Delinquent Taxes (Socrata) ─────────────────────────
    print("\n── Source 2: Richmond Delinquent Real Estate Taxes ──")
    all_leads.extend(scrape_richmond_delinquent())

    # ── Source 3: Henrico CAMA Distressed Sales ───────────────────────────────
    print("\n── Source 3: Henrico County CAMA Distressed Sales ──")
    all_leads.extend(scrape_henrico_cama(LOOKBACK_DAYS))

    # ── Deduplicate ───────────────────────────────────────────────────────────
    seen_ids, unique_leads = set(), []
    for lead in all_leads:
        lid = lead.get("id", "")
        if lid and lid not in seen_ids:
            seen_ids.add(lid)
            unique_leads.append(lead)

    # ── Sort: score DESC, then date DESC ─────────────────────────────────────
    unique_leads.sort(
        key=lambda l: (-(l.get("score") or 0), l.get("recorded_date") or "")
    )

    # ── Export ────────────────────────────────────────────────────────────────
    print(f"\n── Exporting {len(unique_leads)} unique leads ──")
    export_json(unique_leads)
    export_csv_ghl(unique_leads)

    # ── Summary ───────────────────────────────────────────────────────────────
    by_src: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for l in unique_leads:
        s = l.get("source", "Unknown")
        t = l.get("doc_type", "Unknown")
        by_src[s]  = by_src.get(s, 0) + 1
        by_type[t] = by_type.get(t, 0) + 1

    print(f"\n{'='*60}")
    print(f"  Done. {len(unique_leads)} total unique leads.")
    print(f"  By Source:")
    for src, cnt in sorted(by_src.items()):
        print(f"    {src}: {cnt}")
    print(f"  By Type:")
    for typ, cnt in sorted(by_type.items(), key=lambda x: -x[1]):
        print(f"    {typ}: {cnt}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
