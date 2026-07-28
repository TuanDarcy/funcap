"""
Data pipeline for FunCAPTCHA training.

CÁCH TỔ CHỨC DỮ LIỆU (2 cách):
================================

Cách 1: Tổ chức theo FOLDER (khuyến nghị - dễ quản lý nhất)
-----------------------------------------------------------
data/labeled/
  rotate_animal/       ← ảnh capcha dạng "xoay con vật"
    capcha_001.png
    capcha_002.png
  shadow_match/        ← ảnh capcha dạng "ghép bóng"
    capcha_003.png
  select_tiles/
    ...
  match_object/
  pick_image/
  count_objects/

=> Dataset tự đọc tên folder làm label. Bạn CHỈ CẦN kéo ảnh vào đúng folder.

Cách 2: File annotations.json (nâng cao - có kèm góc xoay)
-----------------------------------------------------------
[
  {"image_path": "img001.png", "class_label": 0, "angle": 45.0},
  {"image_path": "img002.png", "class_label": 1, "angle": 0.0},
]

TÊN CLASS & INDEX:
=================
  0: rotate_animal   - "Xoay con vật về đúng hướng"
  1: match_object    - "Ghép object với silhouette"
  2: select_tiles    - "Chọn tiles chứa object"
  3: shadow_match    - "Ghép bóng với object"
  4: pick_image      - "Chọn ảnh đúng"
  5: count_objects   - "Đếm số object"

DÙNG TOOL LABELING:
===================
python -m funcap_solver.data.labeler --input data/raw --output data/labeled

=> Mở ảnh lên, bấm phím 0-5 để phân loại, tự động move vào folder tương ứng.
"""
import json
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


# Mapping giữa tên folder và index
FOLDER_TO_LABEL: Dict[str, int] = {
    "rotate_animal": 0,
    "match_object": 1,
    "select_tiles": 2,
    "shadow_match": 3,
    "pick_image": 4,
    "count_objects": 5,
}

LABEL_TO_NAME: Dict[int, str] = {v: k for k, v in FOLDER_TO_LABEL.items()}


class FolderDataset(Dataset):
    """
    ⭐ Dataset đọc ảnh từ cấu trúc folder.

    Cấu trúc:
      data_dir/
        train/
          rotate_animal/
            img1.png
          shadow_match/
            img2.png
        val/
          rotate_animal/
            img3.png

    Label được lấy từ TÊN FOLDER. Không cần file annotation.
    """

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
        self.samples: List[Dict] = []
        self.class_names: List[str] = []

        self._scan_folders()

        if len(self.samples) == 0:
            print(f"[!] WARNING: No images found in {self.data_dir}")
            print(f"    Expected structure: {self.data_dir}/rotate_animal/*.png")

    def _scan_folders(self):
        """Quét tất cả folder con, mỗi folder = 1 class."""
        if not self.data_dir.exists():
            return

        for folder_name, label_idx in sorted(FOLDER_TO_LABEL.items()):
            folder_path = self.data_dir / folder_name
            if not folder_path.is_dir():
                continue

            if folder_name not in self.class_names:
                self.class_names.append(folder_name)

            for ext in ["*.png", "*.jpg", "*.jpeg", "*.webp"]:
                for img_path in sorted(folder_path.glob(ext)):
                    self.samples.append({
                        "image_path": str(img_path),
                        "class_label": label_idx,
                        "class_name": folder_name,
                        "angle": 0.0,  # Folder mode không có angle
                    })

        # In thống kê
        if self.samples:
            from collections import Counter
            cnt = Counter(s["class_name"] for s in self.samples)
            print(f"[FolderDataset] {self.data_dir}: {len(self.samples)} ảnh")
            for name, n in sorted(cnt.items()):
                print(f"  {name}: {n}")

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


class FunCaptchaDataset(Dataset):
    """
    Dataset với file annotations.json (có angle label).
    Dùng khi bạn cần train cả góc xoay.
    """

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

        self.samples = self._load_annotations()

    def _load_annotations(self) -> List[Dict]:
        ann_file = self.data_dir / "annotations.json"
        if ann_file.exists():
            with open(ann_file, "r", encoding="utf-8") as f:
                return json.load(f)

        samples = []
        for img_path in sorted(self.data_dir.glob("*.png")) + sorted(self.data_dir.glob("*.jpg")):
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


# ---------------------------------------------------------------------------
#  Splitting utility
# ---------------------------------------------------------------------------

def split_into_train_val(
    source_dir: Path,
    output_dir: Path,
    train_ratio: float = 0.8,
    seed: int = 42,
):
    """
    Tự động chia ảnh từ folder source thành train/val theo tỉ lệ.

    Source: data/labeled/rotate_animal/*.png
    Output:
      data/split/train/rotate_animal/*.png  (80%)
      data/split/val/rotate_animal/*.png    (20%)
    """
    random.seed(seed)
    output_dir = Path(output_dir)

    for class_name in FOLDER_TO_LABEL:
        src_folder = Path(source_dir) / class_name
        if not src_folder.is_dir():
            continue

        all_images = list(src_folder.glob("*.png")) + list(src_folder.glob("*.jpg")) + list(src_folder.glob("*.jpeg"))
        random.shuffle(all_images)

        n_train = int(len(all_images) * train_ratio)
        train_imgs = all_images[:n_train]
        val_imgs = all_images[n_train:]

        for split, imgs in [("train", train_imgs), ("val", val_imgs)]:
            dst_folder = output_dir / split / class_name
            dst_folder.mkdir(parents=True, exist_ok=True)
            for img in imgs:
                import shutil
                shutil.copy2(img, dst_folder / img.name)

        print(f"  {class_name}: {len(train_imgs)} train + {len(val_imgs)} val")

    print(f"Done → {output_dir}")
    return output_dir


# ---------------------------------------------------------------------------
#  Transforms
# ---------------------------------------------------------------------------

def get_train_transform(image_size: int = 224) -> A.Compose:
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
    return A.Compose([
        A.Resize(image_size, image_size),
        A.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        ToTensorV2(),
    ])


# ---------------------------------------------------------------------------
#  DataLoader factory
# ---------------------------------------------------------------------------

def create_dataloaders(
    data_dir: Path,
    batch_size: int = 32,
    image_size: int = 224,
    num_workers: int = 4,
    use_folder_dataset: bool = True,
) -> Tuple[DataLoader, DataLoader, DataLoader]:
    """
    Tạo train/val/test dataloaders.

    Nếu use_folder_dataset=True: đọc từ cấu trúc folder (data/split/train/rotate_animal/...)
    Nếu use_folder_dataset=False: đọc từ annotations.json
    """
    DS = FolderDataset if use_folder_dataset else FunCaptchaDataset

    train_dataset = DS(
        data_dir, split="train",
        transform=get_train_transform(image_size),
        image_size=image_size,
    )
    val_dataset = DS(
        data_dir, split="val",
        transform=get_val_transform(image_size),
        image_size=image_size,
    )
    test_dataset = DS(
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
