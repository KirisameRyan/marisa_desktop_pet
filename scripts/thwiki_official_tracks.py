#!/usr/bin/env python
"""从 <作品>/Music 页抽取**官方游戏曲目**（与同人曲目分开，kind='official_track'）。

为什么单独拎出来：
- 数据源不同：同人曲目来自 {{同人曲目信息}} 模板；官方曲目在 /Music 页里是自定义文本格式
- 语义不同：同人曲目挂在专辑下（编曲/原曲/社团）；官方曲目挂在游戏作品下（"3面主题曲"）
- 检索会被淹没：库里有 22 万条同人曲目，官方曲目只有几百条，不分开就永远排不上来

/Music 页的真实格式（实测）：
    == MusicRoom ==
    category
    3面主题曲                     ← 关卡定位，正是"三面道中曲"的答案锚点
    titleJA = {{红魔乡音乐名|2|3}}
    titleZH = [[{{红魔乡音乐名|1|3}}]]
    composer = ZUN
    mp3 = {{音乐室音频文件|th06_03.mp3}}
    ja
    　（日文说明）
    zh
    　（中文说明）

曲名本身是**位置参数查表模板**（{{红魔乡音乐名|1|3}}），而它内部的 #getmap 数据源
在 API 里查不到（Template:/Map: 命名空间都没有）。所以改用**片段渲染**：
`action=parse&text=<片段>` 可以把任意 wikitext 渲染出来——不需要找到那张映射表。

用法：
    python scripts/thwiki_official_tracks.py --limit 3     # 小样
    python scripts/thwiki_official_tracks.py               # 全部 /Music 页
"""

from __future__ import annotations

import argparse
import glob
import gzip
import html as H
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import httpx

API = "https://thwiki.cc/api.php"
UA = "MarisaBOT-thwiki-archiver/1.0"
_TPL_EXPR_RE = re.compile(r"\{\{[^{}]*\}\}")
_FIELD_RE = re.compile(r"^([A-Za-z_]+)\s*=\s*(.*)$")


def split_blocks(wikitext: str) -> List[Dict[str, str]]:
    """把 /Music 页切成一个个曲目块。

    块的形态是：`category` 独占一行，下一行是分类名；随后是 `key = value` 行，
    直到出现裸的 `ja` / `zh` 行（其后是多行说明）。
    """

    blocks: List[Dict[str, str]] = []
    lines = wikitext.split("\n")
    current: Dict[str, str] = {}
    expecting_category = False
    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if stripped == "category":
            if current.get("category"):
                blocks.append(current)
            current = {}
            expecting_category = True
            continue
        if expecting_category and stripped:
            current["category"] = stripped
            expecting_category = False
            continue
        if stripped in ("ja", "zh"):
            current[stripped] = ""
            continue
        if stripped in ("source", "mp3") and current.get("zh") is not None:
            # 同一 category 下可能有第二段 mp3/ja/zh（88Pro 版），沿用同一分类
            current.setdefault("extra", "")
            current["extra"] += line + "\n"
            continue
        field = _FIELD_RE.match(stripped)
        if field:
            current[field.group(1)] = field.group(2).strip()
            continue
        if current.get("zh") is not None and current.get("zh") != "":
            current["zh"] += line + "\n"
        elif current.get("ja") is not None:
            current["ja"] += line + "\n"
    if current.get("category"):
        blocks.append(current)
    return blocks


