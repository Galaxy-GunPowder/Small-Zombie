import os
import sys
import subprocess
import tempfile
import shutil
import time
import traceback
from datetime import datetime

# --- HARDENING FOR TASK SCHEDULER ---
# This forces the script to recognize its own directory as the "Home"
# This prevents "ModuleNotFoundError" when Task Scheduler starts in System32
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(SCRIPT_DIR)
sys.path.append(SCRIPT_DIR)

# Mocking LoggerMixin if not provided; replace with your actual import
try:
    from logger import LoggerMixin
except ImportError:
    class LoggerMixin:
        def get_logger(self, name):
            import logging
            return logging.getLogger(name)


class ChromeLauncher(LoggerMixin):
    def __init__(self, chrome_path=None, user_dir=None, headless=False, port=3000, proxy=None, webgl=False,
                 disable_swiftshader=False, delete_profile=True):
        self.headless = headless
        self.disable_swift = disable_swiftshader
        self.proxy = proxy
        self.port = port
        self.delete_profile = delete_profile
        self.Chrome_Launcher_Logger = self.get_logger("chrome_launcher")

        # 1. ROBUST WINDOWS PATH SEARCH
        if sys.platform.startswith("win"):
            # Potential locations for chrome.exe
            potential_paths = [
                chrome_path,  # Path you might have passed in
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")
            ]

            # Find the first one that actually exists
            self.chrome_path = next((p for p in potential_paths if p and os.path.exists(p)), None)
            self.ip = "localhost"
        elif sys.platform.startswith("linux"):
            self.chrome_path = chrome_path or "/usr/bin/google-chrome"
            self.ip = "127.0.0.1"
        else:
            raise RuntimeError(f"Unsupported OS: {sys.platform}")

        # 2. CRITICAL ERROR CHECK
        if not self.chrome_path:
            self.Chrome_Launcher_Logger.error("Chrome not found in any standard Windows directory.")
            raise RuntimeError("Chrome executable not found. Ensure Chrome is installed.")

        # Set up profile directory
        self.user_dir = user_dir or tempfile.mkdtemp(prefix="chrome_profile_")
        self.proc = None

    def launch_chrome(self):
        base_args = [
            self.chrome_path,
            f"--user-data-dir={self.user_dir}",
            f"--remote-debugging-port={self.port}",
            f"--remote-allow-origins=http://{self.ip}:{self.port}",
            # First-run / welcome / promo suppression. These MUST be spelled exactly
            # as Chrome expects — an unrecognized flag is silently ignored, which is
            # what lands a fresh profile on the "sign in to Chrome" / welcome page.
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-search-engine-choice-screen",   # recent Chrome EU choice dialog
            "--disable-features=ChromeWhatsNewUI",      # suppress the "What's New" tab
            "--no-service-autorun",
            "--password-store=basic",                   # avoid OS keyring unlock prompt
            "--start-maximized",
            # A REAL window size (esp. for headless=new). We intentionally do NOT use
            # Emulation.setDeviceMetricsOverride for this: with a device-metrics override, CDP
            # Input.dispatchMouseEvent coordinates stop matching the visual viewport, so real
            # clicks land off-target (menus don't open). A real window keeps getBoundingClientRect
            # and Input coordinates in the same space.
            "--window-size=1280,1000",
        ]

        if self.headless:
            base_args.append("--headless=new")
        if self.disable_swift:
            base_args.append("--disable-software-rasterizer")

        # 3. TASK SCHEDULER WINDOW PROTECTION
        # CREATE_NO_WINDOW = 0x08000000
        # This prevents the CMD window from "popping" and immediately disappearing
        creation_flags = 0
        if sys.platform.startswith("win"):
            creation_flags = 0x08000000

        try:
            self.Chrome_Launcher_Logger.info(f"Launching Chrome from: {self.chrome_path}")
            self.proc = subprocess.Popen(
                base_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                shell=False,
                creationflags=creation_flags
            )
            # Give Chrome a moment to open the debugging port
            time.sleep(2)

            if self.proc.poll() is not None:
                raise RuntimeError("Chrome process exited immediately after launch.")

        except Exception as e:
            self.Chrome_Launcher_Logger.error(f"Failed to launch Chrome: {e}")
            raise

        return self.proc

    def launch_chrome_alt(self):
        # Construct arguments as a single string for startfile
        # We include the GPU and Task Scheduler stability flags
        args_list = [
            f'--user-data-dir="{self.user_dir}"',
            f'--remote-debugging-port={self.port}',
            f'--remote-allow-origins=http://{self.ip}:{self.port}',
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-search-engine-choice-screen",
            "--disable-features=ChromeWhatsNewUI",
            "--password-store=basic",
            "--start-maximized",
            "--disable-gpu-sandbox",  # CRITICAL for Task Scheduler + GPU
            "--ignore-gpu-blocklist"  # Ensures GPU acceleration is used
        ]
        args_str = " ".join(args_list)

        try:
            self.Chrome_Launcher_Logger.info(f"Using os.startfile to bypass Subprocess restrictions")

            # os.startfile opens the app independently of the script's lifecycle
            os.startfile(self.chrome_path, arguments=args_str)

            # We wait to let Chrome initialize so your subsequent
            # CDP (Small Zombie) connection doesn't fail
            time.sleep(5)

            self.Chrome_Launcher_Logger.info("Chrome launch command sent successfully.")
            return True

        except Exception as e:
            self.Chrome_Launcher_Logger.error(f"startfile failed: {e}")
            raise

    def terminate_chrome(self):
        """Cleanly shuts down the Chrome process and removes the temporary profile."""
        try:
            if self.proc:
                self.proc.terminate()
                self.proc.wait(timeout=5)
        except Exception:
            # Force kill if it won't close
            if sys.platform.startswith("win"):
                os.system(f"taskkill /F /IM chrome.exe /T")

        if self.delete_profile and "chrome_profile_" in self.user_dir:
            try:
                # Short delay to ensure file handles are released
                time.sleep(1)
                shutil.rmtree(self.user_dir, ignore_errors=True)
            except Exception as e:
                self.Chrome_Launcher_Logger.warning(f"Could not delete temp profile: {e}")

    async def __aenter__(self):
        self.launch_chrome_alt()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        self.terminate_chrome()


# Example Usage for Testing:
if __name__ == "__main__":

    # Redirect stdout and stderr to a log file
    log_file = open("task_log.txt", "a", encoding="utf-8")
    sys.stdout = log_file
    sys.stderr = log_file

    print(f"\n--- Task Started: {datetime.now()} ---")

    launcher = ChromeLauncher(headless=False)  # Headless is safer for Task Scheduler
    try:
        launcher.launch_chrome_alt()
        print("Task completed successfully.")
    except Exception:
        print("CRITICAL ERROR:")
        traceback.print_exc(file=sys.stdout)
    finally:
        launcher.terminate_chrome()
        print(f"--- Task Ended: {datetime.now()} ---")
        log_file.close()