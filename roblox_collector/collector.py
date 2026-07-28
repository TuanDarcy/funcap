"""
Roblox FunCAPTCHA Collector — Standalone
=========================================
Thu thập ảnh FunCAPTCHA từ Roblox login để làm dataset train AI.

Cách dùng:
  python collector.py

Cấu trúc:
  roblox_collector/
    input/
      accounts.txt     ← username:password (mỗi dòng 1 account)
      proxies.txt      ← ip:port hoặc user:pass@ip:port
      config.json      ← cấu hình (threads, headless, output...)
    captured/
      rotate_animal/   ← ảnh CAPTCHA tự động phân loại theo folder
      shadow_match/
      select_tiles/
      ...
    collector.py       ← script chính
"""

import asyncio
import json
import os
import sys
import time
import hashlib
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from loguru import logger
except ImportError:
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    logger = logging.getLogger("collector")

# ─── Thư mục gốc ──────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.resolve()
INPUT_DIR = BASE_DIR / "input"
CAPTURED_DIR = BASE_DIR / "captured"
CONFIG_PATH = INPUT_DIR / "config.json"
ACCOUNTS_PATH = INPUT_DIR / "accounts.txt"
PROXIES_PATH = INPUT_DIR / "proxies.txt"

# ─── Default config ───────────────────────────────────────────
DEFAULT_CONFIG = {
    "threads": 5,
    "headless": True,
    "screenshot_quality": 95,
    "max_retries_per_account": 3,
    "wait_between_accounts_sec": 0.5,
    "captcha_timeout_sec": 8,
    "viewport_width": 500,
    "viewport_height": 700,
}

# ─── Class mapping ────────────────────────────────────────────
CLASS_FOLDERS = [
    "rotate_animal",
    "match_object",
    "select_tiles",
    "shadow_match",
    "pick_image",
    "count_objects",
    "unknown",
    "no_captcha",
]

# ═══════════════════════════════════════════════════════════════
#  COLLECTOR CLASS
# ═══════════════════════════════════════════════════════════════

