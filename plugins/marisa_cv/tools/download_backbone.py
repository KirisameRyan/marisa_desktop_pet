# -*- coding: utf-8 -*-
"""下载 wd-convnext-tagger-v3 骨干模型到插件模型目录。

- 断点续传、自动重试（镜像/官方源交替），与数据集下载脚本同一套模式。
- 目标目录: plugins/marisa_cv/models/backbone/
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ID = "SmilingWolf/wd-convnext-tagger-v3"
PLUGIN_DIR = Path(__file__).resolve().parent.parent
TARGET_DIR = PLUGIN_DIR / "models" / "backbone"
WORKERS = 8
PATTERNS = ["model.safetensors", "model.onnx", "config.json", "selected_tags.csv", "sw_jax_cv_config.json"]
MAX_ROUNDS = 40

SOURCES = [
    ("hf-mirror", "https://hf-mirror.com", ""),
    ("huggingface", "https://huggingface.co", "http://127.0.0.1:10808"),
]


def _apply_env(endpoint: str, proxy: str) -> None:
    """按目标源设置环境变量（必须在导入 huggingface_hub 之前调用）。"""

    os.environ["HF_ENDPOINT"] = endpoint
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "no_proxy"):
        os.environ.pop(key, None)
    if proxy:
        os.environ["HTTP_PROXY"] = proxy
        os.environ["HTTPS_PROXY"] = proxy
    else:
        os.environ["NO_PROXY"] = "hf-mirror.com"
        os.environ["no_proxy"] = "hf-mirror.com"


def _verify() -> str:
    """检查关键文件是否完整，返回缺失说明（完整时返回空串）。"""

    required = {
        "model.safetensors": 300 * 1024 * 1024,
        "model.onnx": 300 * 1024 * 1024,
        "config.json": 100,
        "selected_tags.csv": 100 * 1024,
    }
    missing = []
    for name, min_size in required.items():
        path = TARGET_DIR / name
        if not path.is_file() or path.stat().st_size < min_size:
            missing.append(name)
    return "、".join(missing)


def _attempt_once() -> int:
    """单次下载尝试（作为子进程运行）。"""

    endpoint = os.environ.get("TOUHOU_HF_ENDPOINT", "https://hf-mirror.com")
    proxy = os.environ.get("TOUHOU_PROXY", "")
    _apply_env(endpoint, proxy)

    from huggingface_hub import snapshot_download

    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    try:
        snapshot_download(
            repo_id=REPO_ID,
            repo_type="model",
            local_dir=str(TARGET_DIR),
            max_workers=WORKERS,
            allow_patterns=PATTERNS,
            etag_timeout=30,
        )
    except Exception as exc:
        print(f"下载异常: {type(exc).__name__}: {str(exc)[:200]}", flush=True)

    missing = _verify()
    print(f"检查: {'完整' if not missing else '缺失 ' + missing}", flush=True)
    return 0 if not missing else 2


def main() -> int:
    """入口：默认多轮重试；``--once`` 只跑一次（供子进程调用）。"""

    if "--once" in sys.argv:
        return _attempt_once()

    print(f"目标 {TARGET_DIR}", flush=True)
    for round_index in range(1, MAX_ROUNDS + 1):
        name, endpoint, proxy = SOURCES[(round_index - 1) % len(SOURCES)]
        print(f"[{round_index}/{MAX_ROUNDS}] 源={name}", flush=True)
        env = dict(os.environ)
        env["TOUHOU_HF_ENDPOINT"] = endpoint
        env["TOUHOU_PROXY"] = proxy
        env["PYTHONUNBUFFERED"] = "1"
        result = subprocess.run(
            [sys.executable, "-u", str(Path(__file__).resolve()), "--once"],
            env=env,
        )
        if result.returncode == 0:
            print("ALL DONE", flush=True)
            return 0
        print(f"[{round_index}] 未完成（rc={result.returncode}），20 秒后换源重试", flush=True)
        time.sleep(20)
    print("FAILED 达到最大重试次数", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
