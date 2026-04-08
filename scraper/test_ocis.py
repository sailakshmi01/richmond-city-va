"""
OCIS scraper test v3 — fully interacts with Angular form to capture the exact
search payload the app sends, then replays it programmatically.

Usage: python scraper/test_ocis.py
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

OCIS_BASE      = "https://eapps.courts.state.va.us/ocis"
OCIS_REST_BASE = "https://eapps.courts.state.va.us/ocis-rest/api/public"


async def test_ocis():
    print(f"\n{'='*60}")
    print(f"  OCIS Test v3 — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}\n")

    captured = []   # all OCIS REST API calls intercepted

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

        # ── Intercept every OCIS REST call ────────────────────────────────────
        async def on_request(req):
            if "ocis-rest" in req.url:
                body = req.post_data or ""
                captured.append({"url": req.url, "method": req.method, "body": body})
                short = req.url.split("/")[-1]
                print(f"[REQ] {req.method} {short}  body={body[:200] or '(none)'}")

        async def on_response(resp):
            if "ocis-rest/api/public/search" in resp.url:
                try:
                    body = await resp.text()
                    print(f"[RESP] {resp.status} {body[:600]}")
                except Exception:
                    pass

        page.on("request",  on_request)
        page.on("response", on_response)

        # ── Step 1: Load & accept terms ───────────────────────────────────────
        print("── Step 1: Accept terms ──")
        await page.goto(f"{OCIS_BASE}/landing", wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_function(
            "document.querySelector('app-root') && document.querySelector('app-root').children.length > 0",
            timeout=20000,
        )
        await page.wait_for_timeout(2000)
        await (await page.wait_for_selector("#acceptTerms", timeout=10000)).click()
        print("  ✅ Clicked #acceptTerms")
        await page.wait_for_timeout(3000)
        await page.wait_for_load_state("networkidle", timeout=20000)
        print(f"  URL: {page.url}")

        # ── Step 2: Dump full page HTML to understand Angular DOM ─────────────
        print("\n── Step 2: Dump entire search page DOM ──")
        html = await page.content()
        # Write to a file so we can see everything
        Path("/tmp/ocis_search_dom.html").write_text(html)
        print(f"  DOM written to /tmp/ocis_search_dom.html ({len(html)} bytes)")

        # Find ALL input elements including hidden
        all_inputs = await page.evaluate("""
            () => {
                const result = [];
                document.querySelectorAll('input, textarea').forEach(el => {
                    result.push({
                        id:          el.id || '',
                        name:        el.name || '',
                        type:        el.type || '',
                        placeholder: el.placeholder || '',
                        value:       el.value || '',
                        hidden:      el.hidden,
                        display:     window.getComputedStyle(el).display,
                        visibility:  window.getComputedStyle(el).visibility,
                        ariaLabel:   el.getAttribute('aria-label') || '',
                        ngModel:     el.getAttribute('ng-reflect-model') || '',
                    });
                });
                return result;
            }
        """)
        print(f"\n  All inputs ({len(all_inputs)}):")
        for el in all_inputs:
            print(f"    id={el['id']!r} name={el['name']!r} type={el['type']!r} "
                  f"ph={el['placeholder']!r} val={el['value']!r} "
                  f"display={el['display']!r} aria={el['ariaLabel']!r} ngModel={el['ngModel']!r}")

        # ── Step 3: Try to interact with Angular form & trigger real search ────
        print("\n── Step 3: Interact with Angular form ──")

        # The Angular search form uses custom components. Try the name input via
        # Angular binding or by looking at the text node near the search field.
        search_text = "TRUSTEE"

        # Strategy A: Look for the input inside the search-by section
        for selector in [
            "#searchString0",          # likely Angular model binding
            "#nameField",
            "#searchField",
            "input[ng-reflect-model]", # Angular ng-model bound inputs
            "input[formcontrolname]",  # Angular reactive form
            "app-search input[type='text']",
            "app-search-criteria input[type='text']",
            ".search-input",
            "#content input[type='text']",
        ]:
            el = await page.query_selector(selector)
            if el:
                vis = await el.is_visible()
                print(f"  Found selector {selector!r} visible={vis}")
                if vis:
                    await el.fill(search_text)
                    print(f"  ✅ Filled via {selector!r}")
                    break

        # Strategy B: Use keyboard — Tab through to find the search field
        # First click the search area to focus
        try:
            await page.click("#searchCriteriaContentDesktop")
            await page.wait_for_timeout(500)
            # Get all visible inputs after clicking
            visible_inputs = await page.evaluate("""
                () => {
                    const result = [];
                    document.querySelectorAll('input[type="text"], input:not([type])').forEach(el => {
                        const style = window.getComputedStyle(el);
                        if (style.display !== 'none' && style.visibility !== 'hidden' && el.offsetParent !== null) {
                            result.push({
                                id: el.id || '', placeholder: el.placeholder || '',
                                value: el.value || '', className: el.className.substring(0, 40)
                            });
                        }
                    });
                    return result;
                }
            """)
            print(f"  Visible text inputs after clicking search area: {visible_inputs}")
        except Exception as e:
            print(f"  Strategy B error: {e}")

        # Strategy C: Use JavaScript to find and set the Angular component value
        try:
            result = await page.evaluate("""
                () => {
                    // Try to find Angular's ng-model bound inputs
                    const inputs = Array.from(document.querySelectorAll('input'));
                    const info = [];
                    for (const inp of inputs) {
                        // Check for Angular internal properties
                        const keys = Object.keys(inp).filter(k => k.startsWith('__ngContext') || k.startsWith('_ng'));
                        info.push({
                            id: inp.id, type: inp.type,
                            placeholder: inp.placeholder,
                            hasNgContext: keys.length > 0,
                            parentId: inp.parentElement ? inp.parentElement.id : ''
                        });
                    }
                    return info;
                }
            """)
            print(f"\n  Angular context inputs:")
            for inp in result:
                if inp['hasNgContext']:
                    print(f"    ✅ id={inp['id']!r} ph={inp['placeholder']!r} parent={inp['parentId']!r}")
        except Exception as e:
            print(f"  Strategy C error: {e}")

        # ── Step 4: Try clicking Search All to trigger a search ───────────────
        print("\n── Step 4: Click 'Search All' button ──")
        for selector in ["#searchAllCourt", "button:has-text('Search All')",
                         "button:has-text('Search')", "#btnSearch"]:
            btn = await page.query_selector(selector)
            if btn and await btn.is_visible():
                print(f"  Clicking {selector!r}…")
                await btn.click()
                await page.wait_for_timeout(4000)
                print(f"  URL after click: {page.url}")
                break

        await page.screenshot(path="/tmp/ocis_test_03_after_search.png")

        # ── Step 5: Try directly calling getCourtsCodeDetails to understand format ──
        print("\n── Step 5: Inspect getCourtsCodeDetails ──")
        courts_result = await page.evaluate("""
            async () => {
                const r = await fetch('/ocis-rest/api/public/getCourtsCodeDetails',
                    {credentials: 'include'});
                const text = await r.text();
                try { return JSON.parse(text); } catch { return {raw: text.slice(0, 1000)}; }
            }
        """)
        courts_json = json.dumps(courts_result, default=str)
        print(f"  getCourtsCodeDetails response ({len(courts_json)} chars):")
        # Just print the first part showing court structure
        if isinstance(courts_result, dict):
            entity = courts_result.get("context", {}).get("entity", courts_result)
            payload = entity.get("payload") if isinstance(entity, dict) else None
            if isinstance(payload, list) and payload:
                print(f"  Total courts: {len(payload)}")
                # Find our 5 courts
                our_fips = ["760", "087", "041", "085", "075"]
                for court in payload:
                    fc = str(court.get("fipsCode", court.get("fipsCode4", "")))
                    if any(f in fc for f in our_fips):
                        print(f"    {json.dumps(court)}")
            else:
                print(f"  Raw: {courts_json[:800]}")

        # ── Step 6: Inspect getUIConfig for valid division names ──────────────
        print("\n── Step 6: Inspect getUIConfig ──")
        ui_result = await page.evaluate("""
            async () => {
                const r = await fetch('/ocis-rest/api/public/getUIConfig',
                    {credentials: 'include'});
                const text = await r.text();
                try { return JSON.parse(text); } catch { return {raw: text.slice(0, 1000)}; }
            }
        """)
        ui_json = json.dumps(ui_result, default=str)
        print(f"  getUIConfig ({len(ui_json)} chars):")
        if isinstance(ui_result, dict):
            entity = ui_result.get("context", {}).get("entity", ui_result)
            payload = entity.get("payload") if isinstance(entity, dict) else None
            print(f"  Payload keys: {list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__}")
            if isinstance(payload, dict):
                print(f"  Full payload: {json.dumps(payload, default=str)[:2000]}")

        # ── Step 7: Try getLookupCodeDetails for division codes ───────────────
        print("\n── Step 7: getLookupCodeDetails ──")
        lookup_result = await page.evaluate("""
            async () => {
                const r = await fetch('/ocis-rest/api/public/getLookupCodeDetails',
                    {credentials: 'include'});
                const text = await r.text();
                try { return JSON.parse(text); } catch { return {raw: text.slice(0, 1000)}; }
            }
        """)
        if isinstance(lookup_result, dict):
            entity = lookup_result.get("context", {}).get("entity", lookup_result)
            payload = entity.get("payload") if isinstance(entity, dict) else None
            if isinstance(payload, dict):
                # Show division-related keys
                for key in payload:
                    if "div" in key.lower() or "type" in key.lower():
                        print(f"  [{key}]: {json.dumps(payload[key])[:300]}")
            print(f"  Full lookup keys: {list(payload.keys()) if isinstance(payload, dict) else 'n/a'}")

        # ── Step 8: Summary of intercepted requests ───────────────────────────
        print(f"\n── Step 8: All {len(captured)} captured OCIS requests ──")
        for req in captured:
            print(f"  {req['method']} {req['url'].split('/')[-1]}")
            if req['body']:
                print(f"    → {req['body'][:300]}")

        await page.close()
        await context.close()
        await browser.close()

    print(f"\n{'='*60}  Done  {'='*60}\n")


if __name__ == "__main__":
    asyncio.run(test_ocis())
