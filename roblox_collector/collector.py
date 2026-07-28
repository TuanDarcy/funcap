"""
Roblox FunCAPTCHA Collector — Standalone v3
=============================================
Logic: SPAM CAPTCHA -> chụp -> reload -> chụp tiếp -> ... vô hạn.
KHÔNG cần giải CAPTCHA, KHÔNG cần login thành công.
Mục tiêu duy nhất: thu thập CÀNG NHIỀU ảnh CAPTCHA càng tốt.

Flow mỗi account:
  1. Mở browser -> vào Roblox login
  2. Điền user:pass -> bấm Login -> FunCAPTCHA xuất hiện
  3. Click nút Verify/Start Puzzle để hiện game
  4. Chụp ảnh game -> detect loại -> lưu vào folder
  5. Reload trang -> lặp lại vô hạn
  6. Đóng browser -> qua account tiếp theo

Cấu trúc:
  roblox_collector/
    input/
      accounts.txt     <- username:password (mỗi dòng 1 account)
      proxies.txt      <- ip:port hoặc user:pass@ip:port
      config.json      <- cấu hình
    captured/
      rotate_animal/   <- ảnh CAPTCHA tự động phân loại theo folder
      ...
    collector.py       <- script này
"""

import asyncio
import json
import os
import signal
import sys
import time
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from loguru import logger
    _use_loguru = True
except ImportError:
    import logging
    _use_loguru = False
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("collector")

# Global shutdown flag for Ctrl+C
_shutdown = threading.Event()

def _signal_handler(signum, frame):
    print("\n[!] Ctrl+C - exiting NOW...")
    _shutdown.set()
    os._exit(0)  # Kill immediately

signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

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
    "captchas_per_account": 999999,
    "reload_delay_sec": 2.0,
    "headless": False,
    "viewport_width": 500,
    "viewport_height": 700,
    "use_proxy": False,
    "proxy_mode": "per_tab",
    "captcha_timeout_sec": 15,
    "click_to_reveal": True,
    "click_delay_sec": 2.0,
    "debug": True,
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
    SEL_START_PUZZLE = (
        '#root > div > div.sc-99cwso-0.sc-11w6f91-0.fcBZbp.eWRcSj.home.box.screen > button,'
        'button[data-theme="home.verifyButton"],'
        'button[aria-label*="Start Puzzle"]'
    )

    # --- Asset ảnh base64 của nút Start Puzzle ---
    START_PUZZLE_B64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAIwAAAAjCAYAAABGiuIFAAAAAXNSR0IArs4c6QAAAARnQU1BAACxjwv8YQUAAAAJcEhZcwAAEnQAABJ0Ad5mH3gAAAH9SURBVHhe7ZlLcgIxDERNbsUqXCscgHPNKhwr2eCU0yXJ0xp7YIp+VSyQZH3sxgPF6fP766cIsZIPNAgRIcEICglGUEgwgkKCERQSjKCQYASFBCMoJBhBIcEICglGUEgwguK05c/H5XxDU7ncr2j6x3K+dWNGwday5kGYfHvS9j6zx9QNs5xv7ub2fHsxq9asvEchJZgMe2707Fqz878ytGBwsy7369+rBeOORjuXNd+7Qn+H6T0rLb8nHlxvxWFMMWpY6xArD4J5Ec/v2T1fr18vt4WVE3soTh4rrgd9w7R4TdQXg5WrBPZKzy/8PfLsEfQNU4JCkUiiT0D1eXb0Yf2163pg3givH6wX+VrWxhUn1rJl7D1SN4xXYHn8QmI2vjS3Uobsui2MrskcHhM7g5RgysZDjsiKbi9Gz8wIgIm1GLG3acFUqnBwALaprYOMpp3Lm3ErzLxM7ExowUQqzW4o5ppxOK8OMy8T24Liz+wzLZgWPGh8n4Ed4Kgwjxcmdja0YLDh6MbZwuh8e9H27c3ACICJ7YG9Zc5t6M/qFhzOWlNjLB/S5uttopXPikN6eSOsmsjaeb1ZLayc2HuUA2N70DdMeRTxCnk+y1axfJgnGhqx8s3GqmnZnoHXh2ePSN0w4n1J3TDifZFgBIUEIygkGEEhwQgKCUZQ/AILWUXD0s0pvgAAAABJRU5ErkJggg=="
    )

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
        self._debug = self.cfg.get("debug", False)
        if self._debug:
            logger.info(f"[{self.username}] 🐛 DEBUG mode ON")

    def _step(self, msg: str):
        """In log debug từng bước."""
        if self._debug:
            logger.info(f"[{self.username}] {msg}")

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

    # -- Click "Start Puzzle" / "Verify" để hiện game --

    async def _click_by_image(self, iframe_el, frame, round_num: int = 0) -> bool:
        """
        Click nut Start Puzzle bang cach match anh base64.
        Chup man hinh iframe -> dung OpenCV template matching -> click toa do.
        """
        try:
            import base64
            import numpy as np
            import cv2
            from PIL import Image
            import io
        except ImportError:
            self._step("Thieu opencv-python, bo qua image click")
            return False

        try:
            # Luu asset anh mau
            asset_dir = INPUT_DIR / "assets"
            asset_dir.mkdir(exist_ok=True)
            template_path = asset_dir / "start_puzzle.png"
            if not template_path.exists():
                img_bytes = base64.b64decode(self.START_PUZZLE_B64)
                with open(template_path, "wb") as f:
                    f.write(img_bytes)

            # Chup iframe element (khong dung frame.screenshot vi khong co)
            screenshot_bytes = await iframe_el.screenshot()
            screenshot = np.array(Image.open(io.BytesIO(screenshot_bytes)).convert("RGB"))
            screenshot_bgr = cv2.cvtColor(screenshot, cv2.COLOR_RGB2BGR)

            # Load template
            template = cv2.imread(str(template_path))
            if template is None:
                return False

            # Template matching
            result = cv2.matchTemplate(screenshot_bgr, template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)

            if max_val < 0.5:
                self._step(f"Match confidence: {max_val:.2f} < 0.5 — bo qua")
                return False

            # Click giua template - dung mouse move + click nhu nguoi that
            h, w = template.shape[:2]
            center_x = max_loc[0] + w // 2
            center_y = max_loc[1] + h // 2
            self._step(f"Image click: ({center_x}, {center_y}) conf={max_val:.2f}")

            # Cach 1: Tim button element va click bang JS (manh nhat)
            try:
                btn = await frame.wait_for_selector(
                    'button[data-theme="home.verifyButton"], button:has-text("Start Puzzle")',
                    timeout=2000,
                )
                if btn:
                    await btn.scroll_into_view_if_needed()
                    await btn.hover()
                    await asyncio.sleep(0.2)
                    await btn.click(force=True, timeout=3000)
                    self._step("Clicked button element (force)")
                    await asyncio.sleep(self.cfg.get("click_delay_sec", 3.0))
                    return True
            except Exception:
                pass

            # Cach 2: Mouse move + click tai toa do (gia lap nguoi)
            try:
                await frame.click(position={"x": center_x, "y": center_y}, timeout=3000)
                self._step("Clicked at coordinates")
                await asyncio.sleep(self.cfg.get("click_delay_sec", 3.0))
                return True
            except Exception:
                pass

            # Cach 3: JS dispatchEvent
            try:
                btn = await frame.wait_for_selector(
                    'button[data-theme="home.verifyButton"]', timeout=2000,
                )
                if btn:
                    await btn.evaluate("el => el.click()")
                    self._step("Clicked via JS el.click()")
                    await asyncio.sleep(self.cfg.get("click_delay_sec", 3.0))
                    return True
            except Exception:
                pass

            return False

        except Exception as e:
            self._step(f"Image click error: {e}")
            return False

    # -- Click bang anh base64 (OpenCV) + frame_locator --

    async def _click_reveal_captcha(self, page, round_num: int = 0):
        """Click nut Start Puzzle trong iframe - dung frame_locator."""

        # --- Dung frame_locator de truy cap iframe (cach Playwright chinh thong) ---
        try:
            # frame_locator tim iframe #game-core-frame roi thao tac truc tiep ben trong
            captcha = page.frame_locator(self.SEL_CAPTCHA_IFRAME)

            # Doi loading text bien mat
            try:
                loading = captcha.locator("#text-loading, .text.loading")
                await loading.first.wait_for(state="visible", timeout=3000)
                self._step("Phat hien loading text - dang doi bien mat...")
                await loading.first.wait_for(state="hidden", timeout=15000)
                self._step("Loading da bien mat")
            except Exception:
                self._step("Khong thay loading text (da san sang)")

            # Tim button Start Puzzle
            btn = captcha.locator(
                'button[data-theme="home.verifyButton"], button:has-text("Start Puzzle")'
            ).first
            await btn.wait_for(state="visible", timeout=5000)
            await btn.scroll_into_view_if_needed()
            await btn.hover()
            await asyncio.sleep(0.3)
            await btn.click(force=True, timeout=3000)
            self._step("Da click Start Puzzle (frame_locator)")
            await asyncio.sleep(self.cfg.get("click_delay_sec", 3.0))
            return True

        except Exception as e:
            self._step(f"frame_locator click error: {e}")

        # --- Fallback: JS click ---
        try:
            captcha = page.frame_locator(self.SEL_CAPTCHA_IFRAME)
            btn = captcha.locator(
                'button[data-theme="home.verifyButton"], button:has-text("Start Puzzle")'
            ).first
            await btn.evaluate("el => el.click()")
            self._step("Clicked via JS evaluate")
            await asyncio.sleep(3)
            return True
        except Exception:
            pass

        logger.warning(f"[{self.username}] KHONG click duoc nut Verify!")
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
                if self.cfg.get("use_proxy", False) and self.proxy:
                    proxy_cfg = self.parse_proxy(self.proxy)
                if proxy_cfg:
                    launch_opts["proxy"] = proxy_cfg

                browser = await p.chromium.launch(**launch_opts)
                self._step("Browser da mo")

                for round_num in range(target):
                    if _shutdown.is_set():
                        break

                    context = None
                    try:
                        self._step(f"Vong #{round_num+1} - mo tab moi...")
                        context = await browser.new_context(
                            viewport={"width": self.cfg["viewport_width"], "height": self.cfg["viewport_height"]},
                            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
                            locale="en-US",
                        )
                        page = await context.new_page()
                        await page.add_init_script(self.STEALTH_JS)

                        # (1) Vào Roblox login
                        self._step("Dang load trang login...")
                        await page.goto(self.ROBLOX_LOGIN_URL, wait_until="networkidle", timeout=30000)
                        await asyncio.sleep(2)
                        self._step("Trang login da load xong")

                        # (2) Điền form
                        self._step(f"Dien username: {self.username}")
                        await self._fill_login_form(page)
                        await asyncio.sleep(0.3)
                        self._step("Da dien username + password")

                        # (3) Bấm Login
                        self._step("Bam nut Login...")
                        await self._click_login(page)
                        self._step("Da bam Login - doi CAPTCHA...")

                        # (4) Đợi CAPTCHA iframe xuất hiện
                        captcha_appeared = False
                        try:
                            self._step("Doi CAPTCHA iframe xuat hien...")
                            await page.wait_for_selector(
                                self.SEL_CAPTCHA_IFRAME,
                                timeout=self.cfg["captcha_timeout_sec"] * 1000,
                            )
                            captcha_appeared = True
                            self._step("Da phat hien iframe CAPTCHA")
                        except Exception:
                            self._step("Khong thay iframe CAPTCHA - bo qua vong nay")
                            continue

                        await asyncio.sleep(1)

                        # (5) Click Verify/Start Puzzle
                        if self.cfg.get("click_to_reveal", True) and captcha_appeared:
                            self._step("Dang tim nut Start Puzzle...")
                            clicked = await self._click_reveal_captcha(page, round_num)
                            if clicked:
                                self._step("Da click nut Verify - game dang hien...")
                            else:
                                self._step("Khong tim thay nut Verify - van thu chup...")
                            await asyncio.sleep(self.cfg.get("click_delay_sec", 3.0))

                        # (6) Chụp & detect
                        self._step("Dang chup anh CAPTCHA...")
                        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
                        captcha_results = await self._capture_iframe(page, round_num, ts)

                        if captcha_results:
                            captured_all.extend(captcha_results)
                            self.total_captured += len(captcha_results)
                            types = set(r["type"] for r in captcha_results)
                            self._step(f"Chup xong: +{len(captcha_results)} anh ({', '.join(types)})")
                            logger.info(
                                f"[{self.username}] #{self.total_captured} | "
                                f"+{len(captcha_results)} ảnh ({', '.join(types)})"
                            )
                        else:
                            self._step("Khong chup duoc anh nao")

                    except Exception as e:
                        err_msg = str(e)
                        if "Connection closed" in err_msg or "Target closed" in err_msg:
                            logger.warning(f"[{self.username}] Browser crash, khởi động lại...")
                            try:
                                await browser.close()
                            except Exception:
                                pass
                            browser = await p.chromium.launch(**launch_opts)
                            continue
                        logger.error(f"[{self.username}] Lỗi: {e}")
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

        logger.success(f"[{self.username}] Xong: {self.total_captured} ảnh")
        return captured_all

    # -- Chụp CAPTCHA iframe (từng ảnh riêng lẻ) --

    async def _capture_iframe(self, page, round_num: int, ts: str) -> List[Dict]:
        """Chup key-frame + tung anh lua chon bang frame_locator."""
        if _shutdown.is_set():
            return []

        results = []

        try:
            captcha = page.frame_locator(self.SEL_CAPTCHA_IFRAME)

            # Detect game type from iframe content
            game_type = "unknown"
            try:
                html = await captcha.locator("body").inner_text()
                html_lower = html.lower()
                for gtype, keywords in self.GAME_TYPE_KEYWORDS.items():
                    if any(kw in html_lower for kw in keywords):
                        game_type = gtype
                        break
            except Exception:
                pass

            type_dir = CAPTURED_DIR / game_type
            type_dir.mkdir(parents=True, exist_ok=True)

            # --- 1. Chup key-frame ---
            try:
                key = captcha.locator(self.SEL_KEY_FRAME).first
                await key.wait_for(state="visible", timeout=2000)
                key_path = type_dir / f"{self.username}_r{round_num}_key_{ts}.png"
                await key.screenshot(path=str(key_path))
                results.append({"image_path": str(key_path), "type": f"{game_type}_keyframe",
                                "username": self.username, "round": round_num + 1, "ts": ts})
            except Exception:
                pass

            # --- 2. Chup tung anh lua chon ---
            try:
                choices = captcha.locator(self.SEL_CHOICE_IMAGES)
                count = await choices.count()
                for i in range(count):
                    img = choices.nth(i)
                    aria_label = await img.get_attribute("aria-label") or f"choice_{i}"
                    safe_name = aria_label.replace(" ", "_").replace(".", "").replace(":", "")[:30]
                    choice_path = type_dir / f"{self.username}_r{round_num}_{safe_name}_{ts}.png"
                    try:
                        await img.screenshot(path=str(choice_path))
                        results.append({"image_path": str(choice_path), "type": f"{game_type}_choice",
                                        "aria_label": aria_label, "username": self.username,
                                        "round": round_num + 1, "ts": ts})
                    except Exception:
                        pass
            except Exception:
                pass

            # --- 3. Fallback ---
            if not results:
                game_path = type_dir / f"{self.username}_r{round_num}_full_{ts}.png"
                try:
                    area = captcha.locator(self.SEL_GAME_AREA).first
                    await area.screenshot(path=str(game_path))
                except Exception:
                    pass
                results.append({"image_path": str(game_path), "type": game_type,
                                "username": self.username, "round": round_num + 1, "ts": ts})

        except Exception:
            pass

        return results


