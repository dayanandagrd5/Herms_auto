import os
import sys
import json
import time
import platform
import pyotp
from datetime import date
from urllib.parse import urlparse, parse_qs
from kiteconnect import KiteConnect
from termcolor import cprint

from selenium import webdriver
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException

# =========================================================
# PATHS
# =========================================================
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
TOKEN_FILE  = os.path.join(BASE_DIR, "access_token.json")
DRIVER_PATH = os.path.join(BASE_DIR, "msedgedriver.exe")


# =========================================================
# TOKEN CACHE  (JSON with date stamp)
# access_token.json format:
#   { "date": "20250530", "access_token": "xxxx..." }
#
# Token is valid for the calendar day it was generated (IST).
# Kite invalidates all tokens at midnight IST, so date == today
# is the only check needed — no API round-trip required.
# =========================================================

def _load_cached_token() -> str | None:
    """Return today's cached access token, or None if absent / expired."""
    if not os.path.exists(TOKEN_FILE):
        return None
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        today = date.today().strftime("%Y%m%d")
        if data.get("date") == today and data.get("access_token"):
            cprint(f"✅ Valid cached token found for {today}. Skipping browser login.", "green")
            return data["access_token"]
        cprint("⚠️  Cached token is from a previous day. Re-logging in...", "yellow")
    except Exception as e:
        cprint(f"⚠️  Could not read token cache: {e}", "yellow")
    return None


def _save_token(access_token: str):
    """Persist today's access token to access_token.json."""
    data = {
        "date":         date.today().strftime("%Y%m%d"),
        "access_token": access_token
    }
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    cprint(f"💾 Token saved → {TOKEN_FILE}", "green")


def _delete_token_file():
    try:
        os.remove(TOKEN_FILE)
    except OSError:
        pass

# =========================================================
# TOTP
# =========================================================
def get_live_totp_token(totp_secret: str) -> str | None:
    try:
        token = pyotp.TOTP(totp_secret.replace(" ", "").upper()).now()
        cprint(f"⚙️  TOTP Generated: [ {token} ]", "cyan")
        return token
    except Exception as e:
        cprint(f"❌ TOTP failed: {e}", "red")
        return None





# =========================================================
# EDGE DRIVER (Windows)
# =========================================================
def _build_edge_driver() -> webdriver.Edge:
    options = EdgeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,900")
    options.add_argument("--log-level=3")
    options.add_experimental_option("excludeSwitches", ["enable-logging"])

    if os.path.exists(DRIVER_PATH):
        try:
            driver = webdriver.Edge(
                service=EdgeService(executable_path=DRIVER_PATH),
                options=options
            )
            cprint("✅ Edge launched (local driver).", "green")
            return driver
        except Exception as e:
            cprint(f"⚠️  Local driver failed: {e}", "yellow")

    try:
        driver = webdriver.Edge(options=options)
        cprint("✅ Edge launched (PATH driver).", "green")
        return driver
    except Exception as e:
        cprint(f"❌ Edge failed to start: {e}", "red")
        sys.exit(1)


# =========================================================
# SAFARI DRIVER (macOS)
# =========================================================
def _build_safari_driver() -> webdriver.Safari:
    """
    Safari's WebDriver ships with macOS (/usr/bin/safaridriver) — no
    separate driver download needed. One-time setup on the Mac:
        1. Safari -> Settings -> Advanced -> enable "Show Develop menu"
        2. Develop -> Allow Remote Automation
        3. Run once in Terminal: safaridriver --enable
    Note: Safari does not support headless mode or Chromium-style
    --no-sandbox / --disable-gpu style options.
    """
    try:
        driver = webdriver.Safari()
        driver.set_window_size(1280, 900)
        cprint("✅ Safari launched.", "green")
        return driver
    except Exception as e:
        cprint(f"❌ Safari failed to start: {e}", "red")
        cprint(
            "   Run 'safaridriver --enable' and enable "
            "Develop -> Allow Remote Automation in Safari, then retry.",
            "yellow",
        )
        sys.exit(1)


