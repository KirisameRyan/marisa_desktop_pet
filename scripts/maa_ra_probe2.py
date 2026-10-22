import sys, json
from pathlib import Path
sys.path.insert(0, r"D:\MarisaBOT\plugins\marisa_maa")
from maa_driver import MaaDriver
d = MaaDriver(maa_dir=Path("D:/MAA"), window_title="明日方舟",
              screencap_method=16, input_method=128)
d.load()
print("  已连接=%s 客户区=%s\n" % (d.connected(), d.client_size()), flush=True)
VARIANTS = [
    {},
    {"Theme": "RelaunchAnchor"},
    {"Theme": "RelaunchAnchor", "Mode": "RA4"},
    {"theme": "RelaunchAnchor", "mode": 0},
    {"theme": "RelaunchAnchor", "mode": 1},
    {"theme": "RelaunchAnchor", "mode": 2},
    {"theme": "RelaunchAnchor", "mode": 3},
    {"theme": "RelaunchAnchor", "mode": 5},
    {"theme": "RelaunchAnchor", "mode": "RelaunchAnchor"},
    {"theme": "RelaunchAnchor", "increment_mode": 0, "clear_store": False},
]
for p in VARIANTS:
    try: tid = d.append_task("Reclamation", p)
    except Exception as e: print("  %-64s 异常 %s" % (json.dumps(p, ensure_ascii=False)[:62], e)); continue
    print("  %-64s %s (id=%d)" % (json.dumps(p, ensure_ascii=False)[:62],
                                  "✅" if tid else "❌", tid), flush=True)
d.close()
