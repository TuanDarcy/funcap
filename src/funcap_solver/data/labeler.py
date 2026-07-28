"""
Manual CAPTCHA labeling tool
=============================
Hiển thị từng ảnh CAPTCHA, bạn bấm phím để gán nhãn.
Ảnh được tự động MOVE vào folder tương ứng.

Cách dùng:
  python -m funcap_solver.data.labeler --input data/raw --output data/labeled

Phím tắt:
  0: rotate_animal  (Xoay con vật)
  1: match_object   (Ghép object)
  2: select_tiles   (Chọn tiles)
  3: shadow_match   (Ghép bóng)
  4: pick_image     (Chọn ảnh đúng)
  5: count_objects  (Đếm object)
  S: Bỏ qua ảnh này
  D: Xóa ảnh (rác)
  Q: Thoát
  B: Quay lại ảnh trước
"""
import argparse
import os
import shutil
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# Class name mapping
CLASS_NAMES = {
    "0": "rotate_animal",
    "1": "match_object",
    "2": "select_tiles",
    "3": "shadow_match",
    "4": "pick_image",
    "5": "count_objects",
}

CLASS_DESC = {
    "0": "🔄 Xoay con vật về đúng hướng",
    "1": "🧩 Ghép object với silhouette",
    "2": "🟫 Chọn tiles chứa object",
    "3": "👤 Ghép bóng với object",
    "4": "🖼️  Chọn ảnh đúng",
    "5": "🔢 Đếm số object",
}


def print_help():
    print("\n" + "=" * 55)
    print("  🏷️  FunCAPTCHA LABELING TOOL")
    print("=" * 55)
    for key in ["0", "1", "2", "3", "4", "5"]:
        print(f"  [{key}] {CLASS_DESC[key]}")
    print("  [S] Bỏ qua  |  [D] Xóa ảnh  |  [B] Quay lại  |  [Q] Thoát")
    print("=" * 55)


def count_labeled(output_dir: Path) -> dict:
    """Đếm số ảnh đã label trong mỗi folder."""
    counts = {}
    for name in CLASS_NAMES.values():
        folder = output_dir / name
        if folder.is_dir():
            counts[name] = len(list(folder.glob("*.png")) + list(folder.glob("*.jpg")) + list(folder.glob("*.jpeg")))
        else:
            counts[name] = 0
    return counts


def print_stats(output_dir: Path, remaining: int):
    """In thống kê hiện tại."""
    counts = count_labeled(output_dir)
    total = sum(counts.values())
    print(f"\n📊 Đã label: {total} | Còn lại: {remaining}")
    bar_width = 20
    for name, n in counts.items():
        pct = n / max(total, 1)
        bar = "█" * int(pct * bar_width) + "░" * (bar_width - int(pct * bar_width))
        print(f"  {name:20s} {bar} {n:4d}")


def find_images(input_dir: Path, output_dir: Path) -> List[Path]:
    """Tìm tất cả ảnh chưa được label."""
    input_dir = Path(input_dir)
    exts = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

    # Thu thập ảnh đã label để bỏ qua
    labeled_names = set()
    if output_dir.is_dir():
        for folder in output_dir.iterdir():
            if folder.is_dir():
                for f in folder.iterdir():
                    if f.suffix.lower() in exts:
                        labeled_names.add(f.name)

    # Tìm ảnh chưa label
    all_images = []
    for f in input_dir.rglob("*"):
        if f.suffix.lower() in exts and f.name not in labeled_names:
            all_images.append(f)

    return sorted(all_images)


def open_image(img_path: Path):
    """Mở ảnh bằng trình xem mặc định của Windows."""
    webbrowser.open(str(img_path.resolve()))


def move_to_labeled(img_path: Path, label: str, output_dir: Path):
    """Move ảnh vào folder label tương ứng."""
    dst_folder = output_dir / label
    dst_folder.mkdir(parents=True, exist_ok=True)

    # Thêm timestamp để tránh trùng tên
    ts = datetime.now().strftime("%H%M%S%f")[:12]
    new_name = f"{label}_{ts}{img_path.suffix}"
    dst = dst_folder / new_name

    shutil.move(str(img_path), str(dst))
    return dst


