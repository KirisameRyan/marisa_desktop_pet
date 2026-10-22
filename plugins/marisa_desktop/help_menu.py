# -*- coding: utf-8 -*-
"""魔理沙指令菜单：从插件自身的组件声明生成文本与图片两种形态。

数据来源是 ``collect_components`` 收集到的 @Command 声明，因此菜单内容与
真正注册进宿主的命令始终一致，不会出现「帮助里写了但命令并不存在」的漂移。
"""
from __future__ import annotations

from html import escape
from typing import Any, Dict, List, Tuple

from maibot_sdk.components import collect_components

# 分类展示顺序；未列入的命令统一进入「其他」，保证新增命令不会漏出菜单
CATEGORY_ORDER: List[str] = ["总览", "音乐", "桌面", "查询", "账号", "其他"]

# 命令名 -> 分类（仅影响展示分组，不影响注册与鉴权）
COMMAND_CATEGORIES: Dict[str, str] = {
    "marisa_help": "总览",
    "marisa_music_play": "音乐",
    "marisa_music_recommend": "音乐",
    "marisa_music_play_toggle": "音乐",
    "marisa_music_pause": "音乐",
    "marisa_music_next": "音乐",
    "marisa_music_prev": "音乐",
    "marisa_music_volume": "音乐",
    "marisa_playlist_like": "音乐",
    "marisa_lock": "桌面",
    "marisa_open_app": "桌面",
    "marisa_clipboard": "桌面",
    "marisa_weather": "查询",
    "marisa_favor": "查询",
    "marisa_balance": "查询",
    "marisa_uptime": "查询",
    "d_ncm_login": "账号",
}

# 由门控插件（marisa_gate）提供的 QQ 指令，走的是钩子而非 @Command，
# 因此只能在这里静态列出，仅用于提示主人还有这些可用指令。
GATE_COMMANDS: List[Tuple[str, str]] = [
    ("/截图", "截取电脑屏幕并发回群里"),
    ("/体检", "电脑体检：磁盘、回收站、缓存、大文件、重复文件（只读扫描）"),
    ("/项目", "审查代码仓库占用与 Git 状态（只读扫描）"),
    ("/清理", "生成清理方案（临时缓存 + 桌面整理），确认后才执行"),
    ("/撤销", "撤销上一次清理：移动的还原、回收站的尽力找回"),
    ("/语音 <文本>", "让魔理沙用语音念一句（所有人可用，30 秒冷却）"),
    ("/识图", "先发指令再发图片：魔理沙告诉你图里是谁（所有人可用）"),
    ("/终端 <命令>", "在电脑上执行命令（需确认）"),
    ("/关闭电脑", "定时关机（说了就关，可取消）"),
    ("/取消关机", "取消关机计划"),
    ("/dsh <任务>", "交给 DSH（DeepSeek Harness）在电脑上干活，结果发回群里"),
]


def collect_command_entries(plugin: Any) -> List[Dict[str, str]]:
    """从插件实例的组件声明里收集命令条目。

    Args:
        plugin: 插件实例。

    Returns:
        List[Dict[str, str]]: 每项包含 name / usage / title / note / permission / category。
    """

    entries: List[Dict[str, str]] = []
    for component in collect_components(plugin):
        if str(component.get("type", "")).upper() != "COMMAND":
            continue
        metadata = component.get("metadata") or {}
        name = str(component.get("name", "") or "")
        if not name:
            continue
        title, raw_usage = _split_description(str(metadata.get("description", "") or ""))
        usage, note = _split_usage_note(raw_usage)
        entries.append(
            {
                "name": name,
                "title": title,
                "usage": usage or f"/{name}",
                "note": note,
                "permission": str(metadata.get("permission", "public") or "public"),
                "category": COMMAND_CATEGORIES.get(name, "其他"),
            }
        )
    entries.sort(key=lambda item: (CATEGORY_ORDER.index(item["category"]), item["name"]))
    return entries


def _split_description(description: str) -> Tuple[str, str]:
    """把「说明：/用法」形式的组件描述拆成 (说明, 用法)。

    作者在 @Command 里约定用全角冒号分隔说明与用法，这里做兼容拆解；
    没有冒号时整段作为说明，用法留空由调用方补默认值。
    """

    for separator in ("：", ":"):
        if separator in description:
            title, usage = description.split(separator, 1)
            return title.strip(), usage.strip()
    return description.strip(), ""


