#!/usr/bin/env python
"""THBWiki → SQLite 知识库构建器（v1：实体抽取 + 全文检索）。

设计要点（都是前几轮实测得出的结论）：

1. **原始快照不动**：输入是 data/thwiki/pages-*.jsonl.gz，输出是派生的 SQLite。
   清洗规则改了重跑几分钟即可，永远不用重抓。
2. **字段来自真实模板**：信息框模板的参数名就是字段清单（模板普查反推）。
   但**映射未必一次到位**，所以每行都保留 `extra_json`——原始参数全存，
   映射漏了也不丢数据，后续改进映射不用重跑抓取。
3. **行模板 vs 信息框**：`同人曲目信息` 在一张专辑页里被调用几十次（一行一曲），
   所以抽取单位是「模板调用」而不是「页面」；专辑名取自所在页面的标题。
4. **先分流再入库**：重定向页不建正文、只做 alias；空页/导航页丢弃。

用法：
    python scripts/thwiki_build_db.py                       # 构建 data/thwiki/thwiki.db
    python scripts/thwiki_build_db.py --show-fields         # 只打印各模板的真实字段名
    python scripts/thwiki_build_db.py --limit 3000          # 小样先跑通
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

_NAME_RE = re.compile(r"\{\{\s*([^|}\n\[\]<>{]+)")
_REDIRECT_RE = re.compile(r"^\s*#(?:REDIRECT|重定向)\s*[:：]?\s*\[\[([^\]|]+)", re.I)
_CALL_SCAN_LIMIT = 2500  # 单个模板调用的参数扫描上限（字符）

# 模板 → 实体类型。行模板（一页多次）与信息框（一页一次）都在这里统一登记。
TEMPLATES: Dict[str, str] = {
    "同人曲目信息": "track",
    "专辑曲目列表": "track",          # 另一种曲目行模板
    "同人专辑信息": "album",
    "官方游戏信息": "game",
    # 「简单游戏信息」是**同人游戏**（实测样例：空想幻闻录DEMO / Fly Marisa Fly!），
    # 归到 game 会让官方作品列表被同人淹没
    "简单游戏信息": "doujin_game",
    "同人志信息": "doujin",
    # 注意：真正的角色信息框是「官方角色信息」，**不是**「人物信息」——
    # 后者那 2061 条是同人作者/画师条目（实测样例：(YsY)s / 13.c.Gilbert）。
    "官方角色信息": "character",
    "人物信息": "person",            # 同人作者/画师，单独一类，别和角色混
    "符卡登场信息": "spellcard",
    "通用符卡信息": "spellcard",
    "符卡信息（二次）": "spellcard_2nd",
}

# 官方实体（角色/游戏/官方符卡）与同人实体分开标记：同人曲目有 22.9 万条，
# 不区分的话检索会被同人内容淹没（实测：搜"红魔乡"返回的全是同人曲）。
OFFICIAL_KINDS = {"game", "character", "spellcard"}

# schema 字段 → 可能的模板参数名（按优先级）。第一个命中的就用。
FIELD_MAP: Dict[str, List[str]] = {
    # 「人物名」属于 {{官方角色信息}}，其余属于专辑/曲目类信息框
    "name_cn": ["名称", "中文名", "译名", "曲名", "标题", "人物名"],
    "name_jp": ["日文名", "原名称", "日文曲名"],
    "name_romaji": ["罗马音", "日文读音"],
    "name_en": ["英文名", "英文曲名"],
    "circle": ["制作方", "社团", "所属社团"],
    "release_date": ["首发日期", "正式版发行日期", "信息公开日期", "发售日期", "日期"],
    "release_event": ["展会", "会场", "登场展会"],
    "catalog_no": ["编号", "碟号", "制品版编号"],
    "price": ["会场售价", "通贩售价", "售价"],
    "work_type": ["类型", "分类", "职业"],
    "duration": ["时长"],
    "composer": ["作曲"],
    "arranger": ["编曲", "再编曲"],
    "lyricist": ["作词"],
    "vocal": ["演唱", "合唱"],
    "performer": ["演奏"],
    "instrument": ["乐器"],
    "original_for": ["原曲", "非东方原曲"],
    "genre": ["曲风", "风格类型"],
    "characters": ["角色"],
    "owner": ["角色"],
    "work": ["作品", "登场作品"],
    "card_no": ["编号"],
    "stage": ["面数", "关卡"],
    "description": ["描述", "说明"],
    "species": ["种族"],
    "ability": ["能力"],
    "location": ["所在地", "居住地", "活动场所"],
    "first_work": ["初次登场", "登场作品"],
}


def iter_template_calls(text: str) -> Iterator[Tuple[str, Dict[str, str]]]:
    """产出 (模板名, 具名参数)。用正则定位模板起点（C 速度），只对调用体做有界字符扫描。"""

    for match in _NAME_RE.finditer(text):
        name = match.group(1).strip()
        if not name:
            continue
        i = match.end()
        stop = min(len(text), i + _CALL_SCAN_LIMIT)
        # depth 从 1 起（外层 {{ 已被 _NAME_RE 吃掉）。两个必须做对的细节：
        # 1) 嵌套模板的 {{ }} 要**写回 buf**，否则值里的模板标记丢失，
        #    后续清洗无从下手（实测产出「雾雨魔理沙能力|1|红魔乡color:#808080|…」）；
        # 2) }} 使 depth 归 0 时必须**停止**，否则循环会越过本模板边界去吞后面的文本
        #    （实测导致模板普查里字段计数虚高）。
        depth = 1
        params: Dict[str, str] = {}
        buf: List[str] = []
        while i < stop and depth > 0:
            if text.startswith("{{", i):
                depth += 1
                buf.append("{{")
                i += 2
                continue
            if text.startswith("}}", i):
                depth -= 1
                i += 2
                if depth == 0:
                    break
                buf.append("}}")
                continue
            ch = text[i]
            if ch == "|" and depth == 1:
                joined = "".join(buf)
                if "=" in joined:
                    key, value = joined.split("=", 1)
                    params.setdefault(key.strip(), value.strip())
                buf = []
                i += 1
                continue
            buf.append(ch)
            i += 1
        tail = "".join(buf)
        if "=" in tail:
            key, value = tail.split("=", 1)
            params.setdefault(key.strip(), value.strip())
        yield name, params


def pick(params: Dict[str, str], field_names: List[str]) -> str:
    """按候选参数名顺序取第一个非空值。"""

    for name in field_names:
        value = (params.get(name) or "").strip()
        if value:
            return clean_value(value)
    return ""


_LINK_RE = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]")
_TPL_RE = re.compile(r"\{\{[^{}]*\}\}")
_REF_RE = re.compile(r"<ref[\s\S]*?</ref>|<ref[^>]*/>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_templates(text: str) -> str:
    """按括号深度剥掉 {{...}}。

    正则 `\\{\\{[^{}]*\\}\\}` 只能剥最内层，遇到嵌套（能力字段里的
    `{{能力|1|红魔乡}}{{color:#808080|（红魔乡）}}`）会把残渣留下，
    实测产出过「雾雨魔理沙能力|1|红魔乡color:#808080|（红魔乡）」这种脏值。
    """

    out: List[str] = []
    depth = 0
    i = 0
    while i < len(text):
        if text.startswith("{{", i):
            depth += 1
            i += 2
            continue
        if text.startswith("}}", i):
            if depth:
                depth -= 1
            i += 2
            continue
        if depth == 0:
            out.append(text[i])
        i += 1
    return "".join(out)


def clean_value(value: str) -> str:
    """把字段值里的 wiki 标记清掉（值里常有 [[链接]]、<ref>、内嵌模板）。"""

    text = _REF_RE.sub("", value)
    text = _strip_templates(text)
    text = _LINK_RE.sub(r"\1", text)
    text = _TAG_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip().strip("，,")


def extract_summary(wikitext: str, limit: int = 400) -> str:
    """从页面里取"概述/简介"段落做摘要。

    v1 不抓渲染版（那是后台 1-2 小时的任务），先用 wikitext 里最干净的概述段。
    章节名不止「概述」：角色页用「角色信息」，作品页用「概述/简介/游戏介绍」，
    所以这里一并试。
    """

    text = _REF_RE.sub("", wikitext)
    match = re.search(
        r"==+\s*(?:概述|简介|介绍|游戏介绍|作品简介|角色信息|人物信息)\s*==+\s*([\s\S]{0,3000}?)(?=\n==|\Z)",
        text,
    )
    if not match:
        return ""
    body = match.group(1)
    body = re.sub(r"\{\|[\s\S]*?\|\}", " ", body)  # 去掉表格
    body = _strip_templates(body)
    # 去掉文件嵌入：不清的话摘要会变成「缩略图|右|中间角色即八云紫」这种图片说明
    body = re.sub(r"\[\[(?:文件|File|Image):[^\]]*\]\]", " ", body, flags=re.I)
    body = _LINK_RE.sub(r"\1", body)
    body = _TAG_RE.sub(" ", body)
    body = re.sub(r"^[=*#:;].*$", " ", body, flags=re.M)  # 去掉标题行/列表符
    body = re.sub(r"(?:缩略图|thumb|右|左|居中|\d+px)\s*\|?", " ", body, flags=re.I)
    body = re.sub(r"\s+", " ", body).strip()
    return body[:limit]


def extract_body(wikitext: str, limit: int = 1200) -> str:
    """抽取页面的**可读正文**，供 FTS 索引。

    为什么必须做：首版 FTS 只索引 name/summary，而 summary 大面积为空
    （章节名不匹配、角色页没有"概述"段），导致字面检索几乎无内容可搜——
    实测「红魔馆都有哪些成员」直接零结果。这里改为索引清洗后的页面正文。

    只给"页面级"实体存正文（见 BODY_KINDS）：行级实体（曲目/二次符卡/同人作者）
    会让同一张专辑页的正文被重复存十几遍，实测会把索引撑到数 GB。
    """

    text = _REF_RE.sub("", wikitext)
    text = re.sub(r"\{\|[\s\S]*?\|\}", " ", text)      # 表格
    text = _strip_templates(text)                       # 按深度剥模板
    text = _LINK_RE.sub(r"\1", text)                    # [[A|B]] → B
    text = re.sub(r"\[\[(?:文件|File|Image):[^\]]*\]\]", " ", text, flags=re.I)
    text = _TAG_RE.sub(" ", text)
    text = re.sub(r"^[=*#:;{|].*$", " ", text, flags=re.M)   # 标题/列表/表格行标记
    text = re.sub(r"'{2,}", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


# 只有这些 kind 存页面正文；行级实体（一页几十条）不存，否则正文重复十几遍
BODY_KINDS = {"character", "game", "album", "doujin", "spellcard", "official_track", "doujin_game"}


def build(data_dir: Path, db_path: Path, limit: Optional[int], show_fields: bool) -> int:
    """扫描语料并构建数据库。"""

    rows = 0
    counts: Counter = Counter()
    fields_seen: Dict[str, Counter] = defaultdict(Counter)
    pages_scanned = 0

    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists() and not show_fields:
        db_path.unlink()
    conn = sqlite3.connect(db_path) if not show_fields else None

    if conn is not None:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=OFF;

            CREATE TABLE entities (
              entity_id    INTEGER PRIMARY KEY,
              kind         TEXT NOT NULL,      -- track/album/game/doujin/person/character/spellcard/spellcard_2nd
              official     INTEGER NOT NULL DEFAULT 0,  -- 1=官方 0=同人（检索默认该偏向官方）
              name_cn      TEXT, name_jp TEXT, name_romaji TEXT, name_en TEXT,
              parent_title TEXT,               -- 所在页面标题（曲目→所属专辑）
              circle       TEXT, release_date TEXT, release_event TEXT,
              catalog_no   TEXT, price TEXT, work_type TEXT,
              duration     TEXT, composer TEXT, arranger TEXT, lyricist TEXT,
              vocal        TEXT, performer TEXT, instrument TEXT,
              original_for TEXT, genre TEXT, characters TEXT,
              owner        TEXT, work TEXT, card_no TEXT, stage TEXT, description TEXT,
              species      TEXT, ability TEXT, location TEXT, first_work TEXT,
              summary      TEXT,
              body         TEXT,               -- 清洗后的整页正文（FTS 的真正语料）
              extra_json   TEXT NOT NULL,      -- 原始参数全存：映射漏了也不丢数据
              source_pageid INTEGER NOT NULL,
              source_title  TEXT NOT NULL,
              url           TEXT NOT NULL
            );
            CREATE INDEX idx_entities_kind ON entities(kind);
            CREATE INDEX idx_entities_name ON entities(name_cn);
            CREATE INDEX idx_entities_parent ON entities(parent_title);
            -- 同一页会把同一个信息框调用多次（实测：雾雨魔理沙抽出两行），
            -- 用唯一约束 + INSERT OR IGNORE 去重
            CREATE UNIQUE INDEX idx_entities_unique
              ON entities(source_pageid, kind, name_cn);

            CREATE TABLE aliases (alias TEXT PRIMARY KEY, target_title TEXT NOT NULL);

            CREATE VIRTUAL TABLE entities_fts USING fts5(
              name_cn, name_jp, summary, parent_title, body,
              content='', tokenize='trigram'
            );
            """
        )

    files = sorted(glob.glob(str(data_dir / "pages-ns*.jsonl.gz")))
    started = time.time()
    for path in files:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                page = json.loads(line)
                pages_scanned += 1
                if limit is not None and pages_scanned > limit:
                    break
                if pages_scanned % 20000 == 0:
                    print(f"  …已扫 {pages_scanned} 页，抽出实体 {rows}", flush=True)

                wikitext = page.get("wikitext") or ""
                title = str(page.get("title") or "")
                pageid = int(page.get("pageid") or 0)
                ns = int(page.get("ns") or 0)
                url = "https://thwiki.cc/" + title.replace(" ", "_")

                # 重定向 → alias（不建正文）
                redirect = _REDIRECT_RE.match(wikitext)
                if redirect:
                    if conn is not None:
                        conn.execute(
                            "INSERT OR IGNORE INTO aliases(alias, target_title) VALUES (?, ?)",
                            (title.strip(), redirect.group(1).strip()),
                        )
                    counts["alias"] += 1
                    continue

                # 太短 / 导航页 → 不建实体
                if len(wikitext) < 50 or title.endswith(("/导航", "/Navbox")):
                    counts["skip"] += 1
                    continue

                summary = extract_summary(wikitext)
                body = extract_body(wikitext)
                for name, params in iter_template_calls(wikitext):
                    kind = TEMPLATES.get(name)
                    if kind is None:
                        continue
                    if show_fields:
                        for key in params:
                            fields_seen[name][key] += 1
                        counts[f"fields:{name}"] += 1
                        continue

                    extra = json.dumps(params, ensure_ascii=False)
                    values = {field: pick(params, names) for field, names in FIELD_MAP.items()}
                    if not values["name_cn"]:
                        values["name_cn"] = clean_value(title)
                    if conn is None:  # --show-fields 模式：只统计字段，不写库
                        counts[kind] += 1
                        rows += 1
                        continue
                    # 占位符由列数生成，避免手工数数出错（踩过：38 列配了 37 个 ?）
                    _cols = (
                        "kind, official, name_cn, name_jp, name_romaji, name_en, parent_title, "
                        "circle, release_date, release_event, catalog_no, price, work_type, "
                        "duration, composer, arranger, lyricist, vocal, performer, instrument, "
                        "original_for, genre, characters, owner, work, card_no, stage, description, "
                        "species, ability, location, first_work, summary, body, extra_json, "
                        "source_pageid, source_title, url"
                    )
                    _row = (
                        kind, 1 if kind in OFFICIAL_KINDS else 0,
                        values["name_cn"], values["name_jp"], values["name_romaji"],
                        values["name_en"], title,
                        values["circle"], values["release_date"], values["release_event"],
                        values["catalog_no"], values["price"], values["work_type"],
                        values["duration"], values["composer"], values["arranger"],
                        values["lyricist"], values["vocal"], values["performer"],
                        values["instrument"], values["original_for"], values["genre"],
                        values["characters"], values["owner"], values["work"],
                        values["card_no"], values["stage"], values["description"],
                        values["species"], values["ability"], values["location"],
                        values["first_work"], summary,
                        body if kind in BODY_KINDS else "",
                        extra, pageid, title, url,
                    )
                    conn.execute(
                        f"INSERT OR IGNORE INTO entities({_cols}) "
                        f"VALUES ({','.join(['?'] * len(_row))})",
                        _row,
                    )
                    if conn is not None:
                        conn.execute(
                            "INSERT INTO entities_fts(rowid, name_cn, name_jp, summary, parent_title, body) "
                            "VALUES (last_insert_rowid(), ?, ?, ?, ?, ?)",
                            (values["name_cn"], values["name_jp"], summary, title,
                             body if kind in BODY_KINDS else ""),
                        )
                    counts[kind] += 1
                    rows += 1

        if limit is not None and pages_scanned > limit:
            break

    if show_fields:
        print(f"\n扫描 {pages_scanned} 页，用时 {time.time() - started:.0f} 秒\n")
        for name, counter in sorted(fields_seen.items(), key=lambda kv: -sum(kv[1].values())):
            print(f"[{name}]（{sum(counter.values())} 次参数）")
            print("   " + " · ".join(f"{k}({v})" for k, v in counter.most_common(30)))
        return 0

    assert conn is not None
    conn.commit()
    entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    aliases = conn.execute("SELECT COUNT(*) FROM aliases").fetchone()[0]
    conn.close()
    size_mb = db_path.stat().st_size / 1024 / 1024
    print(f"\n完成：扫描 {pages_scanned} 页 · 实体 {entities} · 别名 {aliases} · "
          f"库 {size_mb:.1f} MB · 用时 {time.time() - started:.0f} 秒")
    by_kind = Counter()
    conn = sqlite3.connect(db_path)
    for kind, n in conn.execute("SELECT kind, COUNT(*) FROM entities GROUP BY kind ORDER BY 2 DESC"):
        by_kind[kind] = n
        print(f"  {kind}: {n}")
    conn.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="THBWiki → SQLite 知识库构建器")
    parser.add_argument("--data", type=Path, default=Path("data") / "thwiki")
    parser.add_argument("--db", type=Path, default=None, help="默认 <data>/thwiki.db")
    parser.add_argument("--limit", type=int, default=None, help="只扫前 N 页（小样验证）")
    parser.add_argument("--show-fields", action="store_true", help="只打印模板真实字段名，不建库")
    args = parser.parse_args()

    db_path = args.db or (args.data / "thwiki.db")
    return build(args.data, db_path, args.limit, args.show_fields)


if __name__ == "__main__":
    raise SystemExit(main())
