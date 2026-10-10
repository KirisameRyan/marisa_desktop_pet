#!/usr/bin/env python
"""用**服务端渲染后的整页文本**替换有空洞的块。

为什么必须渲染：
    strip_templates 会把正文里的模板（{{能力}}、{{color|…}} 之类）剥掉却不还原，
    于是角色介绍里出现空洞：
        「她是，也是曾参与创立幻想乡的妖怪贤者之一」   ← "她是"后面空了
        「紫拥有。」「紫属于的角色，。」
    这些位置原本是模板引用。继续修补清洗规则治不了根——正确的做法是走
    `action=parse&page=<标题>&prop=text` 拿 **服务端渲染后的正文**（模板已展开）。

用法：
    python scripts/thwiki_render_pages.py --dry-run
    python scripts/thwiki_render_pages.py
"""

from __future__ import annotations

import argparse
import html
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from thwiki_embed_pages import BATCH as EMBED_BATCH
from thwiki_embed_pages import CHUNK, KANA_LIMIT, MODEL, chunk_text, kana_ratio, prefer_chinese_section

API = "https://thwiki.cc/api.php"
UA = {"User-Agent": "MarisaBOT-thwiki-archiver/1.0 (knowledge base build)"}
MIN_PAGE_CHARS = 200

# 只认真正的空洞：连续标点，或动词/助词直接跟句号（"紫拥有。"）
HOLE_RE = re.compile(r"[，。、；：]{2,}|(?:拥有|属于|具有|有着|操纵|使用|掌管|成为)[。，]")


# 渲染后的 HTML 里这些是**页面结构**而不是正文，必须先丢掉：
# 踩过：原样转文本会让块 0 变成「本页是关于…词条八云紫（官方角色，新作）
# 角色信息中间角色即八云紫基本信息人物名八云紫…」，全是目录与信息框，
# 八云紫的块数从 11 涨到 78。
_DROP_HTML = (
    # 目录
    r'<div[^>]*\bid="toc"[^>]*>[\s\S]*?</div>\s*</div>',
    r'<div[^>]*\bid="toc"[^>]*>[\s\S]*?</ul>',
    # 页面头（"本页是关于…的词条"）与消歧义行
    r'<div[^>]*\bclass="[^"]*page-content-header[^"]*"[^>]*>[\s\S]*?</div>\s*</div>',
    r'<div[^>]*\bclass="[^"]*disambig-description[^"]*"[^>]*>[\s\S]*?</div>',
    # 角色信息框 = 第一个 wikitable 表格；其内容（种族/能力/日文名…）
    # 与 entities 的字段重复，留着只会污染块 0
    r'<table[^>]*\bclass="[^"]*wikitable[^"]*"[^>]*>[\s\S]*?</table>',
    r'<div[^>]*\bclass="[^"]*navbox[^"]*"[^>]*>[\s\S]*?</table>',
    r'<div[^>]*\bid="catlinks"[^>]*>[\s\S]*?</div>',
    r'<span[^>]*\bclass="[^"]*mw-editsection[^"]*"[^>]*>[\s\S]*?</span>',
    r'<div[^>]*\bclass="[^"]*noprint[^"]*"[^>]*>[\s\S]*?</div>',
    r'<table[^>]*\bclass="[^"]*metadata[^"]*"[^>]*>[\s\S]*?</table>',
)
# 文本阶段再兜一层：目录特征是高密度的 "N.M" 编号
_TOC_LINE_RE = re.compile(r"(?:\d+\.\d+[\s\S]{0,40}){4,}")


