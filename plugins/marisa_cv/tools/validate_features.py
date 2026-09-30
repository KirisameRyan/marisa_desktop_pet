# -*- coding: utf-8 -*-
"""验证特征管线 + 离线评测分类头。

1. 报告 X/y 形状；
2. 用图片实测特征，分别在 X_train/X_val 里找最相似行（应出现余弦≈1.0）；
3. 直接用 X_val + head.npz 跑一遍 134 类 LR，输出 val 指标（阈值 0.2）。
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
DATASET_DIR = Path(r"D:\datasets\touhou")
EMB_DIR = DATASET_DIR / "embeddings_backbone"
CHECK_COUNT = 24
THRESHOLD = 0.2


def _match_check(rows: list[str]) -> None:
    """抽查若干 val 图，确认特征能在 npy 里精确命中。"""

    import onnxruntime as ort
    from PIL import Image

    session = ort.InferenceSession(
        str(MODELS_DIR / "wd_features.onnx"), providers=["CPUExecutionProvider"]
    )
    output_name = session.get_outputs()[0].name
    x_train = np.load(EMB_DIR / "X_train.npy")
    x_val = np.load(EMB_DIR / "X_val.npy")
    train_norm = x_train / (np.linalg.norm(x_train, axis=1, keepdims=True) + 1e-8)
    val_norm = x_val / (np.linalg.norm(x_val, axis=1, keepdims=True) + 1e-8)

    sample = rows[:: max(1, len(rows) // CHECK_COUNT)][:CHECK_COUNT]
    hits = 0
    for index, file_name in enumerate(sample):
        image = Image.open(DATASET_DIR / "images" / file_name).convert("RGB")
        size = max(image.size)
        canvas = Image.new("RGB", (size, size), (255, 255, 255))
        canvas.paste(image, ((size - image.width) // 2, (size - image.height) // 2))
        image = canvas.resize((448, 448), Image.Resampling.BICUBIC)
        array = np.asarray(image, dtype=np.float32)[:, :, ::-1]
        array = np.ascontiguousarray(array[None])
        raw = session.run([output_name], {"input": array})[0]
        feature = np.asarray(raw).reshape(-1).astype(np.float32)
        feature = feature / (np.linalg.norm(feature) + 1e-8)
        cos_train = train_norm @ feature
        cos_val = val_norm @ feature
        best_train = float(cos_train.max())
        best_val = float(cos_val.max())
        best = max(best_train, best_val)
        where = "train" if best_train >= best_val else "val"
        if best > 0.999:
            hits += 1
        print(
            f"  img#{index:02d} train_max={best_train:.4f} val_max={best_val:.4f} "
            f"-> {where} (cos={best:.4f})",
            flush=True,
        )
    print(f"精确命中: {hits}/{CHECK_COUNT}", flush=True)


def _eval_head() -> None:
    """在 X_val 上评测 134 个 LR 分类头的指标。"""

    x_val = np.load(EMB_DIR / "X_val.npy").astype(np.float32)
    y_val = np.load(EMB_DIR / "y_val.npy")
    head = np.load(MODELS_DIR / "head.npz")
    weights = head["W"].astype(np.float32)
    bias = head["b"].astype(np.float32)
    labels = json.loads((MODELS_DIR / "labels.json").read_text(encoding="utf-8"))["classes"]

    logits = x_val @ weights.T + bias
    probs = 1.0 / (1.0 + np.exp(-logits))
    preds = (probs >= THRESHOLD).astype(np.int32)

    tp = (preds * y_val).sum(axis=0).astype(np.float64)
    fp = (preds * (1 - y_val)).sum(axis=0).astype(np.float64)
    fn = ((1 - preds) * y_val).sum(axis=0).astype(np.float64)

    precision = tp / np.maximum(tp + fp, 1e-9)
    recall = tp / np.maximum(tp + fn, 1e-9)
    f1 = 2 * precision * recall / np.maximum(precision + recall, 1e-9)

    micro_tp, micro_fp, micro_fn = tp.sum(), fp.sum(), fn.sum()
    micro_p = micro_tp / max(micro_tp + micro_fp, 1e-9)
    micro_r = micro_tp / max(micro_tp + micro_fn, 1e-9)
    micro_f1 = 2 * micro_p * micro_r / max(micro_p + micro_r, 1e-9)
    print(
        f"val 样本={x_val.shape[0]} 阈值={THRESHOLD} "
        f"micro P={micro_p:.4f} R={micro_r:.4f} F1={micro_f1:.4f}",
        flush=True,
    )
    print(f"macro P={precision.mean():.4f} R={recall.mean():.4f} F1={f1.mean():.4f}", flush=True)

    worst = np.argsort(f1)[:10]
    print("F1 最低的 10 类:", flush=True)
    for index in worst:
        print(
            f"  {labels[index]:<28} P={precision[index]:.3f} R={recall[index]:.3f} "
            f"F1={f1[index]:.3f} support={int(y_val[:, index].sum())}",
            flush=True,
        )


def main() -> int:
    """执行验证与评测。"""

    for name in ("X_train", "X_val", "y_train", "y_val"):
        array = np.load(EMB_DIR / f"{name}.npy")
        print(f"{name}: shape={array.shape} dtype={array.dtype}", flush=True)

    rows = []
    with open(DATASET_DIR / "labels.csv", "r", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["split"] == "val":
                rows.append(row["file_name"])
    _match_check(rows)
    _eval_head()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
