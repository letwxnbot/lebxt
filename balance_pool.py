import asyncio
import os
import random
import time
from decimal import Decimal
from typing import Optional, List
from urllib.parse import urlparse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from models import Card


# --- DATABASE MODELS / HELPERS ---

# ======================
# CONFIGURATION
# ======================
WORKER_COUNT = int(os.getenv("WORKER_COUNT", "3"))
RATE_LIMIT_SECONDS = int(os.getenv("RATE_LIMIT_SECONDS", "6"))
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "600"))
JOB_TIMEOUT = int(os.getenv("JOB_TIMEOUT", "45"))
USER_DATA_DIR_BASE = "/tmp/playwright-worker"

# Queue and cache
queue: asyncio.Queue = asyncio.Queue()
_cache = {}

# ======================
# PROXY UTILITIES
# ======================
def _parse_proxy(url: str) -> Optional[dict]:
    if not url:
        return None
    parsed = urlparse(url)
    if not parsed.hostname:
        return None
    server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
    conf = {"server": server}
    if parsed.username:
        conf["username"] = parsed.username
    if parsed.password:
        conf["password"] = parsed.password
    return conf

def _get_proxy_list() -> List[str]:
    p = os.getenv("PROXIES", "") or os.getenv("PROXY_URL", "")
    parts = [x.strip() for x in p.split(",") if x.strip()]
    return parts

# ======================
# BALANCE CHECKER CORE
# ======================
async def fetch_card_balance_on_page(page, bin6: str, card_number: str, exp: Optional[str]=None, pin: Optional[str]=None) -> Decimal:
    """
    Navigate using provided Playwright page and return Decimal balance.
    """
    # Choose provider by BIN
    if bin6 in ("435880", "511332", "403446"):
        url = "https://mygift.giftcardmall.com/"
        result_sel = "div.value:nth-of-type(2)"
        submit_sel = ".btn.ms-2.submit-btn.submit-btn-radius"
        card_sel = "#credit-card-number"
        exp_m = "#expiration_month"
        exp_y = "#expiration_year"
        pin_sel = "#credit-card-code"
    elif bin6 == "409758":
        url = "https://www.securespend.com/"
        result_sel = "#virtual-section-balance-amount"
        submit_sel = "#form-block-button-submit"
        card_sel = "#cardnumber"
        exp_m = "#expirationMonth"
        exp_y = "#expirationYear"
        pin_sel = "#cvv"
    else:
        raise RuntimeError("Unknown BIN provider")

    await page.goto(url, timeout=60000)
    await page.wait_for_timeout(1000)

    # Ensure “Check Balance” tab is active
    try:
        if await page.query_selector("text='Check Balance'"):
            await page.click("text='Check Balance'")
            await page.wait_for_timeout(600)
        else:
            tabs = await page.query_selector_all('[role="tab"]')
            if tabs and len(tabs) >= 2:
                await tabs[1].click()
                await page.wait_for_timeout(600)
    except Exception:
        pass

    # Wait for input field
    try:
        await page.wait_for_selector(card_sel, timeout=20000)
    except Exception as e:
        body = (await page.content())[:2000].lower()
        if "access blocked" in body or "captcha" in body:
            raise RuntimeError("CAPTCHA_OR_BLOCK_DETECTED")
        raise RuntimeError(f"Card input not found: {e}")

    # Fill form
    await page.fill(card_sel, card_number)
    if exp:
        mm, yy = exp[:2], exp[-2:]
        try:
            await page.fill(exp_m, mm)
            await page.fill(exp_y, yy)
        except Exception:
            pass
    if pin:
        try:
            await page.fill(pin_sel, pin)
        except Exception:
            pass

    # Submit form
    try:
        await page.click(submit_sel)
    except Exception:
        try:
            await page.press(card_sel, "Enter")
        except Exception:
            pass

    # Wait for result
    try:
        await page.wait_for_selector(result_sel, timeout=20000)
        text = (await page.inner_text(result_sel)).strip()
    except PlaywrightTimeoutError:
        body = (await page.content()).lower()
        if "access blocked" in body or "captcha" in body:
            raise RuntimeError("CAPTCHA_OR_BLOCK_DETECTED")
        raise RuntimeError("Timeout waiting for balance result")

    # Detect access block
    if any(k in text.lower() for k in ("access blocked", "captcha", "denied")):
        raise RuntimeError("CAPTCHA_OR_BLOCK_DETECTED")

    # Parse numeric balance
    filtered = "".join(ch for ch in text if (ch.isdigit() or ch in ".,"))
    filtered = filtered.replace(",", "")
    if not filtered:
        return Decimal("0")
    return Decimal(filtered)

# ======================
# WORKER LOOP
# ======================
_worker_tasks = []
_worker_status = {}

