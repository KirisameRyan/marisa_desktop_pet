"""魔理沙 MAA 桥：用 QQ 远程控制明日方舟 PC 客户端的挂机。

主人定的方案与策略：
    触发    —— `/maa` + 自然语言描述（不要求写死格式）
    汇报    —— 长挂机每 30 分钟一次；清日常每个子任务完成即报
    汇报链路 —— **不走 LLM**，插件直接发消息（省 token）

为什么不用 adb+模拟器：主人直接用**官方 PC 客户端**。
    MAA 的 Python 绑定只包装了 ADB（AsstConnect），PC 端要 AsstAttachWindow，
    绑定里没有 —— 这一层由同目录的 maa_driver.py 用 ctypes 补上（已实测通过）。

两条入口都保留，且**都不额外调模型**：
    1. `/maa 挂1-7三轮`     → 规则解析（关卡/次数/任务类型），拿下常见说法
    2. 「魔理沙帮我挂三轮1-7」→ 模型在正常对话里调 marisa_maa_farm 工具

本次修复（0.2.0）记要，都是实测踩出来的，改回去之前先看注释：
    · 修复 `ctx.send.text/hybrid` 参数顺序（曾经全反，所有汇报发不出去）；
    · 修复 MaaCore 消息 ID 表（旧表把 TaskChainError 当成 TaskChainStart，
      "出错"被汇报成"开始"）；
    · driver.close() 现在会「停止 → 等 AsstRunning 归零 → 销毁实例」，
      根治任务卡死时 MAA 的 Win32 控制器一直占用鼠标/窗口的问题；
    · 生息演算改用官方 Reclamation 任务，参数按 MaaCore 实际接受的来：
      theme="RelaunchAnchor" + mode（flags：RA-1=16 / RA-4=48 / RA-15=32），
      参数必须给全（tools_to_craft 等缺一个 MaaCore 就会抛 C++ 异常）；
    · 生息演算自动进场（2026-10-11 实机全流程验证通过）：/maa RA4 会先跑
      MarisaGoHome（回主界面）→ MarisaEnterRA（进 RA-4 大地图，约 20 秒）→
      官方 Reclamation 接棒循环；自定义链 json 由 _ensure_extra_tasks_installed
      幂等同步到 MAA 的 resource/tasks（MAA 升级覆盖后会自动补回）；
    · 挂机结束（完成/出错/超时强停）时自动补一张游戏画面截图（report_screenshot，
      默认开），必须在销毁实例之前截——销毁后 controller 就没了。

本次新增（0.3.0）：
    · 刷关连战倍率（fight_series）：**「刷十轮」= 连战倍率调到 10、一次进出打完**，
      而不是进出关卡十次。映射 series=min(轮数,10)，不会超打（MaaCore 内部
      在"已打场次+本轮连战>目标"时会停止）。配置 -2=跟随轮数（默认）/ 0=自动 /
      -1=不切换 / 1~10=固定；Fight 参数已实测被 MaaCore 接受（probe_tasks.py）。
    · 完成后动作（post_action）：关机/睡眠/休眠/锁屏（对应 MAA GUI 的「完成后操作」，
      GUI 自己实现的、MaaCore 没有接口，这里用 Windows 命令执行）。
      执行顺序：任务完成 → 截图/汇报发完 → 再执行；出错时默认跳过（skip_on_error）；
      支持临时说法「刷完关机」（单轮有效）与 /maa 取消关机（shutdown /a，已实测）。
    · 工具描述补充"挂机完成/出错会自动推送通知和截图，不要用 wait 轮询进度"——
      减少决策模型盲目 wait 盯进度（配合主程序 wait 修复）。

本次新增（0.4.1）：
    · **三层资源加载**（maa_driver._load_extra_resources）：在基础 resource 之外，
      追加加载 `cache/resource`（MAA 资源热更层——新活动关卡/限时任务在这里）
      与 `resource/platform_diff/PC/resource`（PC 端差异资源），与 MAA GUI 的
      AsstProxy.LoadResource 完全一致。此前只加载基础层，导致 GUI 能跑的
      内容插件报错（实测：YW-4 入队失败，gui 正常）。
    · **基建队列换班**（infrast_mode="rotation"）：mode=20000 走游戏内置排班
      队列换班（不再由 MAA 逐个设施挑人），facility 为 MaaCore 必填项，
      参数逐项对齐主人 GUI 配置；"rotation"/"default" 可切。
    · **自动肉鸽（黑流树海）**：/maa 肉鸽 / 树海 / 黑流 → 官方标准 Roguelike 链
      自动导航（游戏停哪都行：首页→终端→集成战略→树海），参数对齐主人 GUI
      （theme=BlackFlow、刷等级、难度 2、特勤分队/稳扎稳打/机械师）。
      树海适配：官方链进分队页后会等不存在的 SquadConfirm 而报错（实测），
      插件随资源安装 marisa_blackflow_fix.json 覆盖 Squad-EnterPoint →
      官方限时链 BlackFlowTemporary 接管，循环「进入→投资→放弃→重开」
      （2026-10-12 实机全链路验证通过：首页起步、零人工）。

本次调整（0.4.1）：
    · **鼠标输入换回 Seize(1)（SendInput 真实输入）**：实跑黑流肉鸽两轮，
      每轮都卡在"确认放弃/分队确认"这类按钮上——64（PostMessageWithCursorPos）
      偶发点击不被游戏接收，重试 20 次全在同一"无效窗口期"内耗尽。
      Seize 点击 100% 可靠；配套：
        - 挂机启动前 ensure_foreground（拉不到前台直接失败，不带病开跑）；
        - heartbeat 每 15 秒复查前台并自动拉回（用户切窗口会被"抢回"）；
        - 连续 3 次拉不回 → QQ 警告建议 /maa 停。
      代价（主人已知并接受）：挂机期间电脑不能正常用鼠标键盘。
      键盘保持 SendMessage(2) 不变。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import asyncio
import base64
import ctypes
import ctypes.wintypes
import re
import subprocess
import sys
import threading
import time

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase, Tool  # noqa: E402
from maibot_sdk.types import ToolParameterInfo, ToolParamType  # noqa: E402

from maa_driver import MaaDriver  # noqa: E402

_PROJECT_ROOT = _PLUGIN_DIR.parents[1]

# 命令正则前缀：容忍 〔主人〕 标记、@At 与「魔理沙」前缀。
# 首版我直接把裸正则丢给 @Command，漏了锚点与触发词容错，也没有宿主约定的 raw 捕获组——
# 与 marisa_gate / marisa_dsh 的写法保持一致才不会踩坑。
_AT_MARK = r"(?:\s*〔[^〕]*〕\s*)?"
_TRIGGER = r"(?:\s*(?:\[At:[^\]]*\]|@[\w\u4e00-\u9fa5]+)\s*)*(?:魔理沙[，,、\s]*)?"


def cmd_pattern(inner: str) -> str:
    """统一命令正则前缀（与 marisa_gate 的写法一致）。"""

    return "^" + _AT_MARK + _TRIGGER + "(?P<raw>" + inner + ")$"

# MaaCore 的 MsgId —— 以 D:\MAA\Python\asst\utils.py 的 Message 枚举为准（v6.18.0）。
# 旧版这里整体错位（把 10000 当成 TaskChainStart，实际是 TaskChainError），
# 导致"任务出错"被翻译成"任务开始"汇报给用户——这就是汇报错乱的根源之一。
# 改这个表之前必须先看 asst/utils.py，不要凭记忆填。
MSG_INIT_FAILED = 1
MSG_CONNECTION_INFO = 2
MSG_ALL_TASKS_COMPLETED = 3
MSG_ASYNC_CALL_INFO = 4
MSG_DESTROYED = 5
MSG_TASK_CHAIN_ERROR = 10000
MSG_TASK_CHAIN_START = 10001
MSG_TASK_CHAIN_COMPLETED = 10002
MSG_TASK_CHAIN_EXTRA_INFO = 10003
MSG_TASK_CHAIN_STOPPED = 10004
MSG_SUB_TASK_ERROR = 20000
MSG_SUB_TASK_START = 20001
MSG_SUB_TASK_COMPLETED = 20002
MSG_SUB_TASK_EXTRA_INFO = 20003
MSG_SUB_TASK_STOPPED = 20004

# 沉默多久就主动报一声。实测 StartUp 在游戏不在主界面时静默跑了 114 秒，
# 期间插件一言不发，用户直接以为"没启动"——这个值就是为它设的。
SILENCE_SEC = 75


class PluginSectionConfig(PluginConfigBase):
    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="0.4.1", description="配置版本")


class MaaSectionConfig(PluginConfigBase):
    maa_dir: str = Field(default="D:\\MAA", description="MAA 安装目录（含 MaaCore.dll 与 Python 绑定）")
    window_title: str = Field(default="明日方舟", description="游戏窗口标题")
    screencap_method: int = Field(
        default=16,
        description="截图方式：**16=PrintWindow（官方要求，提权后可用）** ·"
                    "2=FramePool（会话会失效返回全黑图，需重新附着）·"
                    "1=GDI（抓到的是覆盖窗口）· 8=DXGI桌面（被遮挡处会串窗）",
    )
    input_method: int = Field(
        default=1,
        description="鼠标输入方式：**1=Seize（当前采用）—— SendInput 真实输入，点击 100% 可靠**。"
                    "前提：游戏窗口保持前台（插件自动维持）、挂机期间电脑不能正常用鼠标键盘。"
                    "64=PostMessageWithCursorPos 曾长期采用，但实跑肉鸽时偶发点击不被游戏接收"
                    "（卡在确认按钮上重试 20 次全失效）——这是换回 Seize 的原因。"
                    "2/4=SendMessage/PostMessage 对方舟 PC 端无效；256=WindowPos 会移动窗口。"
                    "前提：必须提权（UIPI 拦消息），且系统 UI 缩放为 100%、游戏分辨率标准 16:9",
    )
    keyboard_method: int = Field(
        default=2,
        description="键盘输入方式，默认 2=SendMessage（游戏里极少用键盘，不需要 Seize 接管）",
    )
    default_stage: str = Field(default="", description="没说关卡时用哪个（空=用游戏内当前/上次关卡）")
    max_times: int = Field(default=200, description="单次挂机次数上限（防手滑写错）")
    connect_timeout_sec: float = Field(default=30.0, description="等待附着成功的超时")
    max_run_minutes: int = Field(
        default=240,
        description="单次挂机总时长上限（分钟）。到时强制停止并交还鼠标/窗口，"
                    "防止任务卡死时 MAA 的 Win32 控制器一直拿着光标（0=不限制）",
    )
    ra_mode: int = Field(
        default=1,
        description="生息演算模式：0=RA-1 · **1=RA-4** · 2=RA-15（对应 MAA「重启锚点」主题）",
    )
    ra_craft_batches: int = Field(default=16, description="生息演算单次最大制造轮数（同 MAA GUI 默认）")
    auto_enter_ra: bool = Field(
        default=True,
        description="生息演算前自动导航：先回主界面、再进入 RA 大地图（用插件自带的"
                    " MarisaGoHome/MarisaEnterRA 任务链，需游戏客户端开着）。"
                    "关闭后要求游戏已手动停在大地图对应关卡、右下角出现「开启建设」",
    )
    fight_series: int = Field(
        default=-2,
        ge=-2,
        le=10,
        description="刷关连战倍率（游戏内代理指挥倍率，1-10）："
                    "**-2=跟随轮数（默认，刷十轮→10 连战一次打完）** ·"
                    "0=MAA 自动按理智选最大倍率 · -1=不改动游戏当前倍率 · 1~10=固定倍率",
    )
    infrast_mode: Literal["rotation", "default"] = Field(
        default="rotation",
        description="基建换班方式：**rotation=按游戏内置排班队列换班（默认，"
                    "主人要求「使用队列更换」）** · default=MAA 逐个设施自动挑人",
    )
    roguelike_theme: str = Field(default="BlackFlow", description="自动肉鸽主题（BlackFlow=黑流树海）")
    roguelike_mode: Literal["exp", "investment", "baby_animal"] = Field(
        default="exp",
        description="肉鸽策略（黑流三种）：**exp=刷等级（默认）** · investment=刷源石锭 · baby_animal=刷幼兽",
    )
    roguelike_difficulty: int = Field(default=2, description="肉鸽难度（-1=不切换，其余按主题难度档；主人选 2 级）")
    roguelike_squad: str = Field(default="特勤分队", description="肉鸽开局分队（主人选特勤分队）")
    roguelike_roles: str = Field(default="稳扎稳打", description="肉鸽职业组（主人选稳扎稳打）")
    roguelike_core_char: str = Field(default="机械师", description="肉鸽开局干员（主人选机械师；空=不指定）")


class ReportSectionConfig(PluginConfigBase):
    long_task_minutes: int = Field(default=30, description="长挂机每隔多少分钟汇报一次进度")
    report_sub_tasks: bool = Field(default=True, description="清日常时每个子任务完成即报")
    report_drops: bool = Field(default=True, description="汇报里带掉落信息")
    report_screenshot: bool = Field(
        default=True,
        description="挂机结束（完成/出错/超时强停）时自动补一张游戏画面截图发到汇报会话",
    )


class PostActionSectionConfig(PluginConfigBase):
    """挂机全部完成后的收尾动作（对应 MAA GUI 的「完成后操作」）。

    这些动作在 MAA GUI 里是 GUI 自己实现的，MaaCore 没有对应接口，
    所以直连插件要自己执行（Windows 命令）。执行顺序：
    任务完成 → 截图/汇报发到 QQ → 再执行动作。
    """

    action: Literal["none", "shutdown", "sleep", "hibernate", "lock"] = Field(
        default="none",
        description="完成后动作：**none=不执行（默认）** · shutdown=关机 · sleep=睡眠 ·"
                    " hibernate=休眠 · lock=锁屏。也可以在下指令时临时说「刷完关机」",
    )
    delay_sec: int = Field(
        default=60,
        ge=0,
        description="关机/睡眠前的缓冲秒数（期间可以发「/maa 取消关机」取消）",
    )
    skip_on_error: bool = Field(
        default=True,
        description="有任务链出错时跳过完成后动作（默认开，防止挂失败了还关机）",
    )


class MarisaMaaConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    maa: MaaSectionConfig = Field(default_factory=MaaSectionConfig)
    report: ReportSectionConfig = Field(default_factory=ReportSectionConfig)
    post_action: PostActionSectionConfig = Field(default_factory=PostActionSectionConfig)


# 「挂1-7三轮」「刷CE-6五次」这类说法的解析规则
_TIMES_WORDS = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
                "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
# 「挂1-7三轮」「刷CE-6五次」这类说法的解析规则。
# 注意边界写法：**不能用 \b** —— 中文和数字在 Unicode 正则里都算 word 字符，
# 所以「挂1-7」里 1 之前没有词边界，\b 会导致关卡名一个都匹配不到（实测踩过）。
# 改成 ASCII 边界：(?<![A-Za-z0-9-]) / (?![A-Za-z0-9-])。
_STAGE_RE = re.compile(
    r"(?<![A-Za-z0-9-])"
    r"([A-Z]{1,3}-\d{1,2}(?:-\d)?|[A-Z]{1,3}-[A-Z]-\d|\d{1,2}-\d{1,2}(?:-(?:NORMAL|HARD))?)"
    r"(?![A-Za-z0-9-])",
    re.I,
)
_TIMES_ARABIC_RE = re.compile(r"(\d+)\s*(?:次|轮|把|遍|局)")
_TIMES_CN_RE = re.compile(r"([一两二三四五六七八九十])\s*(?:次|轮|把|遍|局)")
# 生息演算（RA）说法：「生息演算」「生息」「RA4」「RA-15」等。
# RA4 没有连字符时不会进 _STAGE_RE；有连字符的 RA-4 会被它误当关卡名，
# 所以 intent 判定为生息演算时由 _build_tasks 忽略 stage。
# 边界不能用 \b：中文和字母都算 word 字符，「生息RA4」里 R 前面没有词边界（同 _STAGE_RE 的坑）。
# 两个正则分开搜：单靠一个 alternation 时，regex 会先命中位置靠前的「生息」，
# 「生息RA-15」的 15 就丢了（实测）。
_RA_WORD_RE = re.compile(r"(?:生息演算|重启锚点|生息)")
_RA_MODE_RE = re.compile(r"(?<![A-Za-z])RA[\s-]?(1|4|15)(?![0-9])", re.I)
_RA_MODE_WORDS = {"1": 0, "4": 1, "15": 2}
_TASK_WORDS = (
    ("清日常", "daily"), ("日常", "daily"), ("一键长草", "linkstart"), ("长草", "linkstart"),
    ("招募", "recruit"), ("公招", "recruit"), ("基建", "infrast"), ("换班", "infrast"),
    ("信用", "mall"), ("商店", "mall"), ("采购", "mall"), ("领奖", "award"),
    ("访问", "visit"), ("刷", "fight"), ("打", "fight"), ("挂", "fight"), ("作战", "fight"),
)
# 完成后动作的临时说法（一次挂机有效，不落配置）：「刷完关机」「刷完锁屏」等
_POST_ACTION_WORDS = (
    ("关机", "shutdown"), ("睡眠", "sleep"), ("休眠", "hibernate"), ("锁屏", "lock"),
)
# 自动肉鸽的说法（黑流树海为主）。注意「刷等级」里含「刷」，
# 肉鸽判定必须先于 _TASK_WORDS 的 fight 词，否则会被误判成刷关。
_ROGUE_WORD_RE = re.compile(r"(?:肉鸽|集成战略|树海|黑流)")
_RG_MODE_WORDS = (
    ("源石锭", "investment"), ("存钱", "investment"),
    ("幼兽", "baby_animal"), ("培育", "baby_animal"),
    ("等级", "exp"), ("刷级", "exp"),
)


def parse_request(text: str) -> Dict[str, Any]:
    """把 `/maa` 后面的自然语言解析成任务描述。

    只覆盖**常见说法**（守卫成本最低的那批），解析不出来就让调用方提示用户换个说法——
    刻意不在这里调模型：一次挂机指令不值得再花一次 LLM 调用，而且规则解析是可预测的。
    """

    raw = (text or "").strip()
    result: Dict[str, Any] = {
        "intent": "", "stage": "", "times": 0, "raw": raw,
        "ra_mode": None, "rg_mode": None, "post_action": "",
    }
    if not raw:
        return result

    match = _STAGE_RE.search(raw)
    if match:
        result["stage"] = match.group(1).upper()
    times = 0
    match = _TIMES_ARABIC_RE.search(raw)
    if match:
        times = int(match.group(1))
    else:
        match = _TIMES_CN_RE.search(raw)
        if match:
            times = _TIMES_WORDS.get(match.group(1), 0)
    result["times"] = times

    # 「刷完关机」这类完成后动作的说法
    for word, action in _POST_ACTION_WORDS:
        if word in raw:
            result["post_action"] = action
            break

    # 自动肉鸽优先于普通任务词（「刷等级」里含「刷」，先判肉鸽再判刷关）
    if _ROGUE_WORD_RE.search(raw):
        result["intent"] = "roguelike"
        for word, rg_mode in _RG_MODE_WORDS:
            if word in raw:
                result["rg_mode"] = rg_mode
                break
        return result

    # 生息演算优先于普通任务词：它可能同时含有「打」等字（如「帮我打生息演算」）
    ra_mode_match = _RA_MODE_RE.search(raw)
    if ra_mode_match or _RA_WORD_RE.search(raw):
        result["intent"] = "reclamation"
        if ra_mode_match:
            result["ra_mode"] = _RA_MODE_WORDS.get(ra_mode_match.group(1), None)
        return result

    for word, intent in _TASK_WORDS:
        if word in raw:
            result["intent"] = intent
            break
    return result


class MarisaMaaPlugin(MaiBotPlugin):
    """MAA 远程控制插件。"""

    config_model = MarisaMaaConfig

    def __init__(self) -> None:
        super().__init__()
        self._driver: Optional[MaaDriver] = None
        self._driver_lock = asyncio.Lock()
        self._worker: Optional[threading.Thread] = None
        self._events: asyncio.Queue = asyncio.Queue()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._report_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._notify_stream: str = ""       # 最近交互会话（即时回复）
        self._report_stream: str = ""       # 挂机发起会话（完成/进度汇报固定发这里）
        self._post_action_override: str = ""  # 本轮挂机的临时完成后动作（「刷完关机」等）
        self._started_at: float = 0.0
        self._last_report: float = 0.0
        self._last_event_at: float = 0.0    # 最近一次收到回调的时间（沉默检测用）
        self._current_chain: str = ""       # 当前在跑哪条任务链
        self._chain_done: int = 0
        self._chain_failed: bool = False    # 本轮挂机是否有任务链出错（完成汇报用）
        self._pending_times: int = 0
        self._long_running: bool = False    # 本轮是否长任务（RA 等循环任务：汇总汇报而非逐事件刷屏）
        self._fg_miss_count: int = 0        # 连续几次无法把游戏拉回前台（Seize 模式用）
        self._farming = False

    # ─────────────────── 生命周期 ───────────────────

    async def on_load(self) -> None:
        """宿主强制要求覆写（plugin_loader 会校验，缺了插件直接被拒）。"""

        # 事件循环引用：MaaCore 回调线程要把它丢回这里。自检/挂机前就会用到。
        self._loop = asyncio.get_running_loop()
        maa_dir = Path(self.config.maa.maa_dir)
        exists = (maa_dir / "MaaCore.dll").exists()
        self.ctx.logger.info(
            "[marisa_maa] 已加载：MAA 目录=%s（%s），窗口=%s，截图方式=%s，长任务每 %s 分钟汇报",
            maa_dir, "存在" if exists else "**缺失**", self.config.maa.window_title,
            self.config.maa.screencap_method, self.config.report.long_task_minutes,
        )

    async def on_unload(self) -> None:
        # 两个常驻任务必须显式取消：heartbeat 早先没保存引用，
        # 卸载后还会继续跑（虽然后来 loop 关闭会连带杀掉，但现在要干净退出）。
        for task in (self._report_task, self._heartbeat_task):
            if task is not None:
                task.cancel()
        self._report_task = None
        self._heartbeat_task = None
        await self._release_driver()

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        self.ctx.logger.info(
            "[marisa_maa] 配置已更新（scope=%s，version=%s）：%s",
            scope, version, "开启" if self.config.plugin.enabled else "关闭",
        )
        # 连接参数可能变了，下次重连。
        # **必须走 _release_driver 销毁实例**：早先这里只写 `self._driver = None`
        # 丢引用不销毁，MAA 的 Win32 控制器会继续活着并霸占游戏窗口
        # （表现为"游戏一点开就自动最小化"）。
        await self._release_driver()

    async def _release_driver(self) -> None:
        """销毁 MAA 实例，把游戏窗口和鼠标交还给主人。

        MaaCore 的 Win32 控制器只要活着就会拿捏游戏窗口/光标（任务卡死重试期间
        表现为"鼠标一直被占用"）。而 close() 内部要等 AsstStop 生效后才能销毁，
        这段等待不能堵插件的事件循环 —— 放线程里执行。
        任务结束、用户说停、配置变更、插件卸载都必须走这里。
        """

        driver = self._driver
        self._driver = None
        if driver is None:
            return
        try:
            await asyncio.to_thread(driver.close)
        except Exception:   # noqa: BLE001 - 必须让失败显式暴露在日志里，不能静默吞掉
            self.ctx.logger.error("[marisa_maa] 销毁 MAA 实例失败，游戏窗口可能仍被占用", exc_info=True)

    # ─────────────────── 驱动器 ───────────────────

    async def _get_driver(self) -> MaaDriver:
        """懒加载并附着游戏窗口。"""

        if self._driver is not None and self._driver.connected():
            return self._driver
        async with self._driver_lock:
            if self._driver is not None and self._driver.connected():
                return self._driver
            if self._driver is not None:
                # 旧实例已失联：覆盖前必须先销毁，否则它的 Win32 控制器还活着
                await self._release_driver()
            maa = self.config.maa
            driver = MaaDriver(
                maa_dir=Path(maa.maa_dir),
                window_title=maa.window_title,
                screencap_method=int(maa.screencap_method),
                input_method=int(maa.input_method),
                keyboard_method=int(maa.keyboard_method),
                on_event=self._on_driver_event_threadsafe,
            )
            await asyncio.to_thread(driver.load)
            self._driver = driver
            return driver

    def _on_driver_event_threadsafe(self, event: Dict[str, Any]) -> None:
        """MaaCore 的回调线程 → asyncio 队列。

        回调跑在核心自己的线程上，**不能直接 await**，必须用 call_soon_threadsafe
        丢回事件循环；回调里也绝不能做耗时的事，否则会拖慢整个核心。
        """

        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._events.put_nowait, event)
        except RuntimeError:
            pass

    # ─────────────────── 汇报（不走 LLM） ───────────────────

    def _describe(self, msg: int, data: Dict[str, Any]) -> Optional[str]:
        """把 MaaCore 回调翻译成一句人话。

        data 的字段结构见 MAA 的「回调消息协议」：任务链级事件带 taskchain/taskid，
        子任务级事件额外带 task（任务名）等。这里只关心任务链级 + 掉落。
        """

        chain = data.get("taskchain") or data.get("task_chain") or "任务"
        if msg == MSG_TASK_CHAIN_START:
            return f"▶ 开始：{chain}"
        if msg == MSG_TASK_CHAIN_COMPLETED:
            return f"✔ 完成：{chain}"
        if msg == MSG_TASK_CHAIN_ERROR:
            # 子任务的最后一次报错原因在 MAA 自己的日志里（D:\MAA\debug\asst.log），
            # 回调里只有任务链名；执行的线程异常等伪造事件会带 why，一并展示。
            why = str(data.get("why") or "")
            suffix = f"（{why}）" if why else "（详情看 MAA 日志：D:\\MAA\\debug\\asst.log）"
            return f"✘ 出错：{chain}{suffix}"
        if msg == MSG_TASK_CHAIN_STOPPED:
            return f"⏹ 已停止：{chain}"
        if msg == MSG_INIT_FAILED:
            return "✘ MaaCore 初始化失败（资源缺失或目录不对，检查插件配置的 maa_dir）"
        if msg == MSG_SUB_TASK_EXTRA_INFO and self.config.report.report_drops:
            what = data.get("what") or ""
            details = data.get("details") or {}
            if what == "StageDrops":
                drops = details.get("drops") or []
                if drops:
                    items = "、".join(
                        f"{d.get('itemName')}×{d.get('quantity')}" for d in drops[:8] if d.get("itemName")
                    )
                    return f"　掉落：{items}" if items else None
            if what in ("RecruitResult", "RecruitTagsDetected"):
                tags = details.get("tags") or []
                if tags:
                    return f"　公招标签：{'、'.join(tags)}"
        return None

    async def _consume_events(self) -> None:
        """事件循环里的消费者：把回调事件变成 QQ 消息。"""

        while True:
            event = await self._events.get()
            if event.get("type") != "maa_message":
                continue
            msg = int(event.get("msg") or 0)
            data = event.get("data") or {}
            self._last_event_at = time.time()
            chain = str(data.get("taskchain") or data.get("task_chain") or "")
            if chain:
                self._current_chain = chain

            if msg == MSG_ALL_TASKS_COMPLETED:
                if not self._farming:
                    # 手动停止/超时强停之后的收尾事件：实例已释放，不重复汇报。
                    await self._release_driver()
                    continue
                self._farming = False
                elapsed = int(time.time() - self._started_at)
                failed = self._chain_failed
                self._chain_failed = False
                if failed:
                    await self._report(
                        f"⚠ 挂机结束（有任务链出错）：用时 {elapsed // 60} 分 {elapsed % 60} 秒，"
                        f"完成 {self._chain_done} 个任务链。详情看 MAA 日志。"
                    )
                else:
                    await self._report(
                        f"🎉 挂机全部完成だぜ！用时 {elapsed // 60} 分 {elapsed % 60} 秒，"
                        f"共 {self._chain_done} 个任务链。"
                    )
                # 完成截图：必须在销毁实例之前截（截完再释放，把鼠标/窗口交还）
                if self.config.report.report_screenshot:
                    await self._send_final_screenshot()
                # 必须销毁 MAA 实例：只要实例活着，Win32 控制器就活着，
                # 会持续拿捏游戏窗口和光标（任务卡死时主人会遇到"鼠标一直被占用"）。
                await self._release_driver()
                # 完成后动作（关机/睡眠/锁屏等）放最后：确保上面的汇报和截图都发出去
                await self._run_post_action(failed=failed)
                continue

            if msg == MSG_TASK_CHAIN_COMPLETED:
                self._chain_done += 1
            if msg == MSG_TASK_CHAIN_ERROR:
                self._chain_failed = True

            # 清日常：每个子任务/任务链都报；长挂机：不逐事件刷屏，
            # 但出错/停止必须立刻报（错误比进度重要，不能吞）。
            long_running = self._is_long_task()
            if long_running:
                if msg in (MSG_TASK_CHAIN_ERROR, MSG_TASK_CHAIN_STOPPED):
                    line = self._describe(msg, data)
                    if line:
                        await self._report(line)
                continue
            if msg in (MSG_TASK_CHAIN_START, MSG_TASK_CHAIN_COMPLETED, MSG_TASK_CHAIN_ERROR,
                       MSG_TASK_CHAIN_STOPPED, MSG_SUB_TASK_EXTRA_INFO):
                if not self.config.report.report_sub_tasks and msg == MSG_SUB_TASK_EXTRA_INFO:
                    continue
                line = self._describe(msg, data)
                if line:
                    await self._report(line)

    def _is_long_task(self) -> bool:
        """当前是否算"长时间挂机"（决定用心跳汇报还是逐个子任务报）。"""

        return self._long_running or self._pending_times >= 20

    async def _heartbeat(self) -> None:
        """进度心跳 + 前台保持 + 总时长兜底。

        三种触发：
        · **沉默期**：任何任务只要超过 SILENCE_SEC 秒没有任何回调，就主动报一声。
          实测 StartUp 在游戏不在主界面时静默跑了 **114 秒**，期间插件一言不发，
          用户以为"没启动"——这是最伤体验的一种失败。
        · **长挂机**：达到 long_task_minutes 间隔再报一次。
        · **总时长上限**：max_run_minutes 到时强制停止并销毁实例。任务卡死时
          MAA 的 Win32 控制器会一直占用鼠标/窗口，这是主人实测遇到的现象；
          上限是"人不在电脑前"时的最后一道保险。

        另外（Seize 模式）：每轮复查游戏是否还在前台——Seize 的真实输入
        发往当前前台窗口，用户中途切走窗口时必须立刻拉回来（否则点击会
        落到别的程序上，既跑偏挂机又有误点风险）。连续拉不回 3 次就警告。
        """

        long_interval = max(60, int(self.config.report.long_task_minutes) * 60)
        max_run_minutes = int(self.config.maa.max_run_minutes)
        while True:
            await asyncio.sleep(15)
            if not self._farming:
                continue
            now = time.time()
            elapsed = int(now - self._started_at)
            silent = now - self._last_event_at if self._last_event_at else elapsed
            # 长任务（RA 循环一类）子任务事件稀疏，沉默阈值放宽到 5 分钟，
            # 否则每 75 秒就报一次"还在跑"，刷屏（实测 RA 一轮就 1-2 分钟）。
            silence_limit = 300 if self._is_long_task() else SILENCE_SEC

            # Seize 前置复查：游戏不在前台就拉回来（用户在用电脑时会感觉被"抢回"，
            # 这是 Seize 模式的预期行为——挂机期间电脑不该被正常使用）。
            if int(self.config.maa.input_method) == 1:
                driver = self._driver
                if driver is not None:
                    restored = await asyncio.to_thread(driver.ensure_foreground)
                    if restored:
                        self._fg_miss_count = 0
                    else:
                        self._fg_miss_count += 1
                        if self._fg_miss_count >= 3:
                            self._fg_miss_count = 0
                            await self._report(
                                "⚠ 连续多次无法把游戏窗口拉回前台。Seize 输入可能点到别的窗口，"
                                "先发 `/maa 停` 最稳妥。"
                            )

            if max_run_minutes > 0 and elapsed >= max_run_minutes * 60:
                self._farming = False
                driver = self._driver
                if driver is not None:
                    try:
                        await asyncio.to_thread(driver.stop)
                    except Exception:   # noqa: BLE001 - 停止失败也要继续走销毁
                        self.ctx.logger.warning("[marisa_maa] 超时停止失败，直接销毁实例", exc_info=True)
                if self.config.report.report_screenshot:
                    await self._send_final_screenshot()
                await self._release_driver()
                await self._report(
                    f"⏰ 挂机已到 {max_run_minutes} 分钟上限，强制停止，鼠标和游戏窗口已交还だぜ。"
                )
                continue

            if silent >= silence_limit:
                self._last_event_at = now
                self._last_report = now
                where = self._current_chain or "准备中"
                await self._report(
                    f"⏳ 还在跑（{elapsed // 60} 分 {elapsed % 60} 秒）：当前 {where}。"
                    "启动阶段有时要一两分钟，别急だぜ。"
                )
                continue

            if not self._is_long_task():
                continue
            if now - self._last_report < long_interval:
                continue
            self._last_report = now
            driver = self._driver
            alive = driver is not None and driver.connected()
            await self._report(
                f"⏳ 挂机进行中：已 {elapsed // 60} 分钟，"
                f"目标 {self._pending_times} 轮，当前 {self._current_chain or '准备中'}，"
                f"状态{'正常' if alive else '**连接异常**'}。"
            )

    async def _send_text(self, stream_id: str, text: str) -> None:
        """向指定会话直发文本（公共发送逻辑）。

        注意 SDK 的签名是 ``send.text(text, stream_id)``（文本在前）；
        早先这里写反了参数顺序，导致所有汇报都发到不存在的会话级失败。
        发送失败只记日志、不中断调用方（汇报链路不能把挂机循环带崩）。
        """

        if not stream_id:
            return
        try:
            await self.ctx.send.text(text, stream_id)
        except Exception as exc:   # noqa: BLE001 - 发送失败要暴露但不该中断循环
            self.ctx.logger.warning("[marisa_maa] 汇报发送失败: %s", exc)

    async def _notify(self, text: str) -> None:
        """即时回复：发到最近交互的会话（命令/Tool 的即时反馈）。"""

        await self._send_text(self._notify_stream, text)

    async def _report(self, text: str) -> None:
        """挂机汇报：固定发到**发起挂机的那次会话**。

        与 _notify 分开的原因：挂机期间用户可能在别的会话发「/maa 进度」或
        别的指令（_notify_stream 会被改走），但完成/出错/进度这类汇报应当
        始终回到发起挂机的会话，否则通知会散落到别的群/私聊。
        """

        await self._send_text(self._report_stream or self._notify_stream, text)

    # ─────────────────── 任务 ───────────────────

    async def _run_tasks(self, tasks: List[Tuple[str, Dict[str, Any]]], times: int,
                         long_running: bool = False) -> None:
        """排队并执行任务（阻塞的 start 放线程里）。

        **必须检查 append_task 的返回值**：MAA 的 AsstAppendTask 失败时返回 0，
        首版忽略它，于是任务根本没进队列、却回了用户"已经开始"——等于说谎。
        """

        driver = await self._get_driver()

        # Seize 输入只发前台窗口：启动前必须先把游戏拉到前台，
        # 拉不到就直接失败（如实报错），绝不带着"输入会点错窗口"的状态开跑。
        if int(self.config.maa.input_method) == 1:
            if not await asyncio.to_thread(driver.ensure_foreground):
                raise RuntimeError(
                    "Seize 输入要求游戏窗口在前台，但拉前台失败。"
                    "请先手动点一下游戏窗口再试。"
                )

        queued: List[int] = []
        for type_name, params in tasks:
            task_id = driver.append_task(type_name, params)
            if not task_id:
                raise RuntimeError(f"任务 {type_name} 入队失败（MAA 返回 0），参数：{params}")
            queued.append(task_id)
        self.ctx.logger.info(
            "[marisa_maa] 已入队 %d 个任务（id=%s），times=%s，long_running=%s",
            len(queued), queued, times, long_running,
        )
        self._pending_times = times
        self._long_running = long_running
        # 固定本轮挂机的通知目标：整个挂机期间的完成/进度/错误汇报都发到这里，
        # 之后即使用户在别的会话发指令（_notify_stream 会被改走）也不会丢。
        self._report_stream = self._notify_stream
        self._started_at = time.time()
        self._last_report = self._started_at
        self._last_event_at = self._started_at
        self._current_chain = ""
        self._chain_done = 0
        self._chain_failed = False
        self._farming = True
        # 两个常驻任务只创建一次并保存引用（on_unload 要取消它们）。
        # 早先 heartbeat 没保存引用，插件卸载后还在跑。
        if self._report_task is None:
            self._report_task = asyncio.create_task(self._consume_events())
        if self._heartbeat_task is None:
            self._heartbeat_task = asyncio.create_task(self._heartbeat())

        def _blocking() -> None:
            try:
                # **必须检查 start() 的返回值**：AsstStart 在"已在运行"等情况下返回
                # False，不检查的话就是"入队成功、什么都没发生"。
                if not driver.start():
                    raise RuntimeError("AsstStart 返回 False（任务未启动）")
            except Exception as exc:   # noqa: BLE001 - 线程里的异常要带回主循环
                loop = self._loop
                if loop is not None and not loop.is_closed():
                    loop.call_soon_threadsafe(
                        self._events.put_nowait,
                        {"type": "maa_message", "msg": MSG_TASK_CHAIN_ERROR,
                         "data": {"taskchain": "执行线程", "why": str(exc)}},
                    )

        self._worker = threading.Thread(target=_blocking, name="marisa-maa-worker", daemon=True)
        self._worker.start()

    def _ensure_extra_tasks_installed(self) -> None:
        """把插件的自定义/覆盖任务链幂等同步到 MAA 的 resource/tasks 目录。

        MaaCore 只在加载资源时扫描 resource/tasks，自定义任务必须放在那里才能
        被任务链引用。MAA 更新会覆盖整个 resource 目录，所以每次下发相关任务前
        按内容比较一次、不一致就重新写入。当前同步两份：
          · marisa_enter_ra.json      —— 生息演算自动进场链（MarisaGoHome/MarisaEnterRA）；
          · marisa_blackflow_fix.json —— 黑流树海适配：覆盖官方 Squad-EnterPoint，
            分队页优先交给官方限时链 BlackFlowTemporary（否则标准链在树海活动
            界面会等一个不存在的 SquadConfirm 按钮、20 次重试后报错，实测）。

        RA 那份 json 的两个坑（已在插件内修正，改之前务必看 resources 里的注释）：
          · `algorithm` 必须写 "MatchTemplate"，写 "TemplateMatch" 会整条任务失效；
          · 模板字段名是 `template`（不是 `templates`），否则 MAA 找不到模板图，
            会直接判定整个资源加载失败（Asst.load 返回 False）。
        """

        tasks_dir = Path(self.config.maa.maa_dir) / "resource" / "tasks"
        for name in ("marisa_enter_ra.json", "marisa_blackflow_fix.json"):
            src = _PLUGIN_DIR / "resources" / name
            if not src.exists():
                raise FileNotFoundError(f"缺少自定义任务链文件：{src}")
            dst = tasks_dir / name
            content = src.read_bytes()
            if dst.exists() and dst.read_bytes() == content:
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(content)
            self.ctx.logger.info("[marisa_maa] 已同步任务链到 %s", dst)

    def _build_tasks(self, intent: str, stage: str, times: int,
                     ra_mode: Optional[int] = None,
                     rg_mode: Optional[str] = None) -> List[Tuple[str, Dict[str, Any]]]:
        """按意图组装 MAA 任务列表。"""

        maa = self.config.maa
        stage = stage or maa.default_stage
        if intent == "reclamation":
            # 生息演算「重启锚点（RelaunchAnchor）」。
            # **参数值以 MaaCore 的 ReclamationConfig 为准（v6.18.0，已用探测脚本实测）**：
            #   theme 必须是 "RelaunchAnchor"（"Fire" 已下线；"Tales" 是另一个主题）；
            #   mode 是 flags 值 —— RA-1=16(1<<4)、RA-4=48(3<<4)、RA-15=32(2<<4)。
            # 插件配置对用户暴露的逻辑值是 0/1/2，这里映射到真实值。
            # **参数必须给全**：只给 theme+mode 时 MaaCore 内部会抛 C++ 异常
            # （ctypes 报 OSError 0xe06d7363），下面是照 GUI 序列化结果补齐的。
            mode_map = {0: 16, 1: 48, 2: 32}
            logical = ra_mode if ra_mode is not None else int(maa.ra_mode)
            mode = mode_map.get(int(logical), 48)
            tasks: List[Tuple[str, Dict[str, Any]]] = []
            if maa.auto_enter_ra:
                # 自动导航（2026-10-11 实机全流程验证通过）：
                #   回主界面（1 秒）→ 进 RA-4 大地图（约 20 秒）→ 官方任务接棒。
                # 不前置 StartUp：StartUp 会把游戏引导回主界面，破坏 RA 起点。
                self._ensure_extra_tasks_installed()
                tasks.append(("Custom", {"task_names": ["MarisaGoHome"]}))
                tasks.append(("Custom", {"task_names": ["MarisaEnterRA"]}))
            tasks.append(("Reclamation", {
                "theme": "RelaunchAnchor",
                "mode": mode,
                "increment_mode": 0,
                "num_craft_batches": int(maa.ra_craft_batches),
                "tools_to_craft": ["荧光棒"],
                "clear_store": False,
            }))
            return tasks
        if intent == "daily" or intent == "linkstart" or intent == "":
            # 清日常 = 一键长草的那套固定流程。
            # **没有独立的 "Visit" 任务类型**：实测 AsstAppendTask("Visit") 返回 0，
            # 会把整条日常链带崩（入队失败）——访问好友是 Mall 的子功能 visit_friends。
            return [
                ("StartUp", {"client_type": "Official", "start_game_enabled": False}),
                ("Recruit", self._recruit_params(4)),
                ("Infrast", self._infrast_params()),
                ("Mall", {"shopping": True, "buy_first": ["招聘许可", "龙门币"], "visit_friends": True}),
                ("Award", {"award": True, "mail": True}),
            ]
        if intent == "recruit":
            return [("Recruit", self._recruit_params(times or 4))]
        if intent == "infrast":
            return [("Infrast", self._infrast_params())]
        if intent == "mall":
            return [("Mall", {"shopping": True, "buy_first": ["招聘许可", "龙门币"]})]
        if intent == "award":
            return [("Award", {"award": True, "mail": True})]
        if intent == "visit":
            # 同日常链：访问好友只能走 Mall 的 visit_friends，不要单独下发 "Visit"
            return [("Mall", {"shopping": False, "visit_friends": True})]
        if intent == "roguelike":
            # 自动肉鸽（黑流树海等）——官方标准 Roguelike 任务：
            #   · 自带从游戏首页进入肉鸽的完整导航（终端→集成战略→选主题→待进入），
            #     游戏停在哪都不用管；
            #   · 参数对齐主人 GUI（theme=BlackFlow、刷等级、难度 2、特勤分队/稳扎稳打/机械师）。
            # ★ 树海活动界面适配：官方标准链进分队页后会等一个树海界面不存在的
            #   SquadConfirm 按钮而报错（2026-10-12 实测）——随资源安装的
            #   marisa_blackflow_fix.json 覆盖 Squad-EnterPoint 交点给官方限时链
            #   BlackFlowTemporary，之后循环「进入→投资→放弃→重开」
            #   （同日晚实机全链路验证通过：首页起步、零人工、自动循环）。
            self._ensure_extra_tasks_installed()
            return [
                ("StartUp", {"client_type": "Official", "start_game_enabled": False}),
                ("Roguelike", self._roguelike_params(rg_mode)),
            ]
        # 默认：理智作战（挂机刷关）
        # series = 连战倍率：「刷十轮」= 倍率调到 10 一次进出打完，而不是进出十次。
        # 映射 series=min(轮数,10) 不会超打：MAA 内部在"已打场次+本轮连战>目标"时会停止。
        return [
            ("StartUp", {"client_type": "Official", "start_game_enabled": False}),
            ("Fight", {
                "stage": stage,
                "times": max(times, 1),
                "series": self._resolve_fight_series(times),
            }),
        ]

    def _infrast_params(self) -> Dict[str, Any]:
        """基建换班参数 —— 对齐 MAA GUI 的 AsstInfrastTask 序列化 + 主人的 GUI 配置。

        **mode=20000（Rotation）**：按游戏内置的基建排班队列换班
        （MaaCore 走 InfrastEnterRotation 并跳过逐个设施挑人），
        而不是 MAA 默认模式的"一个一个选干员"——主人明确要求用队列更换。
        facility 是 MaaCore 的**必填项**（缺失直接入队失败），
        顺序对齐主人 GUI 的房间列表；其余开关照抄 GUI：
        无人机→龙门币、心情阈值 30%、宿舍空余筛选/信赖优先、源石碎片补货、会客室三开关。
        """

        mode_map = {"rotation": 20000, "default": 0}
        mode = mode_map.get(str(self.config.maa.infrast_mode), 20000)
        return {
            "facility": ["Power", "Office", "Control", "Mfg", "Trade", "Reception",
                         "Dorm", "Processing", "Training", "AssistantChange"],
            "drones": "Money",
            "mode": mode,
            "threshold": 0.3,
            "continue_training": False,
            "dorm_notstationed_enabled": True,
            "dorm_trust_enabled": True,
            "replenish": True,
            "reception_message_board": True,
            "reception_clue_exchange": True,
            "reception_send_clue": True,
        }

    def _roguelike_params(self, rg_mode: Optional[str] = None) -> Dict[str, Any]:
        """自动肉鸽参数 —— 对齐 MAA GUI 的 AsstRoguelikeTask 序列化 + 主人配置。

        策略=刷等级、难度 2 级、开局分队=特勤分队、职业组=稳扎稳打、开局干员=机械师。
        黑流三种策略：exp=刷等级(mode 0) / investment=刷源石锭(1) / baby_animal=刷幼兽(30001)。
        """

        maa = self.config.maa
        mode_map = {"exp": 0, "investment": 1, "baby_animal": 30001}
        mode_key = str(rg_mode or maa.roguelike_mode)
        mode = mode_map.get(mode_key, 0)
        params: Dict[str, Any] = {
            "theme": str(maa.roguelike_theme),
            "mode": mode,
            "difficulty": int(maa.roguelike_difficulty),
            "starts_count": 999999,
            "investment_enabled": True,
            "investment_with_more_score": False,
            "investments_count": 999,
            "stop_when_investment_full": False,
            "use_nonfriend_support": False,
        }
        squad = str(maa.roguelike_squad or "").strip()
        if squad:
            params["squad"] = squad
        roles = str(maa.roguelike_roles or "").strip()
        if roles:
            params["roles"] = roles
        core_char = str(maa.roguelike_core_char or "").strip()
        if core_char:
            params["core_char_list"] = [{"name": core_char, "use_support": False}]
        if mode == 0:
            # 刷等级模式的两个可选参数（GUI 序列化在 Exp 下必带）
            params["stop_at_final_boss"] = False
            params["stop_at_max_level"] = False
        return params

    def _resolve_fight_series(self, times: int) -> int:
        """解析刷关连战倍率（游戏内代理指挥倍率 1-10）。

        fight_series 配置值：
          · -2（默认）= 跟随轮数：刷十轮 → 10 连战，一次进出打完；
          · 0  = MAA 自动：按剩余理智选最大倍率；
          · -1 = 不切换：保持游戏里当前的倍率设置；
          · 1~10 = 固定倍率。
        """

        cfg = int(self.config.maa.fight_series)
        if cfg == -2:
            return max(1, min(max(int(times), 1), 10))
        return cfg

    def _recruit_params(self, times: int) -> Dict[str, Any]:
        """公开招募（公招）参数 —— 对齐 MAA GUI 的 Recruit 序列化。

        实测（resources/probe_tasks.py，MaaCore v6.18.0）这些参数全部被接受：
          · refresh / force_refresh：自动刷新掉 3 星标签组合（同 GUI 的
            RefreshLevel3 / ForceRefresh 策略，省时间）；
          · select [4]：4 星标签组合自动出手；confirm [3,4]：3/4 星都确认；
          · set_time + recruitment_time：自动把招募时间设为 9:00（540 分钟）；
          · expedite：不使用加急许可。
        """

        return {
            "refresh": True,
            "force_refresh": True,
            "select": [4],
            "confirm": [3, 4],
            "times": max(int(times), 1),
            "set_time": True,
            "expedite": False,
            "recruitment_time": {"3": 540, "4": 540},
        }

    # ─────────────────── 指令入口 ───────────────────

    @Command(
        "marisa_maa",
        description="远程控制明日方舟挂机：/maa 挂1-7三轮（仅管理员）",
        pattern=cmd_pattern(r"/maa(?:\s+[\s\S]*)?\s*$"),
        permission="operator",
    )
    async def cmd_maa(self, **kwargs: Any) -> tuple:
        if not isinstance(kwargs, dict):
            return False, "", False
        stream_id = str(kwargs.get("stream_id") or "")
        # 命令原文由宿主放在 matched_groups["raw"]（见 cmd_pattern 的命名捕获组）
        matched = kwargs.get("matched_groups")
        raw = str((matched or {}).get("raw") or "") if isinstance(matched, dict) else ""
        if not raw:
            raw = str(kwargs.get("raw") or kwargs.get("content") or "")
        raw = raw.strip()
        body = raw[len("/maa"):].strip() if raw.startswith("/maa") else raw

        self._notify_stream = stream_id
        self._loop = asyncio.get_running_loop()

        if body in ("", "帮助", "help", "?"):
            await self._notify(
                "用法：/maa <你想怎么挂>\n"
                "　刷关：/maa 挂1-7三轮　/maa 刷CE-6五次（“刷几轮”=连战倍率调到几，一次打完）\n"
                "　日常：/maa 清日常（公招+基建+信用商店+领奖+访问）\n"
                "　单项：/maa 公招四次　/maa 基建换班　/maa 信用商店　/maa 领奖　/maa 访问好友\n"
                "　生息演算：/maa RA4（自动进场，需游戏客户端开着）\n"
                "　肉鸽：/maa 肉鸽（黑流树海·自动进入）　也可说 /maa 树海、/maa 黑流\n"
                "　完成后：加「刷完关机」临时关机；/maa 取消关机 可取消\n"
                "　其它：/maa 自检　/maa 停　/maa 截图　/maa 进度"
            )
            return True, "", True

        if body in ("取消关机", "撤销关机", "别关机"):
            try:
                result = await asyncio.to_thread(
                    subprocess.run, ["shutdown", "/a"], capture_output=True,
                )
                if result.returncode == 0:
                    await self._notify("已取消自动关机だぜ。")
                else:
                    await self._notify("当前没有等待中的关机（或取消失败）。")
            except Exception as exc:   # noqa: BLE001
                await self._notify(f"取消失败：{exc}")
            return True, "", True

        if body in ("停", "停止", "stop", "别挂了"):
            driver = self._driver
            stopped = await asyncio.to_thread(driver.stop) if driver else False
            self._farming = False
            # stop() 只是停任务队列。实例必须走 _release_driver 彻底销毁：
            # 否则 Win32 控制器会继续占着游戏窗口和光标（主人看到"鼠标一直被占用"）。
            await self._release_driver()
            await self._notify("已经让它停下来了だぜ。" if stopped else "现在没有在跑的任务。")
            return True, "", True

        if body in ("自检", "检查", "diagnose", "selftest"):
            await self._notify(await self._self_check())
            return True, "", True

        if body in ("截图", "看看", "情况", "screenshot"):
            await self._send_screenshot(stream_id)
            return True, "", True

        if body in ("进度", "status"):
            await self._notify(self._status_text())
            return True, "", True

        parsed = parse_request(body)
        # 本轮临时完成后动作（「刷完关机」等）：每次命令都重新设置，避免上轮残留
        self._post_action_override = str(parsed.get("post_action") or "")
        times = int(parsed.get("times") or 0)
        if times > int(self.config.maa.max_times):
            await self._notify(f"次数太多了（{times}），上限 {self.config.maa.max_times}。")
            return True, "", True
        if self._farming:
            await self._notify("已经在挂着了，先 `/maa 停` 再下新的吧。")
            return True, "", True

        try:
            intent = str(parsed.get("intent") or "")
            tasks = self._build_tasks(
                intent, str(parsed.get("stage") or ""), times,
                ra_mode=parsed.get("ra_mode"),
                rg_mode=parsed.get("rg_mode"),
            )
            await self._run_tasks(tasks, times, long_running=(intent in ("reclamation", "roguelike")))
        except Exception as exc:   # noqa: BLE001 - 失败原因要如实回给用户
            self.ctx.logger.error("[marisa_maa] 启动失败: %s", exc, exc_info=True)
            await self._notify(f"启动失败：{exc}")
            return True, "", True

        plan = "、".join(name for name, _ in tasks)
        await self._notify(f"收到だぜ！开始执行：{plan}" + (f"（{times} 轮）" if times else ""))
        if int(self.config.maa.input_method) == 1:
            await self._notify("（真实输入模式：挂机期间请不要操作电脑，游戏窗口会保持在最前）")
        if parsed.get("intent") == "reclamation" and not self.config.maa.auto_enter_ra:
            await self._notify(
                "提醒：自动进场已关闭（auto_enter_ra=false），需要游戏已停在大地图对应关卡、"
                "右下角出现「开启建设」"
            )
        return True, "", True

    def _status_text(self) -> str:
        if not self._farming:
            return "现在没有在挂机。"
        elapsed = int(time.time() - self._started_at)
        target = f"目标 {self._pending_times} 轮，" if self._pending_times else ""
        return (f"挂机中：已 {elapsed // 60} 分 {elapsed % 60} 秒，"
                f"{target}已完成 {self._chain_done} 个任务链，当前 {self._current_chain or '准备中'}。")

    async def _send_image(self, stream_id: str, shot: bytes) -> bool:
        """把 PNG 截图发到指定会话。返回是否发送成功。

        SDK 签名是 hybrid(segments, stream_id)（消息段在前）——参数顺序反过来会发不出去。
        用 hybrid 而不是 send.image：插件的能力声明里只有 send.text / send.hybrid。
        """

        if not stream_id:
            return False
        image = "base64://" + base64.b64encode(shot).decode()
        try:
            await self.ctx.send.hybrid([{"type": "image", "content": image}], stream_id)
            return True
        except Exception as exc:   # noqa: BLE001
            self.ctx.logger.warning("[marisa_maa] 发送截图失败: %s", exc)
            return False

    async def _send_screenshot(self, stream_id: str) -> None:
        """手动 /maa 截图：现截现发（失败时提示发当前会话）。"""

        driver = self._driver
        if driver is None:
            await self._notify("还没连上游戏，先发一条挂机指令让我连一下。")
            return
        shot = await asyncio.to_thread(driver.screenshot)
        if not shot:
            await self._notify("截图失败了。")
            return
        if not await self._send_image(stream_id, shot):
            await self._notify("截图发不出去（看插件日志）。")

    async def _send_final_screenshot(self) -> None:
        """挂机结束时的收尾截图：必须在销毁实例之前调用。

        完成/出错/超时强停都走这里，画面就是游戏在任务结束那一刻的状态——
        和汇报文字一起发到发起挂机的会话，方便主人一眼看到结果。
        """

        driver = self._driver
        if driver is None:
            return
        try:
            shot = await asyncio.to_thread(driver.screenshot)
        except Exception as exc:   # noqa: BLE001 - 截图失败不该影响收尾流程
            self.ctx.logger.warning("[marisa_maa] 完成截图失败: %s", exc)
            return
        if not shot:
            self.ctx.logger.warning("[marisa_maa] 完成截图失败（取图为空）")
            return
        await self._send_image(self._report_stream or self._notify_stream, shot)

    async def _run_post_action(self, *, failed: bool) -> None:
        """挂机全部完成后的收尾动作（关机/睡眠/休眠/锁屏）。

        对应 MAA GUI 的「完成后操作」——GUI 是自己实现的，MaaCore 没有这个接口，
        所以直连插件只能自己执行 Windows 命令。调用时机：**汇报和截图都发完之后**，
        否则关机就把汇报掐死了。
        单轮临时说法（「刷完关机」）优先于配置；执行过就清掉。
        """

        cfg = self.config.post_action
        action = self._post_action_override or str(cfg.action or "none")
        self._post_action_override = ""
        if action == "none":
            return
        if failed and cfg.skip_on_error:
            await self._report("（本轮有任务链出错，按配置跳过完成后动作）")
            return

        delay = max(0, int(cfg.delay_sec))
        try:
            if action == "shutdown":
                await self._report(
                    f"💤 挂机完成，{delay} 秒后自动关机。想取消就发 `/maa 取消关机`。"
                )
                await asyncio.to_thread(
                    subprocess.run,
                    ["shutdown", "/s", "/t", str(delay), "/c", "Marisa挂机完成"],
                    capture_output=True,
                    check=True,
                )
            elif action == "sleep":
                await self._report("💤 挂机完成，进入睡眠。")
                await asyncio.to_thread(
                    subprocess.run,
                    ["rundll32.exe", "powrprof.dll,SetSuspendState", "Sleep"],
                    capture_output=True,
                    check=True,
                )
            elif action == "hibernate":
                await self._report("💤 挂机完成，进入休眠。")
                await asyncio.to_thread(
                    subprocess.run,
                    ["shutdown", "/h"],
                    capture_output=True,
                    check=True,
                )
            elif action == "lock":
                await self._report("🔒 挂机完成，锁屏。")
                await asyncio.to_thread(
                    subprocess.run,
                    ["rundll32.exe", "user32.dll,LockWorkStation"],
                    capture_output=True,
                    check=True,
                )
            else:
                await self._report(
                    f"未知的完成后动作：{action}（可用 shutdown/sleep/hibernate/lock）"
                )
        except Exception as exc:   # noqa: BLE001 - 执行失败要如实暴露
            self.ctx.logger.error("[marisa_maa] 完成后动作执行失败: %s", exc, exc_info=True)
            await self._report(f"完成后动作执行失败：{exc}")

    async def _self_check(self) -> str:
        """自检：把「莫名其妙跑不起来」的常见原因一次查完。

        实测踩过的门槛（少一条都跑不起来）：
          1) 未提权 → UIPI 拦掉窗口消息，MAA 日志报 error=5；
          2) 游戏窗口不是标准 16:9（1280x720 / 1920x1080）→ 附着失败；
          3) 窗口标题不对（游戏没开或标题变了）→ FindWindow 找不到。
        """

        maa = self.config.maa
        maa_dir = Path(maa.maa_dir)
        lines: List[str] = []

        core = maa_dir / "MaaCore.dll"
        lines.append(f"{'✅' if core.exists() else '❌'} MaaCore.dll：{core}")
        py_bind = maa_dir / "Python" / "asst" / "asst.py"
        lines.append(f"{'✅' if py_bind.exists() else '❌'} Python 绑定：{py_bind}")

        try:
            is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:   # noqa: BLE001 - 权限 API 失败按未提权处理
            is_admin = False
        lines.append(f"{'✅' if is_admin else '❌'} 管理员权限：{is_admin}（未提权时 MAA 日志会报 error=5）")

        user32 = ctypes.windll.user32
        hwnd = int(user32.FindWindowW(None, maa.window_title))
        if not hwnd:
            lines.append(f"❌ 没找到游戏窗口「{maa.window_title}」——游戏没开，或标题变了")
            return "MAA 自检：\n" + "\n".join(lines)

        rect = ctypes.wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rect))
        width = max(rect.right - rect.left, 0)
        height = max(rect.bottom - rect.top, 0)
        is_169 = height > 0 and abs(width / height - 16 / 9) < 0.02
        lines.append(
            f"{'✅' if is_169 else '❌'} 游戏客户区：{width}x{height}"
            "（要求标准 16:9，推荐 1280x720 / 1920x1080；非标准会附着失败）"
        )

        # Seize 模式特有的检查：输入只发前台窗口，先确认能否拉到前台
        if int(maa.input_method) == 1:
            in_foreground = int(user32.GetForegroundWindow()) == hwnd
            lines.append(
                f"{'✅' if in_foreground else 'ℹ'} 游戏窗口前台：{'是' if in_foreground else '否'}"
                "（Seize 输入要求前台，挂机启动时/挂机中会自动拉回）"
            )

        try:
            driver = await self._get_driver()
            shot = await asyncio.to_thread(driver.screenshot)
            if shot:
                lines.append(f"✅ 附着 + 截图：{len(shot)} 字节")
            else:
                lines.append("❌ 附着成功但取图失败（看 D:\\MAA\\debug\\asst.log）")
        except Exception as exc:   # noqa: BLE001 - 自检要把失败原样带出来
            lines.append(f"❌ 附着失败：{exc}")
        finally:
            # 自检也走完整销毁流程，把窗口/鼠标还给主人
            await self._release_driver()
        return "MAA 自检：\n" + "\n".join(lines)

    # ─────────────────── 工具（给模型的自然语言入口） ───────────────────

    @Tool(
        "marisa_maa_farm",
        description=(
            "远程操控明日方舟（PC 客户端）挂机/日常。"
            "支持：理智作战（刷关）、清日常、公开招募（公招/招募/recruit）、基建换班、"
            "信用购物（买东西/商店）、领取奖励（领奖/邮件）、访问好友、生息演算（RA）、"
            "自动肉鸽（黑流树海 roguelike）。"
            "用户说「帮我挂三轮1-7」「刷CE-6五次」「清一下日常」「公招四次」「基建换班」"
            "「买信用商店」「领奖励」「访问好友」「挂生息演算」「挂肉鸽/树海/黑流」"
            "「刷完关机」这类话时调用本工具。"
            "stage 传关卡名（如 1-7、CE-6、LS-6），times 传轮数（会自动用同等连战倍率一次打完），"
            "intent 传任务类型，post_action 传完成后动作（如关机）。"
            "任务完成/出错/超时时插件会自动推送通知和截图，不需要用 wait 工具轮询进度。"
        ),
        parameters=[
            ToolParameterInfo(name="stage", param_type=ToolParamType.STRING,
                              description="关卡名，如 1-7 / CE-6 / LS-6（仅刷关时用）", required=False),
            ToolParameterInfo(name="times", param_type=ToolParamType.INTEGER,
                              description="轮数（刷关）/招募次数（公招，默认 4）", required=False),
            ToolParameterInfo(name="intent", param_type=ToolParamType.STRING,
                              description=(
                                  "任务类型：fight=理智作战/刷关（默认）；daily=清日常（公招+基建+信用+领奖+访问）；"
                                  "recruit=公开招募/公招；infrast=基建换班；mall=信用购物；award=领取奖励；"
                                  "visit=访问好友；reclamation=生息演算（RA）；roguelike=自动肉鸽（黑流树海，自动导航进入）"
                              ), required=False),
            ToolParameterInfo(name="ra_mode", param_type=ToolParamType.INTEGER,
                              description="生息演算模式：0=RA-1、1=RA-4（默认）、2=RA-15", required=False),
            ToolParameterInfo(name="rg_mode", param_type=ToolParamType.STRING,
                              description=("肉鸽策略（intent=roguelike 时用）：exp=刷等级（默认）、"
                                           "investment=刷源石锭、baby_animal=刷幼兽；黑流树海进图后由官方限时链接管"), required=False),
            ToolParameterInfo(name="post_action", param_type=ToolParamType.STRING,
                              description=("挂机完成后的动作（用户说「刷完关机」时传 shutdown）："
                                           "shutdown=关机 / sleep=睡眠 / hibernate=休眠 / lock=锁屏；"
                                           "不填=用配置（默认不执行）"), required=False),
        ],
    )
    async def tool_farm(self, stage: str = "", times: int = 0, intent: str = "fight",
                        ra_mode: Optional[int] = None, rg_mode: str = "",
                        post_action: str = "", **kwargs: Any) -> str:
        stream_id = str(kwargs.get("stream_id") or self._notify_stream or "")
        if stream_id:
            self._notify_stream = stream_id
        self._loop = asyncio.get_running_loop()
        if self._farming:
            return "已经在挂着了，先停下来再下新的。"
        if times > int(self.config.maa.max_times):
            return f"次数太多了（{times}），上限 {self.config.maa.max_times}。"
        # 临时完成后动作（本轮有效），空字符串表示用配置
        self._post_action_override = post_action.strip().lower()
        try:
            tasks = self._build_tasks(intent, stage, times, ra_mode=ra_mode,
                                      rg_mode=rg_mode.strip().lower() or None)
            await self._run_tasks(tasks, times, long_running=(intent in ("reclamation", "roguelike")))
        except Exception as exc:   # noqa: BLE001
            return f"启动失败：{exc}"
        plan = "、".join(name for name, _ in tasks)
        hint = ""
        if intent == "reclamation":
            hint = "（会自动回主界面并进入 RA-4；自动进场关闭时才需手动停在「开启建设」界面）"
        if intent == "roguelike":
            hint = "（自动肉鸽·黑流树海：自动导航进入并循环刷）"
        if int(self.config.maa.input_method) == 1:
            hint += "（真实输入模式：挂机期间请勿操作电脑）"
        if post_action:
            hint += f"（完成后动作：{post_action}）"
        return f"已经开始：{plan}" + (f"（{times} 轮）" if times else "") + hint

    @Tool("marisa_maa_status", description="查看明日方舟挂机的当前进度。")
    async def tool_status(self, **kwargs: Any) -> str:
        del kwargs
        return self._status_text()

    @Tool("marisa_maa_stop", description="停止正在进行的明日方舟挂机。")
    async def tool_stop(self, **kwargs: Any) -> str:
        del kwargs
        driver = self._driver
        ok = await asyncio.to_thread(driver.stop) if driver else False
        self._farming = False
        # 同对话指令里的"停"：必须走 _release_driver 彻底销毁实例，
        # 否则 Win32 控制器会继续占着游戏窗口和光标
        await self._release_driver()
        return "已停止。" if ok else "当前没有在跑的任务。"

    @Tool("marisa_maa_screenshot", description="截一张明日方舟当前画面并发给用户。")
    async def tool_screenshot(self, **kwargs: Any) -> str:
        stream_id = str(kwargs.get("stream_id") or self._notify_stream or "")
        if stream_id:
            self._notify_stream = stream_id
            await self._send_screenshot(stream_id)
            return "截图已发出。"
        return "拿不到会话，没法发图。"


def create_plugin() -> MarisaMaaPlugin:
    """创建插件实例。"""

    return MarisaMaaPlugin()
