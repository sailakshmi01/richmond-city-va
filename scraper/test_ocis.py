"""
OCIS scraper test v2 — intercepts real Angular network requests to learn the
exact API payload format, then replays searches for motivated-seller lead types.

Usage: python scraper/test_ocis.py
"""

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

OCIS_BASE      = "https://eapps.courts.state.va.us/ocis"
OCIS_REST_BASE = "https://eapps.courts.state.va.us/ocis-rest/api/public"

TEST_COURTS = [
    {"fips": "760", "ocis_id": "760C", "name": "Richmond City",      "city": "Richmond"},
    {"fips": "087", "ocis_id": "087C", "name": "Henrico County",      "city": "Henrico"},
    {"fips": "041", "ocis_id": "041C", "name": "Chesterfield County", "city": "Chesterfield"},
    {"fips": "085", "ocis_id": "085C", "name": "Hanover County",      "city": "Hanover"},
    {"fips": "075", "ocis_id": "075C", "name": "Goochland County",    "city": "Goochland"},
]

# Searches for motivated sellers
TEST_SEARCHES = ["TRUSTEE", "BANK", "INTERNAL REVENUE", "PENNYMAC", "NEWREZ"]


async def test_ocis():
    print(f"\n{'='*60}")
    print(f"  OCIS Playwright Test v2 — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}\n")

    captured_requests = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            ignore_https_errors=True,
        )
        page = await context.new_page()

        # ── Intercept all OCIS REST requests ─────────────────────────────────
        async def capture_request(request):
            if "ocis-rest" in request.url:
                try:
                    body = request.post_data or ""
                    captured_requests.append({
                        "url":     request.url,
                        "method":  request.method,
                        "body":    body,
                        "headers": dict(request.headers),
                    })
                    print(f"  [INTERCEPT] {request.method} {request.url.split('/')[-1]}")
                    if body:
                        print(f"    Payload: {body[:300]}")
                except Exception as e:
                    print(f"  [INTERCEPT ERROR] {e}")

        async def capture_response(response):
            if "ocis-rest/api/public/search" in response.url:
                try:
                    body = await response.text()
                    print(f"  [RESPONSE] {response.status} — {body[:500]}")
                except Exception:
                    pass

        page.on("request", capture_request)
        page.on("response", capture_response)

        # ── Step 1: Accept terms ──────────────────────────────────────────────
        print("Step 1: Loading OCIS landing page and accepting terms…")
        await page.goto(f"{OCIS_BASE}/landing", wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_function(
            "document.querySelector('app-root') && document.querySelector('app-root').children.length > 0",
            timeout=20000,
        )
        await page.wait_for_timeout(2000)

        btn = await page.wait_for_selector("#acceptTerms", timeout=10000, state="visible")
        await btn.click()
        print(f"  ✅ Clicked #acceptTerms")

        await page.wait_for_timeout(3000)
        await page.wait_for_load_state("networkidle", timeout=20000)
        print(f"  URL after terms: {page.url}")

        await page.screenshot(path="/tmp/ocis_test_01_search.png")

        # ── Step 2: Inspect the search page form ──────────────────────────────
        print("\nStep 2: Inspecting search form elements…")
        form_elements = await page.evaluate("""
            () => {
                const result = [];
                // Find all inputs, selects, buttons
                document.querySelectorAll('input, select, button, [role="combobox"], [role="listbox"]').forEach(el => {
                    result.push({
                        tag:   el.tagName,
                        id:    el.id || '',
                        name:  el.name || el.getAttribute('name') || '',
                        type:  el.type || '',
                        class: el.className.substring(0, 60),
                        text:  (el.textContent || el.value || '').trim().substring(0, 50),
                        role:  el.getAttribute('role') || '',
                    });
                });
                return result;
            }
        """)
        for el in form_elements[:30]:
            print(f"  <{el['tag'].lower()}> id={el['id']!r} name={el['name']!r} type={el['type']!r} text={el['text']!r}")

        # ── Step 3: Try to interact with the form ─────────────────────────────
        print("\nStep 3: Interacting with search form…")

        # Find and fill the search input
        search_filled = False
        for selector in [
            "input[id*='search' i]", "input[placeholder*='name' i]",
            "input[placeholder*='search' i]", "input[type='text']",
            "#searchInput", "#nameSearch", ".search-input input",
        ]:
            try:
                el = await page.query_selector(selector)
                if el and await el.is_visible():
                    await el.fill("TRUSTEE")
                    print(f"  ✅ Filled search input via: {selector}")
                    search_filled = True
                    break
            except Exception:
                continue

        if not search_filled:
            print("  ⚠️  Could not find search input — trying Angular component approach")
            # Try clicking a court level first
            try:
                # Look for court level selector
                court_level_els = await page.query_selector_all("[id*='courtLevel' i], [id*='court-level' i]")
                print(f"  Court level elements found: {len(court_level_els)}")
            except Exception as e:
                print(f"  Error: {e}")

        # Look for search button and click
        await page.wait_for_timeout(1000)
        search_clicked = False
        for selector in [
            "button:has-text('Search')", "input[type='submit']",
            "button[type='submit']", "#searchBtn", "[id*='search' i][type='button']",
        ]:
            try:
                btn = await page.query_selector(selector)
                if btn and await btn.is_visible():
                    await btn.click()
                    print(f"  ✅ Clicked search via: {selector}")
                    search_clicked = True
                    break
            except Exception:
                continue

        if search_clicked:
            await page.wait_for_timeout(5000)
            await page.screenshot(path="/tmp/ocis_test_02_results.png")

        # ── Step 4: Test payload variations directly ──────────────────────────
        print("\nStep 4: Testing payload variations via page.evaluate()…")

        PAYLOAD_VARIANTS = [
            # Minimal — just search string
            {"searchBy": "Name", "searchString": ["BANK"]},
            # With divisions default
            {"searchBy": "Name", "searchString": ["BANK"], "divisions": ["Adult Criminal/Traffic"]},
            # With all courts
            {"searchBy": "Name", "searchString": ["BANK"], "selectedCourts": ["All"], "divisions": ["All"]},
            # Single court, no divisions
            {"searchBy": "Name", "searchString": ["BANK"], "selectedCourts": ["760C"]},
            # Circuit only
            {"courtLevels": ["C"], "searchBy": "Name", "searchString": ["BANK"]},
            # Criminal division (the default)
            {"courtLevels": ["C"], "selectedCourts": ["760C"], "searchBy": "Name",
             "searchString": ["BANK"], "divisions": ["Adult Criminal/Traffic"]},
            # Case number search
            {"searchBy": "Case Number", "searchString": ["CL24"], "selectedCourts": ["760C"]},
        ]

        for i, payload in enumerate(PAYLOAD_VARIANTS):
            try:
                result = await page.evaluate(
                    """
                    async (args) => {
                        const [url, payload] = args;
                        const resp = await fetch(url, {
                            method: "POST",
                            headers: {"Content-Type": "application/json", "Accept": "application/json"},
                            body: JSON.stringify(payload),
                            credentials: "include",
                        });
                        const text = await resp.text();
                        try { return { status: resp.status, data: JSON.parse(text) }; }
                        catch { return { status: resp.status, raw: text.slice(0, 300) }; }
                    }
                    """,
                    [f"{OCIS_REST_BASE}/search", payload],
                )
                data   = result.get("data", {})
                entity = data.get("context", {}).get("entity", data.get("entity", {}))
                api_status = entity.get("status") if isinstance(entity, dict) else "?"
                msgs = [m.get("messageCode") for m in (entity.get("messages", []) if isinstance(entity, dict) else [])]
                print(f"\n  Variant {i+1}: {json.dumps(payload)}")
                print(f"    → HTTP {result.get('status')} | API: {api_status} | msgs: {msgs}")
                if api_status == "SUCCESS":
                    p = entity.get("payload")
                    print(f"    ✅ SUCCESS! Payload type: {type(p).__name__}")
                    print(f"    Payload sample: {json.dumps(p, default=str)[:800]}")
            except Exception as e:
                print(f"  Variant {i+1} ERROR: {e}")

        # ── Step 5: Show captured intercepted requests ─────────────────────────
        print(f"\nStep 5: Captured {len(captured_requests)} OCIS REST requests during session:")
        for req in captured_requests:
            print(f"  {req['method']} {req['url']}")
            if req['body']:
                print(f"    Body: {req['body'][:400]}")

        await page.close()
        await context.close()
        await browser.close()

    print(f"\n{'='*60}")
    print("  Test complete. See /tmp/ocis_test_*.png for screenshots.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(test_ocis())
