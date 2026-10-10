#!/usr/bin/env python
"""THBWiki 语料普查：量化"脏数据"到底有多少，并抽样例给人看。

为什么需要它：在决定"要不要清洗、清洗什么"之前，先把分布量出来。
凭感觉说"应该挺脏"没法做工程决策——要的是「重定向 3510 页 / 空页 3 页 /
模板占比 >60% 的页 N 页」这种可核对、可复跑的数字。

输出：<data>/CENSUS.md（统计 + 各类脏数据实例），供人直接打开查看。

用法：
    python scripts/thwiki_census.py                       # 普查 data/thwiki
    python scripts/thwiki_census.py --render-samples 3    # 另抓几页渲染版做对比
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List

# 判定"脏"的规则：每条都要能说清为什么算脏
_TEMPLATE_RE = re.compile(r"\{\{[^{}]*\}\}|\{\|[\s\S]*?\|\}")
_TAG_RE = re.compile(r"<[^>]+>")
_REDIRECT_RE = re.compile(r"^\s*#(?:REDIRECT|重定向)\s*\[\[([^\]]+)\]\]", re.I)


def _template_ratio(text: str) -> float:
    """模板/表格标记占正文的比例——越高说明"渲染前几乎没有可读正文"。"""

    if not text:
        return 0.0
    covered = sum(len(m.group(0)) for m in _TEMPLATE_RE.finditer(text))
    return covered / len(text)


def load_pages(data_dir: Path) -> List[Dict[str, Any]]:
    """载入全部命名空间的原始快照。"""

    rows: List[Dict[str, Any]] = []
    for path in sorted(glob.glob(str(data_dir / "pages-ns*.jsonl.gz"))):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                rows.append(json.loads(line))
    return rows


def classify(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """把"脏"分门别类，每类单独计数——便于逐类决定处理方式。"""

    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        text = row.get("wikitext") or ""
        title = str(row.get("title") or "")
        length = len(text)
        if _REDIRECT_RE.match(text):
            buckets["重定向页"].append(row)
        if length < 50:
            buckets["空/极短页(<50字)"].append(row)
        elif length < 200:
            buckets["短页(50-200字)"].append(row)
        if length and _template_ratio(text) > 0.6:
            buckets["模板占比>60%(渲染前几乎无正文)"].append(row)
        if "__OMAKETXT__" in text or "<pre" in text.lower():
            buckets["原始转储页(__OMAKETXT__/<pre>)"].append(row)
        if title.endswith(("/导航", "/Navbox")) or "导航" in title:
            buckets["导航页"].append(row)
        if "消歧义" in title:
            buckets["消歧义页"].append(row)
        if re.search(r"\{\{\s*(待补充|施工中|stub|小作品)", text, re.I):
            buckets["标注为待补充/小作品"].append(row)
        if text.count("<ref") > 20:
            buckets["脚注极多(>20个<ref>)"].append(row)
    return buckets


def render_samples(rows: List[Dict[str, Any]], titles: List[str]) -> Dict[str, str]:
    """抓几页的渲染版纯文本，用于和原始 wikitext 做对比。"""

    import html as H

    import httpx

    out: Dict[str, str] = {}
    for title in titles:
        try:
            data = httpx.get(
                "https://thwiki.cc/api.php",
                params={
                    "action": "parse",
                    "format": "json",
                    "formatversion": "2",
                    "page": title,
                    "prop": "text",
                    "disablelimitreport": "1",
                    "disableeditsection": "1",
                    "disabletoc": "1",
                },
                headers={"User-Agent": "MarisaBOT-thwiki-archiver/1.0"},
                timeout=60.0,
            ).json()
        except Exception as exc:  # noqa: BLE001 - 样例抓取失败不该让普查整体失败
            out[title] = f"（渲染失败：{exc}）"
            continue
        html = (data.get("parse") or {}).get("text")
        if not html:
            out[title] = "（该页没有渲染结果）"
            continue
        body = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", html, flags=re.I)
        body = re.sub(r"<[^>]+>", "\n", body)
        body = H.unescape(body)
        body = re.sub(r"[ \t\u00a0]+", " ", body)
        lines, seen = [], None
        for line in body.split("\n"):
            line = line.strip()
            if line and line != seen:
                lines.append(line)
            seen = line
        out[title] = "\n".join(lines)
    return out


def _excerpt(text: str, limit: int = 700) -> str:
    """截断长文本用于报告展示。"""

    body = (text or "").strip()
    return body if len(body) <= limit else body[:limit] + "\n…（截断）"


def main() -> int:
    parser = argparse.ArgumentParser(description="THBWiki 语料普查")
    parser.add_argument("--data", type=Path, default=Path("data") / "thwiki")
    parser.add_argument("--render-samples", type=int, default=0, help="额外抓几页渲染版做对比")
    args = parser.parse_args()

    rows = load_pages(args.data)
    if not rows:
        print(f"没找到语料：{args.data}/pages-ns*.jsonl.gz")
        return 2

    lengths = [len(r.get("wikitext") or "") for r in rows]
    buckets = classify(rows)
    ns_counter = Counter(int(r.get("ns") or 0) for r in rows)
    total_chars = sum(lengths)

    lines: List[str] = []
    lines.append("# THBWiki 语料普查\n")
    lines.append(f"- 页面总数：**{len(rows)}**")
    lines.append(f"- 正文字符总数：**{total_chars:,}**（约 {total_chars / 10000:.0f} 万字）")
    lines.append(f"- 命名空间分布：{dict(ns_counter)}")
    lines.append(f"- 正文长度：中位数 {statistics.median(lengths):.0f} 字 · "
                 f"均值 {statistics.mean(lengths):.0f} 字 · 最长 {max(lengths):,} 字")
    lines.append("")
    lines.append("## 一、脏数据分类计数\n")
    lines.append("| 类别 | 页数 | 占比 | 处理建议 |")
    lines.append("|---|---:|---:|---|")
    advice = {
        "重定向页": "不建正文，只登记成 alias（问题里常出现别名）",
        "空/极短页": "丢弃或只留标题",
        "短页(50-200字)": "保留，但检索时降权",
        "模板占比>60%(渲染前几乎无正文)": "**必须渲染**，原始 wikitext 对模型无意义",
        "原始转储页(__OMAKETXT__/<pre>)": "保留但单独标记，格式噪声大",
        "导航页": "丢弃（纯目录，对问答无用）",
        "消歧义页": "保留为并列候选，不要当答案",
        "标注为待补充/小作品": "保留但降权",
        "脚注极多(>20个<ref>)": "渲染时去掉 <ref>，否则正文被注释淹没",
    }
    for name, items in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        pct = len(items) / len(rows) * 100
        lines.append(f"| {name} | {len(items)} | {pct:.1f}% | {advice.get(name, '按需处理')} |")
    lines.append("")
    lines.append("> 注意：这些类别**允许重叠**（一页可能既是短页又是导航页），"
                 "所以不能相加；上表用于逐类决定策略，不是分区。\n")

    lines.append("## 二、原始 wikitext vs 渲染后纯文本（同页对比）\n")
    sample_titles = ["东方红魔乡", "Dream Express（符卡）"]
    if args.render_samples:
        extra = [str(r["title"]) for r in rows if str(r["title"]).endswith("/Music")][: args.render_samples]
        for title in extra:
            if title not in sample_titles:
                sample_titles.append(title)
    rendered = render_samples(rows, sample_titles) if args.render_samples else {}

    by_title = {str(r.get("title")): r for r in rows}
    for title in sample_titles:
        row = by_title.get(title)
        lines.append(f"### {title}\n")
        if row is None:
            lines.append("（语料里没有这一页）\n")
            continue
        lines.append(f"长度：原始 {len(row.get('wikitext') or ''):,} 字"
                     + (f" · 渲染后 {len(rendered.get(title, '')):,} 字" if rendered else "")
                     + "\n")
        lines.append("**原始 wikitext：**\n")
        lines.append("```text")
        lines.append(_excerpt(row.get("wikitext") or "", 600))
        lines.append("```\n")
        if rendered:
            lines.append("**渲染后纯文本：**\n")
            lines.append("```text")
            lines.append(_excerpt(rendered.get(title, ""), 600))
            lines.append("```\n")

    lines.append("## 三、各类脏数据实例\n")
    for name in ["空/极短页(<50字)", "模板占比>60%(渲染前几乎无正文)", "导航页", "消歧义页"]:
        items = buckets.get(name, [])[:3]
        if not items:
            continue
        lines.append(f"### {name}\n")
        for row in items:
            lines.append(f"- **{row.get('title')}**（{len(row.get('wikitext') or '')} 字）")
            lines.append("```text")
            lines.append(_excerpt(row.get("wikitext") or "", 240))
            lines.append("```")
        lines.append("")

    report = args.data / "CENSUS.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已写出：{report}")
    print(f"页面 {len(rows)} · 正文 {total_chars / 10000:.0f} 万字")
    for name, items in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        print(f"  {name}: {len(items)} ({len(items) / len(rows) * 100:.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
