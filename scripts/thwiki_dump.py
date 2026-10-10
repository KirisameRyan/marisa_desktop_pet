#!/usr/bin/env python
"""THBWiki 全量正文备份（走 MediaWiki API，不爬 HTML）。

用途：给 MarisaBOT 的东方知识库提供语料底座——一份**离线、可重建**的全站正文备份。

为什么走 API 而不是爬页面：
- thwiki.cc 是 MediaWiki 1.39（`writeapi` 为空 = 只读），`api.php` 能按 `allpages`
  分页拿到**原始 wikitext**，比解析 HTML 干净、稳定、可增量，也不给人家服务器添堵；
- HTML 里混着导航/页脚/脚本，清洗成本比取数本身还高。

礼貌与合规：
- 站点版权为 **CC BY-NC-SA 3.0**（署名 + 非商业 + 相同方式共享）→ 自用没问题，
  对外分发语料/衍生库必须遵守；因此本脚本把产物写到 `data/`（已被 .gitignore 忽略），
  **语料不入库**，只入库这个脚本。
- 默认每次请求间隔 0.7 秒并带重试退避；可通过 `--sleep` 调更慢，别调更快。

用法：
    python scripts/thwiki_dump.py                      # 全量（约 2100 次请求，25-40 分钟）
    python scripts/thwiki_dump.py --limit 200          # 小样验证
    python scripts/thwiki_dump.py --resume             # 断点续跑
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import httpx

API = "https://thwiki.cc/api.php"
USER_AGENT = "MarisaBOT-thwiki-archiver/1.0 (personal offline knowledge base; MediaWiki API client)"
DEFAULT_OUT = Path("data") / "thwiki"
# 一次取 50 页：allpages 上限能到 500，但带上正文后响应体很大（条目上限 4MB），50 更稳
BATCH = 50


def _log(msg: str) -> None:
    """带时间戳打到 stderr（进度看得见，也不污染将来的管道用法）。"""

    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def fetch_batch(
    client: httpx.Client,
    gapcontinue: Optional[str],
    rvcontinue: Optional[str],
    batch: int,
    retries: int = 4,
) -> Dict[str, Any]:
    """取一批页面（含最新版本的原始 wikitext）。"""

    params: Dict[str, Any] = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "generator": "allpages",
        "gapnamespace": "0",
        "gaplimit": str(batch),
        # 只要最新一版的正文；rvslots=main 是 MW1.32+ 的写法
        "prop": "revisions",
        "rvprop": "content|timestamp|ids|size",
        "rvslots": "main",
        # 注意两点（都是实测踩出来的）：
        # 1) generator=allpages 时**不能**带 redirects=1（API 报 params），要过滤用 gapfilterredir；
        # 2) 同理**不能**显式带 rvlimit —— 多页供给时该参数只允许用于单页，
        #    不写时默认就是每页 1 版（正是我们要的最新版）。
    }
    if gapcontinue:
        params["gapcontinue"] = gapcontinue
    if rvcontinue:
        params["rvcontinue"] = rvcontinue

    last_error: Optional[Exception] = None
    for attempt in range(retries):
        try:
            resp = client.get(API, params=params)
            resp.raise_for_status()
            payload = resp.json()
            # MediaWiki 的 HTTP 200 也可能是错误体（参数不合法、被限流等）。
            # 必须显式暴露——踩过：这里不检查，脚本会「成功」抓 0 页静默收工。
            if isinstance(payload, dict) and payload.get("error"):
                detail = json.dumps(payload["error"], ensure_ascii=False)[:300]
                raise RuntimeError(f"API 报错：{detail}")
            return payload
        except Exception as exc:  # noqa: BLE001 - 网络问题就要重试，最终失败要暴露
            last_error = exc
            wait = 2.0**attempt
            _log(f"请求失败（第 {attempt + 1}/{retries} 次）：{exc}；{wait:.0f}s 后重试")
            time.sleep(wait)
    raise RuntimeError(f"批次请求连续失败：{last_error}")


def iter_pages(client: httpx.Client, batch: int, sleep_sec: float, max_pages: Optional[int]) -> Iterator[Dict[str, Any]]:
    """按 allpages 顺序产出页面记录，直到取完或达到上限。"""

    gapcontinue: Optional[str] = None
    rvcontinue: Optional[str] = None
    emitted = 0

    while True:
        data = fetch_batch(client, gapcontinue, rvcontinue, batch)
        query = data.get("query") or {}
        pages: List[Dict[str, Any]] = query.get("pages") or []

        for page in pages:
            revisions = page.get("revisions") or []
            revision = revisions[0] if revisions else {}
            slots = revision.get("slots") or {}
            main = slots.get("main") or {}
            content = main.get("content")
            if content is None:
                # 某些页没有可取的正文（极少），照样记下标题，别静默丢
                content = ""
            yield {
                "pageid": page.get("pageid"),
                "ns": page.get("ns"),
                "title": page.get("title"),
                "revid": revision.get("revid"),
                "timestamp": revision.get("timestamp"),
                "size": revision.get("size"),
                "wikitext": content,
            }
            emitted += 1
            if max_pages is not None and emitted >= max_pages:
                return

        cont = data.get("continue") or {}
        gapcontinue = cont.get("gapcontinue")
        rvcontinue = cont.get("rvcontinue")
        if not cont:
            return
        time.sleep(sleep_sec)


def main() -> int:
    parser = argparse.ArgumentParser(description="THBWiki 全量正文备份（MediaWiki API）")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出目录（默认 data/thwiki）")
    parser.add_argument("--batch", type=int, default=BATCH, help="每批页数（默认 50）")
    parser.add_argument("--sleep", type=float, default=0.7, help="每次请求间隔秒数（默认 0.7，别调更快）")
    parser.add_argument("--limit", type=int, default=None, help="最多抓多少页（小样验证用）")
    parser.add_argument("--resume", action="store_true", help="追加到已有文件（断点续跑）")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    pages_path = args.out / "pages.jsonl.gz"
    state_path = args.out / "state.json"

    if pages_path.exists() and not args.resume:
        _log(f"输出已存在：{pages_path}；要追加请加 --resume，否则先移走它")
        return 2

    mode = "ab" if args.resume else "wb"
    started = time.time()
    count = 0
    bytes_written = 0

    _log(f"开始抓取 → {pages_path}（batch={args.batch} sleep={args.sleep}s limit={args.limit or '全部'}）")
    with httpx.Client(timeout=60.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
        with gzip.open(pages_path, mode) as handle:
            for page in iter_pages(client, args.batch, args.sleep, args.limit):
                line = json.dumps(page, ensure_ascii=False) + "\n"
                raw = line.encode("utf-8")
                handle.write(raw)
                count += 1
                bytes_written += len(raw)
                if count % 200 == 0:
                    elapsed = time.time() - started
                    rate = count / elapsed if elapsed else 0.0
                    _log(
                        f"已抓 {count} 页 · {bytes_written / 1024 / 1024:.1f} MB · "
                        f"{elapsed / 60:.1f} 分钟 · {rate:.1f} 页/秒"
                    )

    elapsed = time.time() - started
    state_path.write_text(
        json.dumps(
            {
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "pages": count,
                "uncompressed_mb": round(bytes_written / 1024 / 1024, 2),
                "elapsed_sec": round(elapsed, 1),
                "batch": args.batch,
                "sleep_sec": args.sleep,
                "limit": args.limit,
                "api": API,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    _log(f"完成：{count} 页 · {bytes_written / 1024 / 1024:.1f} MB · 用时 {elapsed / 60:.1f} 分钟 → {pages_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