def html_to_text(raw_html: str) -> str:
    """渲染后的 HTML → 纯文本（丢掉目录/信息框/导航，保留段落边界）。"""

    text = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", raw_html, flags=re.I)
    for pattern in _DROP_HTML:
        text = re.sub(pattern, " ", text, flags=re.I)
    text = re.sub(r"</(p|div|li|h[1-6]|tr|br)\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    # 干掉目录残渣（"1 角色信息1.1 角色介绍1.1.1 生活状况…"）
    text = _TOC_LINE_RE.sub(" ", text)
    # 去掉页面头部的"本页是关于…的词条"与"查 • 编"导航行
    text = re.sub(r"本页是关于[\s\S]{0,120}?词条", " ", text)
    text = re.sub(r"查\s*[•·]\s*编[\s\S]{0,400}", " ", text)
    # 章节标题后的"编辑"链接；只清理独立成词的，避免误伤正文里的"编辑"
    text = re.sub(r"(?:^|\s)编辑(?=\s|$)", " ", text, flags=re.M)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="用渲染文本替换有空洞的块")
    parser.add_argument("--db", type=Path, default=Path("data") / "thwiki" / "thwiki.db")
    parser.add_argument("--config", type=Path, default=Path("config") / "model_config.toml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row

    # 目标页：角色页（介绍必须完整）+ 任何块里有真空洞的页
    targets: Dict[int, str] = {}
    for row in conn.execute(
        "SELECT DISTINCT e.source_title AS t, pc.pageid AS pid FROM entities e "
        "JOIN page_chunks pc ON pc.title = e.source_title WHERE e.kind='character'"
    ):
        if row["t"]:
            targets[int(row["pid"])] = str(row["t"])
    holes = 0
    for row in conn.execute("SELECT pageid, title, text FROM page_chunks"):
        if int(row["pageid"]) in targets:
            continue
        if HOLE_RE.search(str(row["text"])):
            targets[int(row["pageid"])] = str(row["title"])
            holes += 1
    print(f"目标页 {len(targets)} 个（角色页 + 有真空洞的页 {holes} 个）")
    if args.dry_run:
        for pid, title in list(targets.items())[:10]:
            row = conn.execute("SELECT text FROM page_chunks WHERE pageid=? LIMIT 1", (pid,)).fetchone()
            m = HOLE_RE.search(str(row["text"])) if row else None
            if m:
                print(f"   {title[:26]}: ...{row['text'][max(0, m.start()-18):m.end()+8]}...")
        return 0

    items = list(targets.items())[: args.limit] if args.limit else list(targets.items())
    with httpx.Client(timeout=90.0) as client:
        replaced = 0
        for n, (pageid, title) in enumerate(items, 1):
            if n % 100 == 0:
                print(f"  …已处理 {n}/{len(items)}，替换 {replaced} 页", flush=True)
            data = None
            for attempt in range(3):
                resp = client.get(
                    API,
                    params={"action": "parse", "format": "json", "formatversion": "2",
                            "page": title, "prop": "text", "redirects": "1"},
                    headers=UA,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    break
                time.sleep(2**attempt)
            if not data or "error" in data:
                continue
            text = html_to_text((data.get("parse") or {}).get("text") or "")
            text = prefer_chinese_section(text)
            if len(text) < MIN_PAGE_CHARS:
                continue
            old = conn.execute(
                "SELECT chunk_id FROM page_chunks WHERE pageid=?", (pageid,)
            ).fetchall()
            old_ids = [int(r["chunk_id"]) for r in old]
            new_chunks = [c for c in chunk_text(text) if kana_ratio(c) <= KANA_LIMIT]
            if not new_chunks:
                continue
            if old_ids:
                marks = ",".join("?" * len(old_ids))
                conn.execute(f"DELETE FROM chunk_vectors WHERE chunk_id IN ({marks})", old_ids)
                conn.execute(f"DELETE FROM page_chunks WHERE chunk_id IN ({marks})", old_ids)
            url = "https://thwiki.cc/" + title.replace(" ", "_")
            conn.executemany(
                "INSERT INTO page_chunks(pageid, ns, title, url, chunk_no, text) VALUES (?,?,?,?,?,?)",
                [(pageid, 0, title, url, i, c) for i, c in enumerate(new_chunks)],
            )
            conn.commit()
            first_id = conn.execute("SELECT MAX(chunk_id) FROM page_chunks").fetchone()[0]
            new_ids = list(range(first_id - len(new_chunks) + 1, first_id + 1))
            embed_chunks(conn, client, args.config, new_chunks, new_ids)
            replaced += 1
        print(f"完成：替换 {replaced} 页")

    print("\n=== 复查八云紫介绍 ===")
    row = conn.execute(
        "SELECT text FROM page_chunks WHERE title='八云紫' ORDER BY chunk_no LIMIT 1"
    ).fetchone()
    if row:
        print("  " + str(row["text"])[:200])
    conn.close()
    return 0


def embed_chunks(conn: sqlite3.Connection, client: httpx.Client, config: Path,
                 texts: List[str], ids: List[int]) -> None:
    """给新块生成向量。"""

    import struct
    import tomllib

    cfg = tomllib.loads(config.read_text(encoding="utf-8"))
    base = key = ""
    for provider in cfg.get("api_providers", []):
        if provider.get("name") == "SiliconFlow":
            base, key = str(provider["base_url"]).rstrip("/"), str(provider["api_key"])
    for start in range(0, len(texts), EMBED_BATCH):
        batch = texts[start : start + EMBED_BATCH]
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
                "INSERT OR REPLACE INTO chunk_vectors(chunk_id, vec) VALUES (?,?)",
                (ids[start + offset], struct.pack(f"<{len(vec)}f", *vec)),
            )
        conn.commit()


if __name__ == "__main__":
    raise SystemExit(main())