# ======================================================================
#  MULTI-THREAD ORCHESTRATOR
# ======================================================================

def _run_async(collector) -> list:
    try:
        return asyncio.run(collector.run())
    except KeyboardInterrupt:
        _shutdown.set()
        return []
    except Exception as e:
        if _shutdown.is_set():
            return []
        raise


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

    # Set logging level based on debug flag
    if config.get("debug") and not _use_loguru:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("DEBUG mode ON - verbose logging enabled")

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
    logger.info("FunCAPTCHA Collector")
    logger.info(f"  Accounts: {len(accounts)} | Số ảnh/acc: không giới hạn")
    logger.info(f"  Threads: {n_threads} | Proxy: {'có' if use_proxy else 'không'} ({proxy_mode})")
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
        try:
            for (username, password), proxy in proxy_map.items():
                if _shutdown.is_set():
                    break
                futures[executor.submit(process, username, password, proxy)] = username

            for future in as_completed(futures):
                if _shutdown.is_set():
                    logger.warning("Shutting down - cancelling remaining tasks...")
                    for f in futures:
                        f.cancel()
                    break
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
        except KeyboardInterrupt:
            logger.warning("Interrupted! Stopping...")
            _shutdown.set()
            for f in futures:
                f.cancel()

    # Save metadata
    meta_path = CAPTURED_DIR / "metadata.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False, default=str)

    # Summary
    from collections import Counter
    type_counts = Counter(r.get("type", "?") for r in all_results)

    print("\n" + "=" * 55)
    print("  HOÀN THÀNH THU THẬP")
    print("=" * 55)
    print(f"  Tổng ảnh: {len(all_results)}")
    for t, n in sorted(type_counts.items()):
        bar = "#" * min(n // max(1, len(all_results) // 30), 30)
        print(f"  {t:25s} {n:5d} {bar}")
    print(f"\n  Dữ liệu: {CAPTURED_DIR}")
    print(f"\n  Bước tiếp theo:")
    print(f"    python ../src/funcap_solver/data/labeler.py -i captured -o ../data/labeled")
    print("=" * 55)


if __name__ == "__main__":
    collect_all()
