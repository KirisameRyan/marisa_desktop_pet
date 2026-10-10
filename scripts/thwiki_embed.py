#!/usr/bin/env python
"""为知识库实体生成**结构化摘要**并做 bge-m3 向量嵌入。

关键设计：嵌入什么文本决定了 RAG 有没有用。

    ❌ 嵌整页正文：太长、噪声多、区分度低
    ✅ 嵌结构化摘要：名称+别名+标签+关键字段+一小段正文

结构化摘要正好把"这是谁、干什么的、和谁有关"压缩进几十字，向量区分度远高于正文。

嵌入模型走 MaiBot 自己配的 SiliconFlow `Pro/BAAI/bge-m3`（1024 维，OpenAI 兼容接口）。
之所以能在独立脚本里调：provider 配置（base_url + key）就在 config/model_config.toml 里。

用法：
    python scripts/thwiki_embed.py --kinds character game official_track spellcard
    python scripts/thwiki_embed.py --limit 200          # 小样
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import struct
import time
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Tuple

import httpx

MODEL = "Pro/BAAI/bge-m3"
BATCH = 32
SUMMARY_LIMIT = 220


def load_siliconflow(config_path: Path) -> Tuple[str, str]:
    """从 MaiBot 的 model_config.toml 取出 SiliconFlow 的 base_url 与 key。"""

    cfg = tomllib.loads(config_path.read_text(encoding="utf-8"))
    for provider in cfg.get("api_providers", []):
        if provider.get("name") == "SiliconFlow":
            base = str(provider.get("base_url") or "").rstrip("/")
            key = str(provider.get("api_key") or "")
            if not base or not key:
                raise RuntimeError("SiliconFlow provider 缺 base_url 或 api_key")
            return base, key
    raise RuntimeError("model_config.toml 里没有 SiliconFlow provider")


def build_summary(row: sqlite3.Row, tags: List[str]) -> str:
    """把实体压成一段高信息密度的结构化摘要——这才是该被向量化的文本。"""

    parts: List[str] = []
    name = row["name_cn"] or row["name_jp"] or ""
    alias = " / ".join(x for x in (row["name_jp"], row["name_en"]) if x and x != name)
    parts.append(f"{name}（{alias}）" if alias else name)

    kind_label = {
        "character": "角色", "game": "官方游戏", "official_track": "官方曲目",
        "spellcard": "符卡", "spellcard_2nd": "二次创作符卡",
        "album": "音乐专辑", "doujin": "同人作品",
    }.get(row["kind"], row["kind"])
    parts.append(kind_label)

    for label, value in (
        ("种族", row["species"]), ("职业", row["work_type"]), ("所在地", row["location"]),
        ("初登场", row["first_work"]), ("发行", row["release_date"]),
        ("所属作品", row["parent_title"]), ("关卡", row["stage"]),
        ("角色", row["owner"]), ("作曲", row["composer"]),
    ):
        if value:
            parts.append(f"{label}：{str(value)[:40]}")

    if tags:
        parts.append("标签：" + "、".join(tags[:8]))

    body = (row["body"] or row["summary"] or "").strip()
    if body:
        parts.append(body[:SUMMARY_LIMIT])
    return "｜".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="bge-m3 向量嵌入")
    parser.add_argument("--db", type=Path, default=Path("data") / "thwiki" / "thwiki.db")
    parser.add_argument("--config", type=Path, default=Path("config") / "model_config.toml")
    parser.add_argument("--kinds", nargs="+",
                        default=["character", "game", "official_track", "spellcard", "spellcard_2nd"])
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    base, key = load_siliconflow(args.config)
    print(f"嵌入服务：{base} · 模型 {MODEL}")

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS vectors ("
        " entity_id INTEGER PRIMARY KEY, model TEXT, dim INTEGER, vec BLOB)"
    )

    placeholders = ",".join("?" * len(args.kinds))
    rows = conn.execute(
        f"SELECT * FROM entities WHERE kind IN ({placeholders})", args.kinds
    ).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    print(f"待嵌入实体：{len(rows)} 条（kinds={args.kinds}）")

    # 标签按实体聚合，拼进摘要
    tag_map: Dict[int, List[str]] = {}
    for tag in conn.execute("SELECT entity_id, tag_type, tag_value FROM tags"):
        tag_map.setdefault(int(tag["entity_id"]), []).append(f"{tag['tag_type']}={tag['tag_value']}")

    texts = [build_summary(row, tag_map.get(int(row["entity_id"]), [])) for row in rows]
    print("摘要样例：\n  " + texts[0][:180] if texts else "  （无）")

    done = 0
    started = time.time()
    with httpx.Client(timeout=120.0) as client:
        for start in range(0, len(texts), BATCH):
            chunk = texts[start : start + BATCH]
            ids = [int(r["entity_id"]) for r in rows[start : start + BATCH]]
            resp = client.post(
                f"{base}/embeddings",
                json={"model": MODEL, "input": chunk},
                headers={"Authorization": f"Bearer {key}"},
            )
            if resp.status_code != 200:
                raise RuntimeError(f"嵌入失败 HTTP {resp.status_code}: {resp.text[:200]}")
            vectors = [item["embedding"] for item in resp.json()["data"]]
            for entity_id, vec in zip(ids, vectors):
                blob = struct.pack(f"<{len(vec)}f", *vec)
                conn.execute(
                    "INSERT OR REPLACE INTO vectors(entity_id, model, dim, vec) VALUES (?,?,?,?)",
                    (entity_id, MODEL, len(vec), blob),
                )
            conn.commit()
            done += len(chunk)
            if done % 512 == 0 or done == len(texts):
                print(f"  …已嵌入 {done}/{len(texts)}（{time.time() - started:.0f} 秒）", flush=True)

    total = conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
    print(f"完成：本次 {done} 条，库内累计 {total} 条，用时 {time.time() - started:.0f} 秒")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
