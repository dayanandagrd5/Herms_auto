"""
ip_update.py — Kite Developer Portal IP updater (Windows / Edge, macOS / Safari)
====================================================
Logs in to developers.kite.trade with credentials from config.yaml,
then replaces the IP-whitelist field with both current public IPs
(IPv4 + IPv6, newline-separated, fetched in parallel).

The browser driver is picked automatically based on the host OS:
  - Windows -> Microsoft Edge (msedgedriver)
  - macOS   -> Safari (built-in safaridriver)

ONE-TIME SETUP (Windows):
1. Check your Edge version: Edge -> Settings -> About Microsoft Edge
2. Download the matching msedgedriver from:
   https://developer.microsoft.com/en-us/microsoft-edge/tools/webdriver/
3. Place msedgedriver.exe either:
   - In the same folder as this script, OR
   - In a folder that is on your system PATH

ONE-TIME SETUP (macOS):
1. Safari -> Settings -> Advanced -> enable "Show Develop menu"
2. Develop -> Allow Remote Automation
3. Run once in Terminal: safaridriver --enable

Standalone:    python ip_update.py
Programmatic:  from ip_update import ensure_ip_whitelisted
"""

import logging
import platform
import yaml
import requests
import concurrent.futures
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

log = logging.getLogger("ip_update")

BASE_DIR        = Path(__file__).resolve().parent
CONFIG_FILE     = BASE_DIR / "config.yaml"
EDGE_DRIVER_PATH = BASE_DIR / "msedgedriver.exe"

LOGIN_URL   = "https://developers.kite.trade/login"
PROFILE_URL = "https://developers.kite.trade/profile"


# ── Config ─────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_last_known_ip(ip: str):
    """
    Updates ONLY the runtime.last_known_ip line in config.yaml.
    Does a targeted line replacement — all comments and formatting are preserved.
    """
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()

    new_lines  = []
    in_runtime = False
    ip_written = False

    for line in lines:
        stripped = line.rstrip()

        # Detect the runtime: section header
        if stripped == "runtime:":
            in_runtime = True
            new_lines.append(line)
            continue

        # Inside runtime block — replace the last_known_ip line
        if in_runtime and stripped.lstrip().startswith("last_known_ip:"):
            new_lines.append('  last_known_ip: "{}"\n'.format(ip))
            ip_written = True
            in_runtime = False
            continue

        # Left the runtime block without finding last_known_ip — inject it
        if in_runtime and stripped != "" and not stripped.startswith(" "):
            new_lines.append('  last_known_ip: "{}"\n'.format(ip))
            ip_written = True
            in_runtime = False

        new_lines.append(line)

    # runtime section not found at all — append it
    if not ip_written:
        new_lines.append("\n")
        new_lines.append("# -- Runtime State (auto-managed by ip_update.py) --\n")
        new_lines.append("runtime:\n")
        new_lines.append('  last_known_ip: "{}"\n'.format(ip))

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


# ── Public IP detection — IPv4 + IPv6 fetched in parallel ─────────────────────

def _fetch(url: str) -> str:
    return requests.get(url, timeout=5).text.strip()


def get_current_ips() -> str:
    """
    Fetches IPv4 and IPv6 concurrently.
    Returns newline-separated IPs e.g. "103.21.58.12\n2406:7400:..."
    """
    tasks = {
        "v4": ["https://api.ipify.org",  "https://ipv4.icanhazip.com"],
        "v6": ["https://api6.ipify.org", "https://ipv6.icanhazip.com"],
    }

    def fetch_family(urls, expect_colon):
        for url in urls:
            try:
                ip = _fetch(url)
                if ip and ((":" in ip) == expect_colon):
                    return ip
            except Exception:
                continue
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f4   = ex.submit(fetch_family, tasks["v4"], False)
        f6   = ex.submit(fetch_family, tasks["v6"], True)
        ipv4 = f4.result()
        ipv6 = f6.result()

    if not ipv4 and not ipv6:
        raise RuntimeError("Could not detect any public IP")

    combined = "\n".join(a for a in [ipv4, ipv6] if a)
    log.info("[IP] %r", combined)
    return combined


# ── Edge driver (Windows) ────────────────────────────────────────────────────

