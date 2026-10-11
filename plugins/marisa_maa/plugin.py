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
"""

from __future__ import annotations

import asyncio
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

# MaaCore 的 MsgId（见 MAA include/AsstMsg.h / asst/utils.py 的 Message 枚举）
MSG_ALL_TASKS_COMPLETED = 3
MSG_TASK_CHAIN_START = 10000
MSG_TASK_CHAIN_COMPLETED = 10002
MSG_TASK_CHAIN_ERROR = 10003
MSG_SUB_TASK_START = 20000
MSG_SUB_TASK_COMPLETED = 20002
MSG_SUB_TASK_EXTRA_INFO = 20003

# 沉默多久就主动报一声。实测 StartUp 在游戏不在主界面时静默跑了 114 秒，
# 期间插件一言不发，用户直接以为"没启动"——这个值就是为它设的。
SILENCE_SEC = 75


class PluginSectionConfig(PluginConfigBase):
    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="0.1.0", description="配置版本")


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
        default=64,
        description="鼠标输入方式：**64=PostMessageWithCursorPos（推荐）** ——"
                    "把光标移到目标再投递消息，用完还原光标，**不会移动游戏窗口**。"
                    "256=PostMessageWithWindowPos（官方文档推荐，但它靠移动窗口实现，"
                    "表现为窗口乱飞/被最小化，主人实测挂机后游戏再也打不开）。"
                    "1=Seize（真实输入，会抢走鼠标键盘，挂机时电脑没法用）。"
                    "前提：必须提权（未提权时 UIPI 拦掉所有窗口消息，MAA 日志报 error=5），"
                    "且系统 UI 缩放为 100%、游戏分辨率是标准 16:9",
    )
    keyboard_method: int = Field(
        default=2,
        description="键盘输入方式，默认 2=SendMessage（与主人的 MAA GUI 配置一致）",
    )
    default_stage: str = Field(default="", description="没说关卡时用哪个（空=用游戏内当前/上次关卡）")
    max_times: int = Field(default=200, description="单次挂机次数上限（防手滑写错）")
    connect_timeout_sec: float = Field(default=30.0, description="等待附着成功的超时")


class ReportSectionConfig(PluginConfigBase):
    long_task_minutes: int = Field(default=30, description="长挂机每隔多少分钟汇报一次进度")
    report_sub_tasks: bool = Field(default=True, description="清日常时每个子任务完成即报")
    report_drops: bool = Field(default=True, description="汇报里带掉落信息")


class MarisaMaaConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    maa: MaaSectionConfig = Field(default_factory=MaaSectionConfig)
    report: ReportSectionConfig = Field(default_factory=ReportSectionConfig)


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
_TASK_WORDS = (
    ("清日常", "daily"), ("日常", "daily"), ("一键长草", "linkstart"), ("长草", "linkstart"),
    ("招募", "recruit"), ("公招", "recruit"), ("基建", "infrast"), ("换班", "infrast"),
    ("信用", "mall"), ("商店", "mall"), ("采购", "mall"), ("领奖", "award"),
    ("访问", "visit"), ("刷", "fight"), ("打", "fight"), ("挂", "fight"), ("作战", "fight"),
)


def parse_request(text: str) -> Dict[str, Any]:
    """把 `/maa` 后面的自然语言解析成任务描述。

    只覆盖**常见说法**（守卫成本最低的那批），解析不出来就让调用方提示用户换个说法——
    刻意不在这里调模型：一次挂机指令不值得再花一次 LLM 调用，而且规则解析是可预测的。
    """

    raw = (text or "").strip()
    result: Dict[str, Any] = {"intent": "", "stage": "", "times": 0, "raw": raw}
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
        self._notify_stream: str = ""
        self._started_at: float = 0.0
        self._last_report: float = 0.0
        self._last_event_at: float = 0.0    # 最近一次收到回调的时间（沉默检测用）
        self._current_chain: str = ""       # 当前在跑哪条任务链
        self._chain_done: int = 0
        self._farming = False

    # ─────────────────── 生命周期 ───────────────────

    async def on_load(self) -> None:
        """宿主强制要求覆写（plugin_loader 会校验，缺了插件直接被拒）。"""

        maa_dir = Path(self.config.maa.maa_dir)
        exists = (maa_dir / "MaaCore.dll").exists()
        self.ctx.logger.info(
            "[marisa_maa] 已加载：MAA 目录=%s（%s），窗口=%s，截图方式=%s，长任务每 %s 分钟汇报",
            maa_dir, "存在" if exists else "**缺失**", self.config.maa.window_title,
            self.config.maa.screencap_method, self.config.report.long_task_minutes,
        )

    async def on_unload(self) -> None:
        if self._report_task is not None:
            self._report_task.cancel()
            self._report_task = None
        self._release_driver()

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        self.ctx.logger.info(
            "[marisa_maa] 配置已更新（scope=%s，version=%s）：%s",
            scope, version, "开启" if self.config.plugin.enabled else "关闭",
        )
        # 连接参数可能变了，下次重连。
        # **必须走 _release_driver 销毁实例**：早先这里只写 `self._driver = None`
        # 丢引用不销毁，MAA 的 Win32 控制器会继续活着并霸占游戏窗口
        # （表现为"游戏一点开就自动最小化"）。
        self._release_driver()

    def _release_driver(self) -> None:
        """销毁 MAA 实例，把游戏窗口交还给主人。

        PostMsg-WindowPos 模式下 MAA 的 Win32 控制器会移动甚至最小化游戏窗口
        （官方文档原话："窗口会乱飞，截图方式仅 PrintWindow，建议最小化"）。
        所以任务结束、配置变更、插件卸载时都必须销毁实例 —— 只要实例活着，
        控制器就活着，游戏窗口就会被它拿捏，主人手动打开也会被立刻最小化。
        """

        driver = self._driver
        self._driver = None
        if driver is None:
            return
        try:
            driver.close()
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
        """把 MaaCore 回调翻译成一句人话。"""

        if msg == MSG_TASK_CHAIN_START:
            return f"▶ 开始：{data.get('taskchain') or data.get('task_chain') or '任务'}"
        if msg == MSG_TASK_CHAIN_COMPLETED:
            return f"✔ 完成：{data.get('taskchain') or data.get('task_chain') or '任务'}"
        if msg == MSG_TASK_CHAIN_ERROR:
            return f"✘ 出错：{data.get('taskchain') or data.get('task_chain')}（{data.get('why') or ''}）"
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
                self._farming = False
                elapsed = int(time.time() - self._started_at)
                await self._notify(
                    f"🎉 挂机全部完成だぜ！用时 {elapsed // 60} 分 {elapsed % 60} 秒，"
                    f"共 {self._chain_done} 个任务链。"
                )
                # 必须销毁 MAA 实例：PostMsg-WindowPos 模式下 Win32 控制器会移动乃至
                # 最小化游戏窗口（官方描述就是"窗口会乱飞，建议最小化"）。实例活着
                # 控制器就活着，会一直霸着窗口 —— 主人遇到的现象是"游戏一点开就自动最小化"。
                self._release_driver()
                continue

            if msg == MSG_TASK_CHAIN_COMPLETED:
                self._chain_done += 1

            # 清日常：每个子任务/任务链都报；长挂机：只在到点时由心跳报进度
            long_running = self._is_long_task()
            if long_running:
                continue
            if msg in (MSG_TASK_CHAIN_START, MSG_TASK_CHAIN_COMPLETED,
                       MSG_TASK_CHAIN_ERROR, MSG_SUB_TASK_EXTRA_INFO):
                if not self.config.report.report_sub_tasks and msg == MSG_SUB_TASK_EXTRA_INFO:
                    continue
                line = self._describe(msg, data)
                if line:
                    await self._notify(line)

    def _is_long_task(self) -> bool:
        """当前是否算"长时间挂机"（决定用 30 分钟心跳还是逐个子任务报）。"""

        return self._pending_times >= 20

    async def _heartbeat(self) -> None:
        """进度心跳。

        两种触发（都是实测逼出来的）：
        · **沉默期**：任何任务只要超过 SILENCE_SEC 秒没有任何回调，就主动报一声。
          实测 StartUp 在游戏不在主界面时静默跑了 **114 秒**，期间插件一言不发，
          用户以为"没启动"——这是最伤体验的一种失败。
        · **长挂机**：达到 long_task_minutes 间隔再报一次。
        """

        long_interval = max(60, int(self.config.report.long_task_minutes) * 60)
        while True:
            await asyncio.sleep(15)
            if not self._farming:
                continue
            now = time.time()
            elapsed = int(now - self._started_at)
            silent = now - self._last_event_at if self._last_event_at else elapsed

            if silent >= SILENCE_SEC:
                self._last_event_at = now
                self._last_report = now
                where = self._current_chain or "准备中"
                await self._notify(
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
            await self._notify(
                f"⏳ 挂机进行中：已 {elapsed // 60} 分钟，"
                f"目标 {self._pending_times} 轮，当前 {self._current_chain or '准备中'}，"
                f"状态{'正常' if alive else '**连接异常**'}。"
            )

    async def _notify(self, text: str) -> None:
        """直接发消息到会话——**不经过 LLM**（主人要求：汇报链路别浪费 token）。"""

        stream_id = self._notify_stream
        if not stream_id:
            return
        try:
            await self.ctx.send.text(stream_id, text)
        except Exception as exc:   # noqa: BLE001 - 发送失败要暴露但不该中断循环
            self.ctx.logger.warning("[marisa_maa] 汇报发送失败: %s", exc)

    # ─────────────────── 任务 ───────────────────

    async def _run_tasks(self, tasks: List[Tuple[str, Dict[str, Any]]], times: int) -> None:
        """排队并执行任务（阻塞的 start 放线程里）。

        **必须检查 append_task 的返回值**：MAA 的 AsstAppendTask 失败时返回 0，
        首版忽略它，于是任务根本没进队列、却回了用户"已经开始"——等于说谎。
        """

        driver = await self._get_driver()
        queued: List[int] = []
        for type_name, params in tasks:
            task_id = driver.append_task(type_name, params)
            if not task_id:
                raise RuntimeError(f"任务 {type_name} 入队失败（MAA 返回 0），参数：{params}")
            queued.append(task_id)
        self.ctx.logger.info(
            "[marisa_maa] 已入队 %d 个任务（id=%s），times=%s",
            len(queued), queued, times,
        )
        self._pending_times = times
        self._started_at = time.time()
        self._last_report = self._started_at
        self._last_event_at = self._started_at
        self._current_chain = ""
        self._chain_done = 0
        self._farming = True
        if self._report_task is None:
            self._report_task = asyncio.create_task(self._consume_events())
            asyncio.create_task(self._heartbeat())

        def _blocking() -> None:
            try:
                driver.start()
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

    _pending_times: int = 0

    def _build_tasks(self, intent: str, stage: str, times: int) -> List[Tuple[str, Dict[str, Any]]]:
        """按意图组装 MAA 任务列表。"""

        maa = self.config.maa
        stage = stage or maa.default_stage
        if intent == "daily" or intent == "linkstart" or intent == "":
            # 清日常 = 一键长草的那套固定流程
            return [
                ("StartUp", {"client_type": "Official", "start_game_enabled": False}),
                ("Recruit", {"select": [4], "confirm": [3, 4], "times": 4}),
                ("Infrast", {"facility": ["Mfg", "Trade", "Power", "Dorm"], "drones": "Money"}),
                ("Mall", {"shopping": True, "buy_first": ["招聘许可", "龙门币"]}),
                ("Award", {"award": True, "mail": True}),
                ("Visit", {}),
            ]
        if intent == "recruit":
            return [("Recruit", {"select": [4], "confirm": [3, 4], "times": times or 4})]
        if intent == "infrast":
            return [("Infrast", {"facility": ["Mfg", "Trade", "Power", "Dorm"], "drones": "Money"})]
        if intent == "mall":
            return [("Mall", {"shopping": True, "buy_first": ["招聘许可", "龙门币"]})]
        if intent == "award":
            return [("Award", {"award": True, "mail": True})]
        if intent == "visit":
            return [("Visit", {})]
        # 默认：理智作战（挂机刷关）
        return [
            ("StartUp", {"client_type": "Official", "start_game_enabled": False}),
            ("Fight", {"stage": stage, "times": max(times, 1)}),
        ]

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
                "　例：/maa 挂1-7三轮　/maa 刷CE-6五次　/maa 清日常　/maa 公招四次\n"
                "　也说得出「停」「截图」「进度」"
            )
            return True, "", True

        if body in ("停", "停止", "stop", "别挂了"):
            driver = self._driver
            stopped = driver.stop() if driver else False
            self._farming = False
            # stop() 只是停任务队列，实例还在、Win32 控制器还霸着游戏窗口，
            # 所以必须紧接着销毁实例，窗口才会还给主人。
            self._release_driver()
            await self._notify("已经让它停下来了だぜ。" if stopped else "现在没有在跑的任务。")
            return True, "", True

        if body in ("截图", "看看", "情况", "screenshot"):
            await self._send_screenshot(stream_id)
            return True, "", True

        if body in ("进度", "status"):
            await self._notify(self._status_text())
            return True, "", True

        parsed = parse_request(body)
        times = int(parsed.get("times") or 0)
        if times > int(self.config.maa.max_times):
            await self._notify(f"次数太多了（{times}），上限 {self.config.maa.max_times}。")
            return True, "", True
        if self._farming:
            await self._notify("已经在挂着了，先 `/maa 停` 再下新的吧。")
            return True, "", True

        try:
            tasks = self._build_tasks(str(parsed.get("intent") or ""), str(parsed.get("stage") or ""), times)
            await self._run_tasks(tasks, times)
        except Exception as exc:   # noqa: BLE001 - 失败原因要如实回给用户
            self.ctx.logger.error("[marisa_maa] 启动失败: %s", exc, exc_info=True)
            await self._notify(f"启动失败：{exc}")
            return True, "", True

        plan = "、".join(name for name, _ in tasks)
        await self._notify(f"收到だぜ！开始执行：{plan}" + (f"（{times} 轮）" if times else ""))
        return True, "", True

    def _status_text(self) -> str:
        if not self._farming:
            return "现在没有在挂机。"
        elapsed = int(time.time() - self._started_at)
        return (f"挂机中：已 {elapsed // 60} 分 {elapsed % 60} 秒，"
                f"目标 {self._pending_times} 轮，已完成 {self._chain_done} 个任务链。")

    async def _send_screenshot(self, stream_id: str) -> None:
        driver = self._driver
        if driver is None:
            await self._notify("还没连上游戏，先发一条挂机指令让我连一下。")
            return
        shot = await asyncio.to_thread(driver.screenshot)
        if not shot:
            await self._notify("截图失败了。")
            return
        import base64
        image = "base64://" + base64.b64encode(shot).decode()
        try:
            await self.ctx.send.hybrid(stream_id, [{"type": "image", "content": image}])
        except Exception as exc:   # noqa: BLE001
            self.ctx.logger.warning("[marisa_maa] 发截图失败: %s", exc)
            await self._notify(f"截图发不出去：{exc}")

    # ─────────────────── 工具（给模型的自然语言入口） ───────────────────

    @Tool(
        "marisa_maa_farm",
        description=(
            "远程操控明日方舟（PC 客户端）挂机。用户说「帮我挂三轮1-7」「刷CE-6五次」"
            "「清一下日常」这类话时调用。stage 传关卡名（如 1-7、CE-6、LS-6），"
            "times 传战斗次数，intent=daily 表示清日常。"
        ),
        parameters=[
            ToolParameterInfo(name="stage", param_type=ToolParamType.STRING,
                              description="关卡名，如 1-7 / CE-6 / LS-6；清日常可不填", required=False),
            ToolParameterInfo(name="times", param_type=ToolParamType.INTEGER,
                              description="战斗次数", required=False),
            ToolParameterInfo(name="intent", param_type=ToolParamType.STRING,
                              description="daily=清日常；fight=理智作战（默认）", required=False),
        ],
    )
    async def tool_farm(self, stage: str = "", times: int = 0, intent: str = "fight",
                        **kwargs: Any) -> str:
        stream_id = str(kwargs.get("stream_id") or self._notify_stream or "")
        if stream_id:
            self._notify_stream = stream_id
        self._loop = asyncio.get_running_loop()
        if self._farming:
            return "已经在挂着了，先停下来再下新的。"
        if times > int(self.config.maa.max_times):
            return f"次数太多了（{times}），上限 {self.config.maa.max_times}。"
        try:
            tasks = self._build_tasks(intent, stage, times)
            await self._run_tasks(tasks, times)
        except Exception as exc:   # noqa: BLE001
            return f"启动失败：{exc}"
        plan = "、".join(name for name, _ in tasks)
        return f"已经开始：{plan}" + (f"（{times} 轮）" if times else "")

    @Tool("marisa_maa_status", description="查看明日方舟挂机的当前进度。")
    async def tool_status(self, **kwargs: Any) -> str:
        del kwargs
        return self._status_text()

    @Tool("marisa_maa_stop", description="停止正在进行的明日方舟挂机。")
    async def tool_stop(self, **kwargs: Any) -> str:
        del kwargs
        driver = self._driver
        ok = driver.stop() if driver else False
        self._farming = False
        # 同对话指令里的"停"：必须销毁实例，否则 Win32 控制器会继续霸着游戏窗口
        self._release_driver()
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
