"""PyTorch causal TCN for per-hand gesture windows (train + inference)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    from torch.utils.data import DataLoader, Dataset
except ImportError as exc:  # pragma: no cover
    raise ImportError("PyTorch is required: pip install torch") from exc

from .augment import AugmentConfig, augment_window

MODEL_ARCH_V1 = "tcn_torch_v1"
MODEL_ARCH_V2 = "tcn_torch_v2_dilated"
MODEL_ARCH = MODEL_ARCH_V2


@dataclass
class GestureTCNConfig:
    kernel_size: int = 5
    channels: int = 64
    learning_rate: float = 1e-3
    weight_decay: float = 1e-4
    epochs: int = 40
    batch_size: int = 64
    seed: int = 7
    dropout: float = 0.15
    # v2 dilated stack
    n_layers: int = 4
    dilations: tuple[int, ...] = (1, 2, 4, 8)


class _CausalConvBlock(nn.Module):
    """v1: single causal conv + ReLU + dropout."""

    def __init__(self, in_ch: int, out_ch: int, kernel_size: int, dropout: float):
        super().__init__()
        pad = kernel_size - 1
        self.block = nn.Sequential(
            nn.Conv1d(in_ch, out_ch, kernel_size, padding=pad),
            nn.ReLU(inplace=True),
            nn.Dropout(dropout),
        )
        self.trim = pad

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        y = self.block(x)
        if self.trim:
            y = y[:, :, :-self.trim]
        return y


class _DilatedCausalBlock(nn.Module):
    """v2: dilated causal conv (trimmed) + ReLU + dropout."""

    def __init__(self, channels: int, kernel_size: int, dilation: int, dropout: float):
        super().__init__()
        self.pad = (kernel_size - 1) * dilation
        self.conv = nn.Conv1d(
            channels, channels, kernel_size, dilation=dilation, padding=self.pad)
        self.drop = nn.Dropout(dropout)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        y = self.conv(x)
        if self.pad:
            y = y[:, :, :-self.pad]
        return self.drop(F.relu(y, inplace=True))


class GestureTCN(nn.Module):
    """v1: two causal conv layers, mean+max pool, linear head."""

    arch = MODEL_ARCH_V1

    def __init__(self, in_features: int, n_classes: int, cfg: GestureTCNConfig):
        super().__init__()
        self.cfg = cfg
        self.arch = MODEL_ARCH_V1
        c = cfg.channels
        self.conv1 = _CausalConvBlock(in_features, c, cfg.kernel_size, cfg.dropout)
        self.conv2 = _CausalConvBlock(c, c, cfg.kernel_size, cfg.dropout)
        self.head = nn.Linear(2 * c, n_classes)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h = x.transpose(1, 2)
        h = self.conv1(h)
        h = self.conv2(h)
        pooled = torch.cat([h.mean(dim=2), h.amax(dim=2)], dim=1)
        return self.head(pooled)


class GestureTCNDilated(nn.Module):
    """v2: stack of dilated causal convs — better temporal context for chromatic motion."""

    arch = MODEL_ARCH_V2

    def __init__(self, in_features: int, n_classes: int, cfg: GestureTCNConfig):
        super().__init__()
        self.cfg = cfg
        self.arch = MODEL_ARCH_V2
        c = cfg.channels
        dilations = cfg.dilations[: cfg.n_layers]
        self.input_proj = nn.Conv1d(in_features, c, 1)
        self.blocks = nn.ModuleList([
            _DilatedCausalBlock(c, cfg.kernel_size, d, cfg.dropout) for d in dilations
        ])
        self.head = nn.Linear(2 * c, n_classes)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h = self.input_proj(x.transpose(1, 2))
        for block in self.blocks:
            h = block(h)
        pooled = torch.cat([h.mean(dim=2), h.amax(dim=2)], dim=1)
        return self.head(pooled)


class _AugmentedDataset(Dataset):
    def __init__(
        self,
        x: np.ndarray,
        y: np.ndarray,
        augment: AugmentConfig | None,
        aug_prob: float,
        seed: int,
    ):
        self.x = x
        self.y = y
        self.augment = augment
        self.aug_prob = aug_prob
        self.rng = np.random.default_rng(seed)

    def __len__(self) -> int:
        return int(self.y.shape[0])

    def __getitem__(self, idx: int) -> tuple[torch.Tensor, torch.Tensor]:
        row = self.x[idx]
        if self.augment and self.augment.any_enabled() and self.rng.random() < self.aug_prob:
            row = augment_window(row, self.augment, self.rng)
        return torch.as_tensor(row, dtype=torch.float32), torch.as_tensor(int(self.y[idx]), dtype=torch.long)


def build_model(arch: str, in_features: int, n_classes: int, cfg: GestureTCNConfig) -> nn.Module:
    if arch == MODEL_ARCH_V2:
        return GestureTCNDilated(in_features, n_classes, cfg)
    if arch == MODEL_ARCH_V1:
        return GestureTCN(in_features, n_classes, cfg)
    raise ValueError(f"Unknown model arch {arch!r}")


def _default_device() -> torch.device:
    if torch.cuda.is_available():
        return torch.device("cuda")
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def _class_weights(y: np.ndarray, n_classes: int) -> torch.Tensor:
    counts = np.bincount(y, minlength=n_classes).astype(np.float64)
    w = len(y) / (n_classes * np.maximum(counts, 1.0))
    return torch.as_tensor(w, dtype=torch.float32)


@torch.no_grad()
def predict_proba_np(model: nn.Module, x: np.ndarray, device: torch.device | None = None) -> np.ndarray:
    model.eval()
    dev = device or _default_device()
    model.to(dev)
    xt = torch.as_tensor(x, dtype=torch.float32, device=dev)
    logits = model(xt)
    return torch.softmax(logits, dim=1).cpu().numpy()


def predict_np(model: nn.Module, x: np.ndarray, device: torch.device | None = None) -> np.ndarray:
    return predict_proba_np(model, x, device).argmax(axis=1)


def train_gesture_model(
    in_features: int,
    n_classes: int,
    cfg: GestureTCNConfig,
    x_train: np.ndarray,
    y_train: np.ndarray,
    *,
    x_val: np.ndarray | None = None,
    y_val: np.ndarray | None = None,
    progress: Callable[[int, int], None] | None = None,
    device: torch.device | None = None,
    augment: AugmentConfig | None = None,
    aug_prob: float = 0.5,
    model_arch: str = MODEL_ARCH,
) -> dict[str, Any]:
    """Train and return {'model', 'train_acc', 'val_acc', 'device', 'model_arch'}."""
    torch.manual_seed(cfg.seed)
    np.random.seed(cfg.seed)
    dev = device or _default_device()

    model = build_model(model_arch, in_features, n_classes, cfg).to(dev)
    weight = _class_weights(y_train, n_classes).to(dev)
    criterion = nn.CrossEntropyLoss(weight=weight)
    optimizer = torch.optim.AdamW(
        model.parameters(), lr=cfg.learning_rate, weight_decay=cfg.weight_decay)

    aug_cfg = augment if (augment and augment.any_enabled()) else None
    train_ds: Dataset = _AugmentedDataset(x_train, y_train, aug_cfg, aug_prob, cfg.seed)
    loader = DataLoader(train_ds, batch_size=cfg.batch_size, shuffle=True, drop_last=False)

    for epoch in range(cfg.epochs):
        model.train()
        for xb, yb in loader:
            xb, yb = xb.to(dev), yb.to(dev)
            optimizer.zero_grad(set_to_none=True)
            loss = criterion(model(xb), yb)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            optimizer.step()
        if progress is not None:
            progress(epoch + 1, cfg.epochs)

    model.eval()
    train_acc = float((predict_np(model, x_train, dev) == y_train).mean())
    out: dict[str, Any] = {
        "model": model,
        "train_acc": train_acc,
        "device": str(dev),
        "model_arch": model_arch,
    }
    if x_val is not None and y_val is not None and len(y_val):
        out["val_acc"] = float((predict_np(model, x_val, dev) == y_val).mean())
    return out


def save_model(
    model: nn.Module,
    path: Path,
    *,
    in_features: int,
    n_classes: int,
    model_arch: str,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "model_arch": model_arch,
            "state_dict": model.state_dict(),
            "config": model.cfg,
            "in_features": in_features,
            "n_classes": n_classes,
        },
        path,
    )
    return path


def load_model(path: Path, map_location: str | torch.device | None = None) -> nn.Module:
    data = torch.load(path, map_location=map_location or "cpu", weights_only=False)
    arch = data.get("model_arch", MODEL_ARCH_V1)
    cfg = data["config"]
    model = build_model(arch, int(data["in_features"]), int(data["n_classes"]), cfg)
    model.load_state_dict(data["state_dict"])
    model.eval()
    return model