def _build_edge_driver() -> webdriver.Edge:
    options = EdgeOptions()
    options.use_chromium = True
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    # Uncomment to run without a visible browser window:
    # options.add_argument("--headless=new")

    driver_path = str(EDGE_DRIVER_PATH) if EDGE_DRIVER_PATH.exists() else "msedgedriver"

    try:
        service = EdgeService(executable_path=driver_path)
        driver  = webdriver.Edge(service=service, options=options)
        return driver
    except Exception as e:
        raise RuntimeError(
            "Edge failed to start: {}\n"
            "1. Download msedgedriver matching your Edge version from:\n"
            "   https://developer.microsoft.com/en-us/microsoft-edge/tools/webdriver/\n"
            "2. Place msedgedriver.exe next to this script or on your PATH.".format(e)
        ) from e


# ── Safari driver (macOS) ─────────────────────────────────────────────────────

def _build_safari_driver() -> webdriver.Safari:
    """
    Safari's WebDriver ships with macOS (/usr/bin/safaridriver) — no
    separate driver download needed. One-time setup on the Mac:
        1. Safari -> Settings -> Advanced -> enable "Show Develop menu"
        2. Develop -> Allow Remote Automation
        3. Run once in Terminal: safaridriver --enable
    Note: Safari does not support headless mode or Chromium-style options.
    """
    try:
        driver = webdriver.Safari()
        driver.set_window_size(1280, 900)
        return driver
    except Exception as e:
        raise RuntimeError(
            "Safari failed to start: {}\n"
            "Run 'safaridriver --enable' and enable "
            "Develop -> Allow Remote Automation in Safari, then retry.".format(e)
        ) from e


# ── Driver factory — picks browser based on the host OS ───────────────────────

def _build_driver():
    system = platform.system()
    if system == "Darwin":
        log.info("[DRIVER] macOS detected -> using Safari")
        return _build_safari_driver()
    if system == "Windows":
        log.info("[DRIVER] Windows detected -> using Edge")
        return _build_edge_driver()
    log.warning("[DRIVER] Unrecognized OS '%s' -> defaulting to Edge", system)
    return _build_edge_driver()


# ── JS helpers ─────────────────────────────────────────────────────────────────

def _js_set(driver, el, val):
    """Set value on <input> or <textarea> via native JS setter and fire events."""
    driver.execute_script(
        """
        var el  = arguments[0];
        var val = arguments[1];
        var proto = el.tagName === 'TEXTAREA'
            ? window.HTMLTextAreaElement.prototype
            : window.HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, val);
        el.dispatchEvent(new Event('input',  {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        """,
        el, val,
    )


def _js_click(driver, el):
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center'}); arguments[0].click();",
        el,
    )


# ── Login ──────────────────────────────────────────────────────────────────────

def _login(driver, username, password):
    log.info("[LOGIN] Opening login page")
    driver.get(LOGIN_URL)

    WebDriverWait(driver, 15).until(
        EC.presence_of_element_located((By.XPATH,
            '//input[@type="email"] | //input[@name="email"] | (//input)[1]'
        ))
    )

    result = driver.execute_script(
        """
        function setVal(el, val) {
            var proto = el.tagName === 'TEXTAREA'
                ? window.HTMLTextAreaElement.prototype
                : window.HTMLInputElement.prototype;
            Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, val);
            el.dispatchEvent(new Event('input',  {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        }
        var email = document.querySelector(
            'input[type="email"], input[name="email"], input[type="text"]'
        );
        var pwd = document.querySelector('input[type="password"]');
        var btn = document.querySelector(
            'button[type="submit"], input[type="submit"], button'
        );
        if (!email || !pwd) return "FIELDS_NOT_FOUND";
        setVal(email, arguments[0]);
        setVal(pwd,   arguments[1]);
        if (btn) { btn.click(); return "SUBMITTED"; }
        return "NO_BUTTON";
        """,
        username, password,
    )

    log.info("[LOGIN] JS fill+submit result: %s", result)

    if result == "FIELDS_NOT_FOUND":
        raise RuntimeError("[LOGIN] Could not find email/password fields via JS")

    if result == "NO_BUTTON":
        try:
            driver.find_element(By.XPATH, '//input[@type="password"]').send_keys(Keys.RETURN)
        except Exception:
            pass

    try:
        WebDriverWait(driver, 15).until(lambda d: "login" not in d.current_url)
        log.info("[LOGIN] OK -> %s", driver.current_url)
    except TimeoutException:
        raise RuntimeError(
            "[LOGIN] Still on login page — check kite_dev credentials\nURL: {}".format(
                driver.current_url
            )
        )


# ── Profile update ─────────────────────────────────────────────────────────────

