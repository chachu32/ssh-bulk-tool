"""FortiGate web UI upgrade automation (Chrome/Selenium). For SSH/CLI use main.py."""
import csv
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from urllib.request import Request, urlopen

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


# ---------- Config ----------
DEVICE_PORT = 8080
PING_TIMEOUT_MS = 1200
PAGE_WAIT_S = 20
POST_UPGRADE_RECOVERY_TIMEOUT_S = 15 * 60
RECOVERY_POLL_S = 5
ARTIFACTS_DIR = "web_artifacts"
FIRMWARE_CACHE_DIR = "firmware_cache"

# One-by-one only, to avoid multiple branches down at once.
ROLLING_MODE = True
AUTO_BACKUP_CLICK = True
AUTO_UPGRADE_CLICK = True

# CSV input with columns:
# ip,username,password,firmware_file
BRANCHES_CSV = "branches.csv"

# CSS selectors are configurable because FortiGate UI can vary by version/theme.
SELECTORS = {
    "username": "input[name='username'], input[placeholder='Username'], input[type='text']",
    "password": "input[name='secretkey'], input[name='password'], input[placeholder='Password'], input[type='password']",
    "login_button": "button[type='submit'], input[type='submit'], button",
    "file_input": "input[type='file']",
    # This click is intentionally broad; if it is unsafe in your UI, disable it by setting to "".
    "upload_or_upgrade_button": "button, input[type='button'], input[type='submit']",
}

BACKUP_HINT_WORDS = ["backup", "configuration backup", "download", "save"]
FIRMWARE_HINT_WORDS = ["firmware", "system", "maintenance", "upgrade"]
UPGRADE_HINT_WORDS = ["upload", "upgrade", "firmware", "ok", "confirm", "install", "proceed"]


def ping_ok(ip: str) -> bool:
    try:
        r = subprocess.run(
            ["ping", "-n", "1", "-w", str(PING_TIMEOUT_MS), ip],
            capture_output=True,
            text=True,
        )
        return r.returncode == 0
    except Exception:
        return False


def wait_for_ping(ip: str, timeout_s: int) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if ping_ok(ip):
            return True
        time.sleep(RECOVERY_POLL_S)
    return False


def is_url(value: str) -> bool:
    v = (value or "").strip().lower()
    return v.startswith("http://") or v.startswith("https://")


def onedrive_direct_download_url(url: str) -> str:
    """
    Convert common OneDrive/SharePoint links to direct-download style.
    Safe fallback: if conversion doesn't apply, return original URL.
    """
    try:
        p = urlparse(url)
        query = dict(parse_qsl(p.query, keep_blank_values=True))
        query["download"] = "1"
        return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(query), p.fragment))
    except Exception:
        return url


def filename_from_url(url: str) -> str:
    p = urlparse(url)
    name = Path(p.path).name.strip()
    if name:
        return name
    return f"firmware_{int(time.time())}.out"


def resolve_firmware_source(source: str, ip: str) -> Path:
    """
    Accepts either:
    - local file path
    - HTTP/HTTPS URL (including OneDrive share links)
    Returns a local file path to upload.
    """
    source = (source or "").strip()
    if not source:
        raise ValueError("firmware source is empty")

    if not is_url(source):
        p = Path(source)
        if not p.exists():
            raise FileNotFoundError(f"firmware file not found: {source}")
        return p

    Path(FIRMWARE_CACHE_DIR).mkdir(parents=True, exist_ok=True)
    dl_url = onedrive_direct_download_url(source)
    name = filename_from_url(dl_url)
    # Prefix by IP to avoid collisions when same filename used across branches.
    target = Path(FIRMWARE_CACHE_DIR) / f"{ip}_{name}"
    if target.exists() and target.stat().st_size > 0:
        return target

    req = Request(
        dl_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        },
    )
    with urlopen(req, timeout=300) as resp:
        data = resp.read()
    if not data:
        raise RuntimeError("downloaded firmware is empty")
    target.write_bytes(data)
    return target


def make_driver() -> webdriver.Chrome:
    options = Options()
    # Needed for self-signed certificate environments.
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--allow-insecure-localhost")
    # Keep visible browser (operator can intervene).
    options.add_experimental_option("detach", False)
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


def first_visible(wait: WebDriverWait, css: str):
    for sel in [s.strip() for s in css.split(",") if s.strip()]:
        try:
            return wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, sel)))
        except TimeoutException:
            continue
    raise TimeoutException(f"No visible element found for selectors: {css}")


def click_probable_button(driver: webdriver.Chrome, words: list[str]) -> bool:
    candidates = driver.find_elements(By.CSS_SELECTOR, SELECTORS["upload_or_upgrade_button"])
    words_lower = [w.lower() for w in words]
    for el in candidates:
        txt = (el.text or el.get_attribute("value") or "").strip().lower()
        if any(w in txt for w in words_lower):
            try:
                el.click()
                return True
            except Exception:
                continue
    return False