# =========================================================
# DRIVER FACTORY — picks browser based on the host OS
# =========================================================
def _build_driver():
    system = platform.system()
    if system == "Darwin":
        cprint("🍎 macOS detected → using Safari.", "blue")
        return _build_safari_driver()
    if system == "Windows":
        cprint("🪟 Windows detected → using Edge.", "blue")
        return _build_edge_driver()
    cprint(f"⚠️  Unrecognized OS '{system}' → defaulting to Edge.", "yellow")
    return _build_edge_driver()


# =========================================================
# WAIT HELPERS
# =========================================================
def _wait_visible(driver, by, locator, timeout=20):
    return WebDriverWait(driver, timeout).until(
        EC.visibility_of_element_located((by, locator))
    )

def _wait_clickable(driver, by, locator, timeout=20):
    return WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((by, locator))
    )

def _retry_on_stale(fn, retries=4, delay=0.4):
    """Run fn(), re-trying if the page re-renders and the element goes stale."""
    last_exc = None
    for _ in range(retries):
        try:
            return fn()
        except StaleElementReferenceException as e:
            last_exc = e
            time.sleep(delay)
    raise last_exc

def _js_click(driver, element):
    driver.execute_script("arguments[0].click();", element)

def _js_set_value(driver, element, value):
    """Set input value via JS and fire React/Vue change events."""
    driver.execute_script(
        """
        var nativeInputValueSetter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value').set;
        nativeInputValueSetter.call(arguments[0], arguments[1]);
        arguments[0].dispatchEvent(new Event('input', { bubbles: true }));
        arguments[0].dispatchEvent(new Event('change', { bubbles: true }));
        """,
        element,
        value,
    )

def _dump_buttons(driver):
    cprint("   ── All buttons in DOM ──", "magenta")
    buttons = driver.find_elements(By.TAG_NAME, "button")
    for i, b in enumerate(buttons):
        cprint(
            f"   [{i}] text={b.text!r:20} "
            f"type={b.get_attribute('type')!r:12} "
            f"class={b.get_attribute('class')!r}",
            "white",
        )
    return buttons

def _already_redirected(driver) -> bool:
    url = driver.current_url
    return (
        "request_token" in url
        or "127.0.0.1" in url
        or "localhost" in url
    )


