import random
import requests
import time
import threading
import base64
import json
from Chromium_Launcher import ChromeLauncher
from ws_client import WebsocketClient
from logger import LoggerMixin
import asyncio
import httpx
import pandas as pd
from datetime import datetime


class Small_Zombie(ChromeLauncher, WebsocketClient, LoggerMixin):

    def __init__(self, **kwargs):
        ChromeLauncher.__init__(self, **kwargs)
        WebsocketClient.__init__(self)
        LoggerMixin.__init__(self)
        self.logger = self.get_logger("Small Zombie")
        self._tabs_lock = asyncio.Lock()
        self.tab_ws_endpoints = []
        self.current_ws = None
        self.launch_chrome()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await WebsocketClient.__aexit__(self, exc_type, exc_value, traceback)
        await ChromeLauncher.__aexit__(self, exc_type, exc_value, traceback)

    def find_browser_ws_endpoint(self, url=None):
        try:
            url = url if url else f"http://{self.ip}:{self.port}/json/version"
            res = requests.get(url)
            res.raise_for_status()  # Raise exception if not 200
            info = res.json()
            self.browser_ws= info.get("webSocketDebuggerUrl")
            if self.browser_ws:
                self.logger.debug(f"Browser WS Found at {url}")
                return self.browser_ws
            else:
                raise (f"No webSocketDebuggerUrl found at {url}")
        except Exception as e:
            raise (f"Failed to get browser WS endpoint: {e}")

    async def create_a_tab(self):
        await self.connect(self.browser_ws)
        res_create = await self.send_cmd("Target.createTarget", params={"url": "about:blank"}, get_res=True)
        target_id = res_create.get("result", {}).get("targetId")
        print(target_id)
        if not target_id:
            self.logger.error("Failed to create new target")
            return None

        self.logger.debug(f"Created new target: {target_id}")
        self.current_ws = f'ws://{self.ip}:{self.port}/devtools/page/{target_id}'

        async with self._tabs_lock:
            if self.current_ws not in self.tab_ws_endpoints:
                self.tab_ws_endpoints.append(self.current_ws)
                self.logger.debug(f"Tab WS added: {self.current_ws }")

        await self.connect(self.current_ws)
        return self.current_ws

    async def get_tab_wss(self):
        res = await self.send_cmd("Target.getTargets", get_res=True)
        print(res)
        return res.get("result", {}).get("targetInfos", [])

    # Functions --------------------------------------------------------------------------------------
    async def navigate(self, url, reconnect=None, wait_for_load=10):
        # Register the load listener BEFORE issuing the navigation so a fast load can't fire
        # before we're waiting (the old recv-scan had that race and could also drop the
        # event out of the bounded queue on a busy page).
        waiter = None
        if not reconnect and wait_for_load:
            waiter = asyncio.ensure_future(
                self.wait_for_event("Page.loadEventFired", timeout=wait_for_load))

        await self.send_cmd("Page.navigate", params={"url": url})
        self.logger.debug(f"Navigated to: {url}")

        if reconnect:
            await self.ws_close()
            await asyncio.sleep(reconnect)
            await self.connect(page_address=self.current_ws)
        elif waiter is not None:
            if await waiter is not None:
                self.logger.debug("Page loaded")
            else:
                # Not every page fires a load event within the window (SPAs, quiet
                # file:// pages). Warn and continue rather than crash the caller.
                self.logger.warning(f"no Page.loadEventFired within {wait_for_load}s for {url}")

    async def navigate_until_ready(self, url, timeout=12.0):
        """Navigate and confirm the page committed by POLLING document.readyState instead of
        waiting on Page.loadEventFired. The load event never fires on quiet pages (static
        file:// docs, some SPAs), so the event-wait path just warns and continues with no real
        signal. readyState polling is request/response-matched via evaluate() and works even on
        silent pages. This is the base navigation primitive the agent layer builds on."""
        await self.navigate(url, wait_for_load=0)
        start = time.time()
        while time.time() - start < timeout:
            await asyncio.sleep(0.25)
            try:
                rs = await self.evaluate("document.readyState")
                href = await self.evaluate("location.href")
            except Exception:  # noqa: BLE001 — transient during the commit
                continue
            if rs == "complete" and href and not str(href).startswith("about:blank"):
                return True
        return False  # proceed anyway; caller inspects whatever loaded

    async def wait_for_selector(self, selector, timeout=15):
        """Wait until an element matching the selector appears in the DOM."""
        self.logger.debug(f"Waiting for selector: {selector}")
        start_time = time.time()

        while time.time() - start_time < timeout:
            # Check if element exists
            check_js = f"document.querySelector('{selector}') !== null"
            exists = await self.evaluate(check_js)

            if exists:
                return True
            await asyncio.sleep(0.5)

        self.logger.warning(f"Timeout waiting for selector: {selector}")
        return False

    async def get_cookies(self, show=False):
        msg = await self.send_cmd("Network.getAllCookies", get_res=True)
        try:
            cookies = msg.get("result", {}).get("cookies", [])
            return "; ".join(f"{c['name']}={c['value']}" for c in cookies if 'name' in c and 'value' in c)
        except Exception as e:
            self.logger.debug(e)

    async def add_thread(self, function, args):
        if not isinstance(args, tuple):
            args = (args,)
        listener_thread = threading.Thread(target=function, args=args, daemon=True)
        listener_thread.start()

    async def get_document_root_node(self):
        res  =  await self.send_cmd("DOM.getDocument", get_res=True)
        root_node_id = res["result"]["root"]["nodeId"]
        return root_node_id

    async def find_element_root_node(self, selector, root_node=None, timeout=60):
        node_id = await self.find_element(selector, parent_node_id=root_node, timeout=timeout)
        res =  await self.send_cmd("DOM.describeNode",{"nodeId": node_id, "depth": 1}, get_res=True)
        node = res["result"]["node"]

        if "contentDocument" in node:
            content = node["contentDocument"]
            if "nodeId" in content:
                return content["nodeId"]

        if "shadowRoots" in node and len(node["shadowRoots"]) > 0:
            shadow_root = node["shadowRoots"][0]
            return shadow_root["nodeId"]

    async def find_element(self, selector=False, parent_node_selector=None, parent_node_id=None):
        parent_node = None
        if parent_node_id:
            parent_node = parent_node_id
        elif parent_node_selector:
            parent_node = await self.find_element_root_node(parent_node_selector, timeout=10)
        else:
            parent_node = await self.get_document_root_node()

        res = await self.send_cmd( "DOM.querySelector",  {"nodeId": parent_node, "selector": selector}, get_res=True)
        found_node_id = res.get("result", {}).get("nodeId")
        self.logger.debug(f"Found element {selector}")
        return found_node_id

    # ACTIONS
    async def evaluate(self, expression, return_by_value=True):
        """
        Executes JavaScript in the global context.
        :param expression: The JS code string to execute.
        :param return_by_value: If True, returns the actual JSON object;
                                If False, returns a remote object reference.
        """
        params = {
            "expression": expression,
            "returnByValue": return_by_value,
            "awaitPromise": True  # Useful for async JS code
        }

        res = await self.send_cmd("Runtime.evaluate", params=params, get_res=True)

        if "exceptionDetails" in res.get("result", {}):
            exception = res["result"]["exceptionDetails"]["exception"].get("description")
            self.logger.error(f"JS Evaluation failed: {exception}")
            return None

        # Extract the result value
        result = res.get("result", {}).get("result", {})
        return result.get("value")

    async def focus(self, node_id):
        await self.send_cmd("DOM.focus", {"nodeId": node_id}, get_res=False)

    async def type(self, selector, text="good", root_node=None, wait_a=0.05, wait_b=0.2, click=False, focus=True):

        if root_node:
            element = await self.find_element(selector, parent_node_id=root_node)
            if element:
                iframe_element = element
            else:
                return False
        else:
            iframe_element =await  self.find_element(selector)
            if iframe_element:
                pass
            else:
                return False

        if click:
            await self.click(node=iframe_element)

        if focus:
            await self.send_cmd("DOM.focus", { "nodeId": iframe_element})

        for i, char in enumerate(text):
            await self.send_cmd("Input.dispatchKeyEvent", {
                    "type": "char",
                    "text": char
                }
            )

            await asyncio.sleep(random.uniform(wait_a, wait_b))

        await asyncio.sleep(random.uniform(1, 2))

    async def element_location(self, node_id):
        res = await self.send_cmd("DOM.getBoxModel",{"nodeId": node_id}, get_res=True)
        if res:
            self.logger.debug(res["result"]["model"])
            return res["result"]["model"]
        else:
            raise f"element location {res}"

    async def moveto(self, selector=None, parent_node=None, target_x=None, target_y=None, enforce=0, steps=1000):
        if selector:
            node_id = await self.find_element(
                selector=selector,
                parent_node_id=parent_node
            )

            results = await self.element_location(node_id)
            border = results["border"]
            x = (border[0] + border[2]) / 2
            y = (border[1] + border[5]) / 2
        else:
            x = target_x
            y = target_y

        for _ in range(1, steps + 1):
            current_x = x/steps*_ + random.uniform(-2,2)
            current_y = y/steps*_ + random.uniform(-2,2)
            await self.send_cmd("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "force": enforce,
                "x": current_x,
                "y": current_y
            })
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.3)
        self.logger.debug(f"mouse moved to {current_x, current_y} ")

    async def click(self, selector=None,
                    jetter_x=0,
                    jetter_y=0,
                    enforce=0,
                    times=1,
                    parent_node=None,
                    node=None,
                    ):

            if node:
                node_to_use = node
            elif selector:
                node_to_use = await self.find_element(
                    selector=selector,
                    parent_node_id=parent_node
                )

            results = await self.element_location(node_to_use)
            border = results["border"]
            jetter_x = (border[0] + border[2]) / 2 + jetter_x
            jetter_y = (border[1] + border[5]) / 2 + jetter_y

            for _ in range(times):
                await self.send_cmd("Input.dispatchMouseEvent", {
                    "type": "mousePressed",
                    "button": "left",
                    "force": enforce,
                    "clickCount": 1,
                    "x": jetter_x,
                    "y": jetter_y
                })
                await self.send_cmd("Input.dispatchMouseEvent", {
                    "type": "mouseReleased",
                    "button": "left",
                    "force": enforce,
                    "clickCount": 1,
                    "x": jetter_x,
                    "y": jetter_y
                })
                self.logger.debug(f"Clicked {jetter_x},{jetter_y}")
                await asyncio.sleep(random.uniform(1, 3))

    # ---- REAL CDP INPUT (battle-tested primitives the agent acts through) ----------------
    # These drive the page like a user does — real mouse at real coordinates, real focus, real
    # keystrokes — instead of synthesizing DOM events in page JS (which frameworks like
    # react-select ignore). The agent layer (agent/driver.py) resolves a ref to an on-screen
    # (x, y) via browser.js geometry, then calls these.
    _VK = {"Enter": 13, "Tab": 9, "Backspace": 8, "Delete": 46, "Escape": 27,
           "ArrowDown": 40, "ArrowUp": 38, "ArrowLeft": 37, "ArrowRight": 39,
           "Home": 36, "End": 35, "Space": 32}

    # NOTE: every Input.* command is sent with get_res=True (awaited to completion). On a busy
    # CDP socket (the full event firehose) a fire-and-forget press/release can be delayed or
    # interleaved, so the page sees a malformed gesture — react-select then never opens. Awaiting
    # each event guarantees ordered, fully-processed input.
    async def mouse_move(self, x, y):
        await self.send_cmd("Input.dispatchMouseEvent",
                            {"type": "mouseMoved", "x": x, "y": y}, get_res=True)

    async def mouse_click_xy(self, x, y, clicks=1, button="left", delay=0.04):
        """A real left click at viewport (x, y): move -> press -> release (each awaited)."""
        await self.send_cmd("Input.dispatchMouseEvent",
                            {"type": "mouseMoved", "x": x, "y": y}, get_res=True)
        await asyncio.sleep(delay)
        await self.send_cmd("Input.dispatchMouseEvent",
                            {"type": "mousePressed", "x": x, "y": y, "button": button,
                             "buttons": 1, "clickCount": clicks}, get_res=True)
        await asyncio.sleep(delay)
        await self.send_cmd("Input.dispatchMouseEvent",
                            {"type": "mouseReleased", "x": x, "y": y, "button": button,
                             "buttons": 0, "clickCount": clicks}, get_res=True)

    async def insert_text(self, text):
        """Insert a whole string at the focused element (real text input the page/React sees)."""
        await self.send_cmd("Input.insertText", {"text": str(text)}, get_res=True)

    async def press_key(self, key, modifiers=0):
        """Press a named key (Enter/Tab/ArrowDown/Backspace/…) as a real keyDown+keyUp."""
        code = self._VK.get(key)
        down = {"type": "keyDown", "key": key, "modifiers": modifiers}
        if code:
            down["windowsVirtualKeyCode"] = code
            down["nativeVirtualKeyCode"] = code
        if key == "Enter":
            down["text"] = "\r"
        await self.send_cmd("Input.dispatchKeyEvent", down, get_res=True)
        up = {k: v for k, v in down.items() if k != "text"}
        up["type"] = "keyUp"
        await self.send_cmd("Input.dispatchKeyEvent", up, get_res=True)

    async def screenshot(self, filename=False, full_page=False):
        """Capture the page. full_page=True grabs the ENTIRE scrollable content (not just the
        viewport) via captureBeyondViewport + a clip sized to the layout, so a long form is
        shown whole — matching the pageMap, which outlines off-screen controls too."""
        params = {}
        if full_page:
            try:
                m = await self.send_cmd("Page.getLayoutMetrics", get_res=True)
                r = m.get("result", {})
                cs = r.get("cssContentSize") or r.get("contentSize") or {}
                w = min(int(cs.get("width") or 0) or 1280, 4000)
                h = min(int(cs.get("height") or 0) or 2000, 24000)  # cap absurd heights
                if w and h:
                    params = {"captureBeyondViewport": True,
                              "clip": {"x": 0, "y": 0, "width": w, "height": h, "scale": 1}}
            except Exception:  # noqa: BLE001 — fall back to viewport shot
                params = {}
        res = await self.send_cmd("Page.captureScreenshot", params=params, get_res=True)
        data = res.get("result", {}).get("data")
        if not data:
            self.logger.warning("❌ Screenshot capture failed.")
            return False

        decode_imagedata = base64.b64decode(data)
        if filename:
            with open(filename, "wb") as f:
                f.write(decode_imagedata)
            self.logger.debug(f"📸 Screenshot saved to: {filename}")
            return True
        return decode_imagedata

    async def get_html(self, display=True):
        res = await self.send_cmd(
            "DOM.getOuterHTML", params={"backendNodeId": 1}, get_res=True
        )
        try:
            html = res["result"]["outerHTML"]
        except (KeyError, TypeError):
            self.logger.debug(f"get html exception: {res}")
            return None

        if display:
            self.logger.debug(html)
        return html

    # 2CAPTCHA TASK
    async def twocaptcha_task(self, api_key, task_type, site_url, site_key):

        create_url = "https://api.2captcha.com/createTask"
        result_url = "https://api.2captcha.com/getTaskResult"

        payload = {
            "clientKey": api_key,
            "task": {
                "type": task_type,
                "websiteURL": site_url,
                "websiteKey": site_key,
                "isInvisible": False
            }
        }

        async with httpx.AsyncClient() as client:
            # 1. Create the Task
            resp = await client.post(create_url, json=payload)
            task_data = resp.json()

            if task_data.get("errorId") != 0:
                raise Exception(f"2Captcha Error: {task_data.get('errorCode')}")

            task_id = task_data.get("taskId")
            print(f"Task created: {task_id}. Waiting for solution...")

            # 2. Poll for the Result
            while True:
                await asyncio.sleep(5)  # Wait 5 seconds between checks
                result_resp = await client.post(result_url, json={
                    "clientKey": api_key,
                    "taskId": task_id
                })
                result_data = result_resp.json()

                if result_data.get("status") == "ready":
                    token = result_data.get("solution", {}).get("gRecaptchaResponse")
                    print("Captcha Token Response Received")
                    return token

                if result_data.get("errorId") != 0:
                    raise Exception(f"2Captcha Task Failed: {result_data.get('errorCode')}")

                print("Still processing...")


    # NY Court Scraper Fs
    async def wait_for_results(self, timeout=20):
        """Wait for the 'dataList' table to appear and contain data."""
        start_time = time.time()
        self.logger.debug("Waiting for results table to render...")

        while time.time() - start_time < timeout:
            # This JS check looks for the table and ensures there is at least one <tr> in the <tbody>
            check_js = """
            (() => {
                const table = document.querySelector('table.dataList');
                if (!table) return false;
                const rows = table.querySelectorAll('tbody tr');
                return rows.length > 0;
            })()
            """
            is_loaded = await self.evaluate(check_js)

            if is_loaded:
                self.logger.info("Results table detected successfully.")
                return True

            # Check for "No Results Found" message to fail early
            # Adjust the selector below if the site shows a 'No records' div
            no_results = await self.evaluate("document.body.innerText.includes('No entries found')")
            if no_results:
                self.logger.warning("Search completed: No results found for this criteria.")
                return False

            await asyncio.sleep(0.5)

        return False

    async def select_by_value(self, selector, value):
        # Use json.dumps to safely escape the strings for JS
        safe_selector = json.dumps(selector)
        safe_value = json.dumps(value)

        js_code = f"""
           (function() {{
               var element = document.querySelector({safe_selector});
               if (!element) return "NOT_FOUND";

               element.value = {safe_value};

               element.dispatchEvent(new Event('change', {{ bubbles: true }}));
               element.dispatchEvent(new Event('input', {{ bubbles: true }}));

               return "SUCCESS";
           }})();
           """

        result = await self.evaluate(js_code)

        if result == "SUCCESS":
            self.logger.debug(f"Successfully selected value '{value}' in {selector}")
        elif result == "NOT_FOUND":
            self.logger.error(f"Could not find element with selector: {selector}")
        return result





