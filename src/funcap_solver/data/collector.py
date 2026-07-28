"""
Web scraping & data collection for FunCAPTCHA images.
Uses Playwright to capture CAPTCHA challenges and save labeled data.
"""
import asyncio
import json
import time
from pathlib import Path
from typing import Optional, List, Dict

from PIL import Image


class FunCaptchaCollector:
    """Collect FunCAPTCHA challenge data from web pages."""

    def __init__(self, output_dir: Path = Path("data/raw")):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.metadata: List[Dict] = []

    async def collect_from_url(
        self,
        url: str,
        num_challenges: int = 10,
        headless: bool = False,
    ):
        """
        Collect FunCAPTCHA challenges from a URL using Playwright.

        Args:
            url: The page URL containing FunCAPTCHA
            num_challenges: Number of challenges to collect
            headless: Whether to run browser headless
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise ImportError("playwright is required. Run: pip install playwright && playwright install")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless)
            page = await browser.new_page()
            await page.goto(url, timeout=30000)

            for i in range(num_challenges):
                try:
                    # Wait for FunCAPTCHA iframe or element
                    # FunCAPTCHA typically uses an iframe with "arkose" or "funcaptcha"
                    await page.wait_for_selector('iframe[src*="arkose"]', timeout=10000)

                    # Extract iframe content
                    iframe = page.frame_locator('iframe[src*="arkose"]')

                    # Try to capture the challenge image
                    challenge_img = await iframe.locator('img[src*="game"], canvas').first.screenshot()
                    img_path = self.output_dir / f"challenge_{i:04d}.png"
                    with open(img_path, "wb") as f:
                        f.write(challenge_img)

                    # Try to extract challenge type from DOM
                    challenge_type = await self._detect_challenge_type(iframe)
                    correct_angle = await self._extract_angle_hint(iframe)

                    self.metadata.append({
                        "image_path": str(img_path),
                        "challenge_type": challenge_type,
                        "correct_angle": correct_angle,
                        "timestamp": time.time(),
                        "source_url": url,
                    })

                    print(f"[{i+1}/{num_challenges}] Collected: type={challenge_type}, angle={correct_angle}")

                except Exception as e:
                    print(f"  [!] Failed challenge {i}: {e}")

                await asyncio.sleep(1.0)

            await browser.close()

        self._save_metadata()

    async def _detect_challenge_type(self, iframe) -> str:
        """Detect the type of FunCAPTCHA challenge."""
        try:
            text = await iframe.locator("body").inner_text()
            if "rotate" in text.lower() or "orientation" in text.lower():
                return "rotate_animal"
            elif "match" in text.lower() and "shadow" in text.lower():
                return "shadow_match"
            elif "match" in text.lower():
                return "match_object"
            elif "select" in text.lower() or "tile" in text.lower():
                return "select_tiles"
            elif "pick" in text.lower() or "choose" in text.lower():
                return "pick_image"
            elif "count" in text.lower():
                return "count_objects"
        except Exception:
            pass
        return "unknown"

    async def _extract_angle_hint(self, iframe) -> Optional[float]:
        """Try to extract the correct rotation angle from DOM."""
        try:
            # FunCAPTCHA stores the answer in data attributes or JavaScript
            angle = await iframe.locator("[data-correct-angle]").get_attribute("data-correct-angle")
            if angle:
                return float(angle)
        except Exception:
            pass
        return None

    def _save_metadata(self):
        """Save collected metadata."""
        meta_path = self.output_dir / "metadata.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(self.metadata, f, indent=2, ensure_ascii=False)
        print(f"[✓] Metadata saved to {meta_path} ({len(self.metadata)} samples)")


class DataLabeler:
    """Manual / semi-automatic labeling tool for collected CAPTCHA images."""

    def __init__(self, data_dir: Path = Path("data/raw")):
        self.data_dir = Path(data_dir)

    def label_image(self, image_path: Path, class_label: int, angle: float = 0.0):
        """Label a single image with class and angle."""
        return {
            "image_path": str(image_path),
            "class_label": class_label,
            "angle": angle,
        }

    def generate_synthetic_rotated(
        self,
        source_dir: Path,
        output_dir: Path,
        num_variations: int = 36,
    ):
        """
        Generate synthetic training data by rotating images.
        Creates num_variations rotated copies of each image with angle labels.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        annotations = []
        for img_path in sorted(source_dir.glob("*.png")) + sorted(source_dir.glob("*.jpg")):
            image = Image.open(img_path).convert("RGB")
            for i in range(num_variations):
                angle = i * (360.0 / num_variations)
                rotated = image.rotate(angle, expand=False, resample=Image.BILINEAR)
                out_name = f"{img_path.stem}_rot_{angle:.1f}.png"
                out_path = output_dir / out_name
                rotated.save(out_path)

                annotations.append({
                    "image_path": str(out_path),
                    "class_label": 0,  # rotate_animal
                    "angle": angle,
                })

        # Save annotations
        ann_path = output_dir / "annotations.json"
        with open(ann_path, "w", encoding="utf-8") as f:
            json.dump(annotations, f, indent=2)
        print(f"[✓] Generated {len(annotations)} synthetic samples in {output_dir}")

        return annotations
