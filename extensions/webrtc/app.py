

def webrtc_privacy_extension():
    path = os.path.dirname(os.path.abspath(__file__)) + r'\Privacy_Extension'

    if not os.path.exists(path):
        os.makedirs(path)

    # Manifest V3
    manifest_json = '''{
      "manifest_version": 3,
      "name": "WebRTC Privacy Setter",
      "version": "1.0",
      "permissions": [
        "privacy"
      ],
      "background": {
        "service_worker": "background.js"
      }
    }'''

    # Background script to set WebRTC IP handling policy
    background_js = '''
    chrome.runtime.onInstalled.addListener(() => {
      if (chrome.privacy && chrome.privacy.network && chrome.privacy.network.webRTCIPHandlingPolicy) {
        chrome.privacy.network.webRTCIPHandlingPolicy.set(
          { value: "disable_non_proxied_udp" },
          () => { console.log("WebRTC IP policy set."); }
        );
      }
    });
    '''

    with open(os.path.join(path, "manifest.json"), "w", encoding="utf-8") as f:
        f.write(manifest_json)

    with open(os.path.join(path, "background.js"), "w", encoding="utf-8") as f:
        f.write(background_js)

    return path