def _split_usage_note(raw_usage: str) -> Tuple[str, str]:
    """把「/点歌 <歌名>（主人电脑播放）」拆成 (用法, 备注)。

    尾部括号里的内容作为备注展示；「仅管理员」「所有人可用」这类权限说明
    由徽章统一表达，不再重复写进用法里。
    """

    usage = raw_usage.strip()
    notes: List[str] = []
    while usage.endswith(("）", ")")):
        opener = "（" if usage.endswith("）") else "("
        start = usage.rfind(opener, 0, len(usage) - 1)
        if start < 0:
            break
        inner = usage[start + 1 : len(usage) - 1].strip()
        usage = usage[:start].strip()
        # 权限说明交给徽章表达，其余括号内容作为备注保留
        if any(word in inner for word in ("管理员", "所有人", "权限")):
            continue
        if inner:
            notes.append(inner)
    note = "，".join(reversed(notes))
    return (usage or raw_usage.strip(), note)


def render_text(entries: List[Dict[str, str]]) -> str:
    """渲染桌面气泡用的纯文本指令大全。"""

    lines: List[str] = ["魔理沙 /指令大全だぜ："]
    current_category = ""
    for entry in entries:
        if entry["category"] != current_category:
            current_category = entry["category"]
            lines.append(f"【{current_category}】")
        suffix = "（仅主人）" if entry["permission"] == "operator" else ""
        detail = entry["note"] or entry["title"]
        lines.append(f"{entry['usage']}{suffix}" + (f"　{detail}" if detail else ""))
    lines.append("【QQ 终端】")
    lines.extend(f"{usage}　{title}" for usage, title in GATE_COMMANDS)
    return "\n".join(lines)


def render_html(entries: List[Dict[str, str]]) -> str:
    """渲染指令菜单 HTML（供浏览器渲染成图片）。"""

    sections: List[str] = []
    current_category = ""
    cards: List[str] = []

    def flush() -> None:
        if not cards:
            return
        sections.append(
            f'<section class="group"><h2>{escape(current_category)}</h2>'
            f'<div class="cards">{"".join(cards)}</div></section>'
        )
        cards.clear()

    for entry in entries:
        if entry["category"] != current_category:
            flush()
            current_category = entry["category"]
        badge = '<span class="badge">主人</span>' if entry["permission"] == "operator" else ""
        detail = entry["note"] or entry["title"]
        cards.append(
            '<div class="card">'
            f'<div class="cmd">{escape(entry["usage"])}{badge}</div>'
            f'<div class="desc">{escape(detail)}</div>'
            "</div>"
        )
    flush()

    gate_cards = "".join(
        f'<div class="card"><div class="cmd">{escape(usage)}</div>'
        f'<div class="desc">{escape(title)}</div></div>'
        for usage, title in GATE_COMMANDS
    )
    sections.append(
        f'<section class="group"><h2>QQ 终端</h2><div class="cards">{gate_cards}</div></section>'
    )

    return _HTML_TEMPLATE.format(sections="".join(sections))


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
  h1 {{ font-size: 25px; letter-spacing: 2px; }}
  h1 small {{ display: block; font-size: 12px; font-weight: 400; color: #8a7c5f; letter-spacing: 0; margin-top: 4px; }}
  .group {{ margin-top: 20px; }}
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
  .cmd {{ font-size: 15px; font-weight: 700; color: #1f1b16; }}
  .desc {{ margin-top: 3px; font-size: 12px; color: #7d715a; }}
  .badge {{
    margin-left: 6px;
    padding: 1px 7px;
    font-size: 11px;
    font-weight: 400;
    border-radius: 999px;
    background: #fff2c2;
    color: #a97c00;
    vertical-align: middle;
  }}
  .note {{ margin-top: 6px; font-size: 12px; color: #8a7c5f; }}
  footer {{ margin-top: 22px; padding-top: 10px; border-top: 1px dashed #ddd0ae; font-size: 12px; color: #9a8d70; }}
</style>
</head>
<body>
  <header>
    <span class="star">★</span>
    <h1>雾雨魔理沙 · 指令大全<small>QQ 用 /指令，桌面直接输入即可，だぜ！</small></h1>
  </header>
  {sections}
  <footer>标有「主人」的指令仅主人可用；群聊里请先 @魔理沙 再发指令。</footer>
</body>
</html>
"""
