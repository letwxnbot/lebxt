# balance_check.py
import asyncio
from decimal import Decimal
from typing import Optional
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ========== SELECTORS (REAL ONES YOU FOUND) ==========

MYGIFT_SELECTORS = {
    "card_number": "#credit-card-number",
    "exp_month": "#expiration_month",
    "exp_year": "#expiration_year",
    "pin": "#credit-card-code",
    "submit": ".btn.ms-2.submit-btn.submit-btn-radius",
    "result_balance": "div.value:nth-of-type(2)"

}

SECURESPEND_SELECTORS = {
    "card_number": "#cardnumber",
    "exp_month": "#expirationMonth",
    "exp_year": "#expirationYear",
    "pin": "#cvv",
    "submit": "#form-block-button-submit",
    "result_balance": "#virtual-section-balance-amount"
}


# ========== HELPERS ==========

def parse_balance_text(text: str) -> Decimal:
    """Convert something like '$12.34' or '12.34 USD' to Decimal('12.34')."""
    if not text:
        return Decimal("0")
    filtered = "".join(ch for ch in text if (ch.isdigit() or ch in ".,"))
    filtered = filtered.replace(",", "")
    if not filtered:
        return Decimal("0")
    return Decimal(filtered)


async def _check_with_playwright(url: str, selectors: dict, card_number: str, exp: Optional[str] = None, pin: Optional[str] = None) -> Decimal:
    """Generic Playwright checker for a single site (robust tab handling + manual CAPTCHA fallback)."""
    try:
        async with async_playwright() as p:
            # --- Launch stealthy Chrome instance ---
            user_data_dir = "/tmp/playwright-user-data"
            browser = await p.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=False,
                channel="chrome",
                slow_mo=150,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--start-maximized",
                    "--window-size=1280,800",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--disable-web-security",
                    "--ignore-certificate-errors"
                ],
            )
            page = browser.pages[0] if browser.pages else await browser.new_page()

            # --- Add random User-Agent + language headers ---
            import random

            user_agents = [
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
            ]
            await page.set_extra_http_headers({
                "User-Agent": random.choice(user_agents),
                "Accept-Language": "en-US,en;q=0.9"
            })
            
                        # Make us look more human
            await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            """)

            # Load the page and give it some time for dynamic UI to appear
            await page.goto(url, timeout=60000)
            await page.wait_for_timeout(3000)
            print("🌐 Page loaded:", page.url)

            # Try to ensure the "Check Balance" tab/form is visible
            try:
                # Try direct text click for "Check Balance"
                if await page.query_selector("text='Check Balance'"):
                    print("▶ Clicking 'Check Balance' (text match)")
                    try:
                        await page.click("text='Check Balance'")
                        await page.wait_for_timeout(800)
                    except Exception:
                        pass
                else:
                    # Fallback: click the second tab if tabs exist
                    tabs = await page.query_selector_all('[role="tab"]')
                    if tabs and len(tabs) >= 2:
                        print("▶ Clicking second tab via role='tab'")
                        try:
                            await tabs[1].click()
                            await page.wait_for_timeout(800)
                        except Exception:
                            pass
                    else:
                        # Last-resort fallback: click the 2nd visible button (site-dependent)
                        btns = await page.query_selector_all("button")
                        if btns and len(btns) >= 2:
                            print("▶ Clicking 2nd visible button (fallback)")
                            try:
                                await btns[1].click()
                                await page.wait_for_timeout(800)
                            except Exception:
                                pass

                # Give the UI a moment to render the form
                await page.wait_for_timeout(800)

                # If a captcha or block overlay exists, pause for manual solving
                body_text = await page.inner_text("body")
                if "captcha" in body_text.lower() or "are you human" in body_text.lower() or "access blocked" in body_text.lower():
                    print("⚠ CAPTCHA or block detected — pausing for manual solve")
                    await page.pause()  # solve captcha manually, then press Resume in Playwright
                    await page.wait_for_timeout(1500)

            except Exception as e:
                print("⚠ Navigation to Check Balance may have encountered an issue:", e)
                # continue — the next wait_for_selector will handle failure if inputs still don't appear

                # Give the UI a moment to render the form
                await page.wait_for_timeout(800)

                # If a captcha or block overlay exists, pause for manual solving
                body_text = await page.inner_text("body")
                if "captcha" in body_text.lower() or "are you human" in body_text.lower() or "access blocked" in body_text.lower():
                    print("⚠ CAPTCHA or block detected — pausing for manual solve")
                    await page.pause()  # solve captcha manually, then press Resume in Playwright
                    await page.wait_for_timeout(1500)

            except Exception as e:
                print("⚠ Navigation to Check Balance may have encountered an issue:", e)
                # continue — the next wait_for_selector will handle failure if inputs still don't appear

            # Wait for the card number input to be present (longer timeout for dynamic UIs)
            try:
                await page.wait_for_selector(selectors["card_number"], timeout=30000)
            except Exception as e:
                await browser.close()
                raise RuntimeError(f"Card input not found after waiting: {e}")

            # Fill in fields
            await page.fill(selectors["card_number"], card_number)
            if exp:
                mm, yy = exp[:2], exp[-2:]
                if selectors.get("exp_month"):
                    await page.fill(selectors["exp_month"], mm)
                if selectors.get("exp_year"):
                    await page.fill(selectors["exp_year"], yy)
            if pin and selectors.get("pin"):
                await page.fill(selectors["pin"], pin)

            # Submit (use provided submit selector)
            await page.click(selectors["submit"])

            # Wait for result element to appear
            try:
                await page.wait_for_selector(selectors["result_balance"], timeout=20000)
                text = await page.inner_text(selectors["result_balance"])
            except PlaywrightTimeoutError:
                await browser.close()
                raise RuntimeError("Timeout waiting for balance result")

            # Detect obvious blocks or CAPTCHAs in the result text
            if "access blocked" in text.lower() or "access denied" in text.lower() or "captcha" in text.lower():
                await browser.close()
                raise RuntimeError("Site blocked automated access (Access Blocked/CAPTCHA)")

            # Parse numeric balance and return
            balance = parse_balance_text(text)
            await browser.close()
            return balance

    except Exception as e:
        # Re-raise as RuntimeError for caller to handle gracefully
        raise RuntimeError(f"Playwright check failed: {e}")

async def fetch_card_balance(bin_number: str, card_number: str, exp: Optional[str] = None, pin: Optional[str] = None) -> Decimal:
    """Decide which site to use based on BIN and run checker."""
    bin6 = (bin_number or card_number[:6])[:6]
    if bin6 in ("435880", "511332", "403446"):
        url = "https://mygift.giftcardmall.com/"
        return await _check_with_playwright(url, MYGIFT_SELECTORS, card_number, exp, pin)
    elif bin6 == "409758":
        url = "https://www.securespend.com/"
        return await _check_with_playwright(url, SECURESPEND_SELECTORS, card_number, exp, pin)
    else:
        raise RuntimeError("Unknown BIN: cannot determine provider")
