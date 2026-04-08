"""
Standalone OCIS scraper test — run in GitHub Actions to verify output.
Prints full diagnostics including screenshots paths and raw API response shapes.

Usage:
    python scraper/test_ocis.py
"""

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

# Add parent dir so fetch imports work
sys.path.insert(0, str(Path(__file__).parent))

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

OCIS_BASE     = "https://eapps.courts.state.va.us/ocis"
OCIS_REST_BASE = "https://eapps.courts.state.va.us/ocis-rest/api/public"

TEST_COURTS = [
    {"fips": "760", "ocis_id": "760C", "name": "Richmond City",      "city": "Richmond"},
    {"fips": "087", "ocis_id": "087C", "name": "Henrico County",      "city": "Henrico"},
    {"fips": "041", "ocis_id": "041C", "name": "Chesterfield County", "city": "Chesterfield"},
    {"fips": "085", "ocis_id": "085C", "name": "Hanover County",      "city": "Hanover"},
    {"fips": "075", "ocis_id": "075C", "name": "Goochland County",    "city": "Goochland"},
]

# Just a few searches for testing
TEST_SEARCHES = ["TRUSTEE", "BANK", "INTERNAL REVENUE"]


async def test_ocis():
    print(f"\n{'='*60}")
    print(f"  OCIS Playwright Test — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            ignore_https_errors=True,
        )
        page = await context.new_page()

        # ── Step 1: Load landing page ─────────────────────────────────────────
        print("Step 1: Loading OCIS landing page…")
        await page.goto(f"{OCIS_BASE}/landing", wait_until="domcontentloaded", timeout=45000)

        # Wait for Angular
        try:
            await page.wait_for_function(
                "document.querySelector('app-root') && document.querySelector('app-root').children.length > 0",
                timeout=20000,
            )
            print("  Angular bootstrapped")
        except PlaywrightTimeout:
            print("  WARNING: Angular may not have bootstrapped")

        await page.wait_for_timeout(2000)
        await page.screenshot(path="/tmp/ocis_test_01_landing.png")
        print(f"  URL: {page.url}")
        print(f"  Title: {await page.title()}")

        # ── Step 2: Find and log all buttons ─────────────────────────────────
        buttons = await page.query_selector_all("button")
        print(f"\nStep 2: Found {len(buttons)} button(s) on landing page:")
        for btn in buttons:
            txt = (await btn.inner_text()).strip()
            bid = await btn.get_attribute("id") or ""
            bcls = await btn.get_attribute("class") or ""
            print(f"  id={bid!r} class={bcls[:40]!r} text={txt!r}")

        # ── Step 3: Accept terms ──────────────────────────────────────────────
        print("\nStep 3: Accepting terms…")
        terms_clicked = False
        for selector in ["#acceptTerms", "button:has-text('Accept')", "button:has-text('I Accept')",
                         ".btn-primary", "[id*='accept' i]"]:
            try:
                btn = await page.wait_for_selector(selector, timeout=5000, state="visible")
                if btn:
                    await btn.click()
                    print(f"  Clicked via: {selector}")
                    terms_clicked = True
                    break
            except PlaywrightTimeout:
                continue

        if not terms_clicked:
            print("  WARNING: Could not find terms button")
            # Dump page HTML for debugging
            html = await page.content()
            print(f"  Page HTML snippet:\n{html[:2000]}")

        await page.wait_for_timeout(3000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await page.screenshot(path="/tmp/ocis_test_02_after_terms.png")
        print(f"  URL after terms: {page.url}")

        # ── Step 4: Test REST API call ─────────────────────────────────────────
        print("\nStep 4: Testing REST API via page.evaluate()…")
        for court in TEST_COURTS:
            for search_term in TEST_SEARCHES:
                payload = {
                    "courtLevels":    ["C"],
                    "selectedCourts": [court["ocis_id"]],
                    "searchBy":       "Name",
                    "searchString":   [search_term],
                    "divisions":      ["All"],
                }
                try:
                    result = await page.evaluate(
                        """
                        async (args) => {
                            const [url, payload] = args;
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
                            try { return { status: resp.status, data: JSON.parse(text) }; }
                            catch { return { status: resp.status, raw: text.slice(0, 500) }; }
                        }
                        """,
                        [f"{OCIS_REST_BASE}/search", payload],
                    )
                    status = result.get("status")
                    data   = result.get("data", {})
                    raw    = result.get("raw", "")

                    # Pull out the entity status
                    entity = data.get("context", {}).get("entity", data.get("entity", {}))
                    api_status = entity.get("status") if isinstance(entity, dict) else "?"
                    msgs = [m.get("messageCode") for m in (entity.get("messages", []) if isinstance(entity, dict) else [])]
                    payload_keys = list(entity.get("payload", {}).keys()) if isinstance(entity.get("payload"), dict) else type(entity.get("payload")).__name__

                    print(f"\n  [{court['ocis_id']}] '{search_term}'")
                    print(f"    HTTP {status} | API status: {api_status} | msgs: {msgs}")
                    if api_status == "SUCCESS":
                        print(f"    Payload keys: {payload_keys}")
                        # Log a sample of the payload
                        payload_val = entity.get("payload")
                        print(f"    Payload sample: {json.dumps(payload_val, default=str)[:500]}")
                    elif raw:
                        print(f"    Raw: {raw[:200]}")

                except Exception as e:
                    print(f"  [{court['ocis_id']}] '{search_term}' ERROR: {e}")

        await page.close()
        await context.close()
        await browser.close()

    print(f"\n{'='*60}")
    print("  Test complete. Check /tmp/ocis_test_*.png screenshots.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(test_ocis())
