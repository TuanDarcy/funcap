"""
Training module - fine-tuning pipeline for FunCAPTCHA models.
Supports both classification training and rotation angle prediction.
"""
import math
import os
from pathlib import Path
from typing import Dict, Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.optim import AdamW
from torch.optim.lr_scheduler import CosineAnnealingLR, CosineAnnealingWarmRestarts, LinearLR
from torch.cuda.amp import GradScaler, autocast
from tqdm import tqdm
import numpy as np
from sklearn.metrics import accuracy_score, f1_score, confusion_matrix
import matplotlib.pyplot as plt
import seaborn as sns

from ..config import Config, ModelConfig, TrainingConfig
from ..models.model import CaptchaClassifier, RotationPredictor, FunCaptchaModel
from ..data.dataset import FunCaptchaDataset, create_dataloaders
from ..utils.metrics import ClassificationMetrics, AngleMetrics


class Trainer:
    """Unified trainer for FunCAPTCHA model fine-tuning."""

    def __init__(
        self,
        model: nn.Module,
        config: TrainingConfig,
        device: Optional[torch.device] = None,
        use_wandb: bool = False,
    ):
        self.model = model
        self.config = config
        self.device = device or torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.model.to(self.device)
        self.use_wandb = use_wandb

        # Optimizer
        self.optimizer = AdamW(
            self.model.parameters(),
            lr=config.cls_learning_rate,
            weight_decay=config.cls_weight_decay,
        )

        # AMP scaler
        self.scaler = GradScaler(enabled=(config.use_amp if hasattr(config, "use_amp") else True))

        if use_wandb:
            import wandb
            wandb.watch(self.model)

    def train_epoch(
        self,
        dataloader,
        epoch: int,
        task: str = "classification",
    ) -> Dict[str, float]:
        """Train one epoch."""
        self.model.train()
        total_loss = 0.0
        all_preds, all_labels = [], []

        pbar = tqdm(dataloader, desc=f"Epoch {epoch} [train]")
        for step, batch in enumerate(pbar):
            pixel_values = batch["pixel_values"].to(self.device)
            class_labels = batch["class_label"].to(self.device)
            angles = batch.get("angle", None)
            if angles is not None:
                angles = angles.to(self.device)

            with autocast(enabled=True):
                outputs = self.model(pixel_values)

                if task == "classification":
                    loss = F.cross_entropy(outputs["cls_logits"], class_labels)
                    preds = outputs["cls_logits"].argmax(dim=-1)
                elif task == "angle":
                    # Soft classification: convert angle to bin indices
                    angle_bins = (angles / (360.0 / outputs["angle_logits"].size(-1))).long()
                    angle_bins = angle_bins.clamp(0, outputs["angle_logits"].size(-1) - 1)
                    loss = F.cross_entropy(outputs["angle_logits"], angle_bins)
                    preds = outputs["angle_logits"].argmax(dim=-1)
                elif task == "combined":
                    cls_loss = F.cross_entropy(outputs["cls_logits"], class_labels)
                    angle_bins = (angles / (360.0 / outputs["angle_logits"].size(-1))).long()
                    angle_bins = angle_bins.clamp(0, outputs["angle_logits"].size(-1) - 1)
                    angle_loss = F.cross_entropy(outputs["angle_logits"], angle_bins)
                    loss = cls_loss + 0.5 * angle_loss
                    preds = outputs["cls_logits"].argmax(dim=-1)
                else:
                    raise ValueError(f"Unknown task: {task}")

            # Backward
            self.scaler.scale(loss).backward()

            if (step + 1) % self.config.gradient_accumulation_steps == 0:
                self.scaler.unscale_(self.optimizer)
                nn.utils.clip_grad_norm_(self.model.parameters(), self.config.max_grad_norm)
                self.scaler.step(self.optimizer)
                self.scaler.update()
                self.optimizer.zero_grad()

            total_loss += loss.item()
            all_preds.extend(preds.cpu().tolist())
            all_labels.extend(class_labels.cpu().tolist())

            pbar.set_postfix({"loss": f"{loss.item():.4f}"})

            if self.config.logging_steps and step % self.config.logging_steps == 0 and self.use_wandb:
                import wandb
                wandb.log({f"train/{task}_loss": loss.item()})

        avg_loss = total_loss / len(dataloader)
        acc = accuracy_score(all_labels, all_preds)

        return {"loss": avg_loss, "accuracy": acc}

    @torch.no_grad()
    def evaluate(
        self, dataloader, task: str = "classification"
    ) -> Dict[str, float]:
        """Evaluate model on validation/test set."""
        self.model.eval()
        total_loss = 0.0
        all_preds, all_labels = [], []
        all_angle_preds, all_angle_labels = [], []

        for batch in tqdm(dataloader, desc="[eval]"):
            pixel_values = batch["pixel_values"].to(self.device)
            class_labels = batch["class_label"].to(self.device)
            angles = batch.get("angle", None)
            if angles is not None:
                angles = angles.to(self.device)

            outputs = self.model(pixel_values)

            if task in ("classification", "combined"):
                cls_loss = F.cross_entropy(outputs["cls_logits"], class_labels)
                preds = outputs["cls_logits"].argmax(dim=-1)
                total_loss += cls_loss.item()
                all_preds.extend(preds.cpu().tolist())
                all_labels.extend(class_labels.cpu().tolist())

            if task in ("angle", "combined") and angles is not None:
                angle_bins = (angles / (360.0 / outputs["angle_logits"].size(-1))).long()
                angle_bins = angle_bins.clamp(0, outputs["angle_logits"].size(-1) - 1)
                angle_preds = outputs["angle_logits"].argmax(dim=-1)
                all_angle_preds.extend(angle_preds.cpu().tolist())
                all_angle_labels.extend(angle_bins.cpu().tolist())

        metrics = {}
        if all_preds:
            metrics["accuracy"] = accuracy_score(all_labels, all_preds)
            metrics["f1"] = f1_score(all_labels, all_preds, average="macro")
            metrics["loss"] = total_loss / len(dataloader)

        if all_angle_preds:
            angle_acc = sum(1 for p, t in zip(all_angle_preds, all_angle_labels) if p == t)
            metrics["angle_accuracy"] = angle_acc / len(all_angle_preds)
            angle_errors = [min(abs(p - t), 360 - abs(p - t)) for p, t in zip(all_angle_preds, all_angle_labels)]
            metrics["angle_mae"] = np.mean(angle_errors)

        return metrics

    def fit(
        self,
        train_loader,
        val_loader,
        task: str = "classification",
        checkpoint_dir: Optional[Path] = None,
    ):
        """Full training loop with early stopping & checkpointing."""
        checkpoint_dir = Path(checkpoint_dir or self.config.output_dir)
        checkpoint_dir.mkdir(parents=True, exist_ok=True)

        best_val_acc = 0.0
        best_val_loss = float("inf")
        patience_counter = 0
        history = {"train_loss": [], "train_acc": [], "val_loss": [], "val_acc": []}

        num_epochs = getattr(self.config, f"{task}_epochs", self.config.cls_epochs)

        for epoch in range(1, num_epochs + 1):
            train_metrics = self.train_epoch(train_loader, epoch, task)
            val_metrics = self.evaluate(val_loader, task)

            history["train_loss"].append(train_metrics["loss"])
            history["train_acc"].append(train_metrics["accuracy"])
            history["val_loss"].append(val_metrics.get("loss", 0))
            history["val_acc"].append(val_metrics.get("accuracy", 0))

            print(
                f"Epoch {epoch:3d} | "
                f"Train Loss: {train_metrics['loss']:.4f} Acc: {train_metrics['accuracy']:.4f} | "
                f"Val Loss: {val_metrics.get('loss', 0):.4f} Acc: {val_metrics.get('accuracy', 0):.4f}"
            )

            if self.use_wandb:
                import wandb
                wandb.log({
                    "epoch": epoch,
                    "train/loss": train_metrics["loss"],
                    "train/accuracy": train_metrics["accuracy"],
                    "val/loss": val_metrics.get("loss", 0),
                    "val/accuracy": val_metrics.get("accuracy", 0),
                })

            # Save best model
            val_acc = val_metrics.get("accuracy", 0)
            if val_acc > best_val_acc:
                best_val_acc = val_acc
                patience_counter = 0
                torch.save({
                    "epoch": epoch,
                    "model_state_dict": self.model.state_dict(),
                    "optimizer_state_dict": self.optimizer.state_dict(),
                    "val_metrics": val_metrics,
                }, checkpoint_dir / "best_model.pt")
                print(f"  [✓] Saved best model (acc={best_val_acc:.4f})")
            else:
                patience_counter += 1

            if patience_counter >= self.config.early_stopping_patience:
                print(f"  [!] Early stopping at epoch {epoch}")
                break

        return history

    def plot_history(self, history: Dict, save_path: Optional[Path] = None):
        """Plot training history."""
        fig, axes = plt.subplots(1, 2, figsize=(14, 5))

        axes[0].plot(history["train_loss"], label="Train Loss")
        axes[0].plot(history["val_loss"], label="Val Loss")
        axes[0].set_xlabel("Epoch")
        axes[0].set_ylabel("Loss")
        axes[0].set_title("Loss Curve")
        axes[0].legend()
        axes[0].grid(True, alpha=0.3)

        axes[1].plot(history["train_acc"], label="Train Acc")
        axes[1].plot(history["val_acc"], label="Val Acc")
        axes[1].set_xlabel("Epoch")
        axes[1].set_ylabel("Accuracy")
        axes[1].set_title("Accuracy Curve")
        axes[1].legend()
        axes[1].grid(True, alpha=0.3)

        plt.tight_layout()
        if save_path:
            plt.savefig(save_path, dpi=150, bbox_inches="tight")
        plt.show()


