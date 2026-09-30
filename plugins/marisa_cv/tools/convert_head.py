# -*- coding: utf-8 -*-
"""把数据集自带的多标签逻辑回归分类器转成便携 npz 权重。

需要在 numpy 2.x + scikit-learn 环境里运行一次（数据集 joblib 是 numpy 2 存的）。
产出到 ``plugins/marisa_cv/models/``：
- ``head.npz``: W [134,1024] + b [134]，服务端只需 numpy 即可推理
- ``labels.json``: 类别名（Danbooru tag）、阈值等元信息
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

DATASET_DIR = Path(r"D:\datasets\touhou")
OUT_DIR = Path(__file__).resolve().parent.parent / "models"


def main() -> int:
    """读取 joblib 分类器，导出为 npz 与标签表。"""

    import joblib

    mlb = joblib.load(DATASET_DIR / "embeddings_backbone" / "multi_label_binarizer.joblib")
    classifiers = joblib.load(DATASET_DIR / "embeddings_backbone" / "touhou_classifier_br_list.joblib")

    classes = [str(name) for name in mlb.classes_]
    real = 0
    weights = np.zeros((len(classes), 1024), dtype=np.float32)
    bias = np.zeros((len(classes),), dtype=np.float32)
    for index, clf in enumerate(classifiers):
        coef = getattr(clf, "coef_", None)
        intercept = getattr(clf, "intercept_", None)
        if coef is None or intercept is None:
            continue
        weights[index] = np.asarray(coef, dtype=np.float32)[0]
        bias[index] = float(np.asarray(intercept, dtype=np.float32)[0])
        real += 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(OUT_DIR / "head.npz", W=weights, b=bias)
    meta = {
        "classes": classes,
        "threshold": 0.2,
        "input_dim": 1024,
        "real_classifiers": real,
        "note": "LogisticRegression OvR on wd-convnext-tagger-v3 penultimate features",
    }
    (OUT_DIR / "labels.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"classes={len(classes)} real_classifiers={real}")
    for name in classes:
        print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
