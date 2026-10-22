import sys, json
from pathlib import Path
sys.path.insert(0, r"D:\MarisaBOT\plugins\marisa_maa")
from maa_driver import MaaDriver
d = MaaDriver(maa_dir=Path("D:/MAA"), window_title="明日方舟",
              screencap_method=16, input_method=128)
d.load()
print("  已连接=%s\n" % d.connected(), flush=True)
print("  官方文档列出的合法 theme 值：Fire / Tales\n")
for label, p in [
    ("文档值 Fire", {"theme": "Fire"}),
    ("文档值 Tales", {"theme": "Tales"}),
    ("GUI 里的 RelaunchAnchor", {"theme": "RelaunchAnchor"}),
    ("文档示例 Fire+mode1+物品", {"theme": "Fire", "mode": 1,
                                  "tools_to_craft": ["荧光棒"], "increment_mode": 0,
                                  "num_craft_batches": 16}),
]:
    try: tid = d.append_task("Reclamation", p)
    except Exception as e: print("  %-26s 异常 %s" % (label, e)); continue
    print("  %-26s %s (id=%d)   %s" % (label, "✅ 接受" if tid else "❌ 被拒", tid,
                                       json.dumps(p, ensure_ascii=False)[:70]), flush=True)
d.close()
