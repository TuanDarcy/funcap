"""
Roblox FunCAPTCHA Collector — Standalone v2
=============================================
Logic dung: SPAM CAPTCHA -> chup -> reload -> chup tiep -> ... -> het luot thi qua account khac.
KHONG can giai CAPTCHA, KHONG can login thanh cong.
Muc tieu duy nhat: thu thap CANG NHIEU anh CAPTCHA cang tot.

Flow moi account:
  1. Mo browser -> vao Roblox login
  2. Dien user:pass -> bam Login -> FunCAPTCHA xuat hien
  3. Click vao CAPTCHA de reveal game (neu can)
  4. Chup anh game -> detect loai -> luu vao folder
  5. Reload trang -> lap lai N lan (captchas_per_account)
  6. Dong browser -> qua account tiep theo

Cau truc:
  roblox_collector/
    input/
      accounts.txt     <- username:password (moi dong 1 account)
      proxies.txt      <- ip:port hoac user:pass@ip:port
      config.json      <- cau hinh (xem DEFAULT_CONFIG ben duoi)
    captured/
      rotate_animal/   <- anh CAPTCHA tu dong phan loai theo folder
      shadow_match/
      ...
    collector.py       <- script nay
"""

import asyncio
import json
import os
import sys
import time
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from loguru import logger
except ImportError:
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    logger = logging.getLogger("collector")

# --- Paths ---
BASE_DIR = Path(__file__).parent.resolve()
INPUT_DIR = BASE_DIR / "input"
CAPTURED_DIR = BASE_DIR / "captured"
CONFIG_PATH = INPUT_DIR / "config.json"
ACCOUNTS_PATH = INPUT_DIR / "accounts.txt"
PROXIES_PATH = INPUT_DIR / "proxies.txt"

# --- Default config ---
DEFAULT_CONFIG = {
    # -- Threading --
    "threads": 5,

    # -- Collection loop --
    "captchas_per_account": 20,       # Moi account thu thap bao nhieu CAPTCHA roi next
    "reload_delay_sec": 2.0,          # Delay sau khi reload trang

    # -- Browser --
    "headless": True,
    "viewport_width": 500,
    "viewport_height": 700,

    # -- Proxy --
    "use_proxy": True,                # Co dung proxy khong
    "proxy_mode": "per_tab",          # "per_tab" | "per_account" | "round_robin"

    # -- CAPTCHA --
    "captcha_timeout_sec": 15,        # Thoi gian doi CAPTCHA load (tang neu proxy cham)
    "click_to_reveal": True,          # Co can click vao CAPTCHA de hien game khong
    "click_delay_sec": 2.0,           # Delay sau khi click CAPTCHA

    # -- Login form selectors --
    "login_selectors": {
        "username": "#login-username",
        "password": "#login-password",
        "submit_button": "#login-button",
        "fallback_username": "input[name=\"username\"]",
        "fallback_password": "input[name=\"password\"]",
        "fallback_submit": "button[type=\"submit\"]",
    },

    # -- CAPTCHA selectors --
    "captcha_selectors": {
        "iframe": "iframe[src*=\"arkose\"], iframe[src*=\"funcaptcha\"], iframe[src*=\"arkoselabs\"]",
        "game_area": "canvas, img[src*=\"game\"], [class*=\"game\"], [class*=\"challenge\"]",
        "click_target": "button, [class*=\"start\"], [class*=\"play\"], [class*=\"begin\"]",
    },
}

# --- Class folders ---
CLASS_FOLDERS = [
    "rotate_animal", "match_object", "select_tiles",
    "shadow_match", "pick_image", "count_objects",
    "unknown", "no_captcha",
]


# ======================================================================
#  COLLECTOR
# ======================================================================

