#!/usr/bin/env python
"""THBWiki 检索层 + 100 题回归运行器。

检索流程（对应主人定的「SQL + RAG 组合」）：

    自然语言问题
      ↓ ① 别名归一化：名称/别名/重定向 → 命中实体（最长匹配，避免"八云紫"被"八云蓝"截断）
      ↓ ② 意图路由：问「种族/能力/住在哪/发售/符卡/曲」→ 决定查哪个字段、哪个 kind
      ↓ ③ 双路召回
           SQL 路：结构化字段精确命中（官方曲目表 / 符卡表 / 作品表 / 人物表）
           RAG 路：FTS5(trigram) 字面召回，bm25 排序
      ↓ ④ 加权打分（含**同人乘区**）
           final = relevance × official_mult × kind_mult × prominence_mult
      ↓ ⑤ Top-K 返回「资料 + 出处」，由 MaiBot 组织回答

同人乘区是三层：硬过滤（意图明确指向官方时不召回同人）、软乘区（同人 ×0.25）、
同人内部再按热度分级（待接 Mostlinked）。

用法：
    python scripts/touhou_retrieve.py --questions data/thwiki/regression_questions.json
    python scripts/touhou_retrieve.py --q "红魔乡三面道中曲是什么" --limit 3
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ─────────────────── 可调参数（回归测试就是用来调这些的）───────────────────
OFFICIAL_PENALTY = 0.25   # 同人内容的乘区（第 2 层）
KIND_MISMATCH = 0.6       # 意图类型不匹配时的乘区
FIELD_HIT = 1.0           # SQL 字段精确命中
FTS_TOP = 30              # FTS 召回的候选数

# 字段意图：问句里出现这些词 → 该查哪个字段
FIELD_INTENTS: List[Tuple[Tuple[str, ...], str]] = [
    (("什么种族", "种族", "什么妖怪", "是妖怪吗", "是吸血鬼吗", "是什么妖怪", "什么身份"), "species"),
    (("能力", "会什么"), "ability"),
    (("住在哪", "住在", "哪里住", "所在地", "居住"), "location"),
    (("职业", "职务", "工作"), "work_type"),
    (("哪一年", "什么时候出", "什么时候发布", "发售", "发行", "发布时间"), "release_date"),
]

# 类型意图：问句里出现这些词 → 该查哪个 kind
KIND_INTENTS: List[Tuple[Tuple[str, ...], Tuple[str, ...]]] = [
    (("符卡",), ("spellcard", "spellcard_2nd")),
    (("道中", "bgm", "BGM", "主题曲", "曲目", "曲子", "的曲", "音乐"), ("official_track", "track")),
    (("成员", "都有谁", "有哪些角色", "住着哪些", "哪些角色"), ("character",)),
]

LIST_WORDS = ("哪些", "列出", "所有", "都有", "一共有多少", "分别", "有谁", "全部")
# MATCH 查询里要去掉的虚词（trigram 下这些词只会带来噪声）
STOPWORDS = (
    "是什么", "什么的", "什么", "哪些", "哪个", "是谁", "分别", "指的是", "是谁的",
    "还有", "除了", "都有", "所有", "一共", "多少", "介绍", "一下", "这个", "那个",
    "角色", "东方", "作品", "里面", "里的", "住在", "哪里", "关系", "知道", "告诉",
    "以及", "与之", "相关", "关于", "概要", "剧情", "大约", "大概", "么样", "样的",
)


@dataclass
class Hit:
    """一条检索结果。"""

    kind: str
    name: str
    detail: Dict[str, Any] = field(default_factory=dict)
    source: str = ""
    url: str = ""
    score: float = 0.0
    why: str = ""
    official: int = 0


class TouhouRetriever:
    """THBWiki 检索器。"""

    def __init__(self, db_path: Path) -> None:
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self._names: List[Tuple[str, str, int]] = []   # (名称, kind, entity_id)，按长度降序
        self._aliases: Dict[str, str] = {}
        self._load_index()

    # ─────────────────── 索引 ───────────────────

    def _load_index(self) -> None:
        """把实体名称、**短名**与重定向别名装进内存（毫秒级匹配）。

        短名是必须的：库里作品全名是「东方红魔乡 ～ the Embodiment of Scarlet Devil.」，
        而用户只会说「红魔乡」。上一版只索引全名，导致「红魔乡三面道中曲」零结果。
        """

        rows = self.conn.execute(
            """SELECT entity_id, kind, name_cn, name_jp FROM entities
               WHERE kind IN ('character','game','spellcard','spellcard_2nd',
                              'official_track','album','person')"""
        ).fetchall()
        names: List[Tuple[str, str, int]] = []
        for row in rows:
            for key in (row["name_cn"], row["name_jp"]):
                text = (key or "").strip()
                if len(text) < 2:
                    continue
                names.append((text, row["kind"], row["entity_id"]))
                for short in self._short_forms(text):
                    names.append((short, row["kind"], row["entity_id"]))
        names.sort(key=lambda item: -len(item[0]))   # 长名优先，避免被短名截断
        self._names = names
        for row in self.conn.execute("SELECT alias, target_title FROM aliases"):
            self._aliases[str(row["alias"]).strip()] = str(row["target_title"]).strip()
        # 俗称表由 thwiki_enrich.py 写入；没跑过富化时要能照常工作
        self._nicknames: Dict[str, str] = {}
        try:
            for row in self.conn.execute("SELECT nick, canonical FROM nicknames"):
                self._nicknames[str(row["nick"])] = str(row["canonical"])
        except sqlite3.OperationalError:
            self._nicknames = {}

    @staticmethod
    def _short_forms(name: str) -> List[str]:
        """从一个规范名生成用户可能说的短名。

        - 「东方红魔乡 ～ the Embodiment…」→「东方红魔乡」「红魔乡」「the Embodiment…」
        - 「东方永夜抄 ～ Imperishable Night.」→「东方永夜抄」「永夜抄」
        注意不动人名（把「雾雨魔理沙」砍成「魔理沙」会误伤「魔理沙」以外的名字）。
        """

        forms: List[str] = []
        head = re.split(r"[～~（(「【]", name, maxsplit=1)[0].strip()
        if head and head != name and len(head) >= 2:
            forms.append(head)
        for base in (head or name,):
            for prefix in ("东方", "東方"):
                if base.startswith(prefix) and len(base) > len(prefix) + 1:
                    forms.append(base[len(prefix):])
        return [f for f in dict.fromkeys(forms) if len(f) >= 2]

    # ─────────────────── ① 别名归一化 ───────────────────

    def match_entities(self, question: str, cap: int = 6) -> List[Tuple[str, str, int]]:
        """在问题里找出提到的实体（最长匹配优先）。"""

        found: List[Tuple[str, str, int]] = []
        taken: List[Tuple[int, int]] = []   # 已占用的字符区间，防止同一段被重复匹配
        for name, kind, entity_id in self._names:
            start = question.find(name)
            if start < 0:
                continue
            end = start + len(name)
            if any(not (end <= s or start >= e) for s, e in taken):
                continue
            taken.append((start, end))
            found.append((name, kind, entity_id))
            if len(found) >= cap:
                break
        return found

    def normalize(self, question: str) -> str:
        """把问题里的**俗称/别名**替换成规范名。

        这是 E 组（⑨/紫妈/pad长/红白/黑白…）的唯一解法：这些梗称既不在重定向表里，
        也不是任何实体名。俗称表由 thwiki_enrich.py 写入，可继续追加。
        """

        text = question
        # 俗称优先（长俗称先行，避免"紫妈"被"紫"先替换掉）
        for nick, canonical in sorted(self._nicknames.items(), key=lambda kv: -len(kv[0])):
            if nick in text:
                text = text.replace(nick, canonical)
        for alias, target in self._aliases.items():
            if len(alias) >= 3 and alias in text:
                text = text.replace(alias, target)
        return text

    # ─────────────────── 关系网 / 标签检索 ───────────────────

    def _by_relation(self, question: str) -> List[Hit]:
        """关系路：「红魔馆都有哪些成员」→ relations 里 dst=红魔馆 的 src。

        两个必须守住的边界（都是踩过的坑）：
        1) **必须由关系词触发**。首版无条件触发，结果「⑨是谁」也被拉进关系查询，
           返回「大妖精 -所属-> 琪露诺」这种荒谬结果（"琪露诺"被当成了地点）。
        2) **目标不能是人物名**。关系表里的 dst 混进了大量人物（正文链接的噪声），
           只有非人物的目标才可能是场所/组织。
        """

        hits: List[Hit] = []
        rel_word = None
        for word, rel in (("成员", "所属"), ("式神", "所属"), ("弟子", "所属"),
                          ("住在", "所属"), ("归属", "所属"), ("登场", "登场")):
            if word in question:
                rel_word = rel
                break
        if rel_word is None:
            return hits   # 没有关系词就不走这条路
        char_names = {name for name, kind, _i in self._names if kind == "character"}
        targets = [
            r["dst_name"]
            for r in self.conn.execute("SELECT DISTINCT dst_name FROM relations")
            if len(r["dst_name"]) >= 2 and r["dst_name"] in question and r["dst_name"] not in char_names
        ]
        if not targets:
            return hits
        for target in targets[:3]:
            rows = self.conn.execute(
                """SELECT DISTINCT src_name FROM relations
                   WHERE dst_name=? AND rel IN ('所属','关联','登场') ORDER BY weight DESC LIMIT 12""",
                (target,),
            ).fetchall()
            for row in rows:
                name = row["src_name"]
                char = self.conn.execute(
                    """SELECT entity_id, kind, name_cn, source_title, url, official, species, work_type
                       FROM entities WHERE kind='character' AND name_cn=? LIMIT 1""",
                    (name,),
                ).fetchone()
                if char is None:
                    continue
                hits.append(
                    Hit(
                        kind="character", name=char["name_cn"],
                        detail={"种族": char["species"], "职业": char["work_type"]},
                        source=char["source_title"], url=char["url"],
                        score=self._final(0.92, char["official"], True),
                        why=f"关系：{name} -{rel_word or '所属'}-> {target}", official=char["official"],
                    )
                )
        return hits

    def _by_tag(self, question: str) -> List[Hit]:
        """标签路：「所有吸血鬼角色」「魔法使种族的角色」→ tags 表过滤。"""

        hits: List[Hit] = []
        for row in self.conn.execute(
            """SELECT t.tag_value, t.tag_type, e.entity_id, e.kind, e.name_cn, e.name_jp,
                      e.source_title, e.url, e.official, e.species, e.work_type
               FROM tags t JOIN entities e ON e.entity_id = t.entity_id
               WHERE t.tag_type IN ('种族','职业')
                 AND length(t.tag_value) >= 2
                 AND instr(?, t.tag_value) > 0
               LIMIT 12""",
            (question,),
        ):
            hits.append(
                Hit(
                    kind=row["kind"], name=row["name_cn"] or "",
                    detail={"匹配标签": f"{row['tag_type']}={row['tag_value']}"},
                    source=row["source_title"], url=row["url"],
                    score=self._final(0.9, row["official"], True),
                    why=f"标签命中 {row['tag_type']}={row['tag_value']}", official=row["official"],
                )
            )
        return hits

    # ─────────────────── ② 意图路由 ───────────────────

    @staticmethod
    def route_field(question: str) -> Optional[str]:
        for words, field_name in FIELD_INTENTS:
            if any(w in question for w in words):
                return field_name
        return None

    @staticmethod
    def route_kinds(question: str) -> Tuple[str, ...]:
        for words, kinds in KIND_INTENTS:
            if any(w in question for w in words):
                return kinds
        return ()

    @staticmethod
    def is_list(question: str) -> bool:
        return any(w in question for w in LIST_WORDS)

    # ─────────────────── ③④ 召回与打分 ───────────────────

    @staticmethod
    def _final(rel: float, official: int, kind_ok: bool) -> float:
        return rel * (1.0 if official else OFFICIAL_PENALTY) * (1.0 if kind_ok else KIND_MISMATCH)

    def _structured(self, question: str, entities: List[Tuple[str, str, int]]) -> List[Hit]:
        """SQL 路：拿实体 + 字段，直接给出精确答案。"""

        hits: List[Hit] = []
        field_name = self.route_field(question)
        wants_list = self.is_list(question)
        kinds = self.route_kinds(question)

        # (0) 实体卡：只要问题里明确提到某个实体，就先把"这个实体本身"给它。
        #     首版漏了这一步，导致「⑨是谁」「pad长是谁」这类问句虽然正确归一化到了
        #     琪露诺/十六夜咲夜，却只返回 FTS 噪声，答不出"是谁"。
        for _name, _kind, entity_id in entities[:2]:
            row = self.conn.execute("SELECT * FROM entities WHERE entity_id=?", (entity_id,)).fetchone()
            if row is None:
                continue
            hits.append(
                Hit(
                    kind=row["kind"],
                    name=row["name_cn"] or row["name_jp"] or "",
                    detail={
                        "日文名": row["name_jp"], "英文名": row["name_en"],
                        "种族": row["species"], "职业": row["work_type"],
                        "所在地": row["location"], "初登场": row["first_work"],
                        "发行": row["release_date"],
                    },
                    source=row["source_title"], url=row["url"],
                    score=self._final(0.85, row["official"], True),
                    why="实体卡（问题里直接提到的实体）", official=row["official"],
                )
            )

        # (a) 实体 + 字段 → 单点精确查询
        if entities and field_name:
            for _name, _kind, entity_id in entities[:3]:
                row = self.conn.execute(
                    f"SELECT * FROM entities WHERE entity_id=?", (entity_id,)
                ).fetchone()
                if row is None:
                    continue
                value = (row[field_name] or "").strip()
                hits.append(
                    Hit(
                        kind=row["kind"],
                        name=row["name_cn"] or row["name_jp"] or "",
                        detail={field_name: value} if value else {"未收录字段": field_name},
                        source=row["source_title"],
                        url=row["url"],
                        score=self._final(FIELD_HIT if value else 0.35, row["official"], True),
                        why=f"SQL字段命中 {field_name}" if value else f"实体命中但 {field_name} 为空（需渲染补全）",
                        official=row["official"],
                    )
                )

        # (b) 作品 + 曲目意图 → 列出该作品的官方曲目
        if kinds and "official_track" in kinds:
            for _name, _kind, entity_id in entities[:2]:
                row = self.conn.execute(
                    "SELECT name_cn, kind FROM entities WHERE entity_id=?", (entity_id,)
                ).fetchone()
                if row is None:
                    continue
                work = row["name_cn"]
                stage_kw = re.findall(r"(\d+|[一二三四五六七八九十]+)面", question)
                sql = ("SELECT name_cn,name_jp,stage FROM entities WHERE kind='official_track' "
                       "AND parent_title LIKE ?")
                args: List[Any] = [f"%{work}%"]
                if stage_kw:
                    sql += " AND stage LIKE ?"
                    args.append(f"%{stage_kw[0]}面%")
                sql += " ORDER BY entity_id LIMIT 12"
                for track in self.conn.execute(sql, args):
                    hits.append(
                        Hit(
                            kind="official_track",
                            name=track["name_cn"] or "",
                            detail={"stage": track["stage"], "日文名": track["name_jp"]},
                            source=f"{work}/Music",
                            url="https://thwiki.cc/" + str(work).replace(" ", "_") + "/Music",
                            score=self._final(0.95, 1, True),
                            why=f"该作品官方曲目（{track['stage']}）",
                            official=1,
                        )
                    )

        # (c) 列表意图 + 字段 → 按字段值筛人物
        if wants_list and field_name and not entities:
            value_kw = re.sub(r"[？?。，,、\s]|哪些|列出|所有|都有|一共有多少|分别|有谁|全部|角色|东方|的|是", "", question)
            if value_kw:
                for row in self.conn.execute(
                    f"SELECT * FROM entities WHERE kind='character' AND {field_name} LIKE ? LIMIT 12",
                    (f"%{value_kw}%",),
                ):
                    hits.append(
                        Hit(
                            kind="character",
                            name=row["name_cn"] or "",
                            detail={field_name: row[field_name]},
                            source=row["source_title"],
                            url=row["url"],
                            score=self._final(0.9, row["official"], True),
                            why=f"按 {field_name} 筛选",
                            official=row["official"],
                        )
                    )
        return hits

    def _fts(self, question: str, kinds: Tuple[str, ...], entities: List[Tuple[str, str, int]]) -> List[Hit]:
        """RAG 路：FTS5 字面召回，bm25 排序后按官方/类型加权。

        **trigram 索引的正确用法是滑窗 3 字**。首版把整句当一个短语去 MATCH，
        于是「红魔馆都有哪些成员」直接零结果——没有任何页面包含这九个字。
        实测 100 题里 54 题零结果，主因就在这。
        """

        # ① 先把疑问词/虚词当**子串**去掉（长词优先，避免"是什么"被"什么"先啃掉）
        cleaned = question
        for word in sorted(STOPWORDS, key=len, reverse=True):
            cleaned = cleaned.replace(word, " ")
        # ② 实体名是最可靠的检索词，优先放进去
        terms: List[str] = [name for name, _k, _i in entities if len(name) >= 3]
        # ③ 其余部分做滑窗 3 字（trigram 索引要求查询词至少 3 字，2 字查不到）
        for chunk in re.split(r"[^\w\u4e00-\u9fa5]+", cleaned):
            chunk = chunk.strip()
            if len(chunk) >= 3:
                terms.extend(chunk[i : i + 3] for i in range(len(chunk) - 2))
            elif len(chunk) == 3:
                terms.append(chunk)
        terms = list(dict.fromkeys(t for t in terms if len(t) >= 3))[:24]
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms)
        try:
            rows = self.conn.execute(
                """SELECT e.*, bm25(entities_fts) AS s FROM entities_fts
                   JOIN entities e ON e.entity_id = entities_fts.rowid
                   WHERE entities_fts MATCH ? ORDER BY s LIMIT ?""",
                (match, FTS_TOP),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        hits: List[Hit] = []
        for row in rows:
            rel = 1.0 / (1.0 + max(0.0, -float(row["s"]))) if row["s"] is not None else 0.2
            kind_ok = (not kinds) or (row["kind"] in kinds)
            hits.append(
                Hit(
                    kind=row["kind"],
                    name=row["name_cn"] or row["name_jp"] or "",
                    detail={"摘要": (row["summary"] or "")[:120], "所属": row["parent_title"] or ""},
                    source=row["source_title"],
                    url=row["url"],
                    score=self._final(rel, row["official"], kind_ok),
                    why="FTS 字面召回",
                    official=row["official"],
                )
            )
        return hits

    # ─────────────────── 对外接口 ───────────────────

    def search(self, question: str, limit: int = 3) -> List[Hit]:
        normalized = self.normalize(question)
        entities = self.match_entities(normalized)
        kinds = self.route_kinds(normalized)
        hits = (
            self._structured(normalized, entities)
            + self._by_relation(normalized)
            + self._by_tag(normalized)
            + self._fts(normalized, kinds, entities)
        )
        # 同一实体去重：保留分数最高的那条
        best: Dict[Tuple[str, str], Hit] = {}
        for hit in hits:
            key = (hit.kind, hit.name)
            if key not in best or hit.score > best[key].score:
                best[key] = hit
        ranked = sorted(best.values(), key=lambda h: -h.score)
        return ranked[:limit]


def main() -> int:
    parser = argparse.ArgumentParser(description="THBWiki 检索 / 回归运行器")
    parser.add_argument("--db", type=Path, default=Path("data") / "thwiki" / "thwiki.db")
    parser.add_argument("--questions", type=Path, default=None, help="题库 JSON")
    parser.add_argument("--q", type=str, default=None, help="单条查询")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--out", type=Path, default=None, help="回归报告输出路径")
    args = parser.parse_args()

    retriever = TouhouRetriever(args.db)

    if args.q:
        for i, hit in enumerate(retriever.search(args.q, args.limit), 1):
            print(f"  {i}. [{hit.kind}] {hit.name} | {hit.detail} | {hit.why} | 分 {hit.score:.3f}")
        return 0

    if not args.questions:
        parser.error("需要 --q 或 --questions")

    data = json.loads(args.questions.read_text(encoding="utf-8"))
    questions = data["questions"]
    lines: List[str] = [f"# THBWiki 回归测试（{len(questions)} 题）\n"]
    empty_top1 = 0
    for item in questions:
        hits = retriever.search(item["question"], args.limit)
        lines.append(f"\n## {item['id']}　{item['question']}\n")
        if not hits:
            empty_top1 += 1
            lines.append("- **（无任何结果）**\n")
            continue
        for i, hit in enumerate(hits, 1):
            detail = " · ".join(f"{k}={str(v)[:60]}" for k, v in hit.detail.items())
            lines.append(
                f"{i}. `[{hit.kind}]` **{hit.name}**  \n"
                f"   - {detail}  \n"
                f"   - 依据：{hit.why}｜出处：{hit.source}｜分数：{hit.score:.3f}\n"
            )
    report = args.out or (args.questions.parent / "REGRESSION.md")
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已写出：{report}")
    print(f"题目 {len(questions)} · 完全无结果 {empty_top1} 题")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
