# Small Zombie

[![Python Version](https://img.shields.io/badge/Python-3.12%2B-green)](#)
[![Status](https://img.shields.io/badge/Status-Release-orange)](#)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux-lightgrey)](#)

Lightweight **async Chrome DevTools Protocol (CDP) driver** — launch real Chrome, connect over WebSocket, and automate navigation, DOM queries, network capture, and human-like input without Selenium or Playwright overhead.

## Why it exists

Built from first principles on raw CDP so every browser interaction is explicit and debuggable. Ideal for fingerprint research, anti-bot testing, and production scrapers that need full control over Chrome profiles, proxies, and extension injection.

## Features

- **AsyncWSClient** — persistent WebSocket session to Chrome
- **Browser Launcher** — cross-platform Chrome spawn with isolated user profiles
- **Extensions** — proxy routing, WebRTC leak control, WebGL/canvas fingerprint hooks (experimental)
- **Core automation** — navigate, iframe/shadow-DOM queries, screenshots, network body logging, typed clicks with noise, drag-and-drop

Refactored to async in 2025; first demos from 2023.

## Quick start

```python
import asyncio
from main import Small_Zombie

async def main():
    async with Small_Zombie(proxy=False, user_dir=None, chrome_path=None, port=3000, headless=False) as driver:
        await driver.create_a_tab()
        await driver.navigate("https://www.trustpilot.com/evaluate/www.google.com", wait_for_load=20)
        element = await driver.find_element("input.star-selector_star__UckR4:nth-child(1)")
        await driver.focus(element)
        await driver.click(node=element)

asyncio.run(main())
```

## Constructor options

```python
async with Small_Zombie(
    proxy=False,          # "IP:port:user:pass" or None
    user_dir=None,        # Path or auto-generated UID profile folder
    chrome_path=None,     # Auto-detects Windows/Linux Chrome
    port=3000,
    headless=False,
    webgl=True,           # Inject WebGL/canvas overrides via extension
) as driver:
    ...
```

## API surface

| Category | Methods |
|----------|---------|
| Connection | `connect`, `disconnect`, `find_browser_ws_endpoint` |
| Navigation | `navigate`, `reload`, `create_a_tab`, `wait_for_load` |
| DOM | `find_element`, `find_elements`, `get_root_node`, shadow/iframe root resolution |
| Actions | `focus`, `type`, `click` (CDP / JS / pyautogui), `moveto`, `wheel`, drag-and-drop |
| Network | Background listener, response body capture |
| Utility | `evaluate`, `screenshot`, random username/password generator |

## Demos

[YouTube channel — Storm Clouds Development](https://www.youtube.com/@storm-clouds-development)

## Related

- **NY_COURT_SCRAPER** — production court-records workflow built on this driver
- **boobworld / browserenv** — synthetic browser environments for fingerprint comparison
- **BW_Fetcher** — TLS/HTTP/2 layer that matches real browser wire fingerprints
