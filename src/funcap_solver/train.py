"""
Main training entry point for FunCAPTCHA Solver.
"""
import argparse
from pathlib import Path

from funcap_solver.config import Config
from funcap_solver.training.trainer import train_classifier, train_rotation_predictor, train_combined


def main():
    parser = argparse.ArgumentParser(description="FunCAPTCHA Solver Training")
    parser.add_argument("--mode", choices=["classify", "angle", "combined"], default="combined",
                        help="Training mode")
    parser.add_argument("--data-dir", type=Path, default=Path("data/processed"),
                        help="Path to processed data directory")
    parser.add_argument("--output-dir", type=Path, default=Path("checkpoints"),
                        help="Output directory for checkpoints")
    parser.add_argument("--epochs", type=int, default=20, help="Number of epochs")
    parser.add_argument("--batch-size", type=int, default=32, help="Batch size")
    parser.add_argument("--lr", type=float, default=2e-4, help="Learning rate")
    parser.add_argument("--no-lora", action="store_true", help="Disable LoRA")
    parser.add_argument("--wandb", action="store_true", help="Enable Weights & Biases logging")

    args = parser.parse_args()

    config = Config()
    config.training.cls_epochs = args.epochs
    config.training.cls_batch_size = args.batch_size
    config.training.cls_learning_rate = args.lr
    config.training.use_lora = not args.no_lora
    config.training.output_dir = args.output_dir

    if args.mode == "classify":
        train_classifier(config, args.data_dir)
    elif args.mode == "angle":
        train_rotation_predictor(config, args.data_dir)
    elif args.mode == "combined":
        train_combined(config, args.data_dir)


if __name__ == "__main__":
    main()
