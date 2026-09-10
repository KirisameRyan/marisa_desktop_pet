# -*- coding: utf-8 -*-
"""清理方案卡片：把 cleanup_plan 生成的方案渲染成图片与纯文本两种形态。

图片走宿主 ``ctx.render.html2png``（与 /help 菜单同一条已验证的渲染路径），
渲染失败时由调用方退回这里的纯文本版本。
"""
from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any, Dict, List

# 展示顺序：先缓存后整理，与主人直觉一致
CATEGORY_ORDER = ["临时缓存", "桌面整理"]


def _human_size(num_bytes: float) -> str:
    """把字节数转成人类可读文本（1024 进制）。"""

    value = float(max(0.0, num_bytes))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _sorted_categories(items: List[Dict[str, Any]]) -> List[str]:
    """按固定顺序返回方案里出现的类别。"""

    seen: List[str] = []
    for item in items:
        category = str(item.get("category") or "其他")
        if category not in seen:
            seen.append(category)
    return sorted(seen, key=lambda name: CATEGORY_ORDER.index(name) if name in CATEGORY_ORDER else len(CATEGORY_ORDER))


def _totals(plan: Dict[str, Any]) -> Dict[str, int]:
    """统计缓存总量、桌面整理数量与总条目数。"""

    items = list(plan.get("items") or [])
    cache_bytes = sum(int(item.get("size") or 0) for item in items if item.get("kind") == "clean_dir")
    move_count = len([item for item in items if item.get("kind") == "move"])
    return {"cache_bytes": cache_bytes, "move_count": move_count, "total": len(items)}


