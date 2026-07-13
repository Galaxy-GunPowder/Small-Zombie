import os
import json

def proxy_extension(proxy):
    proxy_host, proxy_port, proxy_user, proxy_pass = proxy.split(":")
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'proxy_extension')

    manifest = {
        "version": "1.0.0",
        "manifest_version": 3,
        "name": "Chrome_Proxy_Extension",
        "permissions": [
            "proxy",
            "tabs",
            "unlimitedStorage",
            "storage",
            "webRequest",
            "webRequestAuthProvider",
            "webRequestBlocking"
        ],
        "host_permissions": ["<all_urls>"],
        "background": {"service_worker": "background.js"},
        "minimum_chrome_version": "22.0.0"
    }

    background_js = f"""
var config = {{
    mode: "fixed_servers",
    rules: {{
        singleProxy: {{
            scheme: "http",
            host: "{proxy_host}",
            port: {int(proxy_port)}
        }},
        bypassList: ["localhost"]
    }}
}};

chrome.proxy.settings.set({{value: config, scope: "regular"}}, function() {{}});

function callbackFn(details) {{
    return {{
        authCredentials: {{
            username: "{proxy_user}",
            password: "{proxy_pass}"
        }}
    }};
}}

chrome.webRequest.onAuthRequired.addListener(
    callbackFn,
    {{urls: ["<all_urls>"]}},
    ['blocking']
);
"""

    os.makedirs(path, exist_ok=True)

    with open(os.path.join(path, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=4)

    with open(os.path.join(path, "background.js"), "w") as f:
        f.write(background_js)

    return path
