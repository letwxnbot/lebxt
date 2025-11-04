import asyncio
from urllib.parse import urlparse
import os
from playwright.async_api import async_playwright

def _get_proxy_conf():
    s = os.getenv("PROXY_URL", "").strip()
    if not s:
        return None
    parsed = urlparse(s)
    server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
    conf = {"server": server}
    if parsed.username:
        conf["username"] = parsed.username
    if parsed.password:
        conf["password"] = parsed.password
    return conf

async def main():
    proxy_conf = _get_proxy_conf()
    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir="/tmp/playwright-proxy-test",
            headless=False,
            channel="chrome",
            proxy=proxy_conf
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()
        await page.goto("https://ipinfo.io/ip")
        print("🌍 External IP seen by site:", (await page.inner_text("body")).strip())
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
