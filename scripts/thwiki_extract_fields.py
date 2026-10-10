#!/usr/bin/env python
"""从页面正文抽取模板里**没有对应参数**的字段。

为什么需要：有些字段（如角色的「所在地」）在信息框模板里**根本没有参数**，
渲染也无从下手——179 个角色的 location 全空、extra_json 里也没有对应键。
但这类信息确实存在于页面正文里（"居住在幻想乡的边境"之类），
所以只能从正文用模式抽取。

用法：
    python scripts/thwiki_extract_fields.py --dry-run
    python scripts/thwiki_extract_fields.py
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from thwiki_embed_pages import clean, prefer_chinese_section, chinese_only

# 字段 → 抽取模式（第一个捕获组是答案）。按顺序尝试，取第一个非空结果。
FIELD_PATTERNS: Dict[str, List[str]] = {
    "location": [
        r"居住(?:在|于|地)[：: ]?([^。！？；,，\n]{2,28})",
        r"住在[：: ]?([^。！？；,，\n]{2,28})",
        r"所在地[：: ]?([^。！？；,，\n]{2,28})",
        r"住处[：: ]?([^。！？；,，\n]{2,28})",
        r"居于([^。！？；,，\n]{2,28})",
    ],
    "first_work": [
        r"首次登场于([^。！？；,，\n]{2,28})",
        r"初登场[于:：]?\s*([^。！？；,，\n]{2,28})",
        r"初次登场于([^。！？；,，\n]{2,28})",
    ],
}
# 抽出来的值里不能含这些（否则是脏数据）
REJECT = re.compile(r"[\[\]{}<>|=]|^\s*$|^\d+$")


def main() -> int:
    parser = argparse.ArgumentParser(description="从正文抽取模板缺失的字段")
    parser.add_argument("--data", type=Path, default=Path("data") / "thwiki")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db_path = args.db or (args.data / "thwiki.db")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # 只处理这些 kind 的实体，并按其 source_title 找到页面
    wanted: Dict[str, int] = {}
    for row in conn.execute(
        "SELECT entity_id, kind, name_cn, source_title FROM entities "
        "WHERE kind IN ('character','game')"
    ):
        wanted[str(row["source_title"])] = int(row["entity_id"])
    print(f"待抽取实体：{len(wanted)} 个（character/game）")

    found: Dict[int, Dict[str, str]] = {}
    pages = 0
    for path in sorted(glob.glob(str(args.data / "pages-ns*.jsonl.gz"))):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                page = json.loads(line)
                title = str(page.get("title") or "")
                entity_id = wanted.get(title)
                if entity_id is None:
                    continue
                pages += 1
                text = chinese_only(prefer_chinese_section(clean(page.get("wikitext") or "")))
                values: Dict[str, str] = {}
                for column, patterns in FIELD_PATTERNS.items():
                    for pattern in patterns:
                        match = re.search(pattern, text)
                        if not match:
                            continue
                        value = match.group(1).strip(" ：:，,、。")
                        if 1 < len(value) <= 30 and not REJECT.search(value):
                            values[column] = value
                            break
                if values:
                    found[entity_id] = values

    print(f"扫描到 {pages} 个实体页，其中 {len(found)} 个抽到了字段")

    if args.dry_run:
        for entity_id, values in list(found.items())[:6]:
            row = conn.execute(
                "SELECT name_cn FROM entities WHERE entity_id=?", (entity_id,)
            ).fetchone()
            print(f"   {row['name_cn']}: {values}")
        return 0

    updated = 0
    for entity_id, values in found.items():
        sets = ", ".join(f"{column}=?" for column in values)
        conn.execute(
            f"UPDATE entities SET {sets} WHERE entity_id=?",
            (*values.values(), entity_id),
        )
        updated += len(values)
    conn.commit()
    print(f"已更新 {updated} 个字段")

    print("\n=== 样例 ===")
    for row in conn.execute(
        "SELECT name_cn, location, first_work FROM entities "
        "WHERE kind='character' AND location<>'' LIMIT 8"
    ):
        print(f"   {row['name_cn']}: 所在地={row['location'][:24]}")
    for column in FIELD_PATTERNS:
        total = conn.execute(
            f"SELECT COUNT(*) FROM entities WHERE {column} IS NOT NULL AND {column}<>''"
        ).fetchone()[0]
        print(f"   {column} 非空: {total}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
