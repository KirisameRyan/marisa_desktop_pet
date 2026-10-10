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
import math
import re
import sqlite3
import struct
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

# ─────────────────── 可调参数（回归测试就是用来调这些的）───────────────────
OFFICIAL_PENALTY = 0.25   # 同人内容的乘区（第 2 层）
KIND_MISMATCH = 0.6       # 意图类型不匹配时的乘区
FIELD_HIT = 1.0           # SQL 字段精确命中
FTS_TOP = 30              # FTS 召回的候选数
VEC_TOP = 30              # 向量召回的候选数
RRF_K = 60                # RRF 融合常数：score = Σ 1/(RRF_K + rank)
EMBED_MODEL = "Pro/BAAI/bge-m3"

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

    def __init__(self, db_path: Path, config_path: Path | None = None) -> None:
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self._names: List[Tuple[str, str, int]] = []   # (名称, kind, entity_id)，按长度降序
        self._aliases: Dict[str, str] = {}
        self._vectors: Dict[int, Any] = {}             # entity_id → 向量
        self._embed_api: Optional[Tuple[str, str]] = None
        self._chunk_mat_path = db_path.parent / "chunk_vectors.npy"
        self._load_index()
        self._load_vectors(config_path or (Path("config") / "model_config.toml"))

    # ─────────────────── 向量层 ───────────────────

    def _load_vectors(self, config_path: Path) -> None:
        """载入 bge-m3 向量与嵌入服务的连接信息。

        向量由 thwiki_embed.py 生成（结构化摘要 → bge-m3），查询时把问题也嵌入，
        算余弦相似度。这一步解决的是"字面不同但意思相同"：
        实测「八云紫的式神是谁」vs「紫妈的式神」余弦 0.647，而 FTS 字面完全对不上。
        """

        try:
            import numpy as np
        except ImportError as exc:   # 没装 numpy 就退化为无向量模式，但要显式说明
            print(f"  [警告] numpy 不可用，向量路已禁用：{exc}")
            return
        try:
            cfg = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            return
        for provider in cfg.get("api_providers", []):
            if provider.get("name") == "SiliconFlow" and provider.get("api_key"):
                self._embed_api = (
                    str(provider["base_url"]).rstrip("/"),
                    str(provider["api_key"]),
                )
                break
        ids: List[int] = []
        mats: List[Any] = []
        try:
            for row in self.conn.execute("SELECT entity_id, vec FROM vectors"):
                ids.append(int(row["entity_id"]))
                mats.append(np.frombuffer(row["vec"], dtype="<f4"))
        except sqlite3.OperationalError:
            return   # 还没跑嵌入脚本
        if not ids:
            return
        self._vec_ids = ids
        matrix = np.vstack(mats)
        # 归一化后余弦相似度 = 点积，检索时快得多
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        self._vec_mat = matrix / np.where(norms == 0, 1.0, norms)
        self._np = np
        # 页面分块向量（真正的 RAG 语料）：用 memmap 读，避免 700MB 常驻内存
        chunk_mat = getattr(self, "_chunk_mat_path", None)
        if chunk_mat and chunk_mat.exists():
            id_path = chunk_mat.with_name("chunk_ids.npy")
            if id_path.exists():
                self._chunk_mat = np.load(chunk_mat, mmap_mode="r")
                self._chunk_ids = np.load(id_path)

    def _chunk_vector(self, question: str, topk: int = 6) -> List[Hit]:
        """页面分块语义检索：返回**文本片段**（资料本身），而不是实体名。

        这是"设定/背景"类问题的唯一解：「幻想乡」「符卡规则」「博丽大结界」
        「月面战争」这些内容不是实体，实体向量层根本覆盖不到，
        所以之前报告里绝大多数命中仍是 FTS 字面召回。
        """

        if getattr(self, "_chunk_mat", None) is None:
            return []
        vec = self._embed(question)
        if vec is None:
            return []
        norm = self._np.linalg.norm(vec) or 1.0
        sims = self._chunk_mat @ (vec / norm)      # 向量已在导出时归一化 → 点积即余弦
        count = min(topk, sims.shape[0])
        top = self._np.argpartition(-sims, count - 1)[:count]
        top = top[self._np.argsort(-sims[top])]
        hits: List[Hit] = []
        for index in top:
            score = float(sims[index])
            if score < 0.35:      # 语义太弱不要，避免塞无关段落
                continue
            chunk_id = int(self._chunk_ids[int(index)])
            row = self.conn.execute(
                "SELECT title, url, text, chunk_no FROM page_chunks WHERE chunk_id=?", (chunk_id,)
            ).fetchone()
            if row is None:
                continue
            is_doujin = self._is_doujin_page(row["title"])
            hits.append(
                Hit(
                    kind="passage", name=row["title"],
                    detail={"片段（第%s块）" % row["chunk_no"]: self._clip(row["text"], 400)},
                    source=row["title"], url=row["url"],
                    score=score,
                    why="页面分块语义检索（RAG 语料·同人）" if is_doujin else "页面分块语义检索（RAG 语料）",
                    official=0 if is_doujin else 1,
                )
            )
        return hits

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        """在**句末**截断，而不是硬截字符。

        踩过：显示层用 text[:220] 硬截，导致块看起来"没按句子切"——
        其实是块本身完整，只是展示被砍在半句上。
        """

        if len(text) <= limit:
            return text
        window = text[:limit]
        cut = max(window.rfind(ch) for ch in "。！？…；;")
        return window[: cut + 1] if cut >= limit // 2 else window

    def _embed(self, text: str) -> Optional[Any]:
        if not self._embed_api or not getattr(self, "_vec_mat", None) is not None:
            return None
        base, key = self._embed_api
        try:
            resp = httpx.post(
                f"{base}/embeddings",
                json={"model": EMBED_MODEL, "input": [text]},
                headers={"Authorization": f"Bearer {key}"},
                timeout=60.0,
            )
            if resp.status_code != 200:
                return None
            return self._np.asarray(resp.json()["data"][0]["embedding"], dtype="float32")
        except Exception:
            return None

    def _vector(self, question: str) -> List[Hit]:
        """向量路：语义相似召回。"""

        vec = self._embed(question)
        if vec is None:
            return []
        norm = self._np.linalg.norm(vec) or 1.0
        sims = self._vec_mat @ (vec / norm)
        top = self._np.argsort(-sims)[:VEC_TOP]
        hits: List[Hit] = []
        for index in top:
            score = float(sims[index])
            if score < 0.30:      # 语义太弱的不要，避免拉进无关内容
                continue
            entity_id = self._vec_ids[int(index)]
            row = self.conn.execute(
                "SELECT entity_id,kind,name_cn,name_jp,parent_title,source_title,url,official "
                "FROM entities WHERE entity_id=?", (entity_id,)
            ).fetchone()
            if row is None:
                continue
            hits.append(
                Hit(
                    kind=row["kind"], name=row["name_cn"] or row["name_jp"] or "",
                    detail={"语义相似度": round(score, 3), "所属": row["parent_title"] or ""},
                    source=row["source_title"], url=row["url"],
                    score=score, why="向量语义召回", official=row["official"],
                )
            )
        return hits

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
        # 同人作品的页面标题集合。段落路径必须据此决定 official——
        # 之前硬编码 official=1，导致同人内容在语义检索里完全不受乘区压制，
        # 而"同人必须做好有力区分"是主人一开始就定下的要求。
        self._doujin_titles: set[str] = set()
        for row in self.conn.execute(
            """SELECT DISTINCT source_title FROM entities
               WHERE kind IN ('album','doujin','doujin_game','track','spellcard_2nd')"""
        ):
            if row["source_title"]:
                self._doujin_titles.add(str(row["source_title"]))

    def _is_doujin_page(self, title: str) -> bool:
        """页面是否属于同人作品（含其子页面，如 `<作品>/Omake`）。"""

        parts = str(title).split("/")
        for depth in range(len(parts), 0, -1):
            if "/".join(parts[:depth]) in self._doujin_titles:
                return True
        return False

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
            # 关键守卫：俗称若是规范名的子串、且规范名已在句中，替换会自我污染。
            # 实测俗称表里的单字条目（紫/蓝/橙/心/觉…）会把「八云紫住在哪里」
            # 改成「八云八云紫住在哪里」，实体匹配随之失效。
            if nick in canonical and canonical in text:
                continue
            if nick in text:
                text = text.replace(nick, canonical)
        for alias, target in self._aliases.items():
            if len(alias) >= 3 and alias in text:
                text = text.replace(alias, target)
        return text

    # ─────────────────── 关系网 / 标签检索 ───────────────────

    # 字段 → 正文检索关键词。字段为空时用它在实体自己的页面里找答案。
    FIELD_KEYWORDS: Dict[str, Tuple[str, ...]] = {
        "location": ("居住", "住在", "住所", "住处", "所在地", "居于", "老家", "回到"),
        "ability": ("能力",),
        "species": ("种族", "妖怪", "人类", "妖精", "吸血鬼", "月人", "神明"),
        "work_type": ("职业", "职务", "身份", "担任"),
    }

    def _field_from_page(self, entities: List[Tuple[str, str, int]], field_name: Optional[str]) -> List[Hit]:
        """字段为空时，到该实体**自己的页面正文**里找答案。

        为什么不能只靠加正则：模板里有些字段根本没有参数（角色的「所在地」），
        有些句式千变万化——实测八云紫页面写的是「在幻想乡的住所中睡觉」，
        而模式表里的「居住在/住在/所在地」全都匹配不上。
        固定模式列表永远追不上自然语言，所以改成"字段关键词 × 该页正文句"的通用检索。

        踩过：之前只回一条「未收录字段 location（需渲染补全）」，等于告诉用户"我不知道"，
        而答案其实就写在她自己的页面里。
        """

        keywords = self.FIELD_KEYWORDS.get(field_name or "")
        if not keywords:
            return []
        hits: List[Hit] = []
        for _name, _kind, entity_id in entities[:2]:
            row = self.conn.execute(
                "SELECT source_title, official FROM entities WHERE entity_id=?", (entity_id,)
            ).fetchone()
            if row is None or not row["source_title"]:
                continue
            title = str(row["source_title"])
            try:
                chunks = self.conn.execute(
                    "SELECT text FROM page_chunks WHERE title=? OR title LIKE ? LIMIT 40",
                    (title, title + "/%"),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
            seen: set[str] = set()
            for chunk in chunks:
                for sentence in re.split(r"(?<=[。！？])", str(chunk["text"])):
                    sentence = sentence.strip()
                    if not (6 <= len(sentence) <= 200) or sentence in seen:
                        continue
                    if not any(word in sentence for word in keywords):
                        continue
                    seen.add(sentence)
                    hits.append(
                        Hit(
                            kind="fact", name=title,
                            detail={field_name or "": sentence[:170]},
                            source=title,
                            url="https://thwiki.cc/" + title.replace(" ", "_"),
                            score=self._final(0.88, row["official"], True),
                            why=f"字段为空 → 在该实体页面正文检索「{field_name}」",
                            official=row["official"],
                        )
                    )
                    break   # 每个实体先给最相关的一句，避免刷屏
        return hits

    def _char_hit(self, name: str, why: str, base: float = 0.92) -> Optional[Hit]:
        """把一个人物名变成一条结果（关系路共用）。"""

        row = self.conn.execute(
            """SELECT entity_id, kind, name_cn, source_title, url, official, species, work_type
               FROM entities WHERE kind='character' AND name_cn=? LIMIT 1""",
            (name,),
        ).fetchone()
        if row is None:
            return None
        return Hit(
            kind="character", name=row["name_cn"],
            detail={"种族": row["species"], "职业": row["work_type"]},
            source=row["source_title"], url=row["url"],
            score=self._final(base, row["official"], True), why=why, official=row["official"],
        )

    def _by_relation(self, question: str) -> List[Hit]:
        """关系路，两个方向都要走（都是踩坑踩出来的）：

        ① **人物为主语**：「八云紫的式神是谁」→ relations 里 src=八云紫 的语义边。
           首版把"目标不能是人物"当铁律，结果这一整类问题被堵死。
        ② **场所为主语**：「红魔馆都有哪些成员」→ relations 里 dst=红魔馆 的边。
           这个方向的目标**必须**是非人物（否则"琪露诺"会被当成地点）。
        """

        hits: List[Hit] = []
        char_names = {name for name, kind, _i in self._names if kind == "character"}
        mentioned: List[str] = []
        for name, _kind, _id in self._names:      # 已按长度降序
            if len(name) >= 2 and name in question and name not in mentioned:
                mentioned.append(name)
        if not mentioned:
            return hits

        rel_words = ("式神", "弟子", "妹妹", "姐姐", "哥哥", "弟弟", "主人", "从者",
                     "侍奉", "师父", "师傅", "上司", "下属", "学生", "关系", "亲友", "挚友")
        has_rel_intent = any(w in question for w in rel_words)

        # ① 人物 → 人物的语义关系（由正文抽取的 source='text' 边）
        if has_rel_intent:
            # 特例：问题里同时出现两个人且问"关系" → 直接查这两人之间的边。
            # 之前只查"第一个提到的人的所有关系"，于是「芙兰朵露和蕾米莉亚是什么关系」
            # 返回了第三个人（十六夜咲夜）——因为共现边的排名里她更高。
            chars_in_q = [n for n in mentioned if n in char_names]
            if len(chars_in_q) >= 2 and "关系" in question:
                a, b = chars_in_q[0], chars_in_q[1]
                edge = self.conn.execute(
                    """SELECT src_name, rel, dst_name, weight FROM relations
                       WHERE (src_name=? AND dst_name=?) OR (src_name=? AND dst_name=?)
                       -- 有类型化的边（妹妹/式神/弟子…）就优先于泛化的"关联"
                       ORDER BY CASE rel WHEN '关联' THEN 1 ELSE 0 END, weight DESC LIMIT 1""",
                    (a, b, b, a),
                ).fetchone()
                label = edge["rel"] if edge else "关联"
                pair = [a, b] if not edge else [edge["src_name"], edge["dst_name"]]
                for name in pair:
                    hit = self._char_hit(name, f"两人关系：{a} ↔ {b}（{label}）", base=0.95)
                    if hit:
                        hits.append(hit)
            for name in chars_in_q[:2]:
                rows = self.conn.execute(
                    """SELECT dst_name, rel, weight FROM relations
                       WHERE src_name=? AND dst_name IN
                             (SELECT name_cn FROM entities WHERE kind='character')
                       ORDER BY weight DESC LIMIT 6""",
                    (name,),
                ).fetchall()
                for row in rows:
                    hit = self._char_hit(row["dst_name"], f"关系：{name} -{row['rel']}-> {row['dst_name']}")
                    if hit:
                        hits.append(hit)
                # 反向：别人页面里写到"某人是我的式神"时，本体在 dst 侧
                rows = self.conn.execute(
                    """SELECT src_name, rel, weight FROM relations
                       WHERE dst_name=? AND src_name IN
                             (SELECT name_cn FROM entities WHERE kind='character')
                       ORDER BY weight DESC LIMIT 6""",
                    (name,),
                ).fetchall()
                for row in rows:
                    hit = self._char_hit(row["src_name"], f"关系：{row['src_name']} -{row['rel']}-> {name}")
                    if hit:
                        hits.append(hit)

        # ② 场所/组织 → 成员
        if any(w in question for w in ("成员", "住在", "归属", "住着", "哪些角色", "都有谁")):
            for target in [n for n in mentioned if n not in char_names][:3]:
                rows = self.conn.execute(
                    """SELECT DISTINCT src_name FROM relations
                       WHERE dst_name=? ORDER BY weight DESC LIMIT 12""",
                    (target,),
                ).fetchall()
                for row in rows:
                    if row["src_name"] in char_names:
                        hit = self._char_hit(row["src_name"], f"关系：{row['src_name']} -所属-> {target}")
                        if hit:
                            hits.append(hit)
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

        # (0) 实体卡：**只在"问的就是这个实体本身"时给**。
        #     判断依据：问题里没有字段意图（种族/能力…）也没有关系意图（式神/成员/弟子…）。
        #     踩过两次同一个坑：无条件给实体卡时，「八云紫的式神是谁」会返回八云紫自己；
        #     因为此时八云紫是**主语**，答案另有其人。
        has_field_intent = field_name is not None
        has_rel_intent = any(w in question for w in ("成员", "式神", "弟子", "住在", "归属", "关系", "侍奉"))
        has_kind_intent = bool(kinds)   # 问的是曲/符卡/成员 → 答案不是"这个实体本身"
        if not has_field_intent and not has_rel_intent and not has_kind_intent:
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
                        why="实体卡（问题问的就是这个实体）", official=row["official"],
                    )
                )

        # (a) 实体 + 字段 → 单点精确查询；字段为空则转该实体页面正文检索
        if entities and field_name:
            missing = False
            for _name, _kind, entity_id in entities[:3]:
                row = self.conn.execute(
                    f"SELECT * FROM entities WHERE entity_id=?", (entity_id,)
                ).fetchone()
                if row is None:
                    continue
                value = (row[field_name] or "").strip()
                if not value:
                    missing = True
                    continue
                hits.append(
                    Hit(
                        kind=row["kind"],
                        name=row["name_cn"] or row["name_jp"] or "",
                        detail={field_name: value},
                        source=row["source_title"],
                        url=row["url"],
                        score=self._final(FIELD_HIT, row["official"], True),
                        why=f"SQL字段命中 {field_name}",
                        official=row["official"],
                    )
                )
            if missing and not hits:
                # 字段在模板里没有参数（如角色的所在地）→ 去页面正文里找，而不是回一句"我不知道"
                hits.extend(self._field_from_page(entities, field_name))

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

        # 六个通道各自召回，再按 **加权 RRF（排名倒数融合）** 合并。
        # 为什么不能直接加分数：bm25 是负值、余弦是 0~1、字段命中是常数 1.0——
        # 量纲完全不可比，相加会让某个通道淹没其它通道。RRF 只看排名，天然公平。
        # 为什么还要**加权**：等权时"精确答案"（SQL 字段/关系/标签）只有单通道 1/61，
        # 而噪声内容常同时出现在 FTS+向量两路 → 2/61 反而压过精确答案。
        # 实测「八云紫住在哪里」正确答句被两条符卡挤出前二，就是这个问题。
        paths = [
            (3.0, self._structured(normalized, entities)),   # SQL 字段 / 正文事实：最精确
            (2.5, self._by_relation(normalized)),            # 关系网
            (2.0, self._by_tag(normalized)),                 # 标签过滤
            (1.2, self._chunk_vector(normalized)),           # RAG 语料段落
            (1.0, self._fts(normalized, kinds, entities)),   # 字面召回：噪声最多，权重最低
            (1.0, self._vector(normalized)),                 # 实体向量
        ]
        agg: Dict[Tuple[str, str], List[Any]] = {}
        for weight, hits in paths:
            for rank, hit in enumerate(sorted(hits, key=lambda h: -h.score), 1):
                key = (hit.kind, hit.name)
                slot = agg.setdefault(key, [hit, 0.0])
                slot[1] += weight / (RRF_K + rank)

        ranked: List[Hit] = []
        for hit, rrf in agg.values():
            kind_ok = (not kinds) or (hit.kind in kinds)
            hit.score = rrf * (1.0 if hit.official else OFFICIAL_PENALTY) * (1.0 if kind_ok else KIND_MISMATCH)
            hit.why = f"{hit.why}｜RRF {rrf:.4f}"
            ranked.append(hit)
        ranked.sort(key=lambda h: -h.score)
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
