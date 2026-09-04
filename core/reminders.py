import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any

if getattr(sys, "frozen", False):
    ROOT_DIR = Path(sys.executable).resolve().parent
else:
    ROOT_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = ROOT_DIR / "data"
REMINDER_PATH = DATA_DIR / "reminders.json"

# 结构: [{id, text, time "HH:MM", kind "once"|"daily", enabled, created_at}]


class ReminderStore:
    def __init__(self):
        self.reminders: List[Dict[str, Any]] = self._load()

    def _load(self) -> List[Dict[str, Any]]:
        if REMINDER_PATH.exists():
            try:
                with open(REMINDER_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[Reminder] load error: {e}")
        return []

    def _save(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(REMINDER_PATH, "w", encoding="utf-8") as f:
                json.dump(self.reminders, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Reminder] save error: {e}")

    def add(self, text: str, hm: str, kind: str = "once") -> int:
        """hm 形如 '14:30'。返回新 id。"""
        now = time.time()
        new_id = int(now * 1000) % (2 ** 31)
        self.reminders.append({
            "id": new_id,
            "text": text.strip(),
            "time": hm,
            "kind": kind,
            "enabled": True,
            "created_at": now,
        })
        self._save()
        return new_id

    def remove(self, reminder_id: int) -> bool:
        before = len(self.reminders)
        self.reminders = [r for r in self.reminders if r.get("id") != reminder_id]
        changed = len(self.reminders) != before
        if changed:
            self._save()
        return changed

    def list_reminders(self) -> List[Dict[str, Any]]:
        return sorted(self.reminders, key=lambda r: r.get("time", "00:00"))

    def due_now(self) -> List[Dict[str, Any]]:
        """返回此刻应触发的提醒；一次性提醒触发后自动移除。"""
        now_hm = datetime.now().strftime("%H:%M")
        fired = [r for r in self.reminders if r.get("enabled", True) and r.get("time") == now_hm]
        changed = False
        remaining = []
        for r in self.reminders:
            if r in fired and r.get("kind") == "once":
                changed = True
                continue
            remaining.append(r)
        if changed:
            self.reminders = remaining
            self._save()
        return fired


REMINDER_STORE = ReminderStore()