def delete_image(img_path: Path):
    """Xóa ảnh rác."""
    try:
        os.remove(img_path)
        return True
    except Exception as e:
        print(f"  ❌ Không xóa được: {e}")
        return False


def undo_last_move(last_action: Optional[tuple]):
    """Undo lần move cuối cùng."""
    if last_action is None:
        print("  ⚠️  Không có gì để undo")
        return None

    src, dst = last_action
    if dst.exists():
        shutil.move(str(dst), str(src))
        print(f"  ↩️  Đã hoàn tác: {dst.name} → {src.parent}")
        return None
    return last_action


def interactive_label(input_dir: Path, output_dir: Path):
    """Chạy labeling tương tác qua terminal."""
    print_help()
    images = find_images(input_dir, output_dir)

    if not images:
        print("\n✅ Không còn ảnh nào cần label!")
        print_stats(output_dir, 0)
        return

    print(f"\n🔍 Tìm thấy {len(images)} ảnh chưa label trong {input_dir}")
    print_stats(output_dir, len(images))

    idx = 0
    last_action = None  # (src, dst) để undo

    while idx < len(images):
        img_path = images[idx]
        print(f"\n{'─' * 50}")
        print(f"  📷 [{idx + 1}/{len(images)}] {img_path.name}")
        print(f"  📁 {img_path.parent}")

        # Mở ảnh
        open_image(img_path)

        # Chờ input
        choice = input("  🏷️  Nhập label (0-5/S/D/B/Q): ").strip().upper()

        if choice in CLASS_NAMES:
            label = CLASS_NAMES[choice]
            dst = move_to_labeled(img_path, label, output_dir)
            print(f"  ✅ → {label}/  ({CLASS_DESC[choice]})")
            last_action = (img_path, dst)
            idx += 1
            print_stats(output_dir, len(images) - idx)

        elif choice == "S":
            print(f"  ⏭️  Bỏ qua")
            idx += 1

        elif choice == "D":
            if delete_image(img_path):
                print(f"  🗑️  Đã xóa")
                images.pop(idx)
                print_stats(output_dir, len(images) - idx)
            else:
                idx += 1

        elif choice == "B":
            if idx > 0:
                idx -= 1
                # Undo ảnh trước đó nếu đã move
                if last_action:
                    undo_last_move(last_action)
                    last_action = None
                print(f"  ↩️  Quay lại ảnh [{idx + 1}/{len(images)}]")
            else:
                print(f"  ⚠️  Đang ở ảnh đầu tiên")

        elif choice == "Q":
            print(f"\n👋 Đã lưu {sum(count_labeled(output_dir).values())} ảnh. Tạm biệt!")
            break

        else:
            print(f"  ❌ Không hợp lệ! Bấm 0-5, S, D, B, hoặc Q")

    print("\n" + "=" * 55)
    print("🏁 HOÀN THÀNH LABELING!")
    print_stats(output_dir, 0)
    print(f"\nDữ liệu đã sẵn sàng trong: {output_dir}")
    print("Bước tiếp theo:")
    print(f"  1. Chia train/val:")
    print(f"     python -c \"from funcap_solver.data.dataset import split_into_train_val; split_into_train_val('{output_dir}', 'data/split')\"")
    print(f"  2. Train:")
    print(f"     python -m funcap_solver.train --mode combined --data-dir data/split")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="FunCAPTCHA Labeling Tool - Gán nhãn ảnh CAPTCHA bằng phím",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ví dụ:
  python -m funcap_solver.data.labeler --input data/raw --output data/labeled
  python -m funcap_solver.data.labeler -i data/raw -o data/labeled
        """,
    )
    parser.add_argument("--input", "-i", type=Path, required=True,
                        help="Thư mục chứa ảnh CAPTCHA chưa label")
    parser.add_argument("--output", "-o", type=Path, default=Path("data/labeled"),
                        help="Thư mục output (tự tạo folder theo class)")

    args = parser.parse_args()

    if not args.input.exists():
        print(f"❌ Thư mục không tồn tại: {args.input}")
        sys.exit(1)

    interactive_label(args.input, args.output)


if __name__ == "__main__":
    main()