def _update_ip_on_profile(driver, current_ips):
    log.info("[PROFILE] Loading profile page")
    driver.get(PROFILE_URL)

    wait = WebDriverWait(driver, 15)
    try:
        ip_box = wait.until(EC.presence_of_element_located((By.XPATH,
            '//*[@id="id_ip_addresses"] | //textarea[@name="ip_addresses"]'
        )))
    except TimeoutException:
        raise RuntimeError("[PROFILE] IP Whitelist textarea not found")

    portal_raw  = (ip_box.get_attribute("value") or ip_box.text or "").strip()
    portal_set  = set(ln.strip() for ln in portal_raw.splitlines()  if ln.strip())
    desired_set = set(ln.strip() for ln in current_ips.splitlines() if ln.strip())

    log.info("[PROFILE] Portal  IPs: %s", portal_set)
    log.info("[PROFILE] Current IPs: %s", desired_set)

    if portal_set == desired_set:
        log.info("[PROFILE] Already up-to-date")
        return False

    _js_set(driver, ip_box, current_ips)

    try:
        btn = wait.until(EC.element_to_be_clickable((By.XPATH,
            '//button[contains(text(),"Update")] | //input[@type="submit"] | //button[@type="submit"]'
        )))
        _js_click(driver, btn)
    except TimeoutException:
        driver.execute_script("arguments[0].closest('form').submit();", ip_box)

    try:
        WebDriverWait(driver, 8).until(EC.staleness_of(ip_box))
    except TimeoutException:
        pass

    log.info("[PROFILE] Updated: %s", desired_set)
    return True


# ── Public API ─────────────────────────────────────────────────────────────────

def ensure_ip_whitelisted() -> bool:
    """
    1. Fetch current public IPs (IPv4 + IPv6 in parallel)
    2. Compare with runtime.last_known_ip stored in config.yaml
    3. If different (or first run): login to portal, update whitelist, save new IP
    4. If same: skip entirely — no browser opened
    Returns True if portal was updated, False if already current.
    """
    cfg      = load_config()
    dev_cfg  = cfg.get("kite_dev") or cfg.get("kite", {})
    username = dev_cfg.get("username") or dev_cfg.get("user_id") or ""
    password = dev_cfg.get("password") or ""

    if not username or not password:
        raise RuntimeError("kite_dev.username / kite_dev.password missing in config.yaml")

    current_ips = get_current_ips()

    # Compare with last known IP stored in config.yaml
    runtime     = cfg.get("runtime") or {}
    last_ips    = str(runtime.get("last_known_ip") or "")
    current_set = set(ln.strip() for ln in current_ips.splitlines() if ln.strip())
    last_set    = set(ln.strip() for ln in last_ips.splitlines()    if ln.strip())

    if current_set == last_set and last_ips.strip():
        log.info("[IP CHECK] No change %s — skipping portal update", current_set)
        return False

    log.info("[IP CHECK] IP changed: %s -> %s", last_set, current_set)

    # Login and update the portal
    driver = _build_driver()
    try:
        _login(driver, username, password)
        updated = _update_ip_on_profile(driver, current_ips)
    except WebDriverException as e:
        raise RuntimeError("Browser error: {}".format(e)) from e
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    # Save new IP to config.yaml regardless of whether portal needed updating
    save_last_known_ip(current_ips)
    log.info("[IP CHECK] Saved new IP to config.yaml: %s", current_set)
    log.info("[IP UPDATE] Done")
    return updated


# ── Standalone ─────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    print("[ip_update] Config: {}".format(CONFIG_FILE))
    try:
        updated = ensure_ip_whitelisted()
        print("IPs updated" if updated else "Already up-to-date")
    except RuntimeError as e:
        print("ERROR: {}".format(e))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
# -- Background scheduler -- call this from your production app ----------------

def start_ip_watcher():
    import time
    import threading

    def _watch_loop():
        while True:
            try:
                cfg      = load_config()
                interval = int(cfg.get('ip_update', {}).get('check_interval_minutes', 15))
            except Exception:
                interval = 15

            try:
                updated = ensure_ip_whitelisted()
                if updated:
                    log.info('[IP WATCHER] IP changed -- portal and config.yaml updated')
                else:
                    log.info('[IP WATCHER] No change -- already up-to-date')
            except Exception as e:
                log.error('[IP WATCHER] Error: %s -- will retry in %d min', e, interval)

            log.info('[IP WATCHER] Next check in %d minute(s)', interval)
            time.sleep(interval * 60)

    t = threading.Thread(target=_watch_loop, name='ip-watcher', daemon=True)
    t.start()
    log.info('[IP WATCHER] Started')
    return t