def resolve_templates(expressions: List[str], batch: int = 40) -> Dict[str, str]:
    """用片段渲染把 {{...音乐名|L|N}} 之类的表达式解析成显示文本。"""

    resolved: Dict[str, str] = {}
    separator = "\n@@@@\n"
    with httpx.Client(timeout=90.0, headers={"User-Agent": UA}) as client:
        for start in range(0, len(expressions), batch):
            chunk = expressions[start : start + batch]
            payload = separator.join(chunk)
            data = client.get(
                API,
                params={
                    "action": "parse",
                    "format": "json",
                    "formatversion": "2",
                    "text": payload,
                    "contentmodel": "wikitext",
                    "prop": "text",
                    "disablelimitreport": "1",
                    "disableeditsection": "1",
                },
            ).json()
            if "error" in data:
                raise RuntimeError(f"片段渲染失败：{data['error'].get('info')}")
            rendered = (data.get("parse") or {}).get("text") or ""
            text = re.sub(r"<[^>]+>", "", rendered)
            text = H.unescape(text)
            parts = [p.strip() for p in re.split(r"@+\s*@+\s*@+\s*@+", text)]
            if len(parts) != len(chunk):
                # 长度对不上就逐个兜底，绝不把错位的结果写进库
                print(f"   ⚠ 批次对齐失败（{len(parts)} != {len(chunk)}），改为逐个渲染")
                for expr in chunk:
                    resolved[expr] = resolve_templates([expr], batch=1).get(expr, "")
                continue
            for expr, part in zip(chunk, parts):
                resolved[expr] = re.sub(r"\s+", " ", part).strip()
            time.sleep(0.3)
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description="抽取 THBWiki 官方游戏曲目")
    parser.add_argument("--data", type=Path, default=Path("data") / "thwiki")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 个 /Music 页")
    args = parser.parse_args()

    db_path = args.db or (args.data / "thwiki.db")
    pages: List[Dict[str, Any]] = []
    for path in sorted(glob.glob(str(args.data / "pages-ns*.jsonl.gz"))):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                if str(row.get("title") or "").endswith("/Music"):
                    pages.append(row)
    if args.limit:
        pages = pages[: args.limit]
    print(f"找到 {len(pages)} 个 /Music 页")

    # 先切块，收集所有待解析的模板表达式，再批量解析（一次渲染几十个，省请求）
    parsed: List[Tuple[Dict[str, Any], Dict[str, str]]] = []
    expressions: List[str] = []
    for page in pages:
        for block in split_blocks(page.get("wikitext") or ""):
            parsed.append((page, block))
            for key in ("titleJA", "titleZH"):
                for expr in _TPL_EXPR_RE.findall(block.get(key, "")):
                    expressions.append(expr)
    unique = list(dict.fromkeys(expressions))
    print(f"曲目块 {len(parsed)} 个，待解析模板 {len(unique)} 个")

    resolved = resolve_templates(unique) if unique else {}
    print(f"解析完成 {len(resolved)} 个")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    inserted = 0
    for page, block in parsed:
        title = str(page.get("title") or "")
        work = title[: -len("/Music")]
        category = block.get("category", "")
        title_ja = _TPL_EXPR_RE.sub(lambda m: resolved.get(m.group(0), ""), block.get("titleJA", ""))
        title_zh = _TPL_EXPR_RE.sub(lambda m: resolved.get(m.group(0), ""), block.get("titleZH", ""))
        title_ja = re.sub(r"[\[\]]", "", title_ja).strip()
        title_zh = re.sub(r"[\[\]]", "", title_zh).strip()
        name_cn = title_zh or title_ja
        if not name_cn:
            continue
        conn.execute(
            """INSERT OR IGNORE INTO entities(
                 kind, official, name_cn, name_jp, parent_title, work_type, stage,
                 original_for, composer, summary, extra_json,
                 source_pageid, source_title, url)
               VALUES ('official_track',1,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                name_cn,
                title_ja,
                work,
                "官方曲目",
                category,                # stage ← "3面主题曲"，检索的关键锚点
                category,
                block.get("composer", ""),
                re.sub(r"\s+", " ", (block.get("zh") or "")).strip()[:400],
                json.dumps(block, ensure_ascii=False),
                int(page.get("pageid") or 0),
                title,
                "https://thwiki.cc/" + title.replace(" ", "_"),
            ),
        )
        inserted += 1

    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM entities WHERE kind='official_track'").fetchone()[0]
    conn.close()
    print(f"写入 official_track 共 {inserted} 条（库内累计 {total} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
