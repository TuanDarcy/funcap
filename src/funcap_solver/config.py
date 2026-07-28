"""
FunCAPTCHA Solver - Configuration
Central config for model training, data processing, and inference.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Literal

@dataclass
class ModelConfig:
    """Model architecture configuration."""
    # --- Classification Model ---
    classifier_backbone: str = "google/vit-base-patch16-224"
    num_classes: int = 6  # FunCAPTCHA puzzle types: rotate, match, shadow, etc.
    classifier_dropout: float = 0.1
    classifier_freeze_backbone: bool = False

    # --- Orientation / Rotation Model ---
    rotation_model: str = "google/vit-base-patch16-224"
    rotation_bins: int = 360  # Predict rotation angle in degrees
    rotation_loss: Literal["mse", "classification"] = "classification"

    # --- Image size ---
    image_size: int = 224


@dataclass
class TrainingConfig:
    """Training hyperparameters."""
    # General
    seed: int = 42
    output_dir: Path = Path("checkpoints")

    # Classification training
    cls_epochs: int = 20
    cls_batch_size: int = 32
    cls_learning_rate: float = 2e-4
    cls_weight_decay: float = 0.01
    cls_warmup_ratio: float = 0.1

    # Fine-tuning training
    ft_epochs: int = 15
    ft_batch_size: int = 16
    ft_learning_rate: float = 5e-5
    ft_weight_decay: float = 0.01
    ft_warmup_ratio: float = 0.1

    # LoRA (Parameter-Efficient Fine-Tuning)
    use_lora: bool = True
    lora_r: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.1

    # Optimizer
    optimizer: Literal["adamw", "sgd"] = "adamw"
    scheduler: Literal["cosine", "linear", "cosine_with_restarts"] = "cosine"
    gradient_accumulation_steps: int = 2
    max_grad_norm: float = 1.0

    # Early stopping
    early_stopping_patience: int = 5
    save_best_only: bool = True
    eval_steps: int = 200
    logging_steps: int = 50


@dataclass
class DataConfig:
    """Data pipeline configuration."""
    raw_dir: Path = Path("data/raw")
    processed_dir: Path = Path("data/processed")

    # Train/Val/Test split
    train_ratio: float = 0.7
    val_ratio: float = 0.15
    test_ratio: float = 0.15

    # Augmentation
    use_augmentation: bool = True
    aug_rotation_degree: int = 10
    aug_brightness: float = 0.2
    aug_contrast: float = 0.2
    aug_horizontal_flip: bool = False  # FunCAPTCHA rotation tasks are orientation-sensitive

    # Class labels for FunCAPTCHA puzzle types
    class_labels: List[str] = field(default_factory=lambda: [
        "rotate_animal",      # Rotate animal to correct orientation
        "match_object",       # Match the object to silhouette
        "select_tiles",       # Select tiles containing object
        "shadow_match",       # Match shadow to object
        "pick_image",         # Pick the correct image
        "count_objects",      # Count objects in image
    ])


@dataclass
class CollabConfig:
    """Google Colab collaboration settings."""
    use_gpu: bool = True
    use_tpu: bool = False
    mixed_precision: Literal["no", "fp16", "bf16"] = "fp16"
    mount_drive: bool = True
    drive_data_path: str = "/content/drive/MyDrive/funcap_data"
    ngrok_auth_token: Optional[str] = None  # For collaborative access


@dataclass
class Config:
    """Master configuration."""
    model: ModelConfig = field(default_factory=ModelConfig)
    training: TrainingConfig = field(default_factory=TrainingConfig)
    data: DataConfig = field(default_factory=DataConfig)
    collab: CollabConfig = field(default_factory=CollabConfig)


# Default config instance
default_config = Config()
