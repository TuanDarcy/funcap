"""
Roblox FunCAPTCHA Collector - Multi-threaded browser automation
================================================================
Logs into Roblox with multiple accounts/proxies simultaneously,
captures FunCAPTCHA screenshots, and saves labeled training data.

Architecture:
  API approach (noverify.py) → bypasses login, NO CAPTCHA images
  Browser approach (this module) → triggers real login → CAPTCHA appears → screenshot!

Run:
  python -m funcap_solver.data.roblox_collector --accounts accounts.txt --proxies proxies.txt --threads 5
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

from loguru import logger
from PIL import Image


# ---------------------------------------------------------------------------
#  Roblox FunCAPTCHA Collector (Playwright-based)
# ---------------------------------------------------------------------------

class RobloxCaptchaCollector:
    """
    Collect FunCAPTCHA images from Roblox login page using browser automation.

    Each instance = 1 browser = 1 proxy = 1 account.
    Run multiple instances in parallel for high-throughput data collection.
    """

    # Roblox auth endpoints
    ROBLOX_LOGIN_URL = "https://www.roblox.com/login"
    ROBLOX_SIGNUP_URL = "https://www.roblox.com/"

    # FunCAPTCHA iframe selectors
    FUNCAPTCHA_IFRAME_SELECTOR = 'iframe[src*="arkose"], iframe[src*="funcaptcha"], iframe[src*="arkoselabs"]'
    FUNCAPTCHA_CANVAS_SELECTOR = 'canvas, img[src*="game"], img[src*="fc"], .fc-game-image'
    FUNCAPTCHA_VERIFY_GAME_SELECTOR = 'div[data-theme="home"], .game-container, [class*="game"]'

    def __init__(
        self,
        output_dir: Path = Path("data/raw"),
        proxy: Optional[str] = None,
        headless: bool = True,
        screenshot_quality: int = 95,
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.proxy = proxy
        self.headless = headless
        self.screenshot_quality = screenshot_quality
        self.collected: List[Dict] = []
        self._lock = threading.Lock()

    # ---- Config helpers ----

    def _build_proxy_config(self, proxy_str: str) -> dict:
        """Parse proxy string into Playwright proxy config.
        Supports: user:pass@ip:port, ip:port, http://ip:port
        """
        if not proxy_str:
            return None

        # Strip protocol prefix
        cleaned = proxy_str.replace("http://", "").replace("https://", "").replace("socks5://", "")

        if "@" in cleaned:
            auth, host = cleaned.split("@", 1)
            user, password = auth.split(":", 1)
            return {
                "server": f"http://{host}",
                "username": user,
                "password": password,
            }
        else:
            return {"server": f"http://{cleaned}"}

    def _build_browser_args(self) -> List[str]:
        """Build Chromium launch args for stealth."""
        return [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-web-security",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-setuid-sandbox",
            "--disable-infobars",
            "--window-size=500,700",
        ]

    async def _stealth_patch(self, page):
        """Apply anti-detection patches to avoid FunCAPTCHA bot detection."""
        # Override navigator.webdriver
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

            // Override permissions
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                Promise.resolve({ state: Notification.permission }) :
                originalQuery(parameters)
            );

            // Fake chrome object
            window.chrome = { runtime: {} };

            // Override headless detection
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 4 });
        """)

    # ---- Core collection flow ----

    async def collect_from_login_attempt(
        self,
        username: str,
        password: str,
        max_retries: int = 3,
    ) -> List[Dict]:
        """
        Attempt Roblox login to trigger FunCAPTCHA, then capture screenshots.

        Flow:
          1. Open browser with proxy → Roblox login page
          2. Fill username + password, click Login
          3. FunCAPTCHA appears → screenshot everything
          4. Save images with metadata
        """
        captured = []

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.error("playwright not installed. Run: pip install playwright && playwright install chromium")
            return captured

        for attempt in range(max_retries):
            try:
                async with async_playwright() as p:
                    # Launch browser with proxy
                    launch_opts = {
                        "headless": self.headless,
                        "args": self._build_browser_args(),
                    }

                    proxy_config = self._build_proxy_config(self.proxy)
                    if proxy_config:
                        launch_opts["proxy"] = proxy_config

                    browser = await p.chromium.launch(**launch_opts)

                    context = await browser.new_context(
                        viewport={"width": 500, "height": 700},
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/132.0.0.0 Safari/537.36"
                        ),
                        locale="en-US",
                    )

                    page = await context.new_page()
                    await self._stealth_patch(page)

                    # ---- Step 1: Go to Roblox login ----
                    logger.debug(f"[{username}] Navigating to Roblox login...")
                    await page.goto(self.ROBLOX_LOGIN_URL, wait_until="networkidle", timeout=30000)
                    await asyncio.sleep(2)

                    # ---- Step 2: Fill credentials ----
                    # Roblox uses #login-username and #login-password
                    try:
                        await page.fill("#login-username", username, timeout=5000)
                        await page.fill("#login-password", password, timeout=5000)
                        await asyncio.sleep(0.5)
                    except Exception:
                        # Fallback: try generic selectors
                        try:
                            await page.fill('input[name="username"]', username, timeout=5000)
                            await page.fill('input[name="password"]', password, timeout=5000)
                        except Exception as e:
                            logger.warning(f"[{username}] Could not fill form: {e}")

                    # ---- Step 3: Click Login ----
                    logger.debug(f"[{username}] Clicking login...")
                    try:
                        await page.click("#login-button", timeout=5000)
                    except Exception:
                        try:
                            await page.click('button[type="submit"]', timeout=5000)
                        except Exception:
                            await page.keyboard.press("Enter")

                    # ---- Step 4: Wait for FunCAPTCHA ----
                    await asyncio.sleep(3)

                    # Save full page screenshot
                    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
                    full_page_path = self.output_dir / f"roblox_full_{username}_{ts}.png"
                    await page.screenshot(path=str(full_page_path), full_page=True)

                    # Try to capture CAPTCHA iframe specifically
                    captcha_found = await self._capture_captcha_iframe(page, username, ts)

                    if captcha_found:
                        captured.extend(captcha_found)
                        logger.success(f"[{username}] ✓ Captured {len(captcha_found)} CAPTCHA image(s) via {self.proxy or 'direct'}")
                    else:
                        # Maybe login succeeded (no CAPTCHA) or page structure changed
                        # Save screenshot anyway
                        logger.info(f"[{username}] No CAPTCHA detected (login may have succeeded)")
                        captured.append({
                            "image_path": str(full_page_path),
                            "challenge_type": "no_captcha",
                            "username": username,
                            "proxy": self.proxy,
                            "timestamp": ts,
                        })

                    await browser.close()
                    break  # Success, exit retry loop

            except Exception as e:
                logger.error(f"[{username}] Attempt {attempt+1}/{max_retries} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(3)

        return captured

    async def _capture_captcha_iframe(self, page, username: str, ts: str) -> List[Dict]:
        """Extract and screenshot the FunCAPTCHA iframe/game area."""
        results = []

        try:
            # FunCAPTCHA renders inside an iframe from *.arkoselabs.com or *.funcaptcha.com
            iframe_element = await page.wait_for_selector(
                'iframe[src*="arkose"], iframe[src*="funcaptcha"], iframe[src*="arkoselabs"]',
                timeout=8000,
            )

            if not iframe_element:
                return results

            # Get iframe content
            frame = await iframe_element.content_frame()
            if not frame:
                # Screenshot the iframe element itself
                captcha_path = self.output_dir / f"captcha_{username}_{ts}.png"
                await iframe_element.screenshot(path=str(captcha_path))
                results.append({
                    "image_path": str(captcha_path),
                    "challenge_type": "unknown_captcha",
                    "username": username,
                    "proxy": self.proxy,
                    "timestamp": ts,
                })
                return results

            # ---- Detect game type ----
            game_type = await self._detect_game_type(frame)

            # ---- Screenshot game area ----
            game_path = self.output_dir / f"game_{username}_{game_type}_{ts}.png"

            try:
                # Try to screenshot just the game canvas
                game_area = await frame.wait_for_selector(
                    'canvas, img[src*="game"], [class*="game-image"], [class*="challenge"]',
                    timeout=3000,
                )
                if game_area:
                    await game_area.screenshot(path=str(game_path))
                else:
                    await frame.screenshot(path=str(game_path))
            except Exception:
                # Fallback: screenshot entire iframe
                try:
                    await frame.screenshot(path=str(game_path))
                except Exception:
                    await iframe_element.screenshot(path=str(game_path))

            # ---- Extract instruction text ----
            instruction = await self._extract_instruction(frame)

            results.append({
                "image_path": str(game_path),
                "challenge_type": game_type,
                "instruction": instruction,
                "username": username,
                "proxy": self.proxy,
                "timestamp": ts,
            })

            # ---- Also capture individual tiles (for select_tiles type) ----
            tile_results = await self._capture_tiles(frame, username, game_type, ts)
            results.extend(tile_results)

        except Exception as e:
            logger.debug(f"[{username}] CAPTCHA iframe capture: {e}")

        return results

    async def _detect_game_type(self, frame) -> str:
        """Identify which type of FunCAPTCHA game is showing."""
        try:
            text = await frame.locator("body").inner_text()
            text_lower = text.lower()

            if "rotate" in text_lower or "orientation" in text_lower or "upright" in text_lower:
                return "rotate_animal"
            elif "shadow" in text_lower:
                return "shadow_match"
            elif "match" in text_lower:
                return "match_object"
            elif "select" in text_lower and "tile" in text_lower:
                return "select_tiles"
            elif "pick" in text_lower or "choose" in text_lower:
                return "pick_image"
            elif "count" in text_lower:
                return "count_objects"
        except Exception:
            pass

        # Try detecting from DOM attributes
        try:
            html = await frame.content()
            if "rotate" in html.lower():
                return "rotate_animal"
            if "shadow" in html.lower():
                return "shadow_match"
            if "tile" in html.lower():
                return "select_tiles"
        except Exception:
            pass

        return "unknown"

    async def _extract_instruction(self, frame) -> Optional[str]:
        """Extract the instruction text (e.g., 'Rotate the animal to face upright')."""
        try:
            selectors = [
                '.game-instruction',
                '[class*="instruction"]',
                '[class*="prompt"]',
                'h2', 'h3',
                '[class*="header-text"]',
                '.challenge-description',
            ]
            for sel in selectors:
                try:
                    el = await frame.wait_for_selector(sel, timeout=2000)
                    if el:
                        text = await el.inner_text()
                        if text and len(text) > 3:
                            return text.strip()
                except Exception:
                    continue
        except Exception:
            pass
        return None

    async def _capture_tiles(self, frame, username: str, game_type: str, ts: str) -> List[Dict]:
        """For tile-selection games, capture individual tile images."""
        results = []
        try:
            tiles = await frame.locator('[class*="tile"], [class*="image-cell"], [class*="grid-item"]').all()
            for i, tile in enumerate(tiles[:9]):  # Max 9 tiles
                tile_path = self.output_dir / f"tile_{username}_{game_type}_{i}_{ts}.png"
                try:
                    await tile.screenshot(path=str(tile_path))
                    results.append({
                        "image_path": str(tile_path),
                        "challenge_type": f"{game_type}_tile_{i}",
                        "username": username,
                        "proxy": self.proxy,
                        "timestamp": ts,
                    })
                except Exception:
                    pass
        except Exception:
            pass
        return results

    # ---- Multi-threaded orchestration ----

    @staticmethod
    def _run_async_collect(collector, username: str, password: str) -> List[Dict]:
        """Helper to run async collect in a thread."""
        return asyncio.run(collector.collect_from_login_attempt(username, password))

    @classmethod
    def collect_parallel(
        cls,
        accounts_file: Path,
        proxies_file: Path,
        output_dir: Path = Path("data/raw"),
        num_threads: int = 5,
        headless: bool = True,
    ) -> List[Dict]:
        """
        Collect CAPTCHA data from multiple Roblox accounts simultaneously.

        Args:
            accounts_file: File with username:password per line
            proxies_file: File with proxy per line (ip:port or user:pass@ip:port)
            output_dir: Where to save screenshots
            num_threads: Number of concurrent browser instances
            headless: Run browsers headless (True for server, False for debugging)

        Returns:
            List of metadata dicts for all collected CAPTCHA images
        """
        # Load accounts
        with open(accounts_file, "r", encoding="utf-8") as f:
            accounts = [line.strip() for line in f if line.strip()]

        # Load proxies
        with open(proxies_file, "r", encoding="utf-8") as f:
            proxies = [line.strip() for line in f if line.strip()]

        if not accounts:
            logger.error("No accounts found!")
            return []
        if not proxies:
            logger.error("No proxies found!")
            return []

        # Cycle proxies if fewer than accounts
        proxy_cycle = (proxies * ((len(accounts) // len(proxies)) + 1))[:len(accounts)]

        logger.info(f"Starting CAPTCHA collection: {len(accounts)} accounts × {num_threads} threads")
        logger.info(f"Output dir: {output_dir}")

        all_captured = []
        all_lock = threading.Lock()

        def process_one(username: str, password: str, proxy: str) -> int:
            collector = cls(output_dir=output_dir / username, proxy=proxy, headless=headless)
            try:
                results = cls._run_async_collect(collector, username, password)
            except Exception as e:
                logger.error(f"[{username}] Fatal error: {e}")
                results = []

            with all_lock:
                all_captured.extend(results)

            return len(results)

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = {}
            for i, acc in enumerate(accounts):
                parts = acc.split(":", 1)
                if len(parts) != 2:
                    logger.warning(f"Skipping invalid line: {acc}")
                    continue
                username, password = parts
                proxy = proxy_cycle[i]
                futures[executor.submit(process_one, username, password, proxy)] = username

            for future in as_completed(futures):
                username = futures[future]
                try:
                    n = future.result()
                    logger.info(f"[{username}] Done: {n} images")
                except Exception as e:
                    logger.error(f"[{username}] Thread crashed: {e}")

        # Save aggregate metadata
        meta_path = output_dir / "collection_metadata.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(all_captured, f, indent=2, ensure_ascii=False, default=str)

        logger.success(
            f"Collection complete: {len(all_captured)} images from {len(accounts)} accounts → {output_dir}"
        )
        return all_captured


# ---------------------------------------------------------------------------
#  CLI Entry Point
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Roblox FunCAPTCHA Data Collector")
    parser.add_argument("--accounts", type=Path, default=Path("accounts.txt"),
                        help="File: username:password per line")
    parser.add_argument("--proxies", type=Path, default=Path("proxies.txt"),
                        help="File: proxy per line (ip:port or user:pass@ip:port)")
    parser.add_argument("--output", type=Path, default=Path("data/raw"),
                        help="Output directory for CAPTCHA images")
    parser.add_argument("--threads", type=int, default=5,
                        help="Number of concurrent browsers")
    parser.add_argument("--no-headless", action="store_true",
                        help="Show browser windows (for debugging)")

    args = parser.parse_args()

    if not args.accounts.exists():
        logger.error(f"Accounts file not found: {args.accounts}")
        logger.info("Format: username:password per line")
        sys.exit(1)

    if not args.proxies.exists():
        logger.error(f"Proxies file not found: {args.proxies}")
        logger.info("Format: ip:port or user:pass@ip:port per line")
        sys.exit(1)

    RobloxCaptchaCollector.collect_parallel(
        accounts_file=args.accounts,
        proxies_file=args.proxies,
        output_dir=args.output,
        num_threads=args.threads,
        headless=not args.no_headless,
    )


if __name__ == "__main__":
    main()
