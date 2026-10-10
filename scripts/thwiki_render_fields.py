#!/usr/bin/env python
"""渲染补全角色字段（能力/所在地等）。

为什么必须渲染：
    角色页的「能力」值形如
        {{东风谷早苗能力|1|风神录}}{{color:#808080|（风神录、星莲船…）}}
    这是**位置参数模板**，真正的文本（"操纵风程度的能力"）存在模板内部，
    可能是 `#getmap` 之类的解析器函数取出来的，**不在 wikitext 里**。
    所以直接读 dump 只能得到空值——179 个角色里只有 59 个是纯文本写的。

    解法沿用抽官方曲目时的同一招：把片段丢给 `action=parse&text=` 让服务端渲染成 HTML，
    再剥标签取纯文本。

用法：
    python scripts/thwiki_render_fields.py            # 全量（179 个角色，几十个请求）
    python scripts/thwiki_render_fields.py --dry-run  # 只打印将要渲染的片段
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Dict, List

import httpx

API = "https://thwiki.cc/api.php"
UA = {"User-Agent": "MarisaBOT-thwiki-archiver/1.0 (knowledge base build)"}
BATCH = 30           # 每个渲染请求放多少个片段（用 <hr> 分隔以便切回来）
SEP = "\n<hr>\n"

# 需要渲染补全的字段：实体列名 → 模板参数名的候选
FIELD_PARAMS: Dict[str, tuple] = {
    "ability": ("能力",),
    "location": ("所在地", "居住", "居住地", "所在地点"),
    "work_type": ("职业", "职务", "身份"),
}


def find_templates(value: str) -> List[str]:
    """取出值里所有的顶层 {{...}} 片段（含嵌套）。"""

    out: List[str] = []
    depth = 0
    start = -1
    i = 0
    while i < len(value):
        if value.startswith("{{", i):
            if depth == 0:
                start = i
            depth += 1
            i += 2
            continue
        if value.startswith("}}", i):
            if depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    out.append(value[start : i + 2])
                    start = -1
            i += 2
            continue
        i += 1
    return out


def render(fragments: List[str], client: httpx.Client) -> Dict[str, str]:
    """用 action=parse&text= 批量渲染片段，返回 片段 → 纯文本。"""

    rendered: Dict[str, str] = {}
    unique = list(dict.fromkeys(fragments))
    for start in range(0, len(unique), BATCH):
        chunk = unique[start : start + BATCH]
        payload = SEP.join(chunk)
        for attempt in range(4):
            resp = client.get(
                API,
                params={
                    "action": "parse", "format": "json", "formatversion": "2",
                    "text": payload, "prop": "text", "contentmodel": "wikitext",
                },
                headers=UA,
            )
            if resp.status_code == 200:
                break
            time.sleep(2**attempt)
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"渲染失败: {data['error']}")
        html_text = (data.get("parse") or {}).get("text") or ""
        parts = re.split(r"<hr\s*/?>", html_text)
        if len(parts) != len(chunk):
            # 服务端会把 <hr> 规范化，数量对不上就不做位置对应，避免错位污染
            raise RuntimeError(f"片段数不匹配：请求 {len(chunk)}，返回 {len(parts)}")
        for fragment, part in zip(chunk, parts):
            plain = re.sub(r"<[^>]+>", "", part)
            plain = html.unescape(plain).replace("\xa0", " ")
            plain = re.sub(r"\s+", " ", plain).strip()
            rendered[fragment] = plain
        print(f"  渲染 {min(start + BATCH, len(unique))}/{len(unique)} 片段", flush=True)
    return rendered


def main() -> int:
    parser = argparse.ArgumentParser(description="渲染补全角色字段")
    parser.add_argument("--db", type=Path, default=Path("data") / "thwiki" / "thwiki.db")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row

    # 收集：(entity_id, 列名, 当前值, 需要渲染的片段)
    tasks = []
    for row in conn.execute("SELECT entity_id, kind, name_cn, extra_json FROM entities"):
        try:
            extra = json.loads(row["extra_json"] or "{}")
        except json.JSONDecodeError:
            continue
        for column, param_names in FIELD_PARAMS.items():
            value = next((extra[p] for p in param_names if extra.get(p)), None)
            if not value or "{{" not in str(value):
                continue
            tasks.append((int(row["entity_id"]), column, str(value)))
    print(f"需要渲染补全的字段：{len(tasks)} 个（涉及 {len({t[0] for t in tasks})} 个实体）")

    fragments = [f for _eid, _col, value in tasks for f in find_templates(value)]
    if args.dry_run:
        print(f"将渲染 {len(set(fragments))} 个去重片段，例如：")
        for fragment in list(dict.fromkeys(fragments))[:8]:
            print("   ", fragment[:110])
        return 0

    with httpx.Client(timeout=90.0) as client:
        rendered = render(fragments, client)

    updated = 0
    for entity_id, column, value in tasks:
        result = value
        for fragment in find_templates(value):
            result = result.replace(fragment, rendered.get(fragment, ""))
        # 剩下的 {{color:#808080|（出处）}} 这类，保留括号里的说明文字
        result = re.sub(r"\{\{[^{}]*\|([^{}]*)\}\}", r"\1", result)
        result = re.sub(r"\{\{[^{}]*\}\}", " ", result)
        result = re.sub(r"<[^>]+>", "", result)
        result = re.sub(r"\s+", " ", result).strip(" ，,、")
        if len(result) > 300:
            result = result[:300]
        if result:
            conn.execute(f"UPDATE entities SET {column}=? WHERE entity_id=?", (result, entity_id))
            updated += 1
    conn.commit()

    print(f"\n已更新 {updated} 个字段。样例：")
    for row in conn.execute(
        "SELECT name_cn, ability, location FROM entities WHERE kind='character' "
        "AND ability<>'' ORDER BY entity_id LIMIT 5"
    ):
        print(f"   {row['name_cn']}: {row['ability'][:70]} | 所在地={row['location'][:20]}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
