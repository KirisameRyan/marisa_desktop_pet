#!/usr/bin/env python
"""把资料接成关系链，并补齐角色库。

主人要求四件事：
    ① 字段都填一填（location 覆盖率只有 50/179）
    ② 设定资料也加上关联
    ③ 符卡 ↔ 作品、符卡 ↔ 人物 的关系链条
    ④ 设定内容也加入到角色数据库

数据基础（实测）：
    spellcard.owner  1079/1549  使用者（人物名）
    spellcard.work   1399/1549  登场作品（作品名）
    角色档案 character_sections 里已有 一设资料/东方XX/设定文档 · 符卡 等章节

产出：
    ① location  ← 从「生活状况/角色介绍」章节抽居住信息
    ② relations ← 人物 -符卡-> 符卡（双向各一条）· 符卡 -登场作品-> 作品
    ③ character_sections ← 符卡列表（把该角色的符卡名与出处集中一条）

用法：
    python scripts/thwiki_link_spellcards.py --dry-run
    python scripts/thwiki_link_spellcards.py
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Tuple

# 居住信息的句式（章节正文里比信息框自由得多）
LOCATION_PATTERNS = (
    r"大部分时间都在([^。，；\n]{2,24}?)的住所",
    r"居住在([^。，；\n]{2,24})",
    r"住在([^。，；\n]{2,24})",
    r"位于([^。，；\n]{2,24}?)的(?:住所|住处|家|宅)",
    r"以([^。，；\n]{2,24})为(?:据点|住处|居所)",
    r"所在地[：:]\s*([^。，；\n]{2,24})",
)


def pick_location(sections: List[Tuple[str, str]]) -> str:
    """从角色档案里挑一个居住地。优先「生活状况」，其次「角色介绍」。"""

    order = {"生活状况": 0, "角色介绍": 1}
    ranked = sorted(sections, key=lambda item: next(
        (v for k, v in order.items() if k in item[0]), 9))
    for _section, text in ranked:
        for pattern in LOCATION_PATTERNS:
            match = re.search(pattern, text)
            if match:
                value = match.group(1).strip(" 　的")
                if 1 < len(value) <= 24:
                    return value
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="建关系链并补齐角色库")
    parser.add_argument("--db", type=Path, default=Path("data") / "thwiki" / "thwiki.db")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row

    characters: Dict[str, sqlite3.Row] = {}
    for row in conn.execute("SELECT entity_id, name_cn FROM entities WHERE kind='character'"):
        characters[str(row["name_cn"])] = row
    # 作品名 → game 实体（页名常带副标题，用前缀匹配）
    games: Dict[str, sqlite3.Row] = {}
    for row in conn.execute("SELECT entity_id, name_cn FROM entities WHERE kind='game'"):
        games[str(row["name_cn"])] = row

    def resolve_game(work: str) -> sqlite3.Row | None:
        work = str(work).strip()
        if not work:
            return None
        for name, row in games.items():
            if name == work or name.startswith(work):
                return row
        return None

    # ── ① location：从角色档案章节抽 ──
    located = 0
    for name, row in characters.items():
        sections = [
            (str(r["section"]), str(r["text"]))
            for r in conn.execute(
                "SELECT section, text FROM character_sections WHERE entity_id=?", (row["entity_id"],)
            )
        ]
        if not sections:
            continue
        value = pick_location(sections)
        if value:
            located += 1
            if not args.dry_run:
                conn.execute(
                    "UPDATE entities SET location=? WHERE entity_id=? AND (location IS NULL OR location='')",
                    (value, row["entity_id"]),
                )
    print(f"① location：从角色档案抽到 {located} 个")

    # ── ② 关系链：人物 ↔ 符卡、符卡 ↔ 作品 ──
    edges: List[Tuple[str, str, str, str, str, float, str]] = []
    spell_by_owner: Dict[str, List[Tuple[str, str, str]]] = {}
    for row in conn.execute(
        """SELECT name_cn, owner, work, card_no, kind FROM entities
           WHERE kind IN ('spellcard','spellcard_2nd')"""
    ):
        card = str(row["name_cn"] or "")
        owner = str(row["owner"] or "").strip()
        work = str(row["work"] or "").strip()
        kind = str(row["kind"])
        if not card:
            continue
        if owner and owner in characters:
            edges.append(("character", owner, "符卡", kind, card, 1.0, "spellcard"))
            edges.append((kind, card, "使用者", "character", owner, 1.0, "spellcard"))
            spell_by_owner.setdefault(owner, []).append((card, work, str(row["card_no"] or "")))
        game = resolve_game(work) if work else None
        if game is not None:
            edges.append((kind, card, "登场作品", "game", str(game["name_cn"]), 1.0, "spellcard"))
    print(f"② 关系链：待写入 {len(edges)} 条（人物↔符卡、符卡↔作品）")
    if not args.dry_run:
        conn.executemany(
            "INSERT OR REPLACE INTO relations(src_kind,src_name,rel,dst_kind,dst_name,weight,source) "
            "VALUES (?,?,?,?,?,?,?)",
            edges,
        )

    # ── ③ 符卡列表 + 设定总览 进角色库 ──
    listed = 0
    for name, cards in spell_by_owner.items():
        row = characters[name]
        cards_sorted = sorted(cards, key=lambda c: (c[1], c[2]))
        lines = [f"{card}（{work or '出处不明'}）" for card, work, _no in cards_sorted[:60]]
        text = f"{name}共有 {len(cards)} 张符卡：\n" + "、".join(lines)
        listed += 1
        if not args.dry_run:
            conn.execute(
                "INSERT OR REPLACE INTO character_sections(entity_id, section, text) VALUES (?,?,?)",
                (int(row["entity_id"]), "符卡列表", text[:4000]),
            )
    # 设定总览：把各作品的设定文档合并成一条，便于一次性检索
    overview = 0
    for name, row in characters.items():
        docs = [
            (str(r["section"]), str(r["text"]))
            for r in conn.execute(
                "SELECT section, text FROM character_sections WHERE entity_id=? AND section LIKE '%设定文档%'",
                (row["entity_id"],),
            )
        ]
        if not docs:
            continue
        overview += 1
        if not args.dry_run:
            merged = "\n".join(f"【{s.split('/')[-2] if '/' in s else s}】{t}" for s, t in docs)
            conn.execute(
                "INSERT OR REPLACE INTO character_sections(entity_id, section, text) VALUES (?,?,?)",
                (int(row["entity_id"]), "设定总览", merged[:6000]),
            )

    if not args.dry_run:
        conn.commit()
    print(f"③ 角色库：符卡列表 {listed} 个角色 · 设定总览 {overview} 个角色")

    # ── 汇总 ──
    print("\n=== 结果 ===")
    for column in ("location", "species", "ability", "first_work"):
        n = conn.execute(
            f"SELECT COUNT(*) FROM entities WHERE kind='character' AND {column} IS NOT NULL AND {column}<>''"
        ).fetchone()[0]
        print(f"   {column:12s} {n:3d} / 179")
    total_rel = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    spell_rel = conn.execute("SELECT COUNT(*) FROM relations WHERE source='spellcard'").fetchone()[0]
    print(f"   relations 总数 {total_rel}（其中符卡相关 {spell_rel}）")
    print(f"   character_sections 总数 {conn.execute('SELECT COUNT(*) FROM character_sections').fetchone()[0]}")

    print("\n=== 八云紫复查 ===")
    row = characters.get("八云紫")
    if row:
        info = conn.execute(
            "SELECT location, ability, first_work FROM entities WHERE entity_id=?", (row["entity_id"],)
        ).fetchone()
        print(f"   所在地={info['location']!r} 初登场={info['first_work']!r}")
        cards = conn.execute(
            "SELECT text FROM character_sections WHERE entity_id=? AND section='符卡列表'",
            (row["entity_id"],),
        ).fetchone()
        print("   " + (cards["text"][:200].replace("\n", " ") if cards else "（无符卡列表）"))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