# =========================================================
# SUBMIT TOTP  (returns True if redirect was detected)
# =========================================================
def _submit_totp_and_wait(driver, totp_xpath, pin: str) -> bool:
    """
    Kite's TOTP page behaviour varies:
      A) Auto-submits once 6 digits are keyed in  (no button click needed)
      B) Requires Enter / button click

    Strategy:
      1. Clear & type digits one-by-one with tiny delay (triggers React state).
      2. Wait up to 3 s for auto-redirect (covers case A).
      3. If still on TOTP page, press Enter (covers case B).
      4. Wait again.
      5. If still stuck, find & JS-click any visible submit button.

    The TOTP field is re-located by XPath before every interaction (and
    retried on StaleElementReferenceException) because Kite's page
    re-renders the form right after the field becomes visible, which
    invalidates any cached WebElement reference.
    """
    def get_field():
        return _wait_visible(driver, By.XPATH, totp_xpath, timeout=10)

    # --- 1. Type TOTP digit-by-digit via JS setter then key strokes --------
    def _type_pin():
        field = get_field()
        field.click()
        field.clear()
        # Use JS to set value so React/Vue state registers the full string
        _js_set_value(driver, field, pin)
        # Then also send the keys so the native input events fire properly
        field.send_keys(pin)

    _retry_on_stale(_type_pin)
    cprint(f"   ✓ TOTP entered: {pin}", "white")
    time.sleep(1.0)   # let React debounce settle

    # --- 2. Check for auto-redirect (Kite sometimes submits automatically) --
    try:
        WebDriverWait(driver, 5).until(lambda d: _already_redirected(d))
        cprint("   ✓ TOTP auto-submitted — redirect detected.", "green")
        return True
    except Exception:
        pass

    # --- 3. Press Enter on the TOTP field -----------------------------------
    try:
        _retry_on_stale(lambda: get_field().send_keys(Keys.RETURN))
        cprint("   ✓ Pressed Enter on TOTP field.", "white")
    except Exception:
        pass

    try:
        WebDriverWait(driver, 5).until(lambda d: _already_redirected(d))
        cprint("   ✓ Redirect after Enter.", "green")
        return True
    except Exception:
        pass

    # --- 4. Try submit buttons (skip "Refresh" — that's an error control) ---
    cprint("   ⚠️  Enter didn't redirect — scanning buttons...", "yellow")
    _dump_buttons(driver)

    button_xpaths = [
        '//button[normalize-space(text())="Continue"]',
        '//button[contains(text(),"Continue")]',
        '//button[normalize-space(text())="Verify"]',
        '//button[contains(text(),"Verify")]',
        '//button[contains(@class,"button-orange")]',
        '//button[contains(@class,"blue-button") and not(contains(text(),"Refresh"))]',
        # last-resort: any submit button that is NOT the Refresh button
        '//button[@type="submit" and not(contains(translate(text(),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"refresh"))]',
    ]

    for xpath in button_xpaths:
        try:
            btn = WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable((By.XPATH, xpath))
            )
            _js_click(driver, btn)
            cprint(f"   ✓ Clicked: {xpath}", "white")
            try:
                WebDriverWait(driver, 5).until(lambda d: _already_redirected(d))
                return True
            except Exception:
                pass   # try next selector
        except Exception:
            continue

    # --- 5. Nuclear: submit the parent form via JS --------------------------
    try:
        driver.execute_script(
            "arguments[0].closest('form') && arguments[0].closest('form').submit();",
            _retry_on_stale(get_field),
        )
        cprint("   ✓ JS form.submit() called.", "white")
        try:
            WebDriverWait(driver, 5).until(lambda d: _already_redirected(d))
            return True
        except Exception:
            pass
    except Exception:
        pass

    return False