def click_by_text_xpath(driver: webdriver.Chrome, words: list[str]) -> bool:
    words = [w.strip().lower() for w in words if w.strip()]
    if not words:
        return False
    # Scan common clickable nodes by visible text.
    clickable_xpath = (
        "//button | //a | //span | //div[@role='button'] | "
        "//input[@type='button'] | //input[@type='submit']"
    )
    for el in driver.find_elements(By.XPATH, clickable_xpath):
        txt = (el.text or el.get_attribute("value") or "").strip().lower()
        if not txt:
            continue
        if any(w in txt for w in words):
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                time.sleep(0.2)
                el.click()
                return True
            except Exception:
                continue
    return False


def save_artifact(driver: webdriver.Chrome, ip: str, step: str):
    Path(ARTIFACTS_DIR).mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    png = Path(ARTIFACTS_DIR) / f"{ip}_{step}_{stamp}.png"
    html = Path(ARTIFACTS_DIR) / f"{ip}_{step}_{stamp}.html"
    try:
        driver.save_screenshot(str(png))
    except Exception:
        pass
    try:
        html.write_text(driver.page_source, encoding="utf-8")
    except Exception:
        pass


def run_branch(ip: str, username: str, password: str, firmware_file: str) -> tuple[bool, str]:
    if not ping_ok(ip):
        return False, "SKIP: unreachable (ping failed)"

    try:
        fw = resolve_firmware_source(firmware_file, ip)
    except Exception as e:
        return False, f"SKIP: firmware source error: {e}"

    driver = make_driver()
    wait = WebDriverWait(driver, PAGE_WAIT_S)

    try:
        url = f"https://{ip}:{DEVICE_PORT}"
        driver.get(url)

        # Login
        user_el = first_visible(wait, SELECTORS["username"])
        pass_el = first_visible(wait, SELECTORS["password"])
        user_el.clear()
        user_el.send_keys(username)
        pass_el.clear()
        pass_el.send_keys(password)

        clicked = click_probable_button(driver, ["login", "sign in"])
        if not clicked:
            pass_el.submit()

        # Give UI a moment after login.
        time.sleep(2)

        # Attempt to navigate/click backup path; keep manual checkpoint for safety.
        if AUTO_BACKUP_CLICK:
            click_by_text_xpath(driver, FIRMWARE_HINT_WORDS)
            time.sleep(1)
            click_by_text_xpath(driver, BACKUP_HINT_WORDS)
            time.sleep(1)
        save_artifact(driver, ip, "pre_backup_confirm")
        input(f"\n[{ip}] Confirm backup is completed in GUI, then press Enter to continue firmware upload...")

        # Upload firmware file
        file_input = first_visible(wait, SELECTORS["file_input"])
        file_input.send_keys(str(fw.resolve()))
        time.sleep(1)

        # Try to click a common upgrade/upload button; if not found, operator can finish manually.
        clicked_upgrade = False
        if AUTO_UPGRADE_CLICK:
            clicked_upgrade = click_by_text_xpath(driver, UPGRADE_HINT_WORDS)
            if not clicked_upgrade:
                clicked_upgrade = click_probable_button(driver, UPGRADE_HINT_WORDS)

        if not clicked_upgrade:
            input(f"[{ip}] Could not auto-click upgrade button. Click it manually in browser, then press Enter...")
        save_artifact(driver, ip, "post_upgrade_click")

        # Wait for reboot and recovery
        print(f"[{ip}] Waiting for device to go down/up and recover ping...")
        recovered = wait_for_ping(ip, POST_UPGRADE_RECOVERY_TIMEOUT_S)
        if not recovered:
            return False, "Upgrade sent, but device did not recover ping before timeout"
        return True, "Upgrade flow completed and ping recovered"

    except (NoSuchElementException, TimeoutException) as e:
        return False, f"UI automation failed: {e}"
    except Exception as e:
        return False, f"Unexpected error: {e}"
    finally:
        driver.quit()


def load_branches(csv_path: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        required = {"ip", "username", "password", "firmware_file"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError(f"CSV must contain columns: {', '.join(sorted(required))}")
        for row in reader:
            ip = (row.get("ip") or "").strip()
            if not ip or ip.startswith("#"):
                continue
            rows.append(
                {
                    "ip": ip,
                    "username": (row.get("username") or "").strip(),
                    "password": (row.get("password") or "").strip(),
                    "firmware_file": (row.get("firmware_file") or "").strip(),
                }
            )
    return rows


def main():
    branches = load_branches(BRANCHES_CSV)
    if not branches:
        print("No branches found in branches.csv")
        return

    print(f"Loaded {len(branches)} branch entries. Rolling mode: {ROLLING_MODE}")
    print("Starting one-by-one UI automation. Keep watching browser for each branch.")

    results: list[tuple[str, bool, str]] = []
    for b in branches:
        ip = b["ip"]
        print(f"\n--- Processing {ip} ---")
        ok, msg = run_branch(ip, b["username"], b["password"], b["firmware_file"])
        results.append((ip, ok, msg))
        print(f"[{ip}] {'OK' if ok else 'FAIL'} - {msg}")

        # Explicit operator checkpoint between branches to avoid accidental overlap.
        if ROLLING_MODE:
            input("Press Enter to continue to next branch...")

    print("\n=== SUMMARY ===")
    for ip, ok, msg in results:
        print(f"{ip}: {'OK' if ok else 'FAIL'} - {msg}")


if __name__ == "__main__":
    main()
