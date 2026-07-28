"""
Metrics and evaluation utilities for FunCAPTCHA solver.
"""
from typing import Dict, List

import numpy as np
from sklearn.metrics import (
    accuracy_score, precision_recall_fscore_support,
    confusion_matrix, classification_report,
)
import matplotlib.pyplot as plt
import seaborn as sns


class ClassificationMetrics:
    """Compute and store classification metrics."""

    def __init__(self, class_names: List[str]):
        self.class_names = class_names

    def compute(self, y_true: np.ndarray, y_pred: np.ndarray) -> Dict:
        """Compute all classification metrics."""
        acc = accuracy_score(y_true, y_pred)
        precision, recall, f1, _ = precision_recall_fscore_support(
            y_true, y_pred, average="macro", zero_division=0
        )

        return {
            "accuracy": acc,
            "precision_macro": precision,
            "recall_macro": recall,
            "f1_macro": f1,
        }

    def plot_confusion_matrix(
        self, y_true: np.ndarray, y_pred: np.ndarray, save_path: str = None
    ):
        """Plot confusion matrix."""
        cm = confusion_matrix(y_true, y_pred)
        plt.figure(figsize=(10, 8))
        sns.heatmap(
            cm, annot=True, fmt="d", cmap="Blues",
            xticklabels=self.class_names,
            yticklabels=self.class_names,
        )
        plt.xlabel("Predicted")
        plt.ylabel("True")
        plt.title("Confusion Matrix")
        if save_path:
            plt.savefig(save_path, dpi=150, bbox_inches="tight")
        plt.show()


class AngleMetrics:
    """Metrics for rotation angle prediction."""

    @staticmethod
    def angle_error(pred_angle: float, true_angle: float) -> float:
        """Compute circular angle error in degrees [0, 180]."""
        error = abs(pred_angle - true_angle) % 360
        return min(error, 360 - error)

    @staticmethod
    def compute(
        y_true: np.ndarray, y_pred: np.ndarray, num_bins: int = 360
    ) -> Dict:
        """
        Compute angle prediction metrics.

        Args:
            y_true: True bin indices
            y_pred: Predicted bin indices
            num_bins: Total number of angle bins
        """
        # Convert bins to degrees
        bin_size = 360.0 / num_bins
        true_angles = y_true * bin_size
        pred_angles = y_pred * bin_size

        errors = [AngleMetrics.angle_error(p, t) for p, t in zip(pred_angles, true_angles)]
        errors = np.array(errors)

        return {
            "mae": np.mean(errors),
            "rmse": np.sqrt(np.mean(errors ** 2)),
            "median_error": np.median(errors),
            "accuracy_5deg": np.mean(errors <= 5.0),
            "accuracy_10deg": np.mean(errors <= 10.0),
            "accuracy_15deg": np.mean(errors <= 15.0),
        }
