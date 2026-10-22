import json, re, sys, time
from pathlib import Path
sys.path.insert(0, r"D:\MarisaBOT\plugins\marisa_maa")
from maa_driver import MaaDriver

LOG = Path(r"D:\MAA\debug\asst.log")
idx = int(sys.argv[1])
d = MaaDriver(maa_dir=Path("D:/MAA"), window_title="明日方舟",
              screencap_method=16, input_method=64)
d.load()
before = LOG.stat().st_size
tid = d.append_task("Reclamation", {"theme": idx})
print("  theme=%d → task_id=%s" % (idx, tid), flush=True)
d.start()
time.sleep(50)
d.stop()
time.sleep(2)
txt = LOG.read_text(encoding="utf-8", errors="replace")[before:]
tales = txt.count("Tales@RA@")
relaunch = txt.count("RelaunchAnchor@RA@")
tasks = []
for l in txt.splitlines():
    m = re.search(r'"cur_task":"([^"]+)"', l)
    if m and m.group(1) and m.group(1) not in tasks: tasks.append(m.group(1))
print("  theme=%d 结果: Tales@RA@=%d  RelaunchAnchor@RA@=%d" % (idx, tales, relaunch), flush=True)
print("  任务前缀样本:", tasks[:5], flush=True)
print("  判定:", ("✅ RelaunchAnchor" if relaunch and not tales
                 else ("❌ Tales" if tales and not relaunch else "?")), flush=True)
d.close()