# =========================================================
# LOGIN
# =========================================================
def initialize_kite_session() -> KiteConnect:

    # ── Config ────────────────────────────────────────────────────────────────
    if not os.path.exists(CONFIG_FILE):
        cprint(f"❌ Config missing: {CONFIG_FILE}", "red"); sys.exit(1)
    try:
        with open(CONFIG_FILE) as f:
            config = json.load(f)
    except Exception as e:
        cprint(f"❌ Config load error: {e}", "red"); sys.exit(1)

    for key in ["api_key", "api_secret", "user_id", "password", "totp_secret"]:
        if key not in config:
            cprint(f"❌ '{key}' missing from config.json", "red"); sys.exit(1)

    kite = KiteConnect(api_key=config["api_key"])

    # ── Cached token (date-stamped JSON, no API call needed) ─────────────────
    cached = _load_cached_token()
    if cached:
        kite.set_access_token(cached)
        return kite

    # ── Browser login ─────────────────────────────────────────────────────────
    cprint("🚀 Launching browser...", "blue")
    driver = _build_driver()

    try:
        # ── Step 1: Load page ─────────────────────────────────────────────────
        driver.get(kite.login_url())
        WebDriverWait(driver, 15).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        cprint(f"   ✓ Page loaded: '{driver.title}'", "white")

        # ── Step 2: User ID ───────────────────────────────────────────────────
        _wait_visible(driver, By.ID, "userid").send_keys(config["user_id"])
        cprint(f"   ✓ User ID: {config['user_id']}", "white")

        # ── Step 3: Password ──────────────────────────────────────────────────
        pwd_field = _wait_visible(driver, By.ID, "password")
        pwd_field.send_keys(config["password"])
        cprint("   ✓ Password entered.", "white")

        # ── Step 4: Submit credentials ────────────────────────────────────────
        try:
            _wait_clickable(driver, By.XPATH, '//button[@type="submit"]').click()
            cprint("   ✓ Login submitted (button).", "white")
        except Exception:
            pwd_field.send_keys(Keys.RETURN)
            cprint("   ✓ Login submitted (Enter).", "white")

        # ── Step 5: Wait for TOTP page ────────────────────────────────────────
        cprint("   Waiting for TOTP page...", "white")
        time.sleep(1.5)

        # ── Step 6: Find TOTP field ───────────────────────────────────────────
        totp_field = None
        totp_xpath_used = None
        totp_xpaths = [
            '//input[@label="External TOTP"]',
            '//input[@placeholder="External TOTP"]',
            '//label[contains(text(),"External TOTP")]/following-sibling::input',
            '//div[contains(text(),"External TOTP")]/following::input[1]',
            '//label[contains(text(),"TOTP")]/following-sibling::input',
            '//input[@type="number"]',
            '//input[@maxlength="6"]',
            '//input[@autocomplete="one-time-code"]',
            '(//input)[last()]',
        ]

        for xpath in totp_xpaths:
            try:
                totp_field = _wait_visible(driver, By.XPATH, xpath, timeout=15)
                if totp_field.get_attribute("id") == "password":
                    totp_field = None
                    continue
                totp_xpath_used = xpath
                cprint(f"   ✓ TOTP field found: {xpath}", "white")
                break
            except Exception:
                continue

        if totp_field is None:
            raise RuntimeError("Could not locate TOTP input field.")

        # ── Step 7: Generate & enter TOTP, then submit ────────────────────────
        pin = get_live_totp_token(config["totp_secret"])
        if not pin:
            raise RuntimeError("TOTP returned None.")

        submitted = _submit_totp_and_wait(driver, totp_xpath_used, pin)

        if not submitted:
            cprint("   ⚠️  First TOTP attempt failed. Waiting for next 30-s window...", "yellow")
            time.sleep(15)
            pin2 = get_live_totp_token(config["totp_secret"])
            if pin2 and pin2 != pin:
                try:
                    submitted = _submit_totp_and_wait(driver, totp_xpath_used, pin2)
                except Exception:
                    pass

        if not submitted:
            raise RuntimeError(
                "TOTP submitted but no redirect to request_token URL.\n"
                "Check totp_submit_failed.png — the TOTP may be wrong or "
                "the redirect_url in your Kite app is not http://127.0.0.1"
            )

        # ── Step 8: Catch redirect URL ────────────────────────────────────────
        cprint("   Waiting for request_token in redirect URL...", "white")
        WebDriverWait(driver, 30).until(
            lambda d: "request_token" in d.current_url
        )

        current_url = driver.current_url
        cprint(f"   ✓ Redirect URL: {current_url}", "white")

        # ── Step 9: Parse request_token ───────────────────────────────────────
        params = parse_qs(urlparse(current_url).query)
        if "request_token" not in params:
            raise RuntimeError(
                f"request_token not in URL: {current_url}\n"
                "Ensure your Kite app redirect_url is http://127.0.0.1"
            )
        request_token = params["request_token"][0]
        cprint(f"📥 request_token: [ {request_token} ]", "green")

        # ── Step 10: Generate & save access token ─────────────────────────────
        session      = kite.generate_session(request_token, api_secret=config["api_secret"])
        access_token = session["access_token"]
        _save_token(access_token)           # saves with today's date stamp
        kite.set_access_token(access_token)
        cprint("🎯 Authenticated! Token saved to access_token.json.", "green")
        return kite

    except Exception as e:
        cprint(f"❌ Login failed: {e}", "red")
        sys.exit(1)

    finally:
        try: driver.quit()
        except Exception: pass


# =========================================================
# RUN
# =========================================================
if __name__ == "__main__":
    cprint("=== PROGRAMMATIC HANDS-FREE KITE AUTHENTICATOR ===", "magenta")
    kite = initialize_kite_session()
    cprint(f"🟢 Kite session ready. User: {kite.profile()['user_name']}", "green")