def train_classifier(config: Config, data_dir: Path):
    """Train the puzzle type classifier."""
    print("[*] Training CaptchaClassifier...")
    train_loader, val_loader, test_loader = create_dataloaders(
        data_dir,
        batch_size=config.training.cls_batch_size,
        image_size=config.model.image_size,
    )

    model = CaptchaClassifier(
        backbone_name=config.model.classifier_backbone,
        num_classes=config.model.num_classes,
        dropout=config.model.classifier_dropout,
        freeze_backbone=config.model.classifier_freeze_backbone,
        use_lora=config.training.use_lora,
        lora_r=config.training.lora_r,
        lora_alpha=config.training.lora_alpha,
    )

    trainer = Trainer(model, config.training)
    history = trainer.fit(train_loader, val_loader, task="classification")
    test_metrics = trainer.evaluate(test_loader, task="classification")
    print(f"\n[✓] Test Results: {test_metrics}")

    return model, history, test_metrics


def train_rotation_predictor(config: Config, data_dir: Path):
    """Train the rotation angle predictor."""
    print("[*] Training RotationPredictor...")
    train_loader, val_loader, test_loader = create_dataloaders(
        data_dir,
        batch_size=config.training.ft_batch_size,
        image_size=config.model.image_size,
    )

    model = RotationPredictor(
        backbone_name=config.model.rotation_model,
        num_bins=config.model.rotation_bins,
        dropout=config.model.classifier_dropout,
        use_lora=config.training.use_lora,
        lora_r=config.training.lora_r,
        lora_alpha=config.training.lora_alpha,
    )

    trainer = Trainer(model, config.training)
    history = trainer.fit(train_loader, val_loader, task="angle")
    test_metrics = trainer.evaluate(test_loader, task="angle")
    print(f"\n[✓] Test Results: {test_metrics}")

    return model, history, test_metrics


def train_combined(config: Config, data_dir: Path):
    """Train the combined classification + rotation model."""
    print("[*] Training FunCaptchaModel (combined)...")
    train_loader, val_loader, test_loader = create_dataloaders(
        data_dir,
        batch_size=config.training.ft_batch_size,
        image_size=config.model.image_size,
    )

    model = FunCaptchaModel(
        backbone_name=config.model.classifier_backbone,
        num_classes=config.model.num_classes,
        num_angle_bins=config.model.rotation_bins,
        dropout=config.model.classifier_dropout,
        use_lora=config.training.use_lora,
        lora_r=config.training.lora_r,
        lora_alpha=config.training.lora_alpha,
    )

    trainer = Trainer(model, config.training)
    history = trainer.fit(train_loader, val_loader, task="combined")
    test_metrics = trainer.evaluate(test_loader, task="combined")
    print(f"\n[✓] Test Results: {test_metrics}")

    return model, history, test_metrics
