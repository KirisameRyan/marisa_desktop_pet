#!/usr/bin/env python
"""THBWiki 知识库富化：tags（标签）+ relations（关系网）+ nicknames（俗称）。

为什么需要这一层（实测依据）：
- THBWiki 的分类系统**不能当标签体系**：112,638 页里只有 10,363 页（9%）带分类，
  1,904 个分类里含"种族"的仅 20 条，主体是"符卡/同人画师/游戏对话"这类维护性分类。
  所以标签必须**派生**，不能照搬。
- 标签三层来源：
    ① 模板字段 → 种族/职业/能力/登场作品（权威、结构清晰）
    ② 正文链接 → 所属地点/组织、人物↔人物关联（关系网的边就在这里）
    ③ 分类     → 补充标签
- 有了 tags/relations，原来"答不对"的问题就变成确定性查询：
    「红魔馆都有哪些成员」→ relations(rel='成员', dst='红魔馆')
    「东方里有哪些吸血鬼」→ tags(tag_type='种族', tag_value='吸血鬼')

用法：
    python scripts/thwiki_enrich.py                 # 全量富化
    python scripts/thwiki_enrich.py --limit 5000    # 小样
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import re
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

_LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:\|[^\]]*)?\]\]")
_CAT_RE = re.compile(r"\[\[\s*(?:分类|Category)\s*:\s*([^\]|]+)")
_SPLIT_RE = re.compile(r"[，,、/／;；|·\s]+")

# 俗称表：圈内黑话 → 规范名。这是 E 组 12 题的关键，且**只能手工维护**——
# 别名重定向表里没有"⑨""紫妈"这类梗称。后续可继续追加。
NICKNAMES: Dict[str, str] = {
    "⑨": "琪露诺", "⑨号": "琪露诺", "笨蛋": "琪露诺", "冰之妖精": "琪露诺", "冰精": "琪露诺",
    "紫妈": "八云紫", "紫大妈": "八云紫", "隙间妖怪": "八云紫", "妖怪贤者": "八云紫",
    "永远的十七岁": "八云紫", "十七岁": "八云紫", "紫": "八云紫",
    "pad长": "十六夜咲夜", "pad": "十六夜咲夜", "女仆长": "十六夜咲夜",
    "红魔馆的女仆长": "十六夜咲夜", "咲夜": "十六夜咲夜",
    "红白": "博丽灵梦", "灵梦": "博丽灵梦", "无节操": "博丽灵梦",
    "黑白": "雾雨魔理沙", "魔理沙": "雾雨魔理沙",
    "中国": "红美铃", "美铃": "红美铃",
    "大小姐": "蕾米莉亚·斯卡蕾特", "蕾米": "蕾米莉亚·斯卡蕾特", "蕾咪": "蕾米莉亚·斯卡蕾特",
    "二小姐": "芙兰朵露·斯卡蕾特", "芙兰": "芙兰朵露·斯卡蕾特",
    "月之贤者": "八意永琳", "永琳": "八意永琳",
    "永远亭的公主": "蓬莱山辉夜", "辉夜": "蓬莱山辉夜", "neet": "蓬莱山辉夜",
    "EX露米娅": "露米娅", "露米娅": "露米娅",
    "幽幽子": "西行寺幽幽子", "uuz": "西行寺幽幽子",
    "妖梦": "魂魄妖梦",
    "蓝": "八云蓝", "橙": "橙",
    "早苗": "东风谷早苗", "青蛙子": "洩矢诹访子", "诹访子": "洩矢诹访子",
    "神奈子": "八坂神奈子", "天子": "比那名居天子",
    "小五": "古明地觉", "觉": "古明地觉", "恋恋": "古明地恋",
    "空": "灵乌路空", "阿空": "灵乌路空", "燐": "火焰猫燐", "阿燐": "火焰猫燐",
    "妹红": "藤原妹红", "慧音": "上白泽慧音",
    "文文": "射命丸文", "射命丸": "射命丸文",
    "萃香": "伊吹萃香", "西瓜": "伊吹萃香",
    "白莲": "圣白莲", "心": "秦心", "秦河胜": "秦心",
    "铃仙": "铃仙·优昙华院·因幡", "因幡帝": "因幡帝", "帝": "因幡帝",
    "帕秋莉": "帕秋莉·诺蕾姬", "姆Q": "帕秋莉·诺蕾姬", "帕琪": "帕秋莉·诺蕾姬",
    "爱丽丝": "爱丽丝·玛格特罗伊德", "小爱": "爱丽丝·玛格特罗伊德",
    "早鬼": "骊驹早鬼", "sakuya": "十六夜咲夜",
}


def _fetch_entities(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """取出参与关系/标签构建的实体：角色 + 作品（游戏/专辑/同人志/官方曲目）。"""

    out: Dict[str, Dict[str, Any]] = {}
    for row in conn.execute(
        """SELECT entity_id, kind, name_cn, name_jp, species, work_type, ability,
                  location, first_work, parent_title, source_pageid, source_title,
                  release_date, official
           FROM entities
           WHERE kind IN ('character','game','album','doujin','doujin_game','official_track','spellcard')"""
    ):
        out[row["source_title"] + "|" + str(row["entity_id"])] = dict(row)
    return out


def build_nicknames(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS nicknames (nick TEXT PRIMARY KEY, canonical TEXT NOT NULL)")
    conn.execute("DELETE FROM nicknames")
    conn.executemany(
        "INSERT OR REPLACE INTO nicknames(nick, canonical) VALUES (?,?)", list(NICKNAMES.items())
    )
    conn.commit()
    return len(NICKNAMES)


def build_tags_and_relations(conn: sqlite3.Connection, data_dir: Path, limit: int | None) -> Tuple[int, int]:
    """扫描页面，派生标签与关系。"""

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tags (
          entity_kind TEXT, entity_id INTEGER,
          tag_type    TEXT, tag_value TEXT, source TEXT, confidence REAL DEFAULT 1.0
        );
        CREATE INDEX IF NOT EXISTS idx_tags ON tags(tag_type, tag_value);
        CREATE INDEX IF NOT EXISTS idx_tags_entity ON tags(entity_kind, entity_id);

        CREATE TABLE IF NOT EXISTS relations (
          src_kind TEXT, src_name TEXT, rel TEXT, dst_kind TEXT, dst_name TEXT,
          weight REAL DEFAULT 1.0, source TEXT,
          PRIMARY KEY (src_kind, src_name, rel, dst_kind, dst_name)
        );
        CREATE INDEX IF NOT EXISTS idx_rel_src ON relations(src_name, rel);
        CREATE INDEX IF NOT EXISTS idx_rel_dst ON relations(dst_name, rel);
        """
    )
    conn.execute("DELETE FROM tags")
    conn.execute("DELETE FROM relations")

    # 角色/作品名字集合，用于从链接里识别"提到的是谁"
    chars: Dict[str, str] = {}   # 名称 → 规范名
    works: Set[str] = set()
    for row in conn.execute("SELECT name_cn, name_jp FROM entities WHERE kind='character'"):
        for key in (row["name_cn"], row["name_jp"]):
            if key and len(key) >= 2:
                chars[str(key).strip()] = str(row["name_cn"] or key).strip()
    for row in conn.execute("SELECT name_cn FROM entities WHERE kind IN ('game','album','doujin','doujin_game')"):
        if row["name_cn"]:
            works.add(str(row["name_cn"]).strip())
    long_names = sorted(chars.items(), key=lambda kv: -len(kv[0]))

    tag_rows: List[Tuple[Any, ...]] = []
    rel_counter: Counter = Counter()
    pages = 0
    started = time.time()

    for path in sorted(glob.glob(str(data_dir / "pages-ns*.jsonl.gz"))):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                page = json.loads(line)
                pages += 1
                if limit is not None and pages > limit:
                    break
                if pages % 20000 == 0:
                    print(f"  …已扫 {pages} 页，标签 {len(tag_rows)}，关系 {len(rel_counter)}", flush=True)

                wikitext = page.get("wikitext") or ""
                title = str(page.get("title") or "")
                pageid = int(page.get("pageid") or 0)

                # ① 分类 → 标签（补充来源）
                for cat in set(_CAT_RE.findall(wikitext)):
                    cat = cat.strip()
                    if cat and not cat.startswith("{{"):
                        tag_rows.append(("page", pageid, "分类", cat, "category", 1.0))

                # ② 正文链接 → 关系边（关系网就从这里长出来）
                links = {m.group(1).strip() for m in _LINK_RE.finditer(wikitext)}

                # 人物页：链接到其它人物 = 关联；链接到地点/组织 = 所属
                cur_char = None
                for name, canonical in long_names:
                    if title == canonical or title.startswith(canonical + "/"):
                        cur_char = canonical
                        break
                if cur_char:
                    for other, other_canonical in long_names:
                        if other_canonical == cur_char or other_canonical not in links:
                            continue
                        rel_counter[("character", cur_char, "关联", "character", other_canonical, "link")] += 1
                    for link in links:
                        if link in works:
                            rel_counter[("character", cur_char, "登场", "work", link, "link")] += 1
                        elif 2 <= len(link) <= 12 and not link.startswith(("文件", "File", "模板", "分类")):
                            # 非人物非作品的链接 → 视为"所属地点/组织"候选（红魔馆、永远亭、守矢神社…）
                            rel_counter[("character", cur_char, "所属", "place", link, "link")] += 1

        if limit is not None and pages > limit:
            break

    # 写库：过滤掉只出现一次的弱链接（噪声），保留有共现强度的边
    for (src_kind, src_name, rel, dst_kind, dst_name, source), weight in rel_counter.items():
        if rel == "关联" and weight < 2:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO relations(src_kind,src_name,rel,dst_kind,dst_name,weight,source) "
            "VALUES (?,?,?,?,?,?,?)",
            (src_kind, src_name, rel, dst_kind, dst_name, float(weight), source),
        )
    conn.executemany(
        "INSERT INTO tags(entity_kind,entity_id,tag_type,tag_value,source,confidence) VALUES (?,?,?,?,?,?)",
        tag_rows,
    )

    # ③ 模板字段 → 权威标签（种族/职业/能力/登场作品）
    field_tags = 0
    for row in conn.execute(
        """SELECT entity_id, kind, name_cn, species, work_type, ability, first_work, official
           FROM entities WHERE kind='character'"""
    ):
        for tag_type, raw in (("种族", row["species"]), ("职业", row["work_type"]), ("能力", row["ability"])):
            for value in _SPLIT_RE.split(str(raw or "")):
                value = value.strip()
                if 1 < len(value) <= 20 and "|" not in value and "{{" not in value:
                    conn.execute(
                        "INSERT INTO tags(entity_kind,entity_id,tag_type,tag_value,source,confidence) "
                        "VALUES ('character',?,?,?, 'template', 1.0)",
                        (row["entity_id"], tag_type, value),
                    )
                    field_tags += 1
        if row["first_work"]:
            conn.execute(
                "INSERT INTO tags(entity_kind,entity_id,tag_type,tag_value,source,confidence) "
                "VALUES ('character',?,?,?, 'template', 0.9)",
                (row["entity_id"], "初登场", str(row["first_work"])[:40]),
            )
            field_tags += 1

    conn.commit()
    tags_total = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
    rels_total = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    print(f"完成：扫描 {pages} 页 · 标签 {tags_total}（其中模板派生 {field_tags}）"
          f"· 关系 {rels_total} · 用时 {time.time() - started:.0f} 秒")
    return tags_total, rels_total


def main() -> int:
    parser = argparse.ArgumentParser(description="THBWiki 知识库富化：tags / relations / nicknames")
    parser.add_argument("--data", type=Path, default=Path("data") / "thwiki")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    db_path = args.db or (args.data / "thwiki.db")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row   # 下面大量用列名取值，不设会变成元组下标
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")

    nicks = build_nicknames(conn)
    print(f"俗称表：{nicks} 条")
    build_tags_and_relations(conn, args.data, args.limit)

    print("\n=== 标签分布（按类型） ===")
    for tag_type, n in conn.execute(
        "SELECT tag_type, COUNT(*) FROM tags GROUP BY tag_type ORDER BY 2 DESC LIMIT 10"
    ):
        print(f"  {tag_type}: {n}")
    print("\n=== 关系分布（按类型） ===")
    for rel, n in conn.execute(
        "SELECT rel, COUNT(*) FROM relations GROUP BY rel ORDER BY 2 DESC LIMIT 10"
    ):
        print(f"  {rel}: {n}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
