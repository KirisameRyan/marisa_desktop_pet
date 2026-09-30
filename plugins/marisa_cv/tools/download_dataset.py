# -*- coding: utf-8 -*-
"""下载东方角色数据集（Preacher-26/touhou-embeddings-dataset）到本地目录。

- 断点续传：可反复执行，huggingface_hub 会跳过已完成的文件。
- 自动重试：每次都用一个全新的子进程（确保 HF_ENDPOINT 在导入前生效），
  失败时自动在 hf-mirror 与官方源之间交替重试，直到图片数量与元数据完整。
- 默认下载目录: D:\datasets\touhou（可用 TOUHOU_DATASET_DIR 覆盖）
- 并发数: TOUHOU_WORKERS（默认 16）
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ID = "Preacher-26/touhou-embeddings-dataset"
TARGET_DIR = Path(os.environ.get("TOUHOU_DATASET_DIR", r"D:\datasets\touhou"))
WORKERS = int(os.environ.get("TOUHOU_WORKERS", "16"))
PATTERNS = ["images/*", "labels.csv", "embeddings_backbone/*", "README.md"]
EXPECTED_IMAGES = 10786
MAX_ROUNDS = 40

# (显示名, HF 端点, 代理)；镜像走直连，官方源走本机代理
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


def _count_images() -> int:
    """统计 images/ 下已下载的图片数量。"""

    root = TARGET_DIR / "images"
    if not root.is_dir():
        return 0
    return sum(1 for item in root.rglob("*") if item.is_file())


def _metadata_ok() -> bool:
    """标签与 embedding 文件是否已就位。"""

    return (TARGET_DIR / "labels.csv").is_file() and (TARGET_DIR / "embeddings_backbone" / "X_train.npy").is_file()


def _attempt_once() -> int:
    """单次下载尝试（作为子进程运行）；完成后校验结果，未完整返回 2。"""

    endpoint = os.environ.get("TOUHOU_HF_ENDPOINT", "https://hf-mirror.com")
    proxy = os.environ.get("TOUHOU_PROXY", "")
    _apply_env(endpoint, proxy)

    from huggingface_hub import snapshot_download

    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    try:
        snapshot_download(
            repo_id=REPO_ID,
            repo_type="dataset",
            local_dir=str(TARGET_DIR),
            max_workers=WORKERS,
            allow_patterns=PATTERNS,
            etag_timeout=30,
        )
    except Exception as exc:
        print(f"下载异常: {type(exc).__name__}: {str(exc)[:200]}", flush=True)

    count = _count_images()
    complete = count >= EXPECTED_IMAGES and _metadata_ok()
    print(
        f"检查: images={count}/{EXPECTED_IMAGES} "
        f"metadata={'ok' if _metadata_ok() else 'missing'} -> {'完成' if complete else '未完成'}",
        flush=True,
    )
    return 0 if complete else 2


def main() -> int:
    """入口：默认多轮重试；``--once`` 只跑一次（供子进程调用）。"""

    if "--once" in sys.argv:
        return _attempt_once()

    print(f"目标 {TARGET_DIR}，共 {EXPECTED_IMAGES} 张图，最多重试 {MAX_ROUNDS} 轮", flush=True)
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
