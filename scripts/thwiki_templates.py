#!/usr/bin/env python
"""模板普查：从语料里读出 THBWiki 的"信息框字段表"。

为什么先做这个：作品库/人物库要分哪些字段，**不该由人凭印象编**，而该由真实的
模板参数决定——信息框模板（{{官方游戏信息}}、{{同人专辑信息}}…）的参数名就是
现成的字段清单。v1 的 schema 直接照着它建，既不会漏字段，也不会造出语料里
根本不存在的字段。

用法：
    python scripts/thwiki_templates.py                    # 全部命名空间
    python scripts/thwiki_templates.py --top 25 --fields 20
    python scripts/thwiki_templates.py --data data/thwiki
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

_NAME_RE = re.compile(r"\{\{\s*([^|}\n\[\]<>{]+)")
# 信息框都在页面顶部，只扫前 N 字就够；这决定了整个普查是几分钟还是几小时
_HEAD_CHARS = 6000
# 单个模板调用的参数扫描上限，防止个别超长模板把时间吃光
_CALL_CHARS = 3000


def iter_template_calls(text: str) -> Iterator[Tuple[str, List[str]]]:
    """逐个产出 (模板名, 顶层参数名列表)。

    参数必须按**括号深度**扫描，因为参数值里经常嵌套模板：
        {{官方游戏信息|中文名 = 东方{{fact|红魔乡}}|序号 = 6}}
    用正则直接切 `|` 会在嵌套处切错。

    只扫页面前 `_HEAD_CHARS` 字、每个调用最多扫 `_CALL_CHARS` 字——信息框在顶部，
    这样把逐字符扫描的代价压下来（首版全页扫描 1.1 亿字，10 分钟都没跑完）。
    """

    head = text[:_HEAD_CHARS]
    for match in _NAME_RE.finditer(head):
        name = match.group(1).strip()
        if not name:
            continue
        # 从模板名之后开始找匹配的 }}
        i = match.end()
        stop = min(len(head), i + _CALL_CHARS)
        depth = 2
        params: List[str] = []
        current_is_key = False
        buf: List[str] = []
        while i < stop and depth > 0:
            if head.startswith("{{", i):
                depth += 1
                i += 2
                continue
            if head.startswith("}}", i):
                depth -= 1
                i += 2
                continue
            ch = head[i]
            if ch == "|" and depth == 2:
                joined = "".join(buf)
                if "=" in joined:
                    params.append(joined.split("=", 1)[0].strip())
                buf = []
                current_is_key = True
                i += 1
                continue
            buf.append(ch)
            i += 1
        if current_is_key:
            tail = "".join(buf)
            if "=" in tail:
                params.append(tail.split("=", 1)[0].strip())
        yield name, params


def main() -> int:
    parser = argparse.ArgumentParser(description="THBWiki 模板普查")
    parser.add_argument("--data", type=Path, default=Path("data") / "thwiki")
    parser.add_argument("--top", type=int, default=30, help="列出最常见的 N 个模板")
    parser.add_argument("--fields", type=int, default=0, help="为最常见的 N 个模板列出参数字段")
    args = parser.parse_args()

    counter: Counter = Counter()
    fields: Dict[str, Counter] = defaultdict(Counter)
    pages_with: Dict[str, int] = defaultdict(int)
    total_pages = 0

    for path in sorted(glob.glob(str(args.data / "pages-ns*.jsonl.gz"))):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                row: Dict[str, Any] = json.loads(line)
                total_pages += 1
                if total_pages % 20000 == 0:
                    print(f"  …已扫 {total_pages} 页", flush=True)
                seen = set()
                for name, params in iter_template_calls(row.get("wikitext") or ""):
                    counter[name] += 1
                    if len(fields) < 400 or name in fields:
                        for p in params:
                            if p:
                                fields[name][p] += 1
                    seen.add(name)
                for name in seen:
                    pages_with[name] += 1

    print(f"扫描 {total_pages} 页，模板调用 {sum(counter.values()):,} 次，不同模板 {len(counter):,} 个\n")
    print(f"=== 最常见 {args.top} 个模板（按调用次数） ===")
    print(f"{'模板':<34}{'调用':>8}{'涉及页数':>10}")
    for name, count in counter.most_common(args.top):
        print(f"{name[:32]:<34}{count:>8}{pages_with[name]:>10}")

    if args.fields:
        print(f"\n=== 最常见 {args.fields} 个模板的参数字段 ===")
        for name, _ in counter.most_common(args.fields):
            cols = list(fields[name].most_common(40))
            if not cols:
                continue
            print(f"\n[{name}]（{pages_with[name]} 页）")
            print("   " + " · ".join(f"{k}({v})" for k, v in cols))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