def render_text(plan: Dict[str, Any], *, allow_permanent: bool = False) -> str:
    """渲染桌面气泡/文本兜底用的清理方案。"""

    code = int(plan.get("code") or 0)
    items = list(plan.get("items") or [])
    ttl_min = max(1, int(plan.get("ttl_sec") or 600) // 60)
    totals = _totals(plan)

    lines: List[str] = [f"🧹 清理方案 P{code}（{ttl_min} 分钟内有效）", ""]
    for category in _sorted_categories(items):
        lines.append(f"【{category}】")
        for item in [entry for entry in items if str(entry.get("category") or "其他") == category]:
            size = _human_size(int(item.get("size") or 0))
            if item.get("kind") == "move":
                target_dir = Path(str(item.get("dst") or "")).parent.name
                lines.append(f"{item.get('id')}. {item.get('label')}  {size} → {target_dir}/")
            else:
                lines.append(f"{item.get('id')}. {item.get('label')}  {size}")
        lines.append("")

    lines.append(
        f"合计 {totals['total']} 项：缓存 {_human_size(totals['cache_bytes'])}"
        f"，桌面整理 {totals['move_count']} 个文件（只移动，可 /撤销）"
    )
    if allow_permanent and totals["cache_bytes"] > 0:
        lines.append("勾选「彻底删除」可真正释放缓存占用；默认只送回收站。")
    lines.append("")
    replies = [f"执行 P{code}（全部）", f"执行 P{code} 1,3（只做这几项）"]
    if allow_permanent:
        replies.append(f"执行 P{code} 彻底（缓存彻底删除）")
    replies.append(f"取消 P{code}")
    lines.append("回复：" + "｜".join(replies))
    return "\n".join(lines)


def render_html(plan: Dict[str, Any], *, allow_permanent: bool = False) -> str:
    """渲染清理方案 HTML（供浏览器渲染成图片）。"""

    code = int(plan.get("code") or 0)
    items = list(plan.get("items") or [])
    ttl_min = max(1, int(plan.get("ttl_sec") or 600) // 60)
    totals = _totals(plan)

    sections: List[str] = []
    for category in _sorted_categories(items):
        cards: List[str] = []
        for item in [entry for entry in items if str(entry.get("category") or "其他") == category]:
            size = _human_size(int(item.get("size") or 0))
            if item.get("kind") == "move":
                target_dir = Path(str(item.get("dst") or "")).parent.name
                detail = f"{escape(str(item.get('reason') or ''))}"
                arrow = f'<span class="arrow">→ {escape(target_dir)}/</span>'
            else:
                detail = escape(str(item.get("reason") or ""))
                arrow = ""
            cards.append(
                '<div class="card">'
                f'<div class="row"><span class="idx">{item.get("id")}</span>'
                f'<span class="name">{escape(str(item.get("label") or ""))}</span>'
                f'<span class="size">{size}</span></div>'
                f'<div class="desc">{detail}{arrow}</div>'
                "</div>"
            )
        sections.append(
            f'<section class="group"><h2>{escape(category)}</h2><div class="cards">{"".join(cards)}</div></section>'
        )

    replies = [f"执行 P{code}", f"执行 P{code} 1,3"]
    if allow_permanent:
        replies.append(f"执行 P{code} 彻底")
    replies.append(f"取消 P{code}")
    reply_html = "".join(f'<span class="reply">{escape(text)}</span>' for text in replies)

    permanent_note = (
        '<p class="note">勾选「彻底删除」可真正释放缓存占用；不勾选则只送系统回收站（可恢复，但不腾空间）。</p>'
        if allow_permanent
        else ""
    )
    return _HTML_TEMPLATE.format(
        code=code,
        ttl_min=ttl_min,
        sections="".join(sections),
        total=totals["total"],
        cache_size=_human_size(totals["cache_bytes"]),
        move_count=totals["move_count"],
        replies=reply_html,
        permanent_note=permanent_note,
    )


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    width: 760px;
    padding: 26px 28px 30px;
    font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
    color: #2b2418;
    background: #fffdf5;
    background-image: radial-gradient(#f2e6bf 1px, transparent 1px);
    background-size: 18px 18px;
  }}
  header {{
    display: flex;
    align-items: center;
    gap: 12px;
    padding-bottom: 14px;
    border-bottom: 3px solid #1f1b16;
  }}
  .star {{ font-size: 30px; color: #e0a800; }}
  h1 {{ font-size: 24px; letter-spacing: 2px; }}
  h1 small {{ display: block; font-size: 12px; font-weight: 400; color: #8a7c5f; letter-spacing: 0; margin-top: 4px; }}
  .group {{ margin-top: 18px; }}
  h2 {{
    display: inline-block;
    font-size: 15px;
    padding: 3px 14px;
    border-radius: 999px;
    background: #1f1b16;
    color: #ffd84d;
    letter-spacing: 3px;
  }}
  .cards {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 10px; }}
  .card {{
    padding: 9px 12px;
    background: #ffffff;
    border: 1px solid #e6dcc0;
    border-left: 4px solid #ffd84d;
    border-radius: 6px;
  }}
  .row {{ display: flex; align-items: baseline; gap: 8px; }}
  .idx {{
    min-width: 20px;
    text-align: center;
    font-size: 12px;
    border-radius: 999px;
    background: #fff2c2;
    color: #a97c00;
  }}
  .name {{ flex: 1; font-size: 14px; font-weight: 700; color: #1f1b16; word-break: break-all; }}
  .size {{ font-size: 13px; color: #a97c00; white-space: nowrap; }}
  .desc {{ margin-top: 4px; font-size: 12px; color: #7d715a; }}
  .arrow {{ margin-left: 6px; color: #4d7c2f; }}
  .summary {{
    margin-top: 20px;
    padding: 12px 16px;
    background: #fff7d6;
    border: 1px dashed #e0c46a;
    border-radius: 8px;
    font-size: 14px;
    line-height: 1.8;
  }}
  .summary b {{ color: #b07200; }}
  .note {{ margin-top: 8px; font-size: 12px; color: #8a7c5f; }}
  .replies {{ margin-top: 16px; display: flex; flex-wrap: wrap; gap: 8px; }}
  .reply {{
    padding: 4px 12px;
    font-size: 13px;
    font-weight: 700;
    color: #1f1b16;
    background: #ffd84d;
    border-radius: 999px;
  }}
  footer {{ margin-top: 20px; padding-top: 10px; border-top: 1px dashed #ddd0ae; font-size: 12px; color: #9a8d70; }}
</style>
</head>
<body>
  <header>
    <span class="star">★</span>
    <h1>雾雨魔理沙 · 清理方案 P{code}<small>{ttl_min} 分钟内有效，过期需重新生成</small></h1>
  </header>
  {sections}
  <div class="summary">
    合计 <b>{total}</b> 项：缓存 <b>{cache_size}</b>，桌面整理 <b>{move_count}</b> 个文件（只移动，可 /撤销）。
    {permanent_note}
  </div>
  <div class="replies">{replies}</div>
  <footer>回复上面的口令即可执行；桌宠在线时也会弹出勾选确认框，两边共享同一份方案。</footer>
</body>
</html>
"""
