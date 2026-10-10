#!/usr/bin/env python
"""解析角色页的**章节结构**，把角色档案存成结构化数据。

为什么需要：之前只抽了信息框里"一句话能写完"的参数（种族/能力/所在地…），
于是角色表 39 列只填上 9 列——外貌、性格、故事、符卡、人际关系**一个字段都没有**。
但这些东西一直都在页面里：实测八云紫的页面有 70 个章节，结构恰好是

    角色介绍 / 外貌特征 · 角色能力 · 人际关系 · 登场剧情 · 生活状况
    一设资料 / 东方XX / 设定文档 · 符卡

所以正确做法是**解析章节**，而不是继续往信息框参数里找。

同时把符卡按 owner 关联到角色（spellcard.owner 已填 1079/1549）。

用法：
    python scripts/thwiki_character_profile.py --dry-run
    python scripts/thwiki_character_profile.py
"""

from __future__ import annotations

import argparse
import html
import re
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Tuple

import httpx

API = "https://thwiki.cc/api.php"
UA = {"User-Agent": "MarisaBOT-thwiki-archiver/1.0 (knowledge base build)"}

# 章节名 → 归一化字段名。命中即作为"档案要点"收录。
SECTION_ALIASES: Dict[str, str] = {
    "外貌特征": "外貌",
    "外貌": "外貌",
    "角色能力": "能力",
    "能力": "能力",
    "性格": "性格",
    "人际关系": "人际关系",
    "登场剧情": "剧情",
    "生活状况": "生活",
    "设定文档": "设定文档",
    "符卡": "符卡",
    "技能与符卡": "符卡",
    "官方文档": "设定文档",
}


def parse_sections(raw_html: str) -> List[Tuple[str, str]]:
    """把渲染后的 HTML 按标题切成 (章节路径, 正文)。

    用渲染后的 HTML 而不是 wikitext：正文里的模板（{{能力}} 之类）在 wikitext 里
    是引用，剥掉就会留下「紫拥有。」这种空洞；渲染后已经是展开好的文本。
    """

    # 标题与内容按顺序切成块
    pieces = re.split(r"<(h[1-6])[^>]*>([\s\S]*?)</\1>", raw_html, flags=re.I)
    sections: List[Tuple[str, str]] = []
    if len(pieces) < 3:
        return sections
    stack: Dict[int, str] = {}
    body = pieces[0]
    for i in range(1, len(pieces) - 2, 3):
        level = int(pieces[i][1])
        title = re.sub(r"<[^>]+>", "", pieces[i + 1])
        title = html.unescape(title).replace("[编辑]", "").strip()
        body = pieces[i + 2]
        stack[level] = title
        for deeper in [k for k in stack if k > level]:
            del stack[deeper]
        path = "/".join(stack[k] for k in sorted(stack))
        text = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", body, flags=re.I)
        text = re.sub(r"</(p|div|li|h[1-6]|tr|br)\s*>", "\n", text, flags=re.I)
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
        text = html.unescape(text).replace("\xa0", " ")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{2,}", "\n", text).strip()
        if text:
            sections.append((path, text))
    return sections


def main() -> int:
    parser = argparse.ArgumentParser(description="解析角色页章节，建立角色档案")
    parser.add_argument("--db", type=Path, default=Path("data") / "thwiki" / "thwiki.db")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS character_sections (
          entity_id INTEGER, section TEXT, text TEXT,
          PRIMARY KEY (entity_id, section)
        );
        CREATE INDEX IF NOT EXISTS idx_cs_entity ON character_sections(entity_id);
        """
    )

    characters = conn.execute(
        "SELECT entity_id, name_cn, source_title FROM entities WHERE kind='character'"
    ).fetchall()
    print(f"角色 {len(characters)} 个")
    if args.dry_run:
        with httpx.Client(timeout=90.0) as client:
            row = conn.execute(
                "SELECT source_title FROM entities WHERE name_cn='八云紫'"
            ).fetchone()
            data = client.get(
                API, params={"action": "parse", "format": "json", "formatversion": "2",
                             "page": row["source_title"], "prop": "text", "redirects": "1"},
                headers=UA,
            ).json()
            sections = parse_sections((data.get("parse") or {}).get("text") or "")
            print(f"八云紫：解析出 {len(sections)} 个章节")
            for path, text in sections[:12]:
                print("   [%s] %s" % (path[:34], text[:60].replace("\n", " ")))
            return 0

    with httpx.Client(timeout=90.0) as client:
        done = 0
        for n, row in enumerate(characters, 1):
            if n % 40 == 0:
                print(f"  …{n}/{len(characters)}（已写入 {done} 个角色的档案）", flush=True)
            data = None
            for attempt in range(3):
                resp = client.get(
                    API, params={"action": "parse", "format": "json", "formatversion": "2",
                                 "page": row["source_title"], "prop": "text", "redirects": "1"},
                    headers=UA,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    break
                time.sleep(2**attempt)
            if not data or "error" in data:
                continue
            sections = parse_sections((data.get("parse") or {}).get("text") or "")
            if not sections:
                continue
            conn.executemany(
                "INSERT OR REPLACE INTO character_sections(entity_id, section, text) VALUES (?,?,?)",
                [(int(row["entity_id"]), path, text[:3000]) for path, text in sections],
            )
            conn.commit()
            done += 1

    # 符卡按 owner 关联（spellcard.owner 已填 1079/1549）
    linked = 0
    for row in conn.execute(
        """SELECT entity_id, name_cn FROM entities WHERE kind='character'"""
    ):
        name = str(row["name_cn"])
        n = conn.execute(
            "SELECT COUNT(*) FROM entities WHERE kind IN ('spellcard','spellcard_2nd') AND owner LIKE ?",
            (f"%{name}%",),
        ).fetchone()[0]
        if n:
            conn.execute(
                "INSERT OR REPLACE INTO character_sections(entity_id, section, text) VALUES (?,?,?)",
                (int(row["entity_id"]), "符卡统计", f"{name}共有 {n} 张符卡（含二次创作）"),
            )
            linked += 1
    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM character_sections").fetchone()[0]
    print(f"\n完成：{done} 个角色、{total} 条章节记录；符卡关联 {linked} 个角色")

    print("\n=== 八云紫的档案要点 ===")
    row = conn.execute("SELECT entity_id FROM entities WHERE name_cn='八云紫'").fetchone()
    for sec in conn.execute(
        "SELECT section, text FROM character_sections WHERE entity_id=? ORDER BY section", (row["entity_id"],)
    ):
        print("   【%s】%s" % (sec["section"][:30], sec["text"][:70].replace("\n", " ")))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
