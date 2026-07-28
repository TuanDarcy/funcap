"""
Data collection & preprocessing module for FunCAPTCHA.
Handles image capture, labeling, augmentation, and dataset creation.
"""
import random
from pathlib import Path
from typing import List, Tuple, Optional, Dict

import numpy as np
from PIL import Image
import torch
from torch.utils.data import Dataset, DataLoader
from torchvision import transforms
import albumentations as A
from albumentations.pytorch import ToTensorV2


class FunCaptchaDataset(Dataset):
    """PyTorch Dataset for FunCAPTCHA images with type + angle labels."""

    def __init__(
        self,
        data_dir: Path,
        split: str = "train",
        transform=None,
        image_size: int = 224,
    ):
        self.data_dir = Path(data_dir) / split
        self.transform = transform
        self.image_size = image_size

        # Load annotation file
        self.samples = self._load_annotations()

    def _load_annotations(self) -> List[Dict]:
        """Load annotations CSV/JSON with columns: image_path, class_label, angle."""
        import json

        ann_file = self.data_dir / "annotations.json"
        if ann_file.exists():
            with open(ann_file, "r", encoding="utf-8") as f:
                return json.load(f)

        # Fallback: scan directory for images
        samples = []
        for img_path in sorted(self.data_dir.glob("*.png")) + sorted(self.data_dir.glob("*.jpg")):
            # Parse label from filename: class_angle_imageid.png
            parts = img_path.stem.split("_")
            class_label = parts[0] if len(parts) > 0 else 0
            angle = float(parts[1]) if len(parts) > 1 else 0.0
            samples.append({
                "image_path": str(img_path),
                "class_label": int(class_label),
                "angle": angle,
            })
        return samples

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, idx: int) -> Dict[str, torch.Tensor]:
        sample = self.samples[idx]
        image = Image.open(sample["image_path"]).convert("RGB")

        if self.transform:
            image_np = np.array(image)
            augmented = self.transform(image=image_np)
            image = augmented["image"]
        else:
            image = transforms.Compose([
                transforms.Resize((self.image_size, self.image_size)),
                transforms.ToTensor(),
                transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                    std=[0.229, 0.224, 0.225]),
            ])(image)

        return {
            "pixel_values": image,
            "class_label": torch.tensor(sample["class_label"], dtype=torch.long),
            "angle": torch.tensor(sample["angle"], dtype=torch.float),
            "image_path": sample["image_path"],
        }


def get_train_transform(image_size: int = 224) -> A.Compose:
    """Training augmentations."""
    return A.Compose([
        A.Resize(image_size, image_size),
        A.Rotate(limit=(-10, 10), p=0.5),
        A.RandomBrightnessContrast(brightness_limit=0.2, contrast_limit=0.2, p=0.5),
        A.HueSaturationValue(hue_shift_limit=10, sat_shift_limit=20, val_shift_limit=10, p=0.3),
        A.GaussNoise(var_limit=(10.0, 30.0), p=0.2),
        A.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        ToTensorV2(),
    ])


def get_val_transform(image_size: int = 224) -> A.Compose:
    """Validation augmentations (resize + normalize only)."""
    return A.Compose([
        A.Resize(image_size, image_size),
        A.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        ToTensorV2(),
    ])


def create_dataloaders(
    data_dir: Path,
    batch_size: int = 32,
    image_size: int = 224,
    num_workers: int = 4,
) -> Tuple[DataLoader, DataLoader, DataLoader]:
    """Create train/val/test dataloaders."""
    train_dataset = FunCaptchaDataset(
        data_dir, split="train",
        transform=get_train_transform(image_size),
        image_size=image_size,
    )
    val_dataset = FunCaptchaDataset(
        data_dir, split="val",
        transform=get_val_transform(image_size),
        image_size=image_size,
    )
    test_dataset = FunCaptchaDataset(
        data_dir, split="test",
        transform=get_val_transform(image_size),
        image_size=image_size,
    )

    train_loader = DataLoader(
        train_dataset, batch_size=batch_size, shuffle=True,
        num_workers=num_workers, pin_memory=True,
    )
    val_loader = DataLoader(
        val_dataset, batch_size=batch_size, shuffle=False,
        num_workers=num_workers, pin_memory=True,
    )
    test_loader = DataLoader(
        test_dataset, batch_size=batch_size, shuffle=False,
        num_workers=num_workers, pin_memory=True,
    )

    return train_loader, val_loader, test_loader
