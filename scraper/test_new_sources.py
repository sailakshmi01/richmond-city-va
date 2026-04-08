"""
Quick deep-dive into the two promising new data sources:
1. Richmond City Socrata — Delinquent Real Estate Taxes
2. Henrico CAMA Data External — FeatureServer fields

Run: python scraper/test_new_sources.py
"""

import json
import urllib.request
import urllib.parse
from datetime import datetime

HEADERS = {
    "User-Agent": "Mozilla/5.0 Chrome/120",
    "Accept": "application/json",
}

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

print(f"New Source Deep-Dive — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

# ─────────────────────────────────────────────────────────────────────────────
# 1. Richmond City Socrata — find dataset IDs for delinquent tax data
# ─────────────────────────────────────────────────────────────────────────────
sep("1. Richmond City Socrata — Dataset Discovery")

# Search Socrata API for Richmond delinquent tax datasets
st, data = fetch("https://data.richmondgov.com/api/views.json?limit=100&q=delinquent")
if st == 200 and isinstance(data, list):
    print(f"  Search 'delinquent': {len(data)} datasets found")
    for d in data:
        print(f"  [{d.get('id','?')}] {d.get('name','?')}")
        print(f"    type={d.get('viewType','?')} rows={d.get('rowsUpdatedAt','?')} updated={d.get('updatedAt','?')}")
elif "_raw" in data:
    print(f"  Raw: {data['_raw'][:300]}")
else:
    print(f"  HTTP {st}: {str(data)[:200]}")

# Try the catalog API instead
sep("1b. Socrata Catalog Search")
st, data = fetch("https://data.richmondgov.com/api/catalog/v1?q=delinquent&limit=20")
if st == 200:
    results = data.get("results", data if isinstance(data, list) else [])
    print(f"  Catalog results: {len(results)}")
    for item in results[:10]:
        res = item.get("resource", item)
        print(f"  [{res.get('id','?')}] {res.get('name','?')}")
elif "_raw" in data:
    print(f"  Raw ({st}): {data['_raw'][:400]}")

# Try direct known dataset patterns for RVA
sep("1c. Try known Richmond Socrata dataset IDs")
KNOWN_IDS = [
    ("Delinquent Real Estate (known-ish)", "https://data.richmondgov.com/api/views.json?method=getByDomainTag&tag=real+estate&limit=20"),
    ("All datasets browse",               "https://data.richmondgov.com/api/views.json?limit=200"),
]
for label, url in KNOWN_IDS:
    st, data = fetch(url)
    if st == 200 and isinstance(data, list):
        print(f"\n  {label}: {len(data)} datasets")
        for d in data:
            name = d.get("name","?").lower()
            if any(k in name for k in ("delinquent","lien","foreclo","tax","property","parcel","assess")):
                print(f"    ✅ [{d.get('id','?')}] {d.get('name','?')}")
    elif st == 200 and isinstance(data, dict):
        print(f"\n  {label}: HTTP 200, JSON keys: {list(data.keys())[:8]}")
        if "results" in data:
            for item in data["results"][:5]:
                print(f"    {item.get('resource',{}).get('name','?')}")
    else:
        print(f"\n  {label}: HTTP {st}")

# ─────────────────────────────────────────────────────────────────────────────
# 2. Henrico CAMA Data External — explore fields
# ─────────────────────────────────────────────────────────────────────────────
sep("2. Henrico CAMA Data External — FeatureServer Layers")

HENRICO_CAMA = "https://portal.henrico.gov/mapping/rest/services/Layers/Tax_Parcels_and_CAMA_Data_External"

# Get service info
st, data = fetch(f"{HENRICO_CAMA}?f=json")
if st == 200:
    print(f"  Service name: {data.get('serviceDescription','?')[:80]}")
    layers = data.get("layers", data.get("tables", []))
    print(f"  Layers ({len(layers)}):")
    for layer in layers:
        print(f"    [{layer.get('id')}] {layer.get('name','?')}")
else:
    print(f"  HTTP {st}: {str(data)[:200]}")

# Also try FeatureServer
st, data = fetch(f"{HENRICO_CAMA}/FeatureServer?f=json")
if st == 200:
    layers = data.get("layers", [])
    print(f"\n  FeatureServer layers ({len(layers)}):")
    for layer in layers:
        print(f"    [{layer.get('id')}] {layer.get('name','?')}")

# Query the first layer fields
for layer_id in range(3):
    st, data = fetch(f"{HENRICO_CAMA}/FeatureServer/{layer_id}?f=json")
    if st == 200 and "fields" in data:
        fields = data["fields"]
        print(f"\n  Layer {layer_id} ({data.get('name','?')}): {len(fields)} fields")
        for f in fields:
            print(f"    {f['name']} ({f['type']}): {f.get('alias','')}")
        break
    elif st == 200 and "error" not in str(data)[:50]:
        print(f"\n  Layer {layer_id}: HTTP {st}, keys={list(data.keys())[:5]}")

# Get sample records from CAMA layer
sep("2b. Henrico CAMA — Sample Records")
for layer_id in range(3):
    st, data = fetch(
        f"{HENRICO_CAMA}/FeatureServer/{layer_id}/query?"
        "where=1%3D1&outFields=*&resultRecordCount=5&f=json"
    )
    if st == 200 and "features" in data:
        features = data["features"]
        print(f"  Layer {layer_id}: {len(features)} sample records")
        for feat in features[:3]:
            a = feat.get("attributes", {})
            print(f"    {json.dumps(a, default=str)[:300]}")
        break

# ─────────────────────────────────────────────────────────────────────────────
# 3. Try the MapServer version for Henrico CAMA
# ─────────────────────────────────────────────────────────────────────────────
sep("3. Henrico CAMA MapServer — Layer Info")

st, data = fetch(f"{HENRICO_CAMA}/MapServer?f=json")
if st == 200:
    layers = data.get("layers", [])
    print(f"  MapServer layers ({len(layers)}):")
    for layer in layers:
        print(f"    [{layer.get('id')}] {layer.get('name','?')}")

for layer_id in range(5):
    st, data = fetch(f"{HENRICO_CAMA}/MapServer/{layer_id}?f=json")
    if st == 200 and "fields" in data:
        print(f"\n  MapServer Layer {layer_id} ({data.get('name','?')}): {len(data['fields'])} fields")
        for f in data["fields"]:
            print(f"    {f['name']}: {f.get('alias','')}")
        # Sample query
        st2, data2 = fetch(
            f"{HENRICO_CAMA}/MapServer/{layer_id}/query?"
            "where=1%3D1&outFields=*&resultRecordCount=3&f=json"
        )
        if st2 == 200 and "features" in data2:
            print(f"  Sample records:")
            for feat in data2["features"]:
                print(f"    {json.dumps(feat.get('attributes',{}), default=str)[:400]}")
        break

# ─────────────────────────────────────────────────────────────────────────────
# 4. Henrico County - look for property transfer / sales data specifically
# ─────────────────────────────────────────────────────────────────────────────
sep("4. Henrico — Search for Sales/Transfer Layers in Portal")

st, data = fetch(
    "https://henrico.maps.arcgis.com/sharing/rest/search?"
    "q=orgid:LxWK4CxNTBBlLshT&num=100&f=json"
)
if st == 200:
    total = data.get("total", 0)
    print(f"  All Henrico public items: {total}")
    results = data.get("results", [])
    # Filter for property-related items
    for item in results:
        title = item.get("title","").lower()
        if any(k in title for k in ("sale","transfer","deed","foreclo","lien","tax","delinquent","cama","assess","parcel")):
            print(f"  ✅ [{item.get('type','?')}] {item.get('title','?')}")
            if item.get("url"):
                print(f"    URL: {item['url']}")

print(f"\n{'='*60}\n  Done.\n{'='*60}\n")
