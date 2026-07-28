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

# --- Default config (behavior only, no selectors) ---
DEFAULT_CONFIG = {
    "threads": 5,
    "captchas_per_account": 20,
    "reload_delay_sec": 2.0,
    "headless": True,
    "viewport_width": 500,
    "viewport_height": 700,
    "use_proxy": True,
    "proxy_mode": "per_tab",
    "captcha_timeout_sec": 15,
    "click_to_reveal": True,
    "click_delay_sec": 2.0,
    "debug": False,
    "debug_dir": "captured/_debug",
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

    # ═══ CSS Selectors — DOM thật từ F12 của FunCAPTCHA Arkose ═══
    # --- Login form ---
    SEL_USERNAME = "#login-username"
    SEL_PASSWORD = "#login-password"
    SEL_SUBMIT = "#login-button"
    SEL_USERNAME_FB = 'input[name="username"]'
    SEL_PASSWORD_FB = 'input[name="password"]'
    SEL_SUBMIT_FB = 'button[type="submit"]'

    # --- FunCAPTCHA iframe (Arkose Labs) ---
    SEL_CAPTCHA_IFRAME = '#game-core-frame, iframe[src*="arkose"], iframe[src*="funcaptcha"]'

    # --- Nút "Start Puzzle" trong iframe ---
    SEL_START_PUZZLE = 'button[data-theme="home.verifyButton"], button[aria-label*="Start Puzzle"]'

    # --- Ảnh lựa chọn: "Image 1 of 5", "Image 2 of 5", ... ---
    SEL_CHOICE_IMAGES = 'img[aria-label*="Image "][aria-label*=" of "]'

    # --- Ảnh chính cần match: "Match This!" ---
    SEL_KEY_FRAME = 'img.key-frame-image, img[aria-label*="Match This"]'

    # --- Game area (canvas chính) ---
    SEL_GAME_AREA = 'canvas, [class*="game"], [class*="challenge"]'

    # --- Nút xoay phải / điều hướng ---
    SEL_ROTATE_RIGHT = 'circle[r="17"]'
    SEL_NAV_BUTTONS = 'button[aria-label*="right"], button[aria-label*="next"], [class*="arrow-right"]'

    # --- Game type detection keywords ---
    GAME_TYPE_KEYWORDS = {
        "rotate_animal": ["rotate", "upright", "orientation"],
        "shadow_match": ["shadow"],
        "select_tiles": ["select", "tile"],
        "match_object": ["match"],
        "pick_image": ["pick", "choose"],
        "count_objects": ["count", "how many"],
    }

    def __init__(self, username: str, password: str, proxy: Optional[str] = None, config: dict = None):
        self.username = username
        self.password = password
        self.proxy = proxy
        self.cfg = config or DEFAULT_CONFIG
        self.total_captured = 0
        self._debug_dir = None
        if self.cfg.get("debug"):
            self._debug_dir = CAPTURED_DIR / self.cfg.get("debug_dir", "_debug") / self.username
            self._debug_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"[{self.username}] DEBUG mode ON -> {self._debug_dir}")

    async def _debug_screenshot(self, page, step: str, round_num: int = 0):
        """Chup screenshot debug tai moi buoc."""
        if not self._debug_dir:
            return
        try:
            ts = datetime.now(timezone.utc).strftime("%H%M%S")
            path = self._debug_dir / f"r{round_num}_{step}_{ts}.png"
            await page.screenshot(path=str(path), full_page=True)
            logger.debug(f"[{self.username}] DEBUG screenshot: {step} -> {path.name}")
        except Exception:
            pass

    async def _debug_log_html(self, page, step: str):
        """Log 1 doan HTML nho de debug DOM."""
        if not self.cfg.get("debug"):
            return
        try:
            # Log iframe src
            iframe = await page.query_selector(self.SEL_CAPTCHA_IFRAME)
            if iframe:
                src = await iframe.get_attribute("src") or "(no src)"
                logger.debug(f"[{self.username}] {step} | iframe src: {src[:120]}...")
            # Log title
            title = await page.title()
            logger.debug(f"[{self.username}] {step} | page title: {title}")
        except Exception:
            pass

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

    # -- Detect game type (từ source HTML trong iframe) --

    async def _detect_game_type(self, frame, iframe_el=None) -> str:
        """
        Đọc game type từ source của FunCAPTCHA iframe.
        Thứ tự ưu tiên:
          1. HTML source của iframe (F12 -> thấy data-game-type, meta tags...)
          2. URL params của iframe (data=... base64)
          3. Text hiển thị trên màn hình
        """
        try:
            # ── Cách 1: Đọc toàn bộ HTML source ──
            html = (await frame.content()).lower()

            # Tìm data attributes chứa game type
            # FunCAPTCHA thường nhúng: data-game-type="..." hoặc data-challenge-type="..."
            import re
            for attr in ['data-game-type', 'data-challenge-type', 'data-puzzle-type', 'data-type']:
                m = re.search(rf'{attr}=["\']([^"\']+)', html)
                if m:
                    val = m.group(1).lower()
                    for gtype, keywords in self.GAME_TYPE_KEYWORDS.items():
                        if any(kw in val for kw in keywords):
                            logger.debug(f"Detected via {attr}: {gtype}")
                            return gtype

            # Tìm trong meta tags
            for meta in ['game-type', 'challenge-type']:
                m = re.search(rf'<meta[^>]+name=["\']{meta}["\'][^>]+content=["\']([^"\']+)', html)
                if m:
                    val = m.group(1).lower()
                    for gtype, keywords in self.GAME_TYPE_KEYWORDS.items():
                        if any(kw in val for kw in keywords):
                            return gtype

            # ── Cách 2: Parse URL data param (base64) ──
            if iframe_el:
                try:
                    src = await iframe_el.get_attribute("src") or ""
                    from urllib.parse import urlparse, parse_qs
                    parsed = urlparse(src)
                    params = parse_qs(parsed.query)
                    data_blob = params.get("data", [None])[0]
                    if data_blob:
                        import base64
                        decoded = base64.b64decode(data_blob).decode("utf-8", errors="ignore").lower()
                        for gtype, keywords in self.GAME_TYPE_KEYWORDS.items():
                            if any(kw in decoded for kw in keywords):
                                logger.debug(f"Detected via iframe URL data: {gtype}")
                                return gtype
                except Exception:
                    pass

            # ── Cách 3: Keyword matching trong text ──
            for gtype, keywords in self.GAME_TYPE_KEYWORDS.items():
                for kw in keywords:
                    if kw in html:
                        return gtype

        except Exception:
            pass

        return "unknown"

    # -- Fill login form --

    async def _fill_login_form(self, page):
        try:
            await page.fill(self.SEL_USERNAME, self.username, timeout=5000)
        except Exception:
            try:
                await page.fill(self.SEL_USERNAME_FB, self.username, timeout=5000)
            except Exception:
                logger.warning(f"[{self.username}] Không điền được username")

        try:
            await page.fill(self.SEL_PASSWORD, self.password, timeout=5000)
        except Exception:
            try:
                await page.fill(self.SEL_PASSWORD_FB, self.password, timeout=5000)
            except Exception:
                logger.warning(f"[{self.username}] Không điền được password")

    async def _click_login(self, page):
        try:
            await page.click(self.SEL_SUBMIT, timeout=5000)
        except Exception:
            try:
                await page.click(self.SEL_SUBMIT_FB, timeout=5000)
            except Exception:
                await page.keyboard.press("Enter")

    # -- Click "Start Puzzle" / "Verify" de reveal game --

    async def _click_reveal_captcha(self, page, round_num: int = 0):
        """
        Tim va bam nut Verify/Start Puzzle trong iframe FunCAPTCHA.
        Phai bam nut nay thi anh game moi hien ra de chup.

        Cac nut co the gap:
          - <button data-theme="home.verifyButton">Start Puzzle</button>
          - <button>Verify</button>
          - Nut play/start bat ky
        """
        logger.debug(f"[{self.username}] Dang tim nut Verify/Start Puzzle...")
        await self._debug_screenshot(page, "01_before_click_verify", round_num)

        # --- Cach 1: Tim iframe -> tim nut Start Puzzle ---
        try:
            iframe_el = await page.wait_for_selector(self.SEL_CAPTCHA_IFRAME, timeout=5000)
            if iframe_el:
                frame = await iframe_el.content_frame()
                if frame:
                    # Thu nhieu selector cho nut verify
                    verify_selectors = [
                        self.SEL_START_PUZZLE,
                        'button[data-theme="home.verifyButton"]',
                        'button:has-text("Start Puzzle")',
                        'button:has-text("Verify")',
                        'button:has-text("Start")',
                        'button:has-text("Play")',
                        '[class*="verify"]',
                        '[class*="start"]',
                        '[class*="play"]',
                    ]
                    for sel in verify_selectors:
                        try:
                            btn = await frame.wait_for_selector(sel, timeout=2000)
                            if btn:
                                text = await btn.inner_text()
                                logger.info(f"[{self.username}] Tim thay nut: '{text.strip()}' -> click")
                                await btn.click(timeout=3000)
                                await self._debug_screenshot(page, "02_after_click_verify", round_num)
                                await asyncio.sleep(self.cfg.get("click_delay_sec", 3.0))
                                return True
                        except Exception:
                            continue
        except Exception as e:
            logger.debug(f"[{self.username}] Cach 1 that bai: {e}")

        # --- Cach 2: Click truc tiep vao iframe ---
        try:
            iframe_el = await page.wait_for_selector(self.SEL_CAPTCHA_IFRAME, timeout=3000)
            if iframe_el:
                logger.debug(f"[{self.username}] Click truc tiep vao iframe...")
                await iframe_el.click(timeout=3000)
                await self._debug_screenshot(page, "02b_after_click_iframe", round_num)
                await asyncio.sleep(3)
                return True
        except Exception as e:
            logger.debug(f"[{self.username}] Cach 2 that bai: {e}")

        logger.warning(f"[{self.username}] KHONG tim thay nut Verify nao!")
        return False

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
                        logger.debug(f"[{self.username}] #{round_num+1} Vao Roblox login...")
                        await page.goto(self.ROBLOX_LOGIN_URL, wait_until="networkidle", timeout=30000)
                        await asyncio.sleep(2)
                        await self._debug_screenshot(page, "A_page_loaded", round_num)

                        # (2) Dien form
                        await self._fill_login_form(page)
                        await asyncio.sleep(0.3)

                        # (3) Bam Login
                        logger.debug(f"[{self.username}] #{round_num+1} Bam Login...")
                        await self._click_login(page)

                        # (4) Doi CAPTCHA iframe xuat hien
                        logger.debug(f"[{self.username}] #{round_num+1} Doi CAPTCHA iframe...")
                        captcha_appeared = False
                        try:
                            await page.wait_for_selector(
                                self.SEL_CAPTCHA_IFRAME,
                                timeout=self.cfg["captcha_timeout_sec"] * 1000,
                            )
                            captcha_appeared = True
                            logger.debug(f"[{self.username}] #{round_num+1} CAPTCHA iframe DA XUAT HIEN")
                            await self._debug_screenshot(page, "B_captcha_iframe_visible", round_num)
                            await self._debug_log_html(page, "B_captcha_iframe")
                        except Exception:
                            logger.debug(f"[{self.username}] #{round_num+1} Khong thay CAPTCHA iframe, bo qua...")
                            await self._debug_screenshot(page, "B_no_captcha", round_num)
                            continue

                        await asyncio.sleep(1)

                        # (5) Click Verify/Start Puzzle de reveal game
                        if self.cfg.get("click_to_reveal", True) and captcha_appeared:
                            logger.debug(f"[{self.username}] #{round_num+1} Tim nut Verify...")
                            clicked = await self._click_reveal_captcha(page, round_num)
                            if clicked:
                                logger.debug(f"[{self.username}] #{round_num+1} Da click Verify")
                                await asyncio.sleep(self.cfg.get("click_delay_sec", 3.0))
                            else:
                                logger.debug(f"[{self.username}] #{round_num+1} Khong click duoc Verify, van thu chup...")
                            await self._debug_screenshot(page, "C_after_verify", round_num)

                        # (6) Chup & detect
                        logger.debug(f"[{self.username}] #{round_num+1} Bat dau chup...")
                        await self._debug_screenshot(page, "D_before_capture", round_num)
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
                        else:
                            logger.warning(f"[{self.username}] #{round_num+1}/{target} KHONG chup duoc anh nao!")
                            await self._debug_screenshot(page, "E_no_images_captured", round_num)

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

    # -- Capture CAPTCHA iframe (chup tung anh rieng le) --

    async def _capture_iframe(self, page, round_num: int, ts: str) -> List[Dict]:
        """Chup key-frame + tung anh lua chon (Image 1 of 5, ...)."""
        results = []

        try:
            iframe_el = await page.wait_for_selector(self.SEL_CAPTCHA_IFRAME, timeout=5000)
            if not iframe_el:
                return results

            frame = await iframe_el.content_frame()
            if not frame:
                return results

            game_type = await self._detect_game_type(frame, iframe_el)
            type_dir = CAPTURED_DIR / game_type
            type_dir.mkdir(parents=True, exist_ok=True)

            # --- 1. Chup "Match This!" key frame ---
            try:
                key_img = await frame.wait_for_selector(self.SEL_KEY_FRAME, timeout=3000)
                if key_img:
                    key_path = type_dir / f"{self.username}_r{round_num}_key_{ts}.png"
                    await key_img.screenshot(path=str(key_path))
                    results.append({"image_path": str(key_path), "type": f"{game_type}_keyframe",
                                    "username": self.username, "round": round_num + 1, "ts": ts})
                    logger.debug(f"[{self.username}] Chup key-frame")
            except Exception:
                pass

            # --- 2. Chup TUNG anh lua chon: Image 1 of 5, Image 2 of 5, ... ---
            try:
                choice_imgs = await frame.locator(self.SEL_CHOICE_IMAGES).all()
                for img_el in choice_imgs:
                    aria_label = await img_el.get_attribute("aria-label") or f"choice_{round_num}"
                    safe_name = aria_label.replace(" ", "_").replace(".", "").replace(":", "")[:30]
                    choice_path = type_dir / f"{self.username}_r{round_num}_{safe_name}_{ts}.png"
                    try:
                        await img_el.screenshot(path=str(choice_path))
                        results.append({"image_path": str(choice_path), "type": f"{game_type}_choice",
                                        "aria_label": aria_label, "username": self.username,
                                        "round": round_num + 1, "ts": ts})
                    except Exception:
                        pass
                if choice_imgs:
                    logger.debug(f"[{self.username}] Chup {len(choice_imgs)} anh lua chon")
            except Exception:
                pass

            # --- 3. Fallback: chup toan bo game ---
            if not results:
                game_path = type_dir / f"{self.username}_r{round_num}_full_{ts}.png"
                try:
                    area = await frame.wait_for_selector(self.SEL_GAME_AREA, timeout=3000)
                    if area:
                        await area.screenshot(path=str(game_path))
                    else:
                        await frame.screenshot(path=str(game_path))
                except Exception:
                    await frame.screenshot(path=str(game_path))
                results.append({"image_path": str(game_path), "type": game_type,
                                "username": self.username, "round": round_num + 1, "ts": ts})

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