class RobloxCaptchaCollector:
    """Moi instance = 1 browser = 1 proxy (tuy config) = 1 account.
       Vong lap: login -> CAPTCHA -> chup -> reload -> lap N lan."""

    ROBLOX_LOGIN_URL = "https://www.roblox.com/login"

    def __init__(self, username: str, password: str, proxy: Optional[str] = None, config: dict = None):
        self.username = username
        self.password = password
        self.proxy = proxy
        self.cfg = config or DEFAULT_CONFIG
        self.total_captured = 0

    # -- Proxy parser --

    @staticmethod
    def parse_proxy(proxy_str: str) -> Optional[dict]:
        if not proxy_str or proxy_str == "direct":
            return None
        cleaned = proxy_str.strip().replace("http://", "").replace("https://", "").replace("socks5://", "")
        if not cleaned:
            return None
        if "@" in cleaned:
            auth, host = cleaned.split("@", 1)
            user, pwd = auth.split(":", 1)
            return {"server": f"http://{host}", "username": user, "password": pwd}
        return {"server": f"http://{cleaned}"}

    # -- Stealth JS --

    STEALTH_JS = """
        Object.defineProperty(navigator, 'webdriver', { get: () => false });
        Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
        const origQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (p) => (
            p.name === 'notifications' ?
            Promise.resolve({state: Notification.permission}) : origQuery(p)
        );
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 4 });
    """

    # -- Detect game type --

    async def _detect_game_type(self, frame) -> str:
        try:
            text = (await frame.locator("body").inner_text()).lower()
            if "rotate" in text or "upright" in text or "orientation" in text:
                return "rotate_animal"
            if "shadow" in text:
                return "shadow_match"
            if "select" in text and "tile" in text:
                return "select_tiles"
            if "match" in text:
                return "match_object"
            if "pick" in text or "choose" in text:
                return "pick_image"
            if "count" in text or "how many" in text:
                return "count_objects"
        except Exception:
            pass
        return "unknown"

    # -- Fill login form --

    async def _fill_login_form(self, page):
        sel = self.cfg.get("login_selectors", {})
        try:
            await page.fill(sel.get("username", "#login-username"), self.username, timeout=5000)
        except Exception:
            try:
                await page.fill(sel.get("fallback_username", 'input[name="username"]'), self.username, timeout=5000)
            except Exception:
                logger.warning(f"[{self.username}] Khong dien duoc username")

        try:
            await page.fill(sel.get("password", "#login-password"), self.password, timeout=5000)
        except Exception:
            try:
                await page.fill(sel.get("fallback_password", 'input[name="password"]'), self.password, timeout=5000)
            except Exception:
                logger.warning(f"[{self.username}] Khong dien duoc password")

    async def _click_login(self, page):
        sel = self.cfg.get("login_selectors", {})
        try:
            await page.click(sel.get("submit_button", "#login-button"), timeout=5000)
        except Exception:
            try:
                await page.click(sel.get("fallback_submit", 'button[type="submit"]'), timeout=5000)
            except Exception:
                await page.keyboard.press("Enter")

    # -- Click to reveal CAPTCHA game --

    async def _click_reveal_captcha(self, page):
        cap_sel = self.cfg.get("captcha_selectors", {})
        try:
            iframe_el = await page.wait_for_selector(
                cap_sel.get("iframe", 'iframe[src*="arkose"]'), timeout=5000,
            )
            if iframe_el:
                await iframe_el.click(timeout=3000)
                await asyncio.sleep(1)
        except Exception:
            pass

        try:
            iframe_el = await page.wait_for_selector(
                cap_sel.get("iframe", 'iframe[src*="arkose"]'), timeout=3000,
            )
            if iframe_el:
                frame = await iframe_el.content_frame()
                if frame:
                    click_sel = cap_sel.get("click_target", 'button, [class*="start"]')
                    btn = await frame.wait_for_selector(click_sel, timeout=3000)
                    if btn:
                        await btn.click(timeout=3000)
                        logger.debug(f"[{self.username}] Clicked CAPTCHA start button")
        except Exception:
            pass

    # -- Main loop --

    async def run(self) -> List[Dict]:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.error("Chua cai playwright! pip install playwright && playwright install chromium")
            return []

        captured_all = []
        target = self.cfg["captchas_per_account"]

        try:
            async with async_playwright() as p:
                launch_opts = {
                    "headless": self.cfg["headless"],
                    "args": [
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        f"--window-size={self.cfg['viewport_width']},{self.cfg['viewport_height']}",
                    ],
                }

                proxy_cfg = None
                if self.cfg.get("use_proxy", True) and self.proxy:
                    proxy_cfg = self.parse_proxy(self.proxy)
                if proxy_cfg:
                    launch_opts["proxy"] = proxy_cfg

                browser = await p.chromium.launch(**launch_opts)

                for round_num in range(target):
                    context = None
                    try:
                        context = await browser.new_context(
                            viewport={"width": self.cfg["viewport_width"], "height": self.cfg["viewport_height"]},
                            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
                            locale="en-US",
                        )
                        page = await context.new_page()
                        await page.add_init_script(self.STEALTH_JS)

                        # (1) Vao Roblox login
                        await page.goto(self.ROBLOX_LOGIN_URL, wait_until="networkidle", timeout=30000)
                        await asyncio.sleep(2)

                        # (2) Dien form
                        await self._fill_login_form(page)
                        await asyncio.sleep(0.3)

                        # (3) Bam Login
                        await self._click_login(page)

                        # (4) Doi CAPTCHA
                        cap_sel = self.cfg.get("captcha_selectors", {})
                        try:
                            await page.wait_for_selector(
                                cap_sel.get("iframe", 'iframe[src*="arkose"]'),
                                timeout=self.cfg["captcha_timeout_sec"] * 1000,
                            )
                        except Exception:
                            logger.debug(f"[{self.username}] #{round_num+1} Khong thay CAPTCHA, bo qua...")
                            continue

                        await asyncio.sleep(1)

                        # (5) Click de reveal game
                        if self.cfg.get("click_to_reveal", True):
                            await self._click_reveal_captcha(page)
                            await asyncio.sleep(self.cfg.get("click_delay_sec", 2.0))

                        # (6) Chup & detect
                        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
                        captcha_results = await self._capture_iframe(page, round_num, ts)

                        if captcha_results:
                            captured_all.extend(captcha_results)
                            self.total_captured += len(captcha_results)
                            types = set(r["type"] for r in captcha_results)
                            logger.info(
                                f"[{self.username}] #{round_num+1}/{target} "
                                f"Captured {len(captcha_results)} ({', '.join(types)}) "
                                f"| proxy={'yes' if proxy_cfg else 'no'}"
                            )

                    except Exception as e:
                        logger.error(f"[{self.username}] #{round_num+1} error: {e}")
                    finally:
                        if context:
                            try:
                                await context.close()
                            except Exception:
                                pass
                        await asyncio.sleep(self.cfg.get("reload_delay_sec", 2.0))

                await browser.close()

        except Exception as e:
            logger.error(f"[{self.username}] Browser crash: {e}")

        logger.success(f"[{self.username}] Done: {self.total_captured}/{target} captures")
        return captured_all

    # -- Capture CAPTCHA iframe --

    async def _capture_iframe(self, page, round_num: int, ts: str) -> List[Dict]:
        results = []
        cap_sel = self.cfg.get("captcha_selectors", {})

        try:
            iframe_el = await page.wait_for_selector(
                cap_sel.get("iframe", 'iframe[src*="arkose"]'), timeout=5000,
            )
            if not iframe_el:
                return results

            frame = await iframe_el.content_frame()
            if not frame:
                return results

            game_type = await self._detect_game_type(frame)
            type_dir = CAPTURED_DIR / game_type
            type_dir.mkdir(parents=True, exist_ok=True)

            # Chup game area
            game_path = type_dir / f"{self.username}_r{round_num}_{ts}.png"
            try:
                area = await frame.wait_for_selector(
                    cap_sel.get("game_area", 'canvas, [class*="game"]'), timeout=3000,
                )
                if area:
                    await area.screenshot(path=str(game_path))
                else:
                    await frame.screenshot(path=str(game_path))
            except Exception:
                await frame.screenshot(path=str(game_path))

            # Doc instruction
            instruction = None
            try:
                for sel in ['h2', 'h3', '[class*="instruction"]', '.game-instruction', '[class*="header"]']:
                    el = await frame.wait_for_selector(sel, timeout=800)
                    if el:
                        txt = (await el.inner_text()).strip()
                        if txt and len(txt) > 3:
                            instruction = txt
                            break
            except Exception:
                pass

            results.append({
                "image_path": str(game_path),
                "type": game_type,
                "instruction": instruction,
                "username": self.username,
                "round": round_num + 1,
                "ts": ts,
            })

            # Capture tiles
            try:
                tiles = await frame.locator('[class*="tile"], [class*="grid-item"]').all()
                for i, tile in enumerate(tiles[:9]):
                    tp = type_dir / f"{self.username}_r{round_num}_t{i}_{ts}.png"
                    try:
                        await tile.screenshot(path=str(tp))
                        results.append({"image_path": str(tp), "type": f"{game_type}_tile{i}", "username": self.username, "round": round_num + 1, "ts": ts})
                    except Exception:
                        pass
            except Exception:
                pass

        except Exception as e:
            logger.debug(f"[{self.username}] _capture_iframe: {e}")

        return results


# ======================================================================
#  MULTI-THREAD ORCHESTRATOR
# ======================================================================

def _run_async(collector) -> list:
    return asyncio.run(collector.run())


def load_config() -> dict:
    config = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user_config = json.load(f)
            for key, value in user_config.items():
                if isinstance(value, dict) and isinstance(config.get(key), dict):
                    config[key].update(value)
                else:
                    config[key] = value
            logger.info(f"Loaded config: {CONFIG_PATH}")
        except Exception as e:
            logger.error(f"Config error: {e}")
    else:
        INPUT_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
        logger.info(f"Created default config: {CONFIG_PATH}")
    return config


def load_accounts() -> list:
    if not ACCOUNTS_PATH.exists():
        INPUT_DIR.mkdir(parents=True, exist_ok=True)
        ACCOUNTS_PATH.write_text("# Format: username:password\n# user1:pass123\n", encoding="utf-8")
        logger.error(f"Chua co {ACCOUNTS_PATH} -> da tao file mau, hay dien account!")
        sys.exit(1)

    accounts = []
    for line in ACCOUNTS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":", 1)
        if len(parts) != 2:
            logger.warning(f"Bo qua dong loi: {line}")
            continue
        accounts.append((parts[0], parts[1]))
    return accounts


def load_proxies() -> list:
    if PROXIES_PATH.exists():
        proxies = [line.strip() for line in PROXIES_PATH.read_text(encoding="utf-8").splitlines()
                   if line.strip() and not line.strip().startswith("#")]
        if proxies:
            return proxies

    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    PROXIES_PATH.write_text("# Format: ip:port hoac user:pass@ip:port\n# 192.168.1.1:8080\n", encoding="utf-8")
    logger.warning("Chua co proxies -> chay direct (khong proxy)")
    return []


def collect_all():
    config = load_config()
    accounts = load_accounts()
    proxies = load_proxies()

    use_proxy = config.get("use_proxy", True)
    proxy_mode = config.get("proxy_mode", "per_tab")

    if use_proxy and not proxies:
        logger.warning("use_proxy=true nhung khong co proxy -> chay direct")

    # Gan proxy cho account
    if use_proxy and proxies:
        proxy_map = {}
        for i, (u, p) in enumerate(accounts):
            proxy_map[(u, p)] = proxies[i % len(proxies)]
    else:
        proxy_map = {acc: None for acc in accounts}

    # Tao folder output
    CAPTURED_DIR.mkdir(parents=True, exist_ok=True)
    for folder in CLASS_FOLDERS:
        (CAPTURED_DIR / folder).mkdir(parents=True, exist_ok=True)

    # Run
    n_threads = config["threads"]
    n_captchas = config["captchas_per_account"]

    logger.info("=" * 55)
    logger.info(f"FunCAPTCHA Collector v2")
    logger.info(f"  Accounts: {len(accounts)} | Captchas/acc: {n_captchas}")
    logger.info(f"  Threads: {n_threads} | Proxy: {'yes' if use_proxy else 'no'} ({proxy_mode})")
    logger.info(f"  Output: {CAPTURED_DIR}")
    logger.info("=" * 55)

    all_results = []
    lock = threading.Lock()
    done_count = 0

    def process(username, password, proxy):
        collector = RobloxCaptchaCollector(username=username, password=password, proxy=proxy, config=config)
        return _run_async(collector)

    with ThreadPoolExecutor(max_workers=n_threads) as executor:
        futures = {}
        for (username, password), proxy in proxy_map.items():
            futures[executor.submit(process, username, password, proxy)] = username

        for future in as_completed(futures):
            username = futures[future]
            try:
                results = future.result()
                with lock:
                    all_results.extend(results)
                    done_count += 1
                logger.info(f"Progress: {done_count}/{len(accounts)} accounts | {username}: {len(results)} captures")
            except Exception as e:
                with lock:
                    done_count += 1
                logger.error(f"[{username}] Crash: {e}")

    # Save metadata
    meta_path = CAPTURED_DIR / "metadata.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False, default=str)

    # Summary
    from collections import Counter
    type_counts = Counter(r.get("type", "?") for r in all_results)

    print("\n" + "=" * 55)
    print("  HOAN THANH THU THAP")
    print("=" * 55)
    print(f"  Tong anh: {len(all_results)}")
    for t, n in sorted(type_counts.items()):
        bar = "#" * min(n // max(1, len(all_results) // 30), 30)
        print(f"  {t:25s} {n:5d} {bar}")
    print(f"\n  Du lieu: {CAPTURED_DIR}")
    print(f"\n  Buoc tiep theo:")
    print(f"    python ../src/funcap_solver/data/labeler.py -i captured -o ../data/labeled")
    print("=" * 55)


if __name__ == "__main__":
    collect_all()