async def _worker_loop(worker_id: int, proxy_url: Optional[str]):
    proxy_conf = _parse_proxy(proxy_url) if proxy_url else None
    user_data = f"{USER_DATA_DIR_BASE}-{worker_id}"
    _worker_status[worker_id] = {"proxy": proxy_url, "state": "starting"}

    async with async_playwright() as pw:
        launch_args = {
            "user_data_dir": user_data,
            "headless": True,
            "channel": "chrome",
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1280,800"
            ],
        }
        if proxy_conf:
            launch_args["proxy"] = proxy_conf

        browser = await pw.chromium.launch_persistent_context(**launch_args)
        page = browser.pages[0] if browser.pages else await browser.new_page()
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
        """)

        _worker_status[worker_id]["state"] = "ready"

        try:
            while True:
                job = await queue.get()
                if job is None:
                    queue.task_done()
                    break
                card_id, bin6, card_number, exp, pin, fut = job
                try:
                    cached = _cache.get(card_id)
                    if cached and (time.time() - cached[1]) < CACHE_TTL_SECONDS:
                        fut.set_result(cached[0])
                    else:
                        bal = await fetch_card_balance_on_page(page, bin6, card_number, exp, pin)
                        _cache[card_id] = (bal, time.time())
                        fut.set_result(bal)
                except Exception as e:
                    msg = str(e)
                    if "CAPTCHA_OR_BLOCK_DETECTED" in msg or "blocked" in msg:
                        _worker_status[worker_id]["state"] = "blocked"
                        await browser.close()
                        fut.set_exception(RuntimeError("CAPTCHA_OR_BLOCK_DETECTED"))
                        queue.task_done()
                        break
                    else:
                        fut.set_exception(e)
                finally:
                    queue.task_done()
                    await asyncio.sleep(RATE_LIMIT_SECONDS)
        finally:
            try:
                await browser.close()
            except Exception:
                pass
            _worker_status[worker_id]["state"] = "stopped"

# ======================
# POOL CONTROL FUNCTIONS
# ======================
async def start_worker_pool():
    proxies = _get_proxy_list()
    for i in range(WORKER_COUNT):
        proxy_url = proxies[i % len(proxies)] if proxies else None
        t = asyncio.create_task(_worker_loop(i, proxy_url))
        _worker_tasks.append(t)
        await asyncio.sleep(0.5)
    print(f"✅ Started {WORKER_COUNT} worker(s)")

async def stop_worker_pool():
    for _ in range(len(_worker_tasks) or WORKER_COUNT):
        await queue.put(None)
    await asyncio.gather(*_worker_tasks, return_exceptions=True)

async def check_card_async(card_id: int, bin6: str, card_number: str, exp: Optional[str]=None, pin: Optional[str]=None, timeout: int = JOB_TIMEOUT) -> Decimal:
    cached = _cache.get(card_id)
    if cached and (time.time() - cached[1]) < CACHE_TTL_SECONDS:
        return cached[0]
    fut = asyncio.get_running_loop().create_future()
    await queue.put((card_id, bin6, card_number, exp, pin, fut))
    return await asyncio.wait_for(fut, timeout=timeout)

# ======================
# AUTO-RESTART MANAGER
# ======================
async def _restart_worker(worker_id: int):
    proxies = _get_proxy_list()
    proxy_url = None
    if proxies:
        used = {s["proxy"] for s in _worker_status.values() if s.get("proxy")}
        free = [p for p in proxies if p not in used]
        proxy_url = random.choice(free or proxies)
    print(f"♻️ Restarting worker {worker_id} with proxy {proxy_url}")
    t = asyncio.create_task(_worker_loop(worker_id, proxy_url))
    _worker_tasks.append(t)
    _worker_status[worker_id] = {"proxy": proxy_url, "state": "restarted"}
    await asyncio.sleep(0.5)

async def monitor_workers(interval: int = 45):
    print(f"🧠 Worker monitor started (interval {interval}s)")
    while True:
        for wid, info in list(_worker_status.items()):
            if info.get("state") in ("blocked", "stopped"):
                print(f"⚠️ Worker {wid} {info['state']} — restarting...")
                await _restart_worker(wid)
        await asyncio.sleep(interval)

# ======================
# PERIODIC BALANCE REFRESH
# ======================
async def periodic_balance_refresh(SessionLocal, compute_rate_for_card, refresh_interval=3600):
    print(f"🔁 Starting periodic balance refresh every {refresh_interval/60:.1f} min...")
    while True:
        db = SessionLocal()
        try:
            unsold = db.query(Card).filter(Card.status == "in_stock").all()
            print(f"🧾 Checking {len(unsold)} unsold cards...")
            for c in unsold:
                try:
                    new_bal = await check_card_async(
                        c.id, c.bin, c.cc_number, c.exp, decrypt_code(c.encrypted_code)
                    )
                    old_bal = Decimal(c.balance or 0)
                    if abs(new_bal - old_bal) > Decimal("0.01"):
                        c.balance = new_bal
                        new_price = (new_bal * compute_rate_for_card(c)).quantize(Decimal("0.01"))
                        db.commit()
                        print(f"💳 Updated {c.id}: ${old_bal:.2f} → ${new_bal:.2f} | new price {new_price}")
                    await asyncio.sleep(5)
                except Exception as e:
                    print(f"❌ Error refreshing card {c.id}: {e}")
                    await asyncio.sleep(2)
        finally:
            db.close()
        print(f"🕐 Refresh done, sleeping {refresh_interval}s...")
        await asyncio.sleep(refresh_interval)

