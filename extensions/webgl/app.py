import os
import json

def load_webglext():
    base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "webgl_extension")
    os.makedirs(path, exist_ok=True)

    # manifest.json for Manifest V3
    manifest = '''
           {
          "manifest_version": 3,
          "name": "WebGL Spoofer",
          "version": "1.0",
          "description": "Spoofs WebGL info for anti-fingerprinting",
          "permissions": [],
          "content_scripts": [
            {
              "matches": ["<all_urls>"],
              "all_frames": true,
              "run_at": "document_start",
              "world": "MAIN",
              "js": ["contentScript.js"]
            }
          ]
        }
    '''

    # Content script to override navigator.platform
    contentscript = '''
// contentScript.js -> inject this pageScript into the page context
(() => {
  const pageScript = `
  (function() {
    'use strict';

    // -----------------------
    // Configuration
    // -----------------------
    const SPOOF = {
      UNMASKED_VENDOR_WEBGL: "NVIDIA Corporation",
      UNMASKED_RENDERER_WEBGL: "NVIDIA GeForce GTX 1080/PCIe/SSE2",
      WEBGPU_ADAPTER_NAME: "NVIDIA GeForce GTX 1080",
      WEBGPU_VENDOR: "NVIDIA"
    };

    // How aggressive: pixelStep = every Nth pixel; maxDelta = maximum color change (0-255)
    const PIXEL_SETTINGS = {
      pixelStep: 11,    // apply noise to every 11th pixel (sparse)
      maxDelta: 2       // maximum ±2 per channel
    };

    // -----------------------
    // Deterministic PRNG (mulberry32) seeded per-origin+UA
    // -----------------------
    function strHash(s) {
      // simple 32-bit mix
      let h = 2166136261 >>> 0;
      for (let i = 0; i < s.length; i++) {
        h = Math.imul(h ^ s.charCodeAt(i), 16777619) >>> 0;
      }
      return h >>> 0;
    }
    function mulberry32(a) {
      return function() {
        a |= 0; a = a + 0x6D2B79F5 | 0;
        let t = Math.imul(a ^ (a >>> 15), 1 | a);
        t = t + Math.imul(t ^ (t >>> 7), 61 | t) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
      };
    }
    const seed = strHash(location.origin + '|' + navigator.userAgent + '|gpu-spoof-v1');
    const rng = mulberry32(seed);

    // Small helper to produce deterministic integer in [-maxDelta, +maxDelta]
    function noiseVal(maxDelta) {
      const r = Math.floor(rng() * (maxDelta * 2 + 1));
      return r - maxDelta;
    }

    // -----------------------
    // GPU strings spoof (WebGL & WebGPU)
    // -----------------------
    const GL_CONST = {
      UNMASKED_VENDOR_WEBGL: 0x9245,
      UNMASKED_RENDERER_WEBGL: 0x9246,
      VENDOR: 0x1F00,
      RENDERER: 0x1F01
    };

    try {
      function patchGetParameter(Proto) {
        if (!Proto || !Proto.prototype) return;
        const orig = Proto.prototype.getParameter;
        if (typeof orig !== 'function') return;
        Proto.prototype.getParameter = function(parameter) {
          try {
            if (parameter === GL_CONST.UNMASKED_VENDOR_WEBGL) return SPOOF.UNMASKED_VENDOR_WEBGL;
            if (parameter === GL_CONST.UNMASKED_RENDERER_WEBGL) return SPOOF.UNMASKED_RENDERER_WEBGL;
            if (parameter === GL_CONST.VENDOR) return SPOOF.UNMASKED_VENDOR_WEBGL;
            if (parameter === GL_CONST.RENDERER) return SPOOF.UNMASKED_RENDERER_WEBGL;
            return orig.call(this, parameter);
          } catch (e) {
            return null;
          }
        };
      }
      patchGetParameter(WebGLRenderingContext);
      patchGetParameter(WebGL2RenderingContext);
    } catch (e) {
      console.warn('[GPU+Pixel-Spoof] patchGetParameter failed', e);
    }

    try {
      function wrapGetExtension(orig) {
        return function(name) {
          const ext = orig.call(this, name);
          if (!ext) return ext;
          if (name === 'WEBGL_debug_renderer_info') {
            const wrapped = Object.create(ext);
            wrapped.getParameter = function(p) {
              if (p === GL_CONST.UNMASKED_VENDOR_WEBGL) return SPOOF.UNMASKED_VENDOR_WEBGL;
              if (p === GL_CONST.UNMASKED_RENDERER_WEBGL) return SPOOF.UNMASKED_RENDERER_WEBGL;
              return ext.getParameter(p);
            };
            return wrapped;
          }
          return ext;
        };
      }
      if (WebGLRenderingContext && WebGLRenderingContext.prototype && WebGLRenderingContext.prototype.getExtension) {
        WebGLRenderingContext.prototype.getExtension = wrapGetExtension(WebGLRenderingContext.prototype.getExtension);
      }
      if (WebGL2RenderingContext && WebGL2RenderingContext.prototype && WebGL2RenderingContext.prototype.getExtension) {
        WebGL2RenderingContext.prototype.getExtension = wrapGetExtension(WebGL2RenderingContext.prototype.getExtension);
      }
    } catch (e) {
      console.warn('[GPU+Pixel-Spoof] wrapGetExtension failed', e);
    }

    // WebGPU minimal spoof (best-effort)
    try {
      const fakeGpu = {
        requestAdapter: async () => ({
          name: SPOOF.WEBGPU_ADAPTER_NAME,
          vendor: SPOOF.WEBGPU_VENDOR,
          requestDevice: async () => ({})
        })
      };
      try {
        Object.defineProperty(navigator, 'gpu', { get: () => fakeGpu, configurable: true });
      } catch (err) {
        try { window.navigator.gpu = fakeGpu; } catch (e) {}
      }
    } catch (e) { /* ignore */ }

    // -----------------------
    // Pixel-noise injection helpers
    // -----------------------
    function applyNoiseToImageData(imageData, pixelStep, maxDelta) {
      const data = imageData.data;
      // data length is width*height*4
      for (let i = 0, pix = 0; i < data.length; i += 4, pix++) {
        if ((pix % pixelStep) !== 0) continue; // sparse
        // only modify RGB, leave alpha unchanged
        data[i] = Math.max(0, Math.min(255, data[i] + noiseVal(maxDelta)));
        data[i+1] = Math.max(0, Math.min(255, data[i+1] + noiseVal(maxDelta)));
        data[i+2] = Math.max(0, Math.min(255, data[i+2] + noiseVal(maxDelta)));
      }
      return imageData;
    }

    // -----------------------
    // Override HTMLCanvasElement.toDataURL and getImageData (2D canvases)
    // -----------------------
    try {
      const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
      HTMLCanvasElement.prototype.toDataURL = function(type, quality) {
        try {
          // If webgl canvas (no 2d context), try draw to 2d then modify
          let ctx2d = this.getContext('2d');
          if (!ctx2d) {
            // create a temporary 2D canvas and draw current canvas onto it
            const tmp = document.createElement('canvas');
            tmp.width = this.width;
            tmp.height = this.height;
            const tctx = tmp.getContext('2d');
            tctx.drawImage(this, 0, 0);
            ctx2d = tctx;
            // mark tmp for cleanup by GC
            const imageData = ctx2d.getImageData(0,0,tmp.width,tmp.height);
            applyNoiseToImageData(imageData, PIXEL_SETTINGS.pixelStep, PIXEL_SETTINGS.maxDelta);
            ctx2d.putImageData(imageData, 0, 0);
            return tmp.toDataURL(type, quality);
          } else {
            const w = this.width, h = this.height;
            const imageData = ctx2d.getImageData(0, 0, w, h);
            applyNoiseToImageData(imageData, PIXEL_SETTINGS.pixelStep, PIXEL_SETTINGS.maxDelta);
            // draw to temp canvas to avoid mutating original canvas state
            const tmp = document.createElement('canvas');
            tmp.width = w; tmp.height = h;
            const tctx = tmp.getContext('2d');
            tctx.putImageData(imageData, 0, 0);
            return tmp.toDataURL(type, quality);
          }
        } catch (e) {
          // fallback to original if anything goes wrong
          try { return origToDataURL.call(this, type, quality); } catch (ee) { return origToDataURL.call(this); }
        }
      };

      // Wrap getContext so that getImageData called on 2d contexts is also modified (defensive)
      const origGetContext = HTMLCanvasElement.prototype.getContext;
      HTMLCanvasElement.prototype.getContext = function(type, opts) {
        const ctx = origGetContext.call(this, type, opts);
        if (!ctx) return ctx;
        if (type === '2d') {
          // wrap getImageData to apply noise before returning
          try {
            const origGetImageData = ctx.getImageData;
            ctx.getImageData = function(x, y, w, h) {
              const imageData = origGetImageData.call(this, x, y, w, h);
              applyNoiseToImageData(imageData, PIXEL_SETTINGS.pixelStep, PIXEL_SETTINGS.maxDelta);
              return imageData;
            };
          } catch (e) { /* ignore */ }
        }
        return ctx;
      };
    } catch (e) {
      console.warn('[GPU+Pixel-Spoof] Canvas 2D patch failed', e);
    }

    // -----------------------
    // Patch WebGL readPixels (modifies the provided ArrayBufferView in-place before returning)
    // -----------------------
    try {
      function wrapReadPixels(Proto) {
        if (!Proto || !Proto.prototype) return;
        const origReadPixels = Proto.prototype.readPixels;
        if (typeof origReadPixels !== 'function') return;
        Proto.prototype.readPixels = function(x, y, width, height, format, type, pixels) {
          // call original (it may write into provided pixels)
          try {
            const result = origReadPixels.apply(this, arguments);
            try {
              // pixels is an ArrayBufferView (Uint8Array/Uint8ClampedArray) typically
              if (pixels && pixels.length) {
                // apply noise sparsely: only on RGB channels, step in pixel units
                const step = PIXEL_SETTINGS.pixelStep;
                const maxD = PIXEL_SETTINGS.maxDelta;
                // Determine bytes per pixel from format/type: commonly RGBA=4 bytes
                // We'll assume 4 bytes per pixel in typical RGBA scenarios
                const bytesPerPixel = 4;
                for (let pix = 0, idx = 0; idx + 3 < pixels.length; pix++, idx += bytesPerPixel) {
                  if ((pix % step) !== 0) continue;
                  // r,g,b channels
                  pixels[idx] = Math.max(0, Math.min(255, pixels[idx] + noiseVal(maxD)));
                  pixels[idx+1] = Math.max(0, Math.min(255, pixels[idx+1] + noiseVal(maxD)));
                  pixels[idx+2] = Math.max(0, Math.min(255, pixels[idx+2] + noiseVal(maxD)));
                  // leave alpha untouched
                }
              }
            } catch (ex) {
              // ignore per-pixel failures
            }
            return result;
          } catch (err) {
            // If original throws (some contexts), rethrow
            throw err;
          }
        };
      }

      wrapReadPixels(WebGLRenderingContext);
      wrapReadPixels(WebGL2RenderingContext);
    } catch (e) {
      console.warn('[GPU+Pixel-Spoof] WebGL readPixels patch failed', e);
    }

    // -----------------------
    // Done
    // -----------------------
    try { console.log('[GPU+Pixel-Spoof] active (GPU strings + canvas pixel noise).'); } catch(e){}
  })();
  `;

  // inject into page context
  const s = document.createElement('script');
  s.textContent = pageScript;
  (document.documentElement || document.head || document.body || document).appendChild(s);
  s.parentNode.removeChild(s);
})();

    '''

    # write files **with UTF-8 encoding**
    with open(os.path.join(path, "manifest.json"), "w", encoding="utf-8") as f:
        f.write(manifest)

    with open(os.path.join(path, "contentScript.js"), "w", encoding="utf-8") as f:
        f.write(contentscript)

    return path