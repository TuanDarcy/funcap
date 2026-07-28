"""
Model definitions for FunCAPTCHA solving.

Architecture:
  1. CaptchaClassifier  - Classifies the puzzle type (rotate, match, select, etc.)
  2. RotationPredictor  - Predicts the rotation angle needed to solve the puzzle
  3. FunCaptchaModel    - Combined model with shared backbone
"""
from typing import Dict, Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F
from transformers import ViTModel, ViTConfig, ViTImageProcessor
from peft import LoraConfig, get_peft_model, TaskType


# ---------------------------------------------------------------------------
#  1. Classifier - Identify which type of FunCAPTCHA puzzle
# ---------------------------------------------------------------------------

class CaptchaClassifier(nn.Module):
    """Vision Transformer classifier for FunCAPTCHA puzzle type detection."""

    def __init__(
        self,
        backbone_name: str = "google/vit-base-patch16-224",
        num_classes: int = 6,
        dropout: float = 0.1,
        freeze_backbone: bool = False,
        use_lora: bool = False,
        lora_r: int = 16,
        lora_alpha: int = 32,
    ):
        super().__init__()
        self.backbone_name = backbone_name
        self.num_classes = num_classes

        # Load pretrained ViT backbone
        self.vit = ViTModel.from_pretrained(backbone_name)
        self.config = self.vit.config
        hidden_size = self.config.hidden_size

        if use_lora:
            lora_config = LoraConfig(
                task_type=TaskType.FEATURE_EXTRACTION,
                r=lora_r,
                lora_alpha=lora_alpha,
                target_modules=["query", "key", "value"],
                lora_dropout=0.1,
            )
            self.vit = get_peft_model(self.vit, lora_config)
            self.vit.print_trainable_parameters()

        if freeze_backbone and not use_lora:
            for param in self.vit.parameters():
                param.requires_grad = False

        # Classification head
        self.classifier = nn.Sequential(
            nn.LayerNorm(hidden_size),
            nn.Dropout(dropout),
            nn.Linear(hidden_size, hidden_size // 2),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_size // 2, num_classes),
        )

    def forward(
        self, pixel_values: torch.Tensor
    ) -> Dict[str, torch.Tensor]:
        vit_outputs = self.vit(pixel_values=pixel_values)
        pooled = vit_outputs.last_hidden_state[:, 0, :]  # CLS token
        logits = self.classifier(pooled)
        return {"logits": logits, "features": pooled}


# ---------------------------------------------------------------------------
#  2. Rotation Predictor - Predict the angle to rotate
# ---------------------------------------------------------------------------

class RotationPredictor(nn.Module):
    """
    Predict rotation angle for orientation-based FunCAPTCHA puzzles.
    Uses classification over 360-degree bins with a soft target.
    """

    def __init__(
        self,
        backbone_name: str = "google/vit-base-patch16-224",
        num_bins: int = 360,
        dropout: float = 0.1,
        use_lora: bool = False,
        lora_r: int = 16,
        lora_alpha: int = 32,
    ):
        super().__init__()
        self.num_bins = num_bins

        self.vit = ViTModel.from_pretrained(backbone_name)
        hidden_size = self.vit.config.hidden_size

        if use_lora:
            lora_config = LoraConfig(
                task_type=TaskType.FEATURE_EXTRACTION,
                r=lora_r,
                lora_alpha=lora_alpha,
                target_modules=["query", "key", "value"],
                lora_dropout=0.1,
            )
            self.vit = get_peft_model(self.vit, lora_config)

        # Angle prediction head
        self.angle_head = nn.Sequential(
            nn.LayerNorm(hidden_size),
            nn.Dropout(dropout),
            nn.Linear(hidden_size, hidden_size // 2),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_size // 2, num_bins),  # 360 bins = 1 degree each
        )

    def forward(self, pixel_values: torch.Tensor) -> Dict[str, torch.Tensor]:
        vit_outputs = self.vit(pixel_values=pixel_values)
        pooled = vit_outputs.last_hidden_state[:, 0, :]
        logits = self.angle_head(pooled)
        return {"logits": logits, "features": pooled}

    def predict_angle(self, pixel_values: torch.Tensor) -> torch.Tensor:
        """Predict continuous angle from softmax-weighted bins."""
        logits = self.forward(pixel_values)["logits"]
        probs = F.softmax(logits, dim=-1)
        bins = torch.arange(0, self.num_bins, device=logits.device, dtype=torch.float)
        angle = (probs * bins).sum(dim=-1)
        return angle


# ---------------------------------------------------------------------------
#  3. Combined FunCAPTCHA Model
# ---------------------------------------------------------------------------

class FunCaptchaModel(nn.Module):
    """
    Combined model that:
      - Classifies the puzzle type
      - Predicts the rotation angle for orientation puzzles

    Uses a shared ViT backbone for efficiency.
    """

    def __init__(
        self,
        backbone_name: str = "google/vit-base-patch16-224",
        num_classes: int = 6,
        num_angle_bins: int = 360,
        dropout: float = 0.1,
        use_lora: bool = True,
        lora_r: int = 16,
        lora_alpha: int = 32,
    ):
        super().__init__()
        self.num_classes = num_classes
        self.num_angle_bins = num_angle_bins

        # Shared backbone
        self.vit = ViTModel.from_pretrained(backbone_name)
        hidden_size = self.vit.config.hidden_size

        if use_lora:
            lora_config = LoraConfig(
                task_type=TaskType.FEATURE_EXTRACTION,
                r=lora_r,
                lora_alpha=lora_alpha,
                target_modules=["query", "key", "value"],
                lora_dropout=0.1,
            )
            self.vit = get_peft_model(self.vit, lora_config)

        # Dual heads
        self.cls_head = nn.Sequential(
            nn.LayerNorm(hidden_size),
            nn.Dropout(dropout),
            nn.Linear(hidden_size, hidden_size // 2),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_size // 2, num_classes),
        )

        self.angle_head = nn.Sequential(
            nn.LayerNorm(hidden_size),
            nn.Dropout(dropout),
            nn.Linear(hidden_size, hidden_size // 2),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_size // 2, num_angle_bins),
        )

    def forward(self, pixel_values: torch.Tensor) -> Dict[str, torch.Tensor]:
        vit_outputs = self.vit(pixel_values=pixel_values)
        pooled = vit_outputs.last_hidden_state[:, 0, :]

        cls_logits = self.cls_head(pooled)
        angle_logits = self.angle_head(pooled)

        return {
            "cls_logits": cls_logits,
            "angle_logits": angle_logits,
            "features": pooled,
        }


def get_processor(model_name: str = "google/vit-base-patch16-224"):
    """Get the image processor for ViT."""
    return ViTImageProcessor.from_pretrained(model_name)
