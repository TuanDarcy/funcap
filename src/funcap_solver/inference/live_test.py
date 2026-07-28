"""
Live Test — Chạy model AI giải FunCAPTCHA trên Roblox thật
===========================================================
Script này mở Roblox login trong browser, bắt CAPTCHA, chạy model,
và hiển thị kết quả dự đoán NGAY TRÊN TRANG WEB.

Cách dùng:
  python -m funcap_solver.inference.live_test --checkpoint checkpoints/best_model.pt

Flow:
  1. Mở Chrome với Playwright
  2. Vào Roblox login → điền user/pass → bấm Login
  3. FunCAPTCHA xuất hiện → chụp ảnh → gửi vào model
  4. Hiển thị prediction trên console + inject vào trang web
  5. (Optional) Tự động xoay/giải CAPTCHA

Yêu cầu:
  pip install playwright && playwright install chromium
"""
import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any

from loguru import logger

# Add project root
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))


# ---------------------------------------------------------------------------
#  Live Tester
# ---------------------------------------------------------------------------

class LiveFunCaptchaTester:
    """
    End-to-end test: mở Roblox → bắt CAPTCHA → chạy model → hiển thị kết quả.

    Dùng để kiểm tra xem model đã train đúng chưa trên CAPTCHA THẬT.
    """

    ROBLOX_LOGIN_URL = "https://www.roblox.com/login"

    def __init__(
        self,
        checkpoint_path: Path,
        headless: bool = False,
        proxy: Optional[str] = None,
        auto_solve: bool = False,
    ):
        self.checkpoint_path = Path(checkpoint_path)
        self.headless = headless
        self.proxy = proxy
        self.auto_solve = auto_solve
        self.solver = None  # Lazy load
        self.results: list = []

    def _load_solver(self):
        """Lazy load model (chỉ load khi cần)."""
        if self.solver is not None:
            return

        from funcap_solver.config import Config
        from funcap_solver.inference.solver import FunCaptchaSolver

        config = Config()
        self.solver = FunCaptchaSolver(
            checkpoint_path=self.checkpoint_path,
            config=config,
        )
        logger.info(f"✅ Model loaded from {self.checkpoint_path}")

    def _build_proxy(self) -> Optional[dict]:
        if not self.proxy:
            return None
        cleaned = self.proxy.replace("http://", "").replace("https://", "")
        if "@" in cleaned:
            auth, host = cleaned.split("@", 1)
            user, pwd = auth.split(":", 1)
            return {"server": f"http://{host}", "username": user, "password": pwd}
        return {"server": f"http://{cleaned}"}

    async def run(
        self,
        username: str = "testuser123",
        password: str = "testpass123",
    ):
        """
        Chạy test trên Roblox login.

        Args:
            username: Tài khoản Roblox (có thể fake - chỉ cần trigger CAPTCHA)
            password: Mật khẩu (có thể fake)
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.error("Chưa cài playwright! Chạy: pip install playwright && playwright install chromium")
            return

        self._load_solver()

        async with async_playwright() as p:
            # Launch
            launch_opts = {
                "headless": self.headless,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--window-size=500,700",
                ],
            }
            proxy_cfg = self._build_proxy()
            if proxy_cfg:
                launch_opts["proxy"] = proxy_cfg

            browser = await p.chromium.launch(**launch_opts)
            context = await browser.new_context(
                viewport={"width": 500, "height": 700},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/132.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

            # Anti-detection
            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => false });
                window.chrome = { runtime: {} };
            """)

            # === STEP 1: Vào Roblox login ===
            print("\n" + "=" * 55)
            print("  🧪 LIVE TEST: FunCAPTCHA Solver trên Roblox")
            print("=" * 55)
            print(f"  Model: {self.checkpoint_path}")
            print(f"  URL: {self.ROBLOX_LOGIN_URL}")
            print()

            logger.info("🌐 Đang vào Roblox login...")
            await page.goto(self.ROBLOX_LOGIN_URL, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(2)

            # === STEP 2: Điền thông tin login ===
            print("📝 Điền username/password...")
            try:
                await page.fill("#login-username", username, timeout=5000)
                await page.fill("#login-password", password, timeout=5000)
            except Exception:
                try:
                    await page.fill('input[name="username"]', username, timeout=5000)
                    await page.fill('input[name="password"]', password, timeout=5000)
                except Exception:
                    logger.warning("Không tìm thấy form login, thử tiếp...")

            # === STEP 3: Bấm Login ===
            print("🖱️  Bấm Login...")
            try:
                await page.click("#login-button", timeout=5000)
            except Exception:
                try:
                    await page.click('button[type="submit"]', timeout=5000)
                except Exception:
                    await page.keyboard.press("Enter")

            # === STEP 4: Đợi FunCAPTCHA ===
            print("⏳ Đợi FunCAPTCHA xuất hiện...")
            await asyncio.sleep(3)

            # === STEP 5: Chụp + phân tích ===
            captcha_found = await self._analyze_captcha(page)

            if not captcha_found:
                # Chụp full page để debug
                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                debug_path = Path(f"debug_screenshot_{ts}.png")
                await page.screenshot(path=str(debug_path), full_page=True)
                print(f"\n⚠️  Không tìm thấy FunCAPTCHA!")
                print(f"   Có thể: (1) Login thành công luôn (2) Web đổi cấu trúc")
                print(f"   Screenshot debug: {debug_path}")

            # === STEP 6: Inject kết quả vào trang ===
            if self.results:
                await self._inject_result_overlay(page)

            print("\n" + "=" * 55)
            print("  🏁 TEST HOÀN THÀNH")
            print("=" * 55)

            # Giữ browser mở để xem nếu không headless
            if not self.headless:
                input("\nNhấn Enter để đóng browser...")

            await browser.close()

    async def _analyze_captcha(self, page) -> bool:
        """Tìm CAPTCHA trong trang, chụp ảnh, chạy model."""
        found = False

        try:
            # Tìm iframe FunCAPTCHA
            iframe_el = await page.wait_for_selector(
                'iframe[src*="arkose"], iframe[src*="funcaptcha"], iframe[src*="arkoselabs"]',
                timeout=8000,
            )

            if not iframe_el:
                return False

            frame = await iframe_el.content_frame()
            if not frame:
                return False

            found = True
            print("✅ FunCAPTCHA đã xuất hiện!\n")

            # === PHÂN TÍCH TỪNG VÙNG ===

            # 1. Chụp toàn bộ game area
            game_path = Path("live_test_game.png")
            try:
                game_area = await frame.wait_for_selector(
                    'canvas, img[src*="game"], [class*="game"], [class*="challenge"]',
                    timeout=2000,
                )
                if game_area:
                    await game_area.screenshot(path=str(game_path))
                else:
                    await frame.screenshot(path=str(game_path))
            except Exception:
                await frame.screenshot(path=str(game_path))

            # 2. Chạy model prediction
            from PIL import Image
            img_pil = Image.open(game_path).convert("RGB")
            prediction = self.solver.solve(img_pil)

            # 3. Đọc instruction text từ CAPTCHA
            instruction = await self._read_instruction(frame)

            # 4. In kết quả
            self.results.append({
                "prediction": prediction,
                "instruction": instruction,
                "screenshot": str(game_path),
            })

            self._print_prediction(prediction, instruction)

            # 5. Thử auto-solve nếu bật
            if self.auto_solve and prediction["puzzle_type"].startswith("rotate"):
                await self._auto_rotate(frame, prediction["angle"])

        except Exception as e:
            logger.debug(f"CAPTCHA analysis error: {e}")

        return found

    async def _read_instruction(self, frame) -> Optional[str]:
        """Đọc text hướng dẫn từ CAPTCHA."""
        try:
            for sel in ['.game-instruction', '[class*="instruction"]', 'h2', 'h3', '[class*="header-text"]']:
                try:
                    el = await frame.wait_for_selector(sel, timeout=1000)
                    if el:
                        text = await el.inner_text()
                        if text and len(text) > 3:
                            return text.strip()
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _print_prediction(self, prediction: dict, instruction: Optional[str]):
        """In kết quả prediction ra console."""
        puzzle_type = prediction["puzzle_type"]
        angle = prediction["angle"]
        cls_conf = prediction["cls_confidence"]

        type_emoji = {
            "rotate_animal": "🔄",
            "match_object": "🧩",
            "select_tiles": "🟫",
            "shadow_match": "👤",
            "pick_image": "🖼️",
            "count_objects": "🔢",
        }

        emoji = type_emoji.get(puzzle_type, "❓")

        print("  ┌──────────────────────────────────────────┐")
        print(f"  │  🤖 MODEL PREDICTION                     │")
        print("  ├──────────────────────────────────────────┤")
        if instruction:
            print(f"  │  📝 Captcha nói: {instruction[:35]:35s} │")
        print(f"  │  {emoji} Loại: {puzzle_type:33s} │")
        print(f"  │  📐 Góc xoay: {angle:6.1f}°                        │")
        print(f"  │  🎯 Confidence: {cls_conf:.1%}                        │")
        print("  └──────────────────────────────────────────┘")
        print()

        # Gợi ý hành động
        print("  💡 HÀNH ĐỘNG CẦN LÀM:")
        if puzzle_type == "rotate_animal":
            print(f"     → KÉO THANH TRƯỢT đến góc {angle:.0f}°")
            print(f"     → Hoặc dùng nút mũi tên xoay {angle:.0f}°")
        elif puzzle_type == "select_tiles":
            print(f"     → CLICK vào các tiles có chứa object")
        elif puzzle_type == "match_object":
            print(f"     → KÉO object vào đúng silhouette")
        elif puzzle_type == "shadow_match":
            print(f"     → GHÉP bóng vào object")
        elif puzzle_type == "pick_image":
            print(f"     → CHỌN ảnh đúng nhất")
        elif puzzle_type == "count_objects":
            print(f"     → ĐẾM và NHẬP số lượng object")
        print()

    async def _inject_result_overlay(self, page):
        """Inject overlay kết quả vào trang web để dễ xem."""
        if not self.results:
            return

        pred = self.results[-1]["prediction"]

        overlay_html = f"""
        <div id="funcap-ai-overlay" style="
            position: fixed; bottom: 10px; left: 10px; z-index: 999999;
            background: #1a1a2e; color: #e0e0e0; padding: 12px 16px;
            border-radius: 10px; font-family: monospace; font-size: 13px;
            box-shadow: 0 4px 20px rgba(0,255,100,0.3); border: 1px solid #00ff64;
            max-width: 300px;
        ">
            <div style="color: #00ff64; font-weight: bold; margin-bottom: 6px;">
              🤖 AI FunCAPTCHA Solver
            </div>
            <div>📋 Loại: <b style="color: #ffcc00;">{pred['puzzle_type']}</b></div>
            <div>📐 Góc: <b style="color: #ffcc00;">{pred['angle']:.0f}°</b></div>
            <div>🎯 Confidence: <b style="color: #ffcc00;">{pred['cls_confidence']:.1%}</b></div>
        </div>
        """
        await page.evaluate(f"document.body.insertAdjacentHTML('beforeend', `{overlay_html}`)")
        print("  ✅ Đã inject overlay kết quả vào góc trái dưới trang web")

    async def _auto_rotate(self, frame, angle: float):
        """Tự động xoay slider trong FunCAPTCHA (experimental)."""
        try:
            slider = await frame.wait_for_selector(
                'input[type="range"], [class*="slider"], [class*="rotate"]',
                timeout=2000,
            )
            if slider:
                # Tính % của góc trên 360
                pct = angle / 360.0
                # Lấy kích thước slider
                box = await slider.bounding_box()
                if box:
                    target_x = box["x"] + box["width"] * pct
                    target_y = box["y"] + box["height"] / 2
                    await frame.dispatch_event(slider, "mousedown")
                    await frame.mouse.move(target_x, target_y)
                    await frame.mouse.up()
                    print(f"  🤖 Auto-solve: đã xoay slider đến {angle:.0f}°")
        except Exception:
            logger.debug("Auto-rotate không khả dụng cho CAPTCHA này")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="🧪 Live Test FunCAPTCHA Solver trên Roblox",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ví dụ:
  # Test với model đã train
  python -m funcap_solver.inference.live_test --checkpoint checkpoints/best_model.pt

  # Test không headless (xem browser)
  python -m funcap_solver.inference.live_test --checkpoint checkpoints/best_model.pt --no-headless

  # Test với proxy
  python -m funcap_solver.inference.live_test --checkpoint checkpoints/best_model.pt --proxy user:pass@1.2.3.4:8080
        """,
    )
    parser.add_argument("--checkpoint", "-c", type=Path, required=True,
                        help="Đường dẫn đến model checkpoint (.pt)")
    parser.add_argument("--username", "-u", type=str, default="testuser_" + str(int(time.time())),
                        help="Username để login (fake cũng được)")
    parser.add_argument("--password", "-p", type=str, default="fakepass123",
                        help="Password để login (fake cũng được)")
    parser.add_argument("--proxy", type=str, default=None,
                        help="Proxy (user:pass@ip:port hoặc ip:port)")
    parser.add_argument("--no-headless", action="store_true",
                        help="Hiện browser để xem trực quan")
    parser.add_argument("--auto-solve", action="store_true",
                        help="Tự động thực hiện hành động giải CAPTCHA")

    args = parser.parse_args()

    if not args.checkpoint.exists():
        logger.error(f"Không tìm thấy checkpoint: {args.checkpoint}")
        logger.info("Hãy train model trước: python -m funcap_solver.train --mode combined")
        sys.exit(1)

    tester = LiveFunCaptchaTester(
        checkpoint_path=args.checkpoint,
        headless=not args.no_headless,
        proxy=args.proxy,
        auto_solve=args.auto_solve,
    )

    asyncio.run(tester.run(username=args.username, password=args.password))


if __name__ == "__main__":
    main()
