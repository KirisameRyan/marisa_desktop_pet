import json
import threading
import time
import sys
from pathlib import Path
from typing import Dict, List, Any

# 统一存储中枢：无论是在开发源码运行还是在打包 dist 中运行，
# 优先绑定全局主项目目录 D:\zhuochong\data，避免桌宠 GUI 与后台大脑 QQ 割裂。
_CANDIDATE_DATA = Path(r"D:\zhuochong\data")
if _CANDIDATE_DATA.parent.exists():
    DATA_DIR = _CANDIDATE_DATA
elif getattr(sys, "frozen", False):
    DATA_DIR = Path(sys.executable).resolve().parent / "data"
else:
    DATA_DIR = Path(__file__).resolve().parent.parent / "data"

PROFILE_PATH = DATA_DIR / "profile.json"
MEMORY_PATH = DATA_DIR / "memory.json"
CHAT_HIST_PATH = DATA_DIR / "chat_history.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_PROFILE = {
    "favorability": 15,          # 初始好感度 (0~100)
    "interaction_count": 0,      # 互动次数
    "pet_name": "雾雨魔理沙",
    "user_title": "魔法使伙伴",  # 对用户的称呼
    "last_pat_time": 0,          # 上次抚摸/互动时间戳
    "created_at": time.time()
}

DEFAULT_MEMORY = {
    "user_info": {
        "nickname": "主人",
        "hobbies": [],
        "preferences": []
    },
    "key_events": [],            # 共同回忆/经历
    "notes": "喜欢让魔理沙陪伴在桌面上，经常写代码或者玩游戏。"
}

class MemoryManager:
    _lock = threading.RLock()

    def __init__(self):
        with self._lock:
            self.profile: Dict[str, Any] = self._load_json(PROFILE_PATH, DEFAULT_PROFILE)
            self.memory: Dict[str, Any] = self._load_json(MEMORY_PATH, DEFAULT_MEMORY)
            self.chat_history: List[Dict[str, str]] = self._load_json(CHAT_HIST_PATH, [])

    def _load_json(self, path: Path, default_val: Any) -> Any:
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[Memory] Failed to load {path.name}: {e}")
        return default_val.copy() if isinstance(default_val, (dict, list)) else default_val

    def _save_json(self, path: Path, data: Any):
        try:
            with self._lock:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Memory] Failed to save {path.name}: {e}")

    # --- 好感度机制 ---
    def get_favorability(self) -> int:
        return self.profile.get("favorability", 15)

    def get_rank(self) -> tuple[str, str]:
        """返回 (等级称号, 阶段描述)"""
        fav = self.get_favorability()
        if fav < 20:
            return "陌生魔法使", "魔理沙对你保持着基本的警惕与傲娇，口癖较少。"
        elif fav < 50:
            return "熟络的助手", "魔理沙已经认可了你，愿意热情地帮你打下手！"
        elif fav < 80:
            return "可靠的搭档", "你是魔理沙重要的冒险搭档，会主动关心你的日常。"
        else:
            return "幻想乡挚友", "最高羁绊！魔理沙将你视为挚友，随时准备为你轰出极限火花！"

    def add_favorability(self, delta: int, reason: str = "") -> int:
        with self._lock:
            old_val = self.profile.get("favorability", 15)
            new_val = max(0, min(100, old_val + delta))
            self.profile["favorability"] = new_val
            self.profile["interaction_count"] = self.profile.get("interaction_count", 0) + 1
            self._save_json(PROFILE_PATH, self.profile)
        if delta != 0:
            print(f"[Favorability] {delta:+d} ({reason}) -> Current: {new_val}")
        return new_val

    def try_pat(self) -> tuple[bool, int]:
        """双击摸头加好感（带 5 分钟冷却）"""
        now = time.time()
        last = self.profile.get("last_pat_time", 0)
        if now - last >= 300: # 5分钟CD
            self.profile["last_pat_time"] = now
            val = self.add_favorability(1, "摸头互动")
            return True, val
        return False, self.get_favorability()

    # --- 陪伴天数 ---
    def get_days_together(self) -> int:
        """从首次陪伴之日算起的第 N 天（>=1）。"""
        created = self.profile.get("created_at", 0) or 0
        if not created:
            return 1
        days = int((time.time() - created) // 86400) + 1
        return max(1, days)

    def mark_day_greeted(self):
        """记录今天已播报过陪伴天数。"""
        self.profile["last_day_greeted"] = time.strftime("%Y-%m-%d")
        self._save_json(PROFILE_PATH, self.profile)

    def has_greeted_today(self) -> bool:
        return self.profile.get("last_day_greeted") == time.strftime("%Y-%m-%d")

    # --- 对话上下文与记忆 ---
    def add_chat(self, role: str, content: str):
        with self._lock:
            self.chat_history.append({"role": role, "content": content})
            # 限制滑动窗口为最近 12 轮 (24 条)
            if len(self.chat_history) > 24:
                self.chat_history = self.chat_history[-24:]
            self._save_json(CHAT_HIST_PATH, self.chat_history)

    def get_recent_history(self, limit: int = 10) -> List[Dict[str, str]]:
        return self.chat_history[-limit:]

    def get_memory_summary(self) -> str:
        """格式化为注入 System Prompt 的紧凑长期记忆摘要"""
        u_info = self.memory.get("user_info", {})
        nick = u_info.get("nickname", "主人")
        hobbies = ", ".join(u_info.get("hobbies", [])) or "未知"
        notes = self.memory.get("notes", "")
        events = self.memory.get("key_events", [])
        events_str = "；".join(events[-3:]) if events else "暂无特殊事件"

        rank_title, _ = self.get_rank()
        fav = self.get_favorability()

        return (
            f"【关于面前的主人】\n"
            f"- 称呼：{nick}\n"
            f"- 爱好/偏好：{hobbies}\n"
            f"- 备忘档案：{notes}\n"
            f"- 最近经历：{events_str}\n"
            f"- 当前与魔理沙的好感度：{fav}/100（当前羁绊等级：{rank_title}）"
        )

    def update_notes(self, new_note: str):
        self.memory["notes"] = new_note
        self._save_json(MEMORY_PATH, self.memory)

    def add_key_event(self, event: str):
        events = self.memory.setdefault("key_events", [])
        events.append(event)
        if len(events) > 10:
            self.memory["key_events"] = events[-10:]
        self._save_json(MEMORY_PATH, self.memory)

MEMORY_MANAGER = MemoryManager()
