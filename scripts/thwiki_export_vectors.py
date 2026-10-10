#!/usr/bin/env python
"""把块向量从 SQLite 导出为**归一化**的 .npy 矩阵，供检索层 memmap 使用。

为什么不用 SQLite 直接查：18 万块 × 1024 维 = 737 MB，全量读进内存会拖垮插件进程。
导出成 .npy 后用 np.load(mmap_mode="r")，靠 OS 分页按需读，内存占用可控；
并且**在导出时就归一化**，检索时余弦相似度退化成一次点积，快得多。

用法：
    python scripts/thwiki_export_vectors.py
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

import numpy as np


def main() -> int:
    parser = argparse.ArgumentParser(description="导出块向量为归一化 npy")
    parser.add_argument("--db", type=Path, default=Path("data") / "thwiki" / "thwiki.db")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    out_dir = args.out or args.db.parent
    conn = sqlite3.connect(str(args.db))
    rows = conn.execute("SELECT chunk_id, vec FROM chunk_vectors ORDER BY chunk_id").fetchall()
    if not rows:
        print("chunk_vectors 为空，先跑 thwiki_embed_pages.py")
        return 2

    ids = np.empty(len(rows), dtype=np.int64)
    dim = len(rows[0][1]) // 4
    mat = np.empty((len(rows), dim), dtype=np.float32)
    for index, (chunk_id, blob) in enumerate(rows):
        ids[index] = chunk_id
        mat[index] = np.frombuffer(blob, dtype="<f4")

    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    mat /= np.where(norms == 0, 1.0, norms)   # 归一化 → 检索只需点积

    np.save(out_dir / "chunk_vectors.npy", mat)
    np.save(out_dir / "chunk_ids.npy", ids)
    size_mb = (out_dir / "chunk_vectors.npy").stat().st_size / 1024 / 1024
    print(f"导出：{mat.shape[0]} 块 × {mat.shape[1]} 维 · {size_mb:.0f} MB → {out_dir}")
    print(f"      {out_dir / 'chunk_vectors.npy'}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