class RobloxCaptchaCollector:
    """
    Mở browser → vào Roblox login → trigger CAPTCHA → chụp ảnh → lưu vào folder theo loại.
    Mỗi instance = 1 browser = 1 proxy = 1 account.
    """

    ROBLOX_LOGIN_URL = "https://www.roblox.com/login"

    def __init__(self, account_id: str, proxy: Optional[str] = None, config: dict = None):
        self.account_id = account_id
        self.proxy = proxy
        self.cfg = config or DEFAULT_CONFIG
        self.collected: List[Dict] = []

    # ── Proxy parser ──────────────────────────────────────────

    @staticmethod
    def parse_proxy(proxy_str: str) -> Optional[dict]:
        if not proxy_str:
            return None
        cleaned = proxy_str.strip().replace("http://", "").replace("https://", "").replace("socks5://", "")
        if "@" in cleaned:
            auth, host = cleaned.split("@", 1)
            user, pwd = auth.split(":", 1)
            return {"server": f"http://{host}", "username": user, "password": pwd}
        return {"server": f"http://{cleaned}"}

    # ── Browser args ──────────────────────────────────────────

    def _browser_args(self) -> list:
        return [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            f"--window-size={self.cfg['viewport_width']},{self.cfg['viewport_height']}",
        ]

    # ── Stealth JS ────────────────────────────────────────────

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

    # ── Detect game type ──────────────────────────────────────

    async def _detect_game_type(self, frame) -> str:
        try:
            text = (await frame.locator("body").inner_text()).lower()
            if "rotate" in text or "upright" in text or "orientation" in text:
                return "rotate_animal"
            if "shadow" in text:
                return "shadow_match"
            if "match" in text:
                return "match_object"
            if "select" in text and "tile" in text:
                return "select_tiles"
            if "pick" in text or "choose" in text:
                return "pick_image"
            if "count" in text or "how many" in text:
                return "count_objects"
        except Exception:
            pass
        return "unknown"

    # ── Main flow ─────────────────────────────────────────────

    async def run(self, username: str, password: str) -> List[Dict]:
        """Chạy 1 vòng: login → chụp CAPTCHA → lưu."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.error("Chưa cài playwright! Chạy: pip install playwright && playwright install chromium")
            return []

        captured = []
        max_retries = self.cfg["max_retries_per_account"]

        for attempt in range(max_retries):
            try:
                async with async_playwright() as p:
                    launch_opts = {
                        "headless": self.cfg["headless"],
                        "args": self._browser_args(),
                    }
                    proxy_cfg = self.parse_proxy(self.proxy) if self.proxy else None
                    if proxy_cfg:
                        launch_opts["proxy"] = proxy_cfg

                    browser = await p.chromium.launch(**launch_opts)
                    context = await browser.new_context(
                        viewport={"width": self.cfg["viewport_width"], "height": self.cfg["viewport_height"]},
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
                        locale="en-US",
                    )
                    page = await context.new_page()
                    await page.add_init_script(self.STEALTH_JS)

                    # ── Step 1: Vào Roblox login ──
                    logger.debug(f"[{username}] Mở Roblox login...")
                    await page.goto(self.ROBLOX_LOGIN_URL, wait_until="networkidle", timeout=30000)
                    await asyncio.sleep(2)

                    # ── Step 2: Điền form ──
                    try:
                        await page.fill("#login-username", username, timeout=5000)
                        await page.fill("#login-password", password, timeout=5000)
                    except Exception:
                        try:
                            await page.fill('input[name="username"]', username, timeout=5000)
                            await page.fill('input[name="password"]', password, timeout=5000)
                        except Exception:
                            logger.warning(f"[{username}] Không điền được form")

                    # ── Step 3: Bấm Login ──
                    try:
                        await page.click("#login-button", timeout=5000)
                    except Exception:
                        try:
                            await page.click('button[type="submit"]', timeout=5000)
                        except Exception:
                            await page.keyboard.press("Enter")

                    # ── Step 4: Đợi CAPTCHA ──
                    await asyncio.sleep(3)

                    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")

                    # ── Step 5: Chụp & lưu vào folder theo loại ──
                    captcha_found = await self._capture_iframe(page, username, ts)

                    if captcha_found:
                        captured.extend(captcha_found)
                        logger.success(f"[{username}] ✓ {len(captcha_found)} ảnh CAPTCHA | {self.proxy or 'direct'}")
                    else:
                        # Không có CAPTCHA → lưu full page vào no_captcha
                        no_cap_dir = CAPTURED_DIR / "no_captcha"
                        no_cap_dir.mkdir(parents=True, exist_ok=True)
                        path = no_cap_dir / f"full_{username}_{ts}.png"
                        await page.screenshot(path=str(path), full_page=True)
                        logger.info(f"[{username}] Không có CAPTCHA (có thể login thành công)")
                        captured.append({"image_path": str(path), "type": "no_captcha", "username": username, "ts": ts})

                    await browser.close()
                    break  # Thoát retry loop

            except Exception as e:
                logger.error(f"[{username}] Lần {attempt+1}/{max_retries} lỗi: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(3)

        return captured

    # ── Capture CAPTCHA iframe ────────────────────────────────

    async def _capture_iframe(self, page, username: str, ts: str) -> List[Dict]:
        results = []
        try:
            iframe_el = await page.wait_for_selector(
                'iframe[src*="arkose"], iframe[src*="funcaptcha"], iframe[src*="arkoselabs"]',
                timeout=self.cfg["captcha_timeout_sec"] * 1000,
            )
            if not iframe_el:
                return results

            frame = await iframe_el.content_frame()
            if not frame:
                return results

            game_type = await self._detect_game_type(frame)
            type_dir = CAPTURED_DIR / game_type
            type_dir.mkdir(parents=True, exist_ok=True)

            # Chụp game area
            game_path = type_dir / f"{username}_{ts}.png"
            try:
                area = await frame.wait_for_selector(
                    'canvas, img[src*="game"], [class*="game"], [class*="challenge"]',
                    timeout=3000,
                )
                if area:
                    await area.screenshot(path=str(game_path))
                else:
                    await frame.screenshot(path=str(game_path))
            except Exception:
                await frame.screenshot(path=str(game_path))

            # Đọc instruction
            instruction = None
            try:
                for sel in ['h2', 'h3', '[class*="instruction"]', '.game-instruction']:
                    el = await frame.wait_for_selector(sel, timeout=1000)
                    if el:
                        txt = await el.inner_text()
                        if txt and len(txt) > 3:
                            instruction = txt.strip()
                            break
            except Exception:
                pass

            results.append({
                "image_path": str(game_path),
                "type": game_type,
                "instruction": instruction,
                "username": username,
                "proxy": self.proxy or "direct",
                "ts": ts,
            })

            # Tiles cho select_tiles
            try:
                tiles = await frame.locator('[class*="tile"], [class*="grid-item"]').all()
                for i, tile in enumerate(tiles[:9]):
                    tp = type_dir / f"{username}_tile{i}_{ts}.png"
                    try:
                        await tile.screenshot(path=str(tp))
                        results.append({"image_path": str(tp), "type": f"{game_type}_tile{i}", "username": username, "ts": ts})
                    except Exception:
                        pass
            except Exception:
                pass

        except Exception as e:
            logger.debug(f"[{username}] CAPTCHA iframe: {e}")

        return results


# ═══════════════════════════════════════════════════════════════
#  MULTI-THREAD ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════

def _run_async(collector, username: str, password: str) -> list:
    return asyncio.run(collector.run(username, password))


def collect_all():
    """Entry point: đọc config → chạy multi-thread → lưu kết quả."""

    # ── Load config ──
    config = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config.update(json.load(f))
        logger.info(f"Loaded config: {CONFIG_PATH}")
    else:
        # Tạo file config mẫu
        CAPTURED_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        logger.info(f"Created default config: {CONFIG_PATH}")

    # ── Load accounts ──
    if not ACCOUNTS_PATH.exists():
        logger.error(f"Không tìm thấy {ACCOUNTS_PATH}")
        logger.info("Tạo file input/accounts.txt với format: username:password mỗi dòng")
        INPUT_DIR.mkdir(parents=True, exist_ok=True)
        ACCOUNTS_PATH.write_text("# Format: username:password (mỗi dòng 1 account)\n# user1:pass123\n", encoding="utf-8")
        sys.exit(1)

    accounts = [line.strip() for line in ACCOUNTS_PATH.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")]

    # ── Load proxies ──
    proxies = []
    if PROXIES_PATH.exists():
        proxies = [line.strip() for line in PROXIES_PATH.read_text(encoding="utf-8").splitlines()
                   if line.strip() and not line.strip().startswith("#")]
    else:
        INPUT_DIR.mkdir(parents=True, exist_ok=True)
        PROXIES_PATH.write_text("# Format: ip:port hoặc user:pass@ip:port (mỗi dòng 1 proxy)\n# 192.168.1.1:8080\n", encoding="utf-8")
        logger.warning(f"Chưa có {PROXIES_PATH} → chạy không proxy")
        proxies = ["direct"]

    # ── Validate ──
    if not accounts:
        logger.error("Không có account nào trong input/accounts.txt!")
        sys.exit(1)

    # Cycle proxies
    if proxies:
        proxy_cycle = (proxies * ((len(accounts) // max(len(proxies), 1)) + 1))[:len(accounts)]
    else:
        proxy_cycle = [None] * len(accounts)

    # ── Create folders ──
    CAPTURED_DIR.mkdir(parents=True, exist_ok=True)
    for folder in CLASS_FOLDERS:
        (CAPTURED_DIR / folder).mkdir(parents=True, exist_ok=True)

    # ── Run ──
    n_threads = config["threads"]
    logger.info(f"🚀 Bắt đầu: {len(accounts)} accounts × {n_threads} threads")
    logger.info(f"   Output: {CAPTURED_DIR}")

    all_results = []
    lock = threading.Lock()
    completed = 0
    total = 0

    # Parse accounts
    tasks = []
    for i, acc in enumerate(accounts):
        parts = acc.split(":", 1)
        if len(parts) != 2:
            logger.warning(f"Bỏ qua dòng lỗi: {acc}")
            continue
        username, password = parts
        proxy = proxy_cycle[i] if proxy_cycle[i] != "direct" else None
        tasks.append((username, password, proxy))
        total += 1

    def process(username, password, proxy):
        collector = RobloxCaptchaCollector(account_id=username, proxy=proxy, config=config)
        return _run_async(collector, username, password)

    with ThreadPoolExecutor(max_workers=n_threads) as executor:
        futures = {executor.submit(process, u, p, pr): u for u, p, pr in tasks}

        for future in as_completed(futures):
            u = futures[future]
            try:
                results = future.result()
                with lock:
                    all_results.extend(results)
                    completed += 1
                logger.info(f"📊 Tiến độ: {completed}/{total} | {u}: {len(results)} ảnh")
            except Exception as e:
                with lock:
                    completed += 1
                logger.error(f"[{u}] Crash: {e}")

    # ── Save metadata ──
    meta_path = CAPTURED_DIR / "metadata.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False, default=str)

    # ── Summary ──
    from collections import Counter
    type_counts = Counter(r.get("type", "?") for r in all_results)

    print("\n" + "=" * 50)
    print("  ✅ HOÀN THÀNH THU THẬP")
    print("=" * 50)
    print(f"  Tổng ảnh: {len(all_results)}")
    for t, n in sorted(type_counts.items()):
        print(f"  {t:25s} {n:5d}")
    print(f"\n  Dữ liệu: {CAPTURED_DIR}")
    print(f"\n  Bước tiếp theo:")
    print(f"    1. Kiểm tra & sửa label:")
    print(f"       python ../src/funcap_solver/data/labeler.py -i captured -o ../data/labeled")
    print(f"    2. Chia train/val rồi train model")
    print("=" * 50)


# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    collect_all()
