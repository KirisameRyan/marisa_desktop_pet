#!/usr/bin/env python
"""页面级分块嵌入：把全站正文切块后嵌成向量（真正的 RAG 语义层）。

为什么必须做（主人指出的核心问题）：
- 之前的向量只覆盖 8,391 条**实体**（角色/游戏/曲目/符卡），
  而「幻想乡」「符卡规则」「博丽大结界」「月面战争」「异变」这类**设定/背景**
  内容根本不是实体，从未被嵌入 → 语义检索对它们完全无效。
- 结果就是报告里绝大多数命中仍是「FTS 字面召回」，语义层几乎没贡献。

做法（标准 RAG）：
   页面正文 → 清洗 → 切块(500 字，overlap 100) → bge-m3 嵌入 → chunk_vectors

检索时返回的是**文本片段**（资料），而不是实体名——这才是模型需要的"依据"。

用法：
    python scripts/thwiki_embed_pages.py --limit-pages 2000     # 小样
    python scripts/thwiki_embed_pages.py                        # 全量（约 6-9 万页）
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import re
import sqlite3
import struct
import time
import tomllib
from pathlib import Path
from typing import Dict, List, Tuple

import httpx

MODEL = "Pro/BAAI/bge-m3"
BATCH = 32
CHUNK = 500
OVERLAP = 100
MIN_PAGE_CHARS = 200      # 短于此不值得嵌入

_REF_RE = re.compile(r"<ref[\s\S]*?</ref>|<ref[^>]*/>", re.I)
_TABLE_RE = re.compile(r"\{\|[\s\S]*?\|\}")
_LINK_RE = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]")
_FILE_RE = re.compile(r"\[\[(?:文件|File|Image):[^\]]*\]\]", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_REDIRECT_RE = re.compile(r"^\s*#(?:REDIRECT|重定向)", re.I)
_KANA_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff]")
KANA_LIMIT = 0.25   # 假名占比超过此值视为日文页


def kana_ratio(text: str) -> float:
    """假名占比——用来识别"未经翻译的日文页"。

    为什么必须过滤：THBWiki 收录大量日本同人作品的原文（附带文档、Omake 等），
    这些页面正文是纯日文。实测「琪露诺的能力是什么」的第 2 名就是
    「氷精武勇録～Record Of Ice Fairy War./Omake」的日文段落。
    中文问答机器人不该把这些当日文语料喂进向量库。
    """

    if not text:
        return 0.0
    return len(_KANA_RE.findall(text)) / len(text)


def strip_templates(text: str) -> str:
    """按括号深度剥模板。

    **关键：遇到未闭合的 `{{` 不能把后面全丢掉。**
    踩过的坑：THBWiki 页面里常见裸的 `{{`（注释、nowiki、占位符），
    首版 depth 一旦 >0 就永不归零，导致整页后半段被删——
    实测 74,429 个候选页**全部**被洗成 <200 字，嵌入任务因此处理了 0 页。
    """

    out: List[str] = []
    depth = 0
    open_at = -1
    i = 0
    while i < len(text):
        if text.startswith("{{", i):
            if depth == 0:
                open_at = i
            depth += 1
            i += 2
            continue
        if text.startswith("}}", i):
            if depth:
                depth -= 1
                if depth == 0:
                    open_at = -1
            i += 2
            continue
        if depth == 0:
            out.append(text[i])
        i += 1
    if depth > 0 and open_at >= 0:
        # 未闭合：把 `{{` 之后的原文保留下来，只去掉那两个花括号
        out.append(text[open_at + 2 :])
    return "".join(out)


def clean(wikitext: str) -> str:
    """把 wikitext 洗成可读正文。"""

    text = _REF_RE.sub(" ", wikitext)
    text = _TABLE_RE.sub(" ", text)
    text = strip_templates(text)
    text = _FILE_RE.sub(" ", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _TAG_RE.sub(" ", text)
    text = re.sub(r"^[=*#:;{|].*$", " ", text, flags=re.M)
    text = re.sub(r"'{2,}", "", text)
    text = re.sub(r"(?:缩略图|thumb|居中|\d+px)", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def split_sentences(text: str) -> List[str]:
    """按中文句末标点与换行分句。

    定长字符窗口会把句子拦腰切断——实测块首出现过「定的妖怪…」「想乡的结界分割…」
    这种半个词开头的片段，检索到的"资料"读起来是残的，也会拉低向量质量。
    """

    parts = re.split(r"(?<=[。！？!?；;])|(?<=[…]{2})|\n+", text)
    return [p.strip() for p in parts if p and p.strip()]


def _split_long_sentence(sentence: str, limit: int) -> List[str]:
    """单句超长时按逗号再切，避免出现远超上限的块。"""

    if len(sentence) <= limit:
        return [sentence]
    pieces = re.split(r"(?<=[，,、：:])", sentence)
    out: List[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) > limit:
            out.append(current)
            current = piece
        else:
            current += piece
    if current:
        out.append(current)
    return out


def chunk_text(text: str, max_chars: int = CHUNK, overlap_sentences: int = 1) -> List[str]:
    """句子感知切块：按句子装箱，**绝不切断句子**；重叠按"句"而非"字符"。

    重叠保留上一块的末尾若干整句，这样跨块检索不会丢上下文，
    也不会像字符重叠那样产生半截句子。
    """

    sentences: List[str] = []
    for sentence in split_sentences(text):
        sentences.extend(_split_long_sentence(sentence, max_chars))

    chunks: List[str] = []
    current: List[str] = []
    current_len = 0
    for sentence in sentences:
        if current and current_len + len(sentence) > max_chars:
            chunks.append("".join(current))
            # 用末尾若干整句做重叠
            current = current[-overlap_sentences:] if overlap_sentences else []
            current_len = sum(len(s) for s in current)
        current.append(sentence)
        current_len += len(sentence)
    if current:
        chunks.append("".join(current))
    return [c for c in chunks if len(c) >= 80]


def load_provider(config_path: Path) -> Tuple[str, str]:
    cfg = tomllib.loads(config_path.read_text(encoding="utf-8"))
    for provider in cfg.get("api_providers", []):
        if provider.get("name") == "SiliconFlow":
            return str(provider["base_url"]).rstrip("/"), str(provider["api_key"])
    raise RuntimeError("model_config.toml 里没有 SiliconFlow provider")


def main() -> int:
    parser = argparse.ArgumentParser(description="页面级分块嵌入")
    parser.add_argument("--data", type=Path, default=Path("data") / "thwiki")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=Path("config") / "model_config.toml")
    parser.add_argument("--limit-pages", type=int, default=None)
    parser.add_argument("--namespaces", nargs="+", type=int, default=[0, 506, 508, 512],
                        help="默认含设定所在的全部内容命名空间")
    args = parser.parse_args()

    db_path = args.db or (args.data / "thwiki.db")
    base, key = load_provider(args.config)
    print(f"嵌入服务：{base} · 模型 {MODEL}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS page_chunks (
          chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
          pageid   INTEGER, ns INTEGER, title TEXT, url TEXT, chunk_no INTEGER, text TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_page ON page_chunks(pageid);
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
          title, text, content='page_chunks', content_rowid='chunk_id', tokenize='trigram'
        );
        CREATE TABLE IF NOT EXISTS chunk_vectors (chunk_id INTEGER PRIMARY KEY, vec BLOB);
        """
    )

    # 断点续跑：已经嵌入过的页面跳过
    done_pages = {int(r["pageid"]) for r in conn.execute("SELECT DISTINCT pageid FROM page_chunks")}
    if done_pages:
        print(f"已有 {len(done_pages)} 页的块，跳过（断点续跑）")

    pending: List[Tuple[int, int, str, str, int, str]] = []
    pages = 0
    with httpx.Client(timeout=120.0) as client:
        for path in sorted(glob.glob(str(args.data / "pages-ns*.jsonl.gz"))):
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                for line in handle:
                    row = json.loads(line)
                    pageid = int(row.get("pageid") or 0)
                    ns = int(row.get("ns") or 0)
                    if ns not in args.namespaces or pageid in done_pages:
                        continue
                    wikitext = row.get("wikitext") or ""
                    if _REDIRECT_RE.match(wikitext) or len(wikitext) < MIN_PAGE_CHARS:
                        continue
                    title = str(row.get("title") or "")
                    if title.endswith(("/导航", "/Navbox")):
                        continue
                    title = str(row.get("title") or "")
                    text = clean(wikitext)
                    if len(text) < MIN_PAGE_CHARS:
                        continue
                    # 跳过以日文为主的页面（多为未翻译的日本同人作品原文）
                    if kana_ratio(text) > KANA_LIMIT:
                        continue
                    pages += 1
                    if args.limit_pages and pages > args.limit_pages:
                        break
                    url = "https://thwiki.cc/" + title.replace(" ", "_")
                    for index, piece in enumerate(chunk_text(text)):
                        # **按块**再过滤一次：整页假名占比达标不代表每块都达标，
                        # 中日混排页（如同人作品页）会带着日文段落一起过关。
                        if kana_ratio(piece) > KANA_LIMIT:
                            continue
                        pending.append((pageid, ns, title, url, index, piece))

                    # 攒够一批就嵌（边扫边嵌，避免把几十万块全放内存）
                    if len(pending) >= 512:
                        flush(client, conn, base, key, pending)
                        pending.clear()
                        print(f"  …已处理 {pages} 页，累计块 {conn.execute('SELECT COUNT(*) FROM page_chunks').fetchone()[0]}",
                              flush=True)
                if args.limit_pages and pages > args.limit_pages:
                    break
            if args.limit_pages and pages > args.limit_pages:
                break
        if pending:
            flush(client, conn, base, key, pending)

    chunks = conn.execute("SELECT COUNT(*) FROM page_chunks").fetchone()[0]
    vecs = conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0]
    print(f"完成：页面 {pages} · 块 {chunks} · 向量 {vecs}")
    conn.close()
    return 0


