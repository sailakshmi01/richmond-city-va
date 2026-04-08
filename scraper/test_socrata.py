"""
Quick test: Richmond City Socrata Delinquent Tax datasets
& Henrico CAMA distressed property filter

Run in GitHub Actions: python scraper/test_socrata.py
"""

import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta

HEADERS = {"User-Agent": "Mozilla/5.0 Chrome/120", "Accept": "application/json"}

def fetch(url, timeout=15):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            try:
                return r.status, json.loads(body)
            except Exception:
                return r.status, {"_raw": body.decode("utf-8", errors="replace")[:2000]}
    except Exception as e:
        return 0, {"_error": str(e)}

def sep(t):
    print(f"\n{'='*60}\n  {t}\n{'='*60}")

print(f"Socrata + CAMA Test — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

# ─── Richmond Delinquent Taxes: d6cc-7wn3 ─────────────────────────────────────
sep("1a. Richmond Delinquent Taxes (Map dataset d6cc-7wn3)")
st, data = fetch("https://data.richmondgov.com/resource/d6cc-7wn3.json?$limit=5")
print(f"  HTTP {st}")
if isinstance(data, list):
    print(f"  Records: {len(data)} (sample of 5)")
    if data:
        print(f"  Fields: {list(data[0].keys())}")
        for rec in data[:3]:
            print(f"  → {json.dumps(rec, default=str)[:300]}")
elif "_raw" in data:
    print(f"  Raw: {data['_raw'][:400]}")
else:
    print(f"  JSON: {str(data)[:300]}")

# ─── Richmond Delinquent Taxes: 83t5-hbac ─────────────────────────────────────
sep("1b. Delinquent Taxes 6+ Months (83t5-hbac)")
st, data = fetch("https://data.richmondgov.com/resource/83t5-hbac.json?$limit=5")
print(f"  HTTP {st}")
if isinstance(data, list):
    print(f"  Records: {len(data)}")
    if data:
        print(f"  Fields: {list(data[0].keys())}")
        for rec in data[:3]:
            print(f"  → {json.dumps(rec, default=str)[:300]}")

# ─── Total record count ────────────────────────────────────────────────────────
sep("1c. Total counts")
for ds_id, label in [("d6cc-7wn3", "Map Delinquent"), ("83t5-hbac", "6+ Month Delinquent")]:
    st, data = fetch(f"https://data.richmondgov.com/resource/{ds_id}.json?$select=count(*)%20as%20total")
    if isinstance(data, list) and data:
        print(f"  {label}: {data[0].get('total','?')} records total")
    else:
        print(f"  {label}: HTTP {st}")

# ─── Henrico CAMA: ACCOUNT_STATUS distinct values ─────────────────────────────
sep("2. Henrico CAMA — ACCOUNT_STATUS & TAX_TYPE_CODE values")

CAMA_URL = ("https://portal.henrico.gov/mapping/rest/services/Layers/"
            "Tax_Parcels_and_CAMA_Data_External/FeatureServer/0/query")

# Get distinct ACCOUNT_STATUS values
st, data = fetch(
    f"{CAMA_URL}?where=1%3D1&outFields=ACCOUNT_STATUS,TAX_TYPE_CODE"
    "&returnDistinctValues=true&returnCountOnly=false&resultRecordCount=50&f=json"
)
if st == 200 and "features" in data:
    vals = [(f["attributes"]["ACCOUNT_STATUS"], f["attributes"]["TAX_TYPE_CODE"]) for f in data["features"]]
    print(f"  Distinct (ACCOUNT_STATUS, TAX_TYPE_CODE) combos ({len(vals)}):")
    for v in vals:
        print(f"    {v}")

# Get properties with non-null or specific ACCOUNT_STATUS
sep("2b. Henrico CAMA — Non-null account status sample")
st, data = fetch(
    f"{CAMA_URL}?where=ACCOUNT_STATUS+IS+NOT+NULL&outFields=GPIN,FULL_ADDRESS,"
    "LAST_SALE_DATE,LAST_SALE_PRICE,LAND_VALUE_CURRENT,IMPROVEMENTS_VALUE_CURRENT,"
    "ACCOUNT_STATUS,TAX_TYPE_CODE,USE_DESCRIPTION,PROPERTY_CONDITION"
    "&resultRecordCount=10&f=json"
)
if st == 200 and "features" in data:
    print(f"  Non-null ACCOUNT_STATUS records: {len(data['features'])}")
    for feat in data["features"][:5]:
        a = feat["attributes"]
        total_val = (a.get("LAND_VALUE_CURRENT") or 0) + (a.get("IMPROVEMENTS_VALUE_CURRENT") or 0)
        sale_price = a.get("LAST_SALE_PRICE") or 0
        ratio = round(sale_price / total_val, 2) if total_val > 0 else None
        print(f"  {a.get('FULL_ADDRESS','?')} | status={a.get('ACCOUNT_STATUS')} | "
              f"tax_type={a.get('TAX_TYPE_CODE')} | price={sale_price} | val={total_val} | ratio={ratio} | "
              f"use={a.get('USE_DESCRIPTION','?')[:20]} | cond={a.get('PROPERTY_CONDITION','?')}")

# ─── Henrico CAMA: filter for recent distressed sales ─────────────────────────
sep("2c. Henrico CAMA — Recent sales with low price/value ratio")

# Properties sold in last 2 years where sale price < 70% of assessed value
two_yrs_ago_ms = int((datetime.now() - timedelta(days=730)).timestamp() * 1000)
st, data = fetch(
    f"{CAMA_URL}?where=LAST_SALE_DATE+%3E+{two_yrs_ago_ms}+AND+"
    "LAST_SALE_PRICE+%3E+0+AND+"
    "LAST_SALE_PRICE+%3C+(LAND_VALUE_CURRENT+%2B+IMPROVEMENTS_VALUE_CURRENT)*0.7"
    "&outFields=GPIN,FULL_ADDRESS,LAST_SALE_DATE,LAST_SALE_PRICE,"
    "LAND_VALUE_CURRENT,IMPROVEMENTS_VALUE_CURRENT,USE_DESCRIPTION,DEED_BOOK,DEED_PAGE"
    "&orderByFields=LAST_SALE_DATE+DESC&resultRecordCount=200&f=json"
)
if st == 200 and "features" in data:
    print(f"  Low-ratio recent sales: {len(data['features'])} records")
    for feat in data["features"][:8]:
        a = feat["attributes"]
        total = (a.get("LAND_VALUE_CURRENT") or 0) + (a.get("IMPROVEMENTS_VALUE_CURRENT") or 0)
        sp = a.get("LAST_SALE_PRICE") or 0
        ratio = round(sp / total, 3) if total else None
        ts = a.get("LAST_SALE_DATE")
        dt = datetime.fromtimestamp(ts/1000).strftime("%Y-%m-%d") if ts else "?"
        print(f"  {a.get('FULL_ADDRESS','?'):<30} {dt}  ${sp:>8,} / ${total:>8,}  ratio={ratio}")
elif st == 200:
    print(f"  Error or no features: {str(data)[:200]}")
else:
    print(f"  HTTP {st}: {str(data)[:200]}")

# ─── Henrico CAMA: recent sales ALL (count) ────────────────────────────────────
sep("2d. Henrico CAMA — Recent sales count (2 yrs)")
st, data = fetch(
    f"{CAMA_URL}?where=LAST_SALE_DATE+%3E+{two_yrs_ago_ms}&returnCountOnly=true&f=json"
)
if st == 200 and "count" in data:
    print(f"  Henrico properties with sale in last 2 years: {data['count']}")

# ─── Henrico CAMA: PROPERTY_CONDITION distinct values ─────────────────────────
sep("2e. Henrico CAMA — PROPERTY_CONDITION values")
st, data = fetch(
    f"{CAMA_URL}?where=PROPERTY_CONDITION+IS+NOT+NULL"
    "&outFields=PROPERTY_CONDITION&returnDistinctValues=true&resultRecordCount=20&f=json"
)
if st == 200 and "features" in data:
    vals = [f["attributes"]["PROPERTY_CONDITION"] for f in data["features"]]
    print(f"  Distinct condition values: {vals}")

print(f"\n{'='*60}\n  Done.\n{'='*60}\n")
