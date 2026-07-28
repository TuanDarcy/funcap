"""
Inference module - Solves FunCAPTCHA challenges using trained models.
"""
from pathlib import Path
from typing import Dict, Optional, Tuple

import torch
import torch.nn.functional as F
from PIL import Image
from torchvision import transforms

from ..config import Config
from ..models.model import CaptchaClassifier, RotationPredictor, FunCaptchaModel, get_processor


class FunCaptchaSolver:
    """
    End-to-end FunCAPTCHA solver.

    Usage:
        solver = FunCaptchaSolver("checkpoints/best_model.pt")
        result = solver.solve("captcha_image.png")
        # result = {"type": "rotate_animal", "angle": 45.0, "confidence": 0.95}
    """

    def __init__(
        self,
        checkpoint_path: Optional[Path] = None,
        config: Config = Config(),
        device: Optional[torch.device] = None,
    ):
        self.config = config
        self.device = device or torch.device("cuda" if torch.cuda.is_available() else "cpu")

        self.model = self._load_model(checkpoint_path)
        self.transform = transforms.Compose([
            transforms.Resize((config.model.image_size, config.model.image_size)),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        ])

        self.class_names = config.data.class_labels

    def _load_model(self, checkpoint_path: Optional[Path]):
        """Load model from checkpoint."""
        model = FunCaptchaModel(
            backbone_name=self.config.model.classifier_backbone,
            num_classes=self.config.model.num_classes,
            num_angle_bins=self.config.model.rotation_bins,
            dropout=self.config.model.classifier_dropout,
            use_lora=False,  # Inference uses merged weights
        )

        if checkpoint_path and Path(checkpoint_path).exists():
            ckpt = torch.load(checkpoint_path, map_location=self.device, weights_only=False)
            model.load_state_dict(ckpt["model_state_dict"], strict=False)
            print(f"[✓] Loaded checkpoint from {checkpoint_path}")

        model.to(self.device)
        model.eval()
        return model

    @torch.no_grad()
    def solve(self, image: Image.Image) -> Dict:
        """
        Solve a FunCAPTCHA from a PIL Image.

        Returns:
            dict with keys: puzzle_type, angle, confidence, raw_logits
        """
        # Preprocess
        img_tensor = self.transform(image).unsqueeze(0).to(self.device)

        # Inference
        outputs = self.model(img_tensor)

        # Classification
        cls_probs = F.softmax(outputs["cls_logits"], dim=-1)
        cls_conf, cls_pred = cls_probs.max(dim=-1)
        puzzle_type = self.class_names[cls_pred.item()]

        # Angle prediction
        angle_probs = F.softmax(outputs["angle_logits"], dim=-1)
        angle_conf, angle_bin = angle_probs.max(dim=-1)
        angle = angle_bin.item() * (360.0 / self.config.model.rotation_bins)

        # For rotation-type puzzles, use weighted average for smoother prediction
        if puzzle_type.startswith("rotate"):
            bins = torch.arange(0, self.config.model.rotation_bins, device=angle_probs.device, dtype=torch.float)
            weighted_angle = (angle_probs.squeeze() * bins).sum() * (360.0 / self.config.model.rotation_bins)
            angle = weighted_angle.item()

        return {
            "puzzle_type": puzzle_type,
            "angle": angle,
            "cls_confidence": round(cls_conf.item(), 4),
            "angle_confidence": round(angle_conf.item(), 4),
        }

    def solve_batch(self, images: list) -> list:
        """Solve multiple CAPTCHA images at once."""
        results = []
        for img in images:
            if isinstance(img, (str, Path)):
                img = Image.open(img).convert("RGB")
            result = self.solve(img)
            results.append(result)
        return results