def embed_batch(client: httpx.Client, base: str, key: str, texts: List[str], retries: int = 5) -> List[List[float]]:
    """带重试退避的批量嵌入。

    踩过：全量跑到 1210 页时 SiliconFlow 返回 **502 Bad Gateway**，首版没有重试
    直接抛错，整个 40 分钟的任务崩掉。502/503/429/5xx 都属于可重试的瞬时故障。
    """

    last: Exception | None = None
    for attempt in range(retries):
        try:
            resp = client.post(
                f"{base}/embeddings",
                json={"model": MODEL, "input": texts},
                headers={"Authorization": f"Bearer {key}"},
            )
            if resp.status_code == 200:
                return [item["embedding"] for item in resp.json()["data"]]
            if resp.status_code in (408, 429, 500, 502, 503, 504):
                wait = min(30, 2**attempt)
                print(f"  [重试] HTTP {resp.status_code}，{wait}s 后再试（{attempt + 1}/{retries}）", flush=True)
                time.sleep(wait)
                last = RuntimeError(f"HTTP {resp.status_code}")
                continue
            raise RuntimeError(f"嵌入失败 HTTP {resp.status_code}: {resp.text[:200]}")
        except RuntimeError:
            raise
        except Exception as exc:   # noqa: BLE001 - 网络抖动要重试，最终失败要暴露
            last = exc
            time.sleep(min(30, 2**attempt))
    raise RuntimeError(f"嵌入连续失败：{last}")


def flush(client: httpx.Client, conn: sqlite3.Connection, base: str, key: str,
          items: List[Tuple[int, int, str, str, int, str]]) -> None:
    """把一批块写入并嵌入。"""

    conn.executemany(
        "INSERT INTO page_chunks(pageid, ns, title, url, chunk_no, text) VALUES (?,?,?,?,?,?)",
        items,
    )
    conn.commit()
    first_id = conn.execute("SELECT MAX(chunk_id) FROM page_chunks").fetchone()[0] - len(items) + 1
    texts = [item[5] for item in items]
    for start in range(0, len(texts), BATCH):
        chunk = texts[start : start + BATCH]
        vectors = embed_batch(client, base, key, chunk)
        for offset, vec in enumerate(vectors):
            conn.execute(
                "INSERT OR REPLACE INTO chunk_vectors(chunk_id, vec) VALUES (?,?)",
                (first_id + start + offset, struct.pack(f"<{len(vec)}f", *vec)),
            )
        conn.commit()


if __name__ == "__main__":
    raise SystemExit(main())
