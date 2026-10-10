#!/usr/bin/env python
"""把角色档案章节（character_sections）也做成向量，补上语义检索。

为什么需要：`character_sections` 存下了外貌/人际关系/登场剧情/设定文档/符卡列表
等 4,776 条内容，但检索层 `_by_section` 走的是 SQL LIKE——**关键词匹配，不是语义**。
问法一变（"她长什么样" vs "外貌特征"）就找不到。
页面级的设定早已嵌进 page_chunks，角色档案这一层却漏了。

用法：
    python scripts/thwiki_embed_sections.py
"""

from __future__ import annotations

import argparse
import sqlite3
import struct
import time
import tomllib
from pathlib import Path
from typing import List, Tuple

import httpx

MODEL = "Pro/BAAI/bge-m3"
BATCH = 32
# 太短的章节（如"分析与考据"这类只有标题的）不值得嵌入
MIN_CHARS = 40


def load_provider(config_path: Path) -> Tuple[str, str]:
    cfg = tomllib.loads(config_path.read_text(encoding="utf-8"))
    for provider in cfg.get("api_providers", []):
        if provider.get("name") == "SiliconFlow":
            return str(provider["base_url"]).rstrip("/"), str(provider["api_key"])
    raise RuntimeError("model_config.toml 里没有 SiliconFlow provider")


def main() -> int:
    parser = argparse.ArgumentParser(description="角色档案章节向量化")
    parser.add_argument("--db", type=Path, default=Path("data") / "thwiki" / "thwiki.db")
    parser.add_argument("--config", type=Path, default=Path("config") / "model_config.toml")
    args = parser.parse_args()

    base, key = load_provider(args.config)
    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS section_vectors ("
        " rowid INTEGER PRIMARY KEY, model TEXT, vec BLOB)"
    )

    rows = conn.execute(
        """SELECT cs.rowid AS rid, cs.section, cs.text, e.name_cn
           FROM character_sections cs JOIN entities e ON e.entity_id = cs.entity_id
           WHERE length(cs.text) >= ?
             AND cs.section NOT IN ('目录', '注释', '词条导航')""",
        (MIN_CHARS,),
    ).fetchall()
    print(f"待嵌入章节：{len(rows)} 条")

    texts = [f"{r['name_cn']}｜{r['section']}｜{r['text'][:1200]}" for r in rows]
    done = 0
    started = time.time()
    with httpx.Client(timeout=120.0) as client:
        for start in range(0, len(texts), BATCH):
            batch = texts[start : start + BATCH]
            for attempt in range(5):
                resp = client.post(
                    f"{base}/embeddings",
                    json={"model": MODEL, "input": batch},
                    headers={"Authorization": f"Bearer {key}"},
                )
                if resp.status_code == 200:
                    break
                if resp.status_code in (408, 429, 500, 502, 503, 504):
                    time.sleep(min(30, 2**attempt))
                    continue
                raise RuntimeError(f"嵌入失败 HTTP {resp.status_code}: {resp.text[:200]}")
            for offset, item in enumerate(resp.json()["data"]):
                vec = item["embedding"]
                conn.execute(
                    "INSERT OR REPLACE INTO section_vectors(rowid, model, vec) VALUES (?,?,?)",
                    (int(rows[start + offset]["rid"]), MODEL, struct.pack(f"<{len(vec)}f", *vec)),
                )
            conn.commit()
            done += len(batch)
            if done % 512 == 0 or done == len(texts):
                print(f"  …{done}/{len(texts)}（{time.time() - started:.0f} 秒）", flush=True)

    total = conn.execute("SELECT COUNT(*) FROM section_vectors").fetchone()[0]
    print(f"完成：库内 {total} 条章节向量，用时 {time.time() - started:.0f} 秒")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
