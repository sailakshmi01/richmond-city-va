"""
Data source discovery — Richmond Metro motivated-seller leads (all 5 jurisdictions)
Checks every free/public source that may yield Lis Pendens, Foreclosure,
Tax Deed, Judgment, Lien, or Probate data.

Run in GitHub Actions: python scraper/test_sources.py
"""

import json
import sys
import urllib.request
import urllib.parse
from datetime import datetime

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/json,*/*",
}

def fetch(url, method="GET", data=None, extra_headers=None, timeout=12):
    h = {**HEADERS, **(extra_headers or {})}
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            try:
                return r.status, json.loads(body), None
            except Exception:
                return r.status, None, body.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:400]
        return e.code, None, body
    except Exception as e:
        return 0, None, str(e)

def sep(title):
    print(f"\n{'='*64}")
    print(f"  {title}")
    print(f"{'='*64}")

print(f"\nData Source Discovery — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

# ─── 0. Richmond City GIS sanity check (known-good baseline) ─────────────────
sep("0. Richmond City GIS — baseline check")

RVA = "https://services1.arcgis.com/k3vhq11XkBNeeOfM/arcgis/rest/services"
for label, filter_code in [("Foreclosure",      "Foreclosure%"),
                            ("Special Financing","Special Financing%")]:
    q = urllib.parse.urlencode({
        "where":           f"val_code_2 LIKE '{filter_code}' AND sale_date >= date '2024-01-01'",
        "outFields":       "OBJECTID2",
        "returnCountOnly": "true",
        "f":               "json",
    })
    st, js, raw = fetch(f"{RVA}/AssessorProValGPINRecTransPublish/FeatureServer/0/query?{q}")
    cnt = js.get("count","?") if js else f"err({st})"
    print(f"  Richmond {label}: {cnt} records")

st, js, _ = fetch(f"{RVA}/2020_Surplus_Properties___New2/FeatureServer/0/query?"
                  "where=1%3D1&returnCountOnly=true&f=json")
cnt = js.get("count","?") if js else f"err({st})"
print(f"  Richmond Surplus: {cnt} properties")

# ─── 1. Sheriff property sale listings ───────────────────────────────────────
sep("1. Sheriff Property Sale Pages")

SHERIFF = [
    ("Richmond City Sheriff",  "https://sheriffofrichmond.com/civil/sales.asp"),
    ("Henrico Sheriff",        "https://henrico.gov/sheriff/civil-process/property-sales/"),
    ("Chesterfield Sheriff",   "https://www.chesterfield.gov/1370/Real-Estate-Sales"),
    ("Hanover Sheriff",        "https://www.hanovercounty.gov/780/Sheriff-Civil-Process"),
    ("Goochland Sheriff",      "https://www.goochlandva.us/340/Sheriff"),
]
for name, url in SHERIFF:
    st, js, raw = fetch(url, timeout=10)
    has_sale = raw and any(k in raw.lower() for k in ("sale", "foreclo", "auction", "parcel", "deed")) if raw else False
    flag = "✅ has sale content" if has_sale else "⚠️  generic page"
    print(f"  {name}: HTTP {st}  {flag}")
    if has_sale and raw:
        import re
        snippet = re.search(r'.{0,40}(?:sale|foreclo|auction|parcel).{0,120}', raw, re.I)
        if snippet:
            print(f"    → {snippet.group(0).strip()[:180]}")

# ─── 2. Henrico County assessor / land records search ────────────────────────
sep("2. Henrico County Assessor / Property Search")

HENRICO = [
    ("Henrico RE Tax",         "https://henrico.gov/services/real-estate-tax-information/"),
    ("Henrico PropertySearch", "https://assessor.henrico.gov/"),
    ("Henrico iasWorld",       "https://ias.henrico.gov/iasworld/IAS.dll/Globals/login.html"),
    ("Henrico GIS Portal",     "https://portal.henrico.gov/mapping/rest/services/Layers/Tax_Parcels/FeatureServer/0/query?where=1%3D1&outFields=GPIN,FULL_ADDRESS&resultRecordCount=3&f=json"),
    ("Henrico OpenGov",        "https://opendata.henrico.gov/"),
]
for name, url in HENRICO:
    st, js, raw = fetch(url, timeout=10)
    print(f"  {name}: HTTP {st}")
    if js and st == 200:
        features = js.get("features", [])
        print(f"    JSON features: {len(features)}")
        if features:
            print(f"    Sample: {features[0].get('attributes',{})}")
    elif raw and st == 200:
        print(f"    Raw ({len(raw)}): {raw[:150].strip()}")

# ─── 3. Chesterfield County assessor / GIS ───────────────────────────────────
sep("3. Chesterfield County Assessor / GIS")

CHESTER = [
    ("Chesterfield RE Tax",    "https://www.chesterfield.gov/389/Real-Estate-Tax-Relief"),
    ("Chesterfield OpenGov",   "https://opendata.chesterfield.gov/"),
    ("Chesterfield GIS",       "https://gisportal.chesterfield.gov/portal/home/"),
    ("Chesterfield iasWorld",  "https://ias.chesterfield.gov/iasworld/"),
    ("Chesterfield Property",  "https://propertyinfo.chesterfield.gov/PropertySearch"),
]
for name, url in CHESTER:
    st, js, raw = fetch(url, timeout=10)
    print(f"  {name}: HTTP {st}")
    if raw and st == 200:
        print(f"    Preview: {raw[:120].strip()}")

# ─── 4. Hanover and Goochland counties ───────────────────────────────────────
sep("4. Hanover & Goochland Counties")

OTHER = [
    ("Hanover GIS Hub",       "https://hanover.maps.arcgis.com/sharing/rest/search?q=assessor+OR+parcel&num=5&f=json"),
    ("Hanover Delinquent",    "https://www.hanovercounty.gov/services/taxes/delinquent"),
    ("Goochland GIS",         "https://www.goochlandva.us/gis"),
    ("Goochland OpenData",    "https://opendata.goochlandva.us/"),
]
for name, url in OTHER:
    st, js, raw = fetch(url, timeout=10)
    print(f"  {name}: HTTP {st}")
    if js and st == 200 and "results" in js:
        for item in js["results"][:3]:
            print(f"    • {item.get('title','?')} — {item.get('url','')[:60]}")

# ─── 5. Virginia Open Data portal ────────────────────────────────────────────
sep("5. Virginia Open Data Portal")

VA_ODP = [
    ("VA OpenData search: foreclosure",   "https://data.virginia.gov/api/views?q=foreclosure&limit=5"),
    ("VA OpenData search: tax delinquent","https://data.virginia.gov/api/views?q=tax+delinquent&limit=5"),
    ("VA OpenData search: lien",          "https://data.virginia.gov/api/views?q=property+lien&limit=5"),
    ("VA GIS opendata parcels",           "https://vgin.maps.arcgis.com/sharing/rest/search?q=parcel+lien+OR+foreclosure+owner:vgin&num=10&f=json"),
]
for name, url in VA_ODP:
    st, js, raw = fetch(url, timeout=10)
    print(f"  {name}: HTTP {st}")
    if js and st == 200:
        items = js if isinstance(js, list) else js.get("results", [])
        for item in items[:4]:
            n = item.get("name") or item.get("title") or str(item)[:60]
            print(f"    • {n}")
    elif raw and st == 200:
        print(f"    Raw: {raw[:200]}")

# ─── 6. CourtListener — free federal court case API ──────────────────────────
sep("6. CourtListener — IRS Liens / Federal Foreclosure")

CL_BASE = "https://www.courtlistener.com/api/rest/v4"
QUERIES = [
    ("IRS lien VA Eastern Dist",  f"{CL_BASE}/dockets/?q=IRS+tax+lien+Richmond+Virginia&order_by=-date_filed&format=json"),
    ("foreclosure Richmond VA",   f"{CL_BASE}/dockets/?q=foreclosure+Richmond+Virginia&order_by=-date_filed&format=json"),
    ("HUD FHA Richmond VA",       f"{CL_BASE}/dockets/?q=HUD+mortgage+Richmond+Virginia&order_by=-date_filed&format=json"),
]
for label, url in QUERIES:
    st, js, raw = fetch(url, extra_headers={"Accept": "application/json"}, timeout=12)
    print(f"  {label}: HTTP {st}")
    if js and st == 200:
        cnt = js.get("count", "?")
        print(f"    Total results: {cnt}")
        for r in js.get("results", [])[:3]:
            print(f"    • {r.get('case_name','?')[:70]}  filed: {r.get('date_filed','?')}")
    elif raw:
        print(f"    Raw: {raw[:150]}")

# ─── 7. HUD Homestore — FHA foreclosures ─────────────────────────────────────
sep("7. HUD Homestore — FHA Foreclosures (Richmond VA)")

HUD_URL = "https://www.hudhomestore.hud.gov/Listing/PropertySearchResult.aspx/GetPropertyList"
payload = json.dumps({
    "stateCode": "VA", "searchCity": "Richmond", "searchZip": "", "propStatus": "I",
    "baths": "0", "beds": "0", "minListPrice": "0", "maxListPrice": "9999999",
    "propType": "SFR", "numResults": "50", "currentPage": "1",
}).encode()
st, js, raw = fetch(HUD_URL, method="POST", data=payload,
                    extra_headers={"Content-Type": "application/json", "Accept": "application/json"})
print(f"  HUD Homestore POST: HTTP {st}")
if js and st == 200:
    print(f"  JSON keys: {list(js.keys())[:10]}")
    # Try to extract property count
    for k in ("d", "properties", "results", "data", "totalCount"):
        if k in js:
            print(f"  [{k}]: {str(js[k])[:200]}")
elif raw:
    print(f"  Raw: {raw[:300]}")

# Also try simple GET listing page
st, _, raw = fetch("https://www.hudhomestore.hud.gov/Listing/Index.aspx?StateCode=VA&searchCity=Richmond")
print(f"  HUD listing page: HTTP {st}")

# ─── 8. Richmond City delinquent tax sale ────────────────────────────────────
sep("8. Richmond City Delinquent Tax / City Data")

RVA_SOURCES = [
    ("RVA Finance Delinquent",  "https://www.rva.gov/finance/revenue/delinquent"),
    ("RVA Open Data parcels",   "https://data.richmondgov.com/api/views?q=foreclosure&limit=5"),
    ("RVA Open Data delinquent","https://data.richmondgov.com/api/views?q=delinquent+tax&limit=5"),
    ("RVA Property",            "https://data.richmondgov.com/api/views?q=property+transfer&limit=5"),
    ("RVA SOCRATA",             "https://data.richmondgov.com/api/views.json?limit=10"),
]
for name, url in RVA_SOURCES:
    st, js, raw = fetch(url, timeout=10)
    print(f"  {name}: HTTP {st}")
    if js and st == 200:
        items = js if isinstance(js, list) else js.get("results", js.get("data", []))
        if isinstance(items, list):
            for item in items[:4]:
                n = item.get("name") or item.get("title") or str(item)[:60]
                print(f"    • {n}")
        else:
            print(f"    JSON keys: {list(js.keys())[:8]}")
    elif raw and st == 200:
        print(f"    Raw: {raw[:200]}")

# ─── 9. Henrico/Chesterfield ArcGIS Online property transfer searches ─────────
sep("9. ArcGIS Online — Property Transfer / Assessor Services")

ARCGIS_SEARCHES = [
    ("Henrico ArcGIS: tax+sale",      "https://henrico.maps.arcgis.com/sharing/rest/search?q=orgid:LxWK4CxNTBBlLshT+tax+sale+OR+transfer&num=10&f=json"),
    ("Henrico ArcGIS: assessor",       "https://henrico.maps.arcgis.com/sharing/rest/search?q=orgid:LxWK4CxNTBBlLshT+assessor+parcel&num=10&f=json"),
    ("Chesterfield ArcGIS Open Hub",   "https://www.arcgis.com/home/search.html?q=owner:ChesterfieldCounty+type:Feature+Service&f=json"),
    ("VA CAMA / Property xfer ESRI",   "https://www.arcgis.com/sharing/rest/search?q=Virginia+property+transfer+CAMA&num=5&f=json"),
]
for name, url in ARCGIS_SEARCHES:
    st, js, raw = fetch(url, timeout=10)
    print(f"  {name}: HTTP {st}")
    if js and st == 200:
        total = js.get("total", len(js.get("results", [])))
        print(f"    Total: {total}")
        for item in js.get("results", [])[:5]:
            print(f"    [{item.get('access','?')}] {item.get('title','?')}")
            if item.get("url"):
                print(f"      URL: {item['url']}")

# ─── 10. Fannie Mae / Freddie Mac REO ─────────────────────────────────────────
sep("10. Fannie Mae HomeSteps / Freddie Mac REO")

FNMA = [
    ("Fannie Mae HomePath API", "https://www.homepath.com/api/search/listings?city=Richmond&state=VA&listingType=REO&limit=10"),
    ("Freddie Mac HomeSteps",   "https://www.homesteps.com/property-search?state=VA&city=Richmond"),
]
for name, url in FNMA:
    st, js, raw = fetch(url, timeout=10)
    print(f"  {name}: HTTP {st}")
    if js and st == 200:
        print(f"    JSON keys: {list(js.keys())[:8]}")
    elif raw and st == 200:
        print(f"    Raw: {raw[:150]}")

print(f"\n{'='*64}")
print("  Discovery complete.")
print(f"{'='*64}\n")
