# -*- coding: utf-8 -*-
"""东方角色识图推理服务。

加载 WD-ConvNeXt v3 特征模型（ONNX）与数据集自带的 134 个逻辑回归分类头，
对外提供 HTTP 接口：

    GET  /            健康检查
    POST /classify    图片 -> 角色列表（中文名 + 置信度）

预处理口径与数据集特征提取完全一致（图片实测余弦≈1.0）：
白底补方形 -> BICUBIC 缩 448 -> BGR -> NHWC 原始 0-255（归一化在 ONNX 图内）。

运行环境：GPT-SoVITS runtime（onnxruntime + numpy + PIL + fastapi）。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import onnxruntime as ort
import uvicorn
from fastapi import FastAPI, HTTPException
from PIL import Image
from pydantic import BaseModel, Field

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_MODELS_DIR = _PLUGIN_DIR / "models"
_INPUT_SIZE = 448
_DEFAULT_PORT = 9881
_MAX_TOP_K = 10

logger = logging.getLogger("marisa_cv.serve")


class ClassifyRequest(BaseModel):
    """识图请求体。"""

    image_base64: str = Field(description="图片 base64，可带 data URI 前缀")
    threshold: float = Field(default=0.2, ge=0.0, le=1.0, description="置信度阈值")
    top_k: int = Field(default=3, ge=1, le=_MAX_TOP_K, description="最多返回几个角色")


class CharacterRecognizer:
    """特征模型 + 线性分类头的封装。"""

    def __init__(self, models_dir: Path) -> None:
        """加载 ONNX 特征模型与 134 类分类头。"""

        features_path = models_dir / "wd_features.onnx"
        head_path = models_dir / "head.npz"
        labels_path = models_dir / "labels.json"
        names_path = models_dir / "labels_zh.json"
        for path in (features_path, head_path, labels_path, names_path):
            if not path.is_file():
                raise FileNotFoundError(f"模型文件缺失: {path}")

        load_started = time.perf_counter()
        self._session = ort.InferenceSession(
            str(features_path), providers=["CPUExecutionProvider"]
        )
        head = np.load(str(head_path))
        self._weights = head["W"].astype(np.float32)
        self._bias = head["b"].astype(np.float32)
        self._classes: List[str] = list(json.loads(labels_path.read_text(encoding="utf-8"))["classes"])
        self._names: Dict[str, str] = json.loads(names_path.read_text(encoding="utf-8"))
        self._input_name = self._session.get_inputs()[0].name
        logger.info(
            "模型加载完成: 特征维度=%s 类别数=%s 耗时=%.1fs",
            self._weights.shape[1],
            len(self._classes),
            time.perf_counter() - load_started,
        )

    @property
    def num_classes(self) -> int:
        """返回类别数量。"""

        return len(self._classes)

    def classify(self, image_bytes: bytes, threshold: float, top_k: int) -> List[Dict[str, Any]]:
        """识别图片中的角色，返回按置信度排序的结果。"""

        array = self._preprocess(image_bytes)
        raw = self._session.run(None, {self._input_name: array})[0]
        features = np.asarray(raw).reshape(-1).astype(np.float32)
        probs = 1.0 / (1.0 + np.exp(-(features @ self._weights.T + self._bias)))

        order = np.argsort(probs)[::-1]
        results: List[Dict[str, Any]] = []
        for index in order[:top_k]:
            score = float(probs[int(index)])
            if score < threshold:
                break
            tag = self._classes[int(index)]
            results.append(
                {
                    "tag": tag,
                    "name_zh": self._names.get(tag, tag),
                    "confidence": round(score, 4),
                }
            )
        return results

    def _preprocess(self, image_bytes: bytes) -> np.ndarray:
        """与数据集口径一致的预处理，产出 [1,448,448,3] NHWC float32。"""

        image = Image.open(io.BytesIO(image_bytes))
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGBA")
            canvas = Image.new("RGBA", image.size, "WHITE")
            canvas.paste(image, mask=image)
            image = canvas
        image = image.convert("RGB")

        size = max(image.size)
        canvas = Image.new("RGB", (size, size), (255, 255, 255))
        canvas.paste(image, ((size - image.width) // 2, (size - image.height) // 2))
        image = canvas.resize((_INPUT_SIZE, _INPUT_SIZE), Image.Resampling.BICUBIC)

        array = np.asarray(image, dtype=np.float32)[:, :, ::-1]
        return np.ascontiguousarray(array[None])


def _decode_image(image_base64: str) -> bytes:
    """解析 base64（容忍 data URI 前缀），返回图片字节。"""

    data = str(image_base64 or "").strip()
    if not data:
        raise ValueError("图片数据为空")
    if data.startswith("data:"):
        marker = data.find(",")
        if marker < 0:
            raise ValueError("data URI 缺少逗号分隔符")
        data = data[marker + 1 :]
    try:
        return base64.b64decode(data)
    except Exception as exc:
        raise ValueError(f"base64 解码失败: {exc}") from exc


app = FastAPI(title="Marisa CV", docs_url=None, redoc_url=None)
_recognizer: Optional[CharacterRecognizer] = None
_semaphore: Optional[asyncio.Semaphore] = None


def _get_recognizer() -> CharacterRecognizer:
    """惰性加载模型（首次调用时加载）。"""

    global _recognizer
    if _recognizer is None:
        _recognizer = CharacterRecognizer(_MODELS_DIR)
    return _recognizer


def _get_semaphore() -> asyncio.Semaphore:
    """惰性创建串行信号量（必须在运行中的事件循环里创建）。"""

    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(1)
    return _semaphore


@app.get("/")
@app.get("/health")
async def health() -> Dict[str, Any]:
    """健康检查：模型信息。"""

    recognizer = _get_recognizer()
    return {
        "ok": True,
        "service": "marisa-cv",
        "engine": "wd-convnext-tagger-v3 + 134x logistic regression",
        "classes": recognizer.num_classes,
        "input_size": _INPUT_SIZE,
    }


@app.post("/classify")
async def classify(request: ClassifyRequest) -> Dict[str, Any]:
    """识别图片中的东方角色。"""

    started = time.perf_counter()
    try:
        image_bytes = _decode_image(request.image_base64)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    recognizer = _get_recognizer()
    try:
        async with _get_semaphore():
            characters = await asyncio.to_thread(
                recognizer.classify, image_bytes, request.threshold, request.top_k
            )
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=f"图片无法解析: {exc}") from exc
    except Exception as exc:
        logger.exception("分类失败")
        raise HTTPException(status_code=500, detail=f"分类失败: {exc}") from exc

    return {
        "ok": True,
        "characters": characters,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def main() -> int:
    """启动服务。"""

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    )
    port = int(os.environ.get("MARISA_CV_PORT", str(_DEFAULT_PORT)))
    logger.info("东方角色识图服务启动: http://127.0.0.1:%s", port)
    _get_recognizer()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
