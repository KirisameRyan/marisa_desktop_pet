# -*- coding: utf-8 -*-
"""魔理沙桌面巡检：宿主侧定时观察屏幕并生成主动搭话。

与旧的桌宠本地巡检相比，这里把生成台词的依据从「一屏截图」扩展成：
当前时间与时段、前台应用与窗口标题、连续专注/离开时长、最近已发提醒
（同类冷却，避免反复念叨喝水眨眼）、好感度与羁绊称号。

台词仍由模型生成，本模块只负责组织上下文与判定「这次该不该说、说什么角度」。
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

# 角度冷却（分钟）：同一类提醒在该时间内不再重复，避免连着几次都在说喝水
ANGLE_COOLDOWN_MINUTES = {
    "rest": 45,
    "hydration": 40,
    "eyes": 45,
    "praise": 30,
    "tease": 35,
    "care": 60,
}

# 前台应用分类关键词（小写匹配 exe 与窗口标题）
APP_CATEGORIES: List[Tuple[str, Tuple[str, ...]]] = [
    ("code", ("code.exe", "cursor", "pycharm", "idea64", "sublime_text", "devenv",
              "windowsterminal", "powershell", "cmd.exe", "windsurf", "trae", "vscode",
              "github", "gitlab", "jupyter", "matlab", "vim", "emacs", "clion", "rider")),
    ("game", ("steam", "genshin", "yuanshen", "原神", "崩坏", "starrail", "league", "lol",
              "valorant", "dota", "cs2", "minecraft", "game", "游戏")),
    ("video", ("bilibili", "哔哩哔哩", "youtube", "potplayer", "vlc", "mpv", "爱奇艺",
               "腾讯视频", "优酷", "netflix", "抖音", "tiktok", "iqiyi", "youku")),
    ("chat", ("qq.exe", "wechat", "weixin", "telegram", "discord", "dingtalk", "feishu",
              "slack", "微信")),
    ("browse", ("chrome.exe", "msedge", "firefox", "brave", "opera", "safari", "浏览器")),
    ("read", ("pdf", "acrobat", "sumatrapdf", "wps", "office", "word", "excel", "powerpnt")),
]

CATEGORY_LABELS = {
    "code": "写代码/开发",
    "game": "玩游戏",
    "video": "看视频/摸鱼",
    "chat": "聊天",
    "browse": "浏览网页",
    "read": "看文档/办公",
    "idle": "离开电脑",
    "other": "用电脑",
}

TIME_SLOT_HINTS = {
    "late_night": "现在是深夜，主人在熬夜",
    "early_morning": "现在是清晨",
    "forenoon": "现在是上午工作时段",
    "noon": "现在是午间，可能刚吃完饭",
    "afternoon": "现在是下午工作时段",
    "evening": "现在是傍晚",
    "night": "现在是晚上",
}


def time_slot(now: datetime) -> str:
    """把当前时刻归入一个时段，供语气分档使用。"""

    hour = now.hour
    if hour >= 23 or hour < 5:
        return "late_night"
    if hour < 9:
        return "early_morning"
    if hour < 12:
        return "forenoon"
    if hour < 14:
        return "noon"
    if hour < 18:
        return "afternoon"
    if hour < 20:
        return "evening"
    return "night"


def classify_app(exe: str, title: str) -> str:
    """按可执行文件名与窗口标题猜测主人正在做什么。"""

    haystack = f"{exe} {title}".lower()
    if not haystack.strip():
        return "idle"
    for category, keywords in APP_CATEGORIES:
        if any(keyword in haystack for keyword in keywords):
            return category
    return "other"


@dataclass
class WorkState:
    """主人在做的事及其持续时长。"""

    category: str = "other"
    exe: str = ""
    title: str = ""
    since: float = field(default_factory=time.time)

    def duration_minutes(self, now: Optional[float] = None) -> int:
        """当前状态已持续的分钟数。"""

        return max(0, int(((now or time.time()) - self.since) // 60))

    def update(self, category: str, exe: str, title: str) -> Optional[str]:
        """刷新状态；返回被切换掉的旧分类（无变化时为 None）。"""

        if category == self.category:
            self.exe, self.title = exe, title
            return None
        previous = self.category
        self.category = category
        self.exe, self.title = exe, title
        self.since = time.time()
        return previous


@dataclass
class PatrolRecord:
    """一条已发出的巡检搭话。"""

    text: str
    angle: str
    category: str
    at: float


class PatrolDirector:
    """组织巡检上下文、挑选搭话角度、生成台词。

    只依赖注入进来的 ``llm_call``（宿主 llm.generate 的薄封装）与日志器，
    便于在没有宿主环境时单独测试。
    """

    def __init__(
        self,
        *,
        llm_call: Callable[..., Any],
        logger: Any,
        max_recent: int = 5,
    ) -> None:
        self._llm_call = llm_call
        self._logger = logger
        self._max_recent = max_recent
        self._records: List[PatrolRecord] = []
        self.work_state = WorkState()

    # ---------------- 上下文采集 ----------------

    def observe_foreground(self, info: Dict[str, str]) -> None:
        """记录前台应用信息，用于计算专注时长。"""

        exe = str(info.get("exe") or "")
        title = str(info.get("title") or "")
        self.work_state.update(classify_app(exe, title), exe, title)

    def recent_texts(self) -> List[str]:
        """最近几条搭话文本（用于提示模型别重复）。"""

        return [record.text for record in self._records[-self._max_recent :]]

    def _angle_available(self, angle: str, now: float) -> bool:
        cooldown = ANGLE_COOLDOWN_MINUTES.get(angle, 30) * 60
        return all(
            record.angle != angle or (now - record.at) >= cooldown
            for record in self._records
        )

    def pick_angle(self, now: Optional[float] = None) -> Optional[str]:
        """按当前状态与冷却情况挑一个搭话角度。

        候选角度按当前处境排序（写代码时先提醒休息、玩游戏时先打气），
        过滤掉仍在冷却期内的角度。全部候选都在冷却时返回 None —— 与其把
        「该喝水了」翻来覆去说第三遍，不如这一轮先不说话。
        """

        moment = now or time.time()
        slot = time_slot(datetime.now())
        category = self.work_state.category
        duration = self.work_state.duration_minutes(moment)

        if category == "idle":
            candidates = ["care"]
        elif category == "game":
            candidates = ["praise", "care"]
        elif category == "video":
            candidates = ["tease", "care"]
        elif category == "code":
            if duration >= 80:
                candidates = ["eyes", "rest", "hydration"]
            elif duration >= 50:
                candidates = ["hydration", "rest", "eyes"]
            else:
                candidates = ["rest", "hydration", "eyes"]
        else:
            candidates = ["rest", "hydration", "tease", "care"]

        if slot == "late_night":
            candidates = ["care", *candidates]

        available = [angle for angle in candidates if self._angle_available(angle, moment)]
        if not available:
            return None
        return random.choice(available)

    # ---------------- 台词生成 ----------------

    def build_prompts(
        self,
        *,
        observation: str,
        favorability: int,
        rank_name: str,
        angle: str,
    ) -> Tuple[str, str]:
        """构造系统提示与用户提示。"""

        now = datetime.now()
        slot = time_slot(now)
        category = self.work_state.category
        duration = self.work_state.duration_minutes()
        recent = self.recent_texts()

        angle_requirements = {
            "rest": "提醒主人站起来活动一下身体（久坐伤腰），但别像健康鸡汤，要有魔理沙的俏皮口气",
            "hydration": "提醒主人喝口水，可以顺便吐槽他的水杯是不是摆设",
            "eyes": "提醒主人让眼睛歇一会儿、看看远处，别一直盯着屏幕",
            "praise": "给正在玩游戏的主人打气加油，甚至可以好奇地问问他打到哪一关了",
            "tease": "带着调皮笑意吐槽他在摸鱼，但要让他觉得被陪伴而不是被说教",
            "care": "关心一下主人的状态；如果他似乎不在电脑前，可以说一句等他回来/别太累",
        }

        system_prompt = (
            "你是「雾雨魔理沙」（Kirisame Marisa），住在主人电脑桌面的伙伴。\n"
            "性格自信元气、嘴硬心软、爱吐槽主人但很在乎他，句末常带「だぜ」「★」。\n"
            f"你与主人的好感度是 {favorability}/100（羁绊：{rank_name}）。\n"
            "【任务】偷偷看了一眼主人的屏幕后，主动说 1~2 句话。\n"
            f"【本轮角度】{angle_requirements.get(angle, angle_requirements['care'])}\n"
            f"【时间】{slot}（{now.strftime('%H:%M')}）\n"
            f"【主人正在做】{CATEGORY_LABELS.get(category, category)}"
            f"，已持续约 {duration} 分钟"
            f"{f'；窗口标题：{self.work_state.title[:60]}' if self.work_state.title else ''}\n"
            f"【屏幕观察报告】{observation}\n"
            + (f"【最近已经说过】{' / '.join(recent)}\n" if recent else "")
            + "【要求】绝对不要重复上面已经说过的句子或句式；不要说教、不要像 AI 助手；"
            "不超过 45 字。只输出要说的那句话本身，不要任何解释或引号。"
        )
        user_prompt = "现在用魔理沙的口吻，主动跟主人说一句话吧！"
        return system_prompt, user_prompt

    async def generate(
        self,
        *,
        observation: str,
        favorability: int,
        rank_name: str,
        model: str,
    ) -> Optional[PatrolRecord]:
        """生成一条搭话并记录；失败时返回 None。"""

        angle = self.pick_angle()
        if angle is None:
            self._logger.info("[marisa_desktop] 本轮候选提醒都还在冷却，跳过这次搭话")
            return None
        system_prompt, user_prompt = self.build_prompts(
            observation=observation,
            favorability=favorability,
            rank_name=rank_name,
            angle=angle,
        )
        text = await self._llm_call(
            model=model,
            prompt=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        cleaned = str(text or "").strip().strip("「」\"'")
        if not cleaned:
            self._logger.info("[marisa_desktop] 巡检台词为空，跳过本次播报")
            return None
        record = PatrolRecord(
            text=cleaned,
            angle=angle,
            category=self.work_state.category,
            at=time.time(),
        )
        self._records.append(record)
        self._records = self._records[-20:]
        return record
