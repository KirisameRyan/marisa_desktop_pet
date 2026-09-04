import sys
import random
from PyQt6.QtCore import Qt, QTimer, QPoint, QThread, pyqtSignal
from PyQt6.QtGui import QPainter, QPixmap, QAction, QIcon, QCursor, QGuiApplication
from PyQt6.QtWidgets import (
    QWidget, QMenu, QSystemTrayIcon, QApplication, QInputDialog
)

from .config import CONFIG, ASSETS_DIR
from .sprite_loader import SpriteLoader
from .state_machine import StateMachine, PetState
from .bubble import ComicBubble
from .input_dialog import ChatInputDialog

# 核心系统引用
from core.config_manager import APP_CONFIG
from core.memory import MEMORY_MANAGER
from core.agent import AgentWorker
from core.tts import TTS_ENGINE
from core.tools import ToolExecutor
from core.inspector import ProactiveInspectorWorker
from core.balance import BALANCE_CHECKER
from core.weather import WEATHER_SERVICE
from core.reminders import REMINDER_STORE
from core import ncm_client
from ui.settings_dialog import SettingsDialog


class MarisaDailyWorker(QThread):
    """每日一次：检测当天魔理推荐歌单是否已生成，未生成则后台合成。"""
    speak_signal = pyqtSignal(str)

    def run(self):
        try:
            from core.ncm_recommend import ensure_daily
            res = ensure_daily()
            if res.get("success") and res.get("created"):
                count = res.get("count", 0)
                self.speak_signal.emit(
                    f"今日魔理推荐已出炉（{count}首）！想听就对魔理沙说「播放推荐歌单」，或点菜单直接切过去连播♪"
                )
        except Exception as e:
            print(f"[MarisaDailyWorker] error: {e}")

class MarisaPetWindow(QWidget):
    def __init__(self):
        super().__init__()
        
        # 1. 窗口属性：无边框、置顶、透明、独立子窗
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.SubWindow
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        
        # 尺寸（从 config.json 读取用户设定的缩放倍率）
        CONFIG.scale = float(APP_CONFIG.get("pet", "scale", 1.0))
        target_size = int(CONFIG.sprite_size * CONFIG.scale)
        self.resize(target_size, target_size)

        # 2. 核心模块初始化
        self.loader = SpriteLoader(scale=CONFIG.scale)
        self.sm = StateMachine(self.loader)
        
        # 对话气泡与输入框
        self.bubble = ComicBubble()
        self.chat_input = ChatInputDialog()
        self.chat_input.submitted.connect(self._on_user_message)
        
        # 3. 物理与鼠标交互状态
        self.is_dragging: bool = False
        self.drag_start_pos: QPoint = QPoint()
        self.drag_last_x: int = 0
        self.vy: float = 0.0  # 下落纵向速度
        self.current_worker: AgentWorker = None
        self.inspector_worker: ProactiveInspectorWorker = None
        self.tts_enabled: bool = True
        
        # 当前帧图像缓存
        self.current_pixmap: QPixmap = self.loader.get_frame("shime1.png")

        # 4. 摆放到屏幕右下角任务栏上沿
        self._init_position()

        # 5. 主动画与物理时钟
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._update_loop)
        self.timer.start(CONFIG.tick_interval_ms)

        # 6. 主动视觉巡检定时器
        self.inspect_timer = QTimer(self)
        self.inspect_timer.timeout.connect(self._trigger_proactive_inspect)
        self.auto_inspect_enabled: bool = APP_CONFIG.get("pet", "auto_screen_inspect", True)
        self.inspect_interval_seconds: int = APP_CONFIG.get("pet", "inspect_interval_seconds", 600)
        
        if self.auto_inspect_enabled:
            self.inspect_timer.start(self.inspect_interval_seconds * 1000)

        # 6.1 切歌播报轮询（3 秒检测一次客户端窗口标题变化）
        self._last_song_title: str = ""
        self._last_notify_ts: float = 0.0
        self.song_notify_enabled: bool = APP_CONFIG.get("pet", "song_notify", True)
        self.song_timer = QTimer(self)
        self.song_timer.timeout.connect(self._check_song_change)
        self.song_timer.start(3000)

        # 6.2 整点报时 + 提醒调度（每 15 秒对一次表）
        self.hourly_chime_enabled: bool = APP_CONFIG.get("pet", "hourly_chime", False)
        self._last_minute_mark: str = ""
        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self._tick_clock)
        self.clock_timer.start(15000)

        # 6.3 每日魔理推荐自动刷新（启动后稍等，避开打招呼）
        self.marisa_daily_notify_enabled: bool = APP_CONFIG.get("pet", "marisa_daily_notify", True)
        QTimer.singleShot(20000, self._maybe_run_daily_refresh)

        # 7. 系统托盘与右键菜单
        self._init_tray()

        # 初始打招呼
        rank_name, _ = MEMORY_MANAGER.get_rank()
        QTimer.singleShot(600, lambda: self.say(
            f"哟！我是雾雨魔理沙，普通的魔法使だぜ！★ (羁绊: {rank_name})"
        ))

    def _init_position(self):
        screen = QGuiApplication.primaryScreen()
        avail_geo = screen.availableGeometry()
        x = avail_geo.right() - self.width() - 80
        y = avail_geo.bottom() - self.height() + CONFIG.ground_margin
        self.move(x, y)

    def _get_ground_y(self) -> int:
        screen = QGuiApplication.screenAt(self.geometry().center())
        if not screen:
            screen = QGuiApplication.primaryScreen()
        return screen.availableGeometry().bottom() - self.height() + CONFIG.ground_margin

    def _get_screen_bounds(self):
        screen = QGuiApplication.screenAt(self.geometry().center())
        if not screen:
            screen = QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        return geo.left(), geo.right() - self.width()

    def _init_tray(self):
        self.tray = QSystemTrayIcon(self)
        # 优先使用高分辨率 .ico 作为托盘图标
        ico_path = ASSETS_DIR.parent / "marisa.ico"
        if ico_path.exists():
            self.tray.setIcon(QIcon(str(ico_path)))
        else:
            icon_pix = self.loader.get_frame("shime1.png")
            if not icon_pix.isNull():
                self.tray.setIcon(QIcon(icon_pix))

        # 单击：将魔理沙带到前台；双击：打开魔法配置室
        self.tray.activated.connect(self._on_tray_activated)
        
        tray_menu = QMenu()
        chat_action = QAction("💬 跟魔理沙对话", self)
        chat_action.triggered.connect(self.open_chat_input)

        look_screen_act = QAction("👀 观察当前屏幕", self)
        look_screen_act.triggered.connect(lambda: self._on_user_message("看看我的屏幕上有什么？"))

        music_menu = tray_menu.addMenu("🎵 网易云音乐")
        rec_act = music_menu.addAction("✨ 播放魔理推荐的歌单")
        rec_act.triggered.connect(lambda: self._marisa_recommend_playlist())
        play_like_act = music_menu.addAction("❤️ 播放我喜欢的音乐")
        play_like_act.triggered.connect(lambda: self._play_ncm_playlist_by_name(""))
        my_pl_act = music_menu.addAction("🎧 播放我的歌单…")
        my_pl_act.triggered.connect(lambda: self._pick_playlist_play())
        save_song_act = music_menu.addAction("💾 收藏这首歌到宝箱")
        save_song_act.triggered.connect(lambda: self.say(ToolExecutor.save_current_song_to_playlist()))
        play_fav_act = music_menu.addAction("▶ 播放/切换收藏音乐")
        play_fav_act.triggered.connect(lambda: self._handle_music_action("play"))
        ncm_login_act = music_menu.addAction("📱 扫码登录网易云")
        ncm_login_act.triggered.connect(lambda: self._ncm_qrcode_login())
        next_track_act = music_menu.addAction("⏭ 下一首")
        next_track_act.triggered.connect(lambda: self._handle_music_action("next"))
        prev_track_act = music_menu.addAction("⏮ 上一首")
        prev_track_act.triggered.connect(lambda: self._handle_music_action("prev"))
        song_notify_toggle = music_menu.addAction(f"🔔 切歌播报: {'开' if self.song_notify_enabled else '关'}")
        song_notify_toggle.triggered.connect(lambda: self._toggle_song_notify(song_notify_toggle))
        
        fav_action = QAction("❤️ 查看好感度档案", self)
        fav_action.triggered.connect(self.show_favorability_info)

        inspect_toggle_act = QAction(f"🔍 桌面主动巡检: {'已开启' if self.auto_inspect_enabled else '已关闭'}", self)
        inspect_toggle_act.triggered.connect(lambda: self.toggle_auto_inspect(inspect_toggle_act))

        tts_toggle_act = QAction("🔊 切换语音开关", self)
        tts_toggle_act.triggered.connect(self.toggle_tts)
        
        walk_action = QAction("🚶 自由漫步", self)
        walk_action.triggered.connect(lambda: self.sm.set_state(PetState.WALK))
        
        sit_action = QAction("🪑 坐下休息", self)
        sit_action.triggered.connect(lambda: self.sm.set_state(PetState.SIT))
        
        reset_pos_action = QAction("📍 召回到底部", self)
        reset_pos_action.triggered.connect(self._init_position)

        quit_action = QAction("❌ 退出桌宠", self)
        quit_action.triggered.connect(QApplication.instance().quit)

        tray_menu.addAction(chat_action)
        tray_menu.addAction(look_screen_act)

        weather_action = QAction("☀️ 实时天气播报", self)
        weather_action.triggered.connect(self.show_weather_info)
        tray_menu.addAction(weather_action)

        balance_action = QAction("💳 查询 DeepSeek 余额", self)
        balance_action.triggered.connect(self.show_balance_info)
        tray_menu.addAction(balance_action)

        tray_menu.addMenu(music_menu)
        tray_menu.addAction(fav_action)
        tray_menu.addAction(inspect_toggle_act)
        tray_menu.addAction(tts_toggle_act)

        # 生活助手（提醒 / 整点报时）
        life_menu = tray_menu.addMenu("⏰ 生活助手")
        add_rem_act = life_menu.addAction("🆕 添加提醒（几分钟后…）")
        add_rem_act.triggered.connect(lambda: self._add_reminder_once())
        add_daily_act = life_menu.addAction("🆕 每日定点提醒…")
        add_daily_act.triggered.connect(lambda: self._add_reminder_daily())
        list_rem_act = life_menu.addAction("📋 查看全部提醒")
        list_rem_act.triggered.connect(self._list_reminders)
        del_rem_act = life_menu.addAction("🗑 删除一条提醒…")
        del_rem_act.triggered.connect(self._delete_reminder)
        life_menu.addSeparator()
        chime_toggle = life_menu.addAction(f"🕐 整点报时: {'开' if self.hourly_chime_enabled else '关'}")
        chime_toggle.triggered.connect(lambda: self._toggle_hourly_chime(chime_toggle))

        clip_act = QAction("📋 解释剪贴板内容", self)
        clip_act.triggered.connect(lambda: self._on_user_message("看看我的剪贴板里是什么内容，帮我看看/解释一下"))

        settings_action = QAction("⚙️ 魔法配置室 (GUI设置)", self)
        settings_action.triggered.connect(self.open_settings_dialog)
        tray_menu.addAction(clip_act)
        tray_menu.addAction(settings_action)

        tray_menu.addSeparator()
        tray_menu.addAction(walk_action)
        tray_menu.addAction(sit_action)
        tray_menu.addAction(reset_pos_action)
        tray_menu.addSeparator()
        tray_menu.addAction(quit_action)

        self.tray.setContextMenu(tray_menu)
        self.tray.show()

    def _on_tray_activated(self, reason):
        """托盘图标点击：双击呼出魔法配置室，单击召回桌宠"""
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.open_settings_dialog()
        elif reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._bring_to_front()

    def _bring_to_front(self):
        """将魔理沙带到前台，并让她打个招呼"""
        self.show()
        self.raise_()
        self.activateWindow()
        self.say("喂！本魔法使在这里呢だぜ！★")

    def _handle_music_action(self, action: str):
        res = ToolExecutor.control_cloudmusic(action)
        self.say(res)
        # 播放音乐时魔理沙坐下打节拍
        if action in ("play", "next", "prev"):
            self.sm.set_state(PetState.SIT)

    def _ncm_qrcode_login(self):
        res = ToolExecutor.login_cloudmusic_qrcode()
        self.say(res)

    def _marisa_recommend_playlist(self):
        self.think("正在切到今天的魔理推荐歌单...")
        res = ToolExecutor.marisa_recommend_playlist()
        self.say(res)
        if "魔理推荐" in res and "切" in res:
            self.sm.set_state(PetState.SIT)

    def _maybe_run_daily_refresh(self):
        """启动后检查：当天歌单未生成则后台合成（生成成功且开关开 → 语音提示一句）"""
        if self.is_dragging or self.chat_input.isVisible():
            QTimer.singleShot(60000, self._maybe_run_daily_refresh)
            return
        if self.current_worker and self.current_worker.isRunning():
            return
        if not ncm_client.check_login()[0]:
            return
        self.daily_worker = MarisaDailyWorker()
        self.daily_worker.speak_signal.connect(self._on_daily_ready)
        self.daily_worker.start()

    def _on_daily_ready(self, text: str):
        if not self.marisa_daily_notify_enabled:
            return
        self.say(text)
        self.sm.set_state(PetState.SIT)

    def _play_ncm_playlist_by_name(self, name: str):
        self.think("正在翻开你的歌单...")
        res = ToolExecutor.play_cloudmusic_playlist(name)
        self.say(res)
        if "已开始播放" in res:
            self.sm.set_state(PetState.SIT)

    def _pick_playlist_play(self):
        """弹歌单选单，选中即播第一首。"""
        if not ncm_client.check_login()[0]:
            self.say(ToolExecutor.login_cloudmusic_qrcode())
            return
        pls = ncm_client.user_playlists()
        if not pls:
            self.say("没读到主人的歌单列表だぜ……")
            return
        names = [f"{p['name']}（{p['count']}首）" for p in pls if p.get("count")]
        if not names:
            self.say("主人好像还没有能播的歌单だぜ……")
            return
        names = list(dict.fromkeys(names))[:30]
        choice, ok = QInputDialog.getItem(self, "选一个歌单", "🎧 要播哪个歌单？", names, 0, False)
        if ok and choice:
            real_name = choice.split("（")[0]
            self._play_ncm_playlist_by_name(real_name)

    def _toggle_song_notify(self, action_item: QAction = None):
        self.song_notify_enabled = not self.song_notify_enabled
        APP_CONFIG.data["pet"]["song_notify"] = self.song_notify_enabled
        APP_CONFIG.save()
        if action_item:
            action_item.setText(f"🔔 切歌播报: {'开' if self.song_notify_enabled else '关'}")
        self.say(f"切歌播报已{'开启' if self.song_notify_enabled else '关闭'}だぜ！")

    def _toggle_hourly_chime(self, action_item: QAction = None):
        self.hourly_chime_enabled = not self.hourly_chime_enabled
        APP_CONFIG.data["pet"]["hourly_chime"] = self.hourly_chime_enabled
        APP_CONFIG.save()
        if action_item:
            action_item.setText(f"🕐 整点报时: {'开' if self.hourly_chime_enabled else '关'}")
        self.say(f"整点报时已{'开启' if self.hourly_chime_enabled else '关闭'}だぜ！")

    # ---- 切歌播报 ----
    def _check_song_change(self):
        if not ncm_client.is_cloudmusic_running():
            return
        title = ncm_client.current_title()
        if not title:
            self._last_song_title = ""
            return
        if title == self._last_song_title:
            return
        changed = bool(self._last_song_title)
        self._last_song_title = title
        if not changed or not self.song_notify_enabled:
            return
        # 避免打断对话/拖拽/思考/讲话
        if self.is_dragging or self.chat_input.isVisible():
            return
        if (self.current_worker and self.current_worker.isRunning()) or \
           (self.inspector_worker and self.inspector_worker.isRunning()):
            return
        if self.sm.state in (PetState.THINKING, PetState.SPEAKING, PetState.WALK, PetState.DRAGGED, PetState.FALL):
            return
        import time as _t
        now = _t.time()
        if now - self._last_notify_ts < 8.0:
            return
        self._last_notify_ts = now
        self.say(f"♪ 现在放的是：{title}")

    # ---- 提醒 / 整点报时调度 ----
    def _tick_clock(self):
        from datetime import datetime
        now = datetime.now()
        minute_mark = now.strftime("%Y-%m-%d %H:%M")
        if minute_mark == self._last_minute_mark:
            return
        self._last_minute_mark = minute_mark
        # 整点报时
        if self.hourly_chime_enabled and now.minute == 0:
            self._hourly_chime_line(now.hour)
        # 到期提醒
        fired = REMINDER_STORE.due_now()
        for r in fired:
            self.say(f"⏰ 提醒时间到：{r.get('text', '')}")

    def _hourly_chime_line(self, hour: int):
        from datetime import datetime
        hour_cn = f"{hour:02d}:00"
        if 5 <= hour < 9:
            tail = "新的一天，元气满满地开始吧だぜ！"
        elif 9 <= hour < 12:
            tail = "上午好！要不要泡杯茶继续干活？"
        elif 12 <= hour < 14:
            tail = "该吃午饭了，别饿着肚子施法だぜ！"
        elif 14 <= hour < 18:
            tail = "下午时光，加油吧搭档！"
        elif 18 <= hour < 22:
            tail = "晚上好！注意别熬太晚哦。"
        else:
            tail = "夜深了，该休息了だぜ……"
        self.say(f"🕐 现在是 {hour_cn}，{tail}")

    def _add_reminder_once(self):
        text, ok = QInputDialog.getText(self, "添加提醒", "提醒内容：")
        if not ok or not text.strip():
            return
        opts = ["5 分钟后", "15 分钟后", "30 分钟后", "1 小时后", "2 小时后"]
        mins, ok2 = QInputDialog.getItem(self, "多久后提醒", "选择时间：", opts, 1, False)
        if not ok2:
            return
        minutes_map = {"5 分钟后": 5, "15 分钟后": 15, "30 分钟后": 30, "1 小时后": 60, "2 小时后": 120}
        from datetime import datetime, timedelta
        hm = (datetime.now() + timedelta(minutes=minutes_map[mins])).strftime("%H:%M")
        REMINDER_STORE.add(text.strip(), hm, "once")
        self.say(f"好，{hm} 会提醒主人：「{text.strip()}」だぜ！")

    def _add_reminder_daily(self):
        text, ok = QInputDialog.getText(self, "每日定点提醒", "提醒内容：")
        if not ok or not text.strip():
            return
        hm, ok2 = QInputDialog.getText(self, "每日定点提醒", "每天几点提醒？（24小时制 HH:MM，如 09:30）", text="09:00")
        if not ok2 or len(hm) != 5 or ":" not in hm:
            return
        REMINDER_STORE.add(text.strip(), hm.strip(), "daily")
        self.say(f"好，以后每天 {hm.strip()} 都会提醒主人：「{text.strip()}」だぜ！")

    def _list_reminders(self):
        items = REMINDER_STORE.list_reminders()
        if not items:
            self.say("主人还没有设置任何提醒だぜ。")
            return
        lines = ["⏰ 主人的提醒："]
        for i, r in enumerate(items, 1):
            kind = "每天" if r.get("kind") == "daily" else "单次"
            lines.append(f"{i}. [{kind} {r.get('time')}] {r.get('text')}")
        self.say("\n".join(lines))

    def _delete_reminder(self):
        items = REMINDER_STORE.list_reminders()
        if not items:
            self.say("没有提醒可以删だぜ。")
            return
        labels = [f"{r.get('time')} {('每天' if r.get('kind') == 'daily' else '单次')}：{r.get('text')}" for r in items]
        choice, ok = QInputDialog.getItem(self, "删除提醒", "选择要删除的提醒：", labels, 0, False)
        if ok and choice:
            idx = labels.index(choice)
            REMINDER_STORE.remove(items[idx]["id"])
            self.say("已删除那条提醒だぜ！")

    def toggle_auto_inspect(self, action_item: QAction = None):
        self.auto_inspect_enabled = not self.auto_inspect_enabled
        if self.auto_inspect_enabled:
            self.inspect_timer.start(self.inspect_interval_seconds * 1000)
            status_text = f"开启（每 {self.inspect_interval_seconds // 60} 分钟巡检一次）"
        else:
            self.inspect_timer.stop()
            status_text = "已关闭"

        if action_item:
            action_item.setText(f"🔍 桌面主动巡检: {'已开启' if self.auto_inspect_enabled else '已关闭'}")

        self.say(f"桌面主动巡检已{status_text}だぜ！")

    def set_inspect_interval(self):
        minutes, ok = QInputDialog.getInt(
            self, "自定义巡检间隔",
            "请输入主动巡检间隔时间（单位：分钟，建议5~30分钟）：",
            int(self.inspect_interval_seconds // 60), 1, 180, 1
        )
        if ok:
            self.inspect_interval_seconds = minutes * 60
            APP_CONFIG.data["pet"]["inspect_interval_seconds"] = self.inspect_interval_seconds
            APP_CONFIG.save()
            if self.auto_inspect_enabled:
                self.inspect_timer.start(self.inspect_interval_seconds * 1000)
            self.say(f"巡检间隔已调整为 {minutes} 分钟一次だぜ！★")

    def _trigger_proactive_inspect(self):
        """定时器触发：主动后台截屏分析并搭话"""
        # 如果正在拖拽、正在聊天或用户正在输入，暂缓本次巡检
        if self.is_dragging or self.chat_input.isVisible():
            return
        if self.current_worker and self.current_worker.isRunning():
            return
        if self.inspector_worker and self.inspector_worker.isRunning():
            return

        self.inspector_worker = ProactiveInspectorWorker()
        self.inspector_worker.speak_signal.connect(self.say)
        self.inspector_worker.start()

    def show_balance_info(self):
        """弹出当前 DeepSeek 余额"""
        self.think("正在连接 DeepSeek 查询余额...")
        ok, msg, _ = BALANCE_CHECKER.query_balance()
        if ok:
            self.say(f"报告搭档！你的 DeepSeek 账户{msg}，魔法能量充裕だぜ！★")
        else:
            self.say(f"查询余额遇到问题：{msg}")

    def show_weather_info(self):
        """弹出实时天气"""
        self.think("正在观察天空气象...")
        ok, summary, _ = WEATHER_SERVICE.get_weather()
        if ok:
            self.say(f"{summary}\n出门要注意防护，元气满满地开始吧だぜ！★")
        else:
            self.say(f"天气侦测遇到问题：{summary}")

    def open_settings_dialog(self):
        """打开 PyQt6 原生设置与向导 GUI"""
        TTS_ENGINE.stop()
        dlg = SettingsDialog(self)
        dlg.config_saved.connect(self._on_settings_reloaded)
        dlg.exec()

    def _on_settings_reloaded(self):
        """当用户在 GUI 中修改并保存配置后即时热重载"""
        self.auto_inspect_enabled = APP_CONFIG.get("pet", "auto_screen_inspect", True)
        self.inspect_interval_seconds = APP_CONFIG.get("pet", "inspect_interval_seconds", 600)
        if self.auto_inspect_enabled:
            self.inspect_timer.start(self.inspect_interval_seconds * 1000)
        else:
            self.inspect_timer.stop()
        self.song_notify_enabled = bool(APP_CONFIG.get("pet", "song_notify", True))
        self.hourly_chime_enabled = bool(APP_CONFIG.get("pet", "hourly_chime", False))
        self.marisa_daily_notify_enabled = bool(APP_CONFIG.get("pet", "marisa_daily_notify", True))
        self._apply_scale(float(APP_CONFIG.get("pet", "scale", 1.0)))
        self.say("魔法配置已即时更新并生效だぜ！★")

    def _apply_scale(self, scale: float):
        """实时应用新的桌宠缩放倍率，无需重启"""
        scale = max(0.5, min(2.0, float(scale)))
        if abs(scale - CONFIG.scale) < 0.01:
            return
        CONFIG.scale = scale

        # 重建帧加载器并同步给状态机
        self.loader = SpriteLoader(scale=scale)
        self.sm.loader = self.loader

        # 调整窗口尺寸并重新贴地
        new_size = int(CONFIG.sprite_size * scale)
        self.resize(new_size, new_size)
        ground_y = self._get_ground_y()
        min_x, max_x = self._get_screen_bounds()
        new_x = max(min_x, min(max_x, self.x()))
        self.move(new_x, ground_y)
        self._sync_bubble_pos()
        self.update()

    def toggle_tts(self):
        self.tts_enabled = not self.tts_enabled
        status = "开启" if self.tts_enabled else "关闭"
        if not self.tts_enabled:
            TTS_ENGINE.stop()
        self.say(f"语音播报已{status}だぜ！")

    def show_favorability_info(self):
        fav = MEMORY_MANAGER.get_favorability()
        rank_name, rank_desc = MEMORY_MANAGER.get_rank()
        count = MEMORY_MANAGER.profile.get("interaction_count", 0)
        self.say(f"【好感度】: {fav}/100\n【当前羁绊】: {rank_name}\n【互动次数】: {count} 次★\n{rank_desc}")

    def _update_loop(self):
        dt = CONFIG.tick_interval_ms / 1000.0
        ground_y = self._get_ground_y()
        curr_x = self.x()
        curr_y = self.y()

        # A. 鼠标拖拽中
        if self.is_dragging:
            cursor_pos = QCursor.pos()
            new_pos = cursor_pos - self.drag_start_pos
            dx = cursor_pos.x() - self.drag_last_x
            self.drag_last_x = cursor_pos.x()
            self.sm.update_drag_velocity(dx)
            self.move(new_pos)
            self._sync_bubble_pos()
            self.current_pixmap, _ = self.sm.tick(dt)
            self.update()
            return

        # B. 悬空受重力下落
        if curr_y < ground_y - 2:
            if self.sm.state != PetState.FALL and self.sm.state != PetState.LANDING:
                self.sm.set_state(PetState.FALL)
            
            self.vy = min(self.vy + CONFIG.gravity, CONFIG.max_fall_speed)
            new_y = curr_y + int(self.vy)
            if new_y >= ground_y:
                new_y = ground_y
                self.vy = 0.0
                self.sm.set_state(PetState.LANDING)
            self.move(curr_x, new_y)
            self._sync_bubble_pos()
            self.current_pixmap, _ = self.sm.tick(dt)
            self.update()
            return

        # 保证不陷到任务栏下方
        if curr_y > ground_y:
            self.move(curr_x, ground_y)
            self.vy = 0.0

        # C. 正常地面状态
        self.current_pixmap, dx = self.sm.tick(dt)

        if dx != 0:
            min_x, max_x = self._get_screen_bounds()
            target_x = curr_x + dx
            # 撞墙掉头
            if target_x < min_x:
                target_x = min_x
                self.sm.facing_right = True
            elif target_x > max_x:
                target_x = max_x
                self.sm.facing_right = False
            self.move(target_x, ground_y)
            self._sync_bubble_pos()

        self.update()

    def _sync_bubble_pos(self):
        """让对话气泡与输入框始终跟随在魔理沙头顶正上方"""
        if self.bubble.isVisible():
            bx = self.x() + (self.width() - self.bubble.width()) // 2
            by = self.y() - self.bubble.height() + 10
            self.bubble.move(bx, by)
        if self.chat_input.isVisible():
            ix = self.x() + (self.width() - self.chat_input.width()) // 2
            iy = self.y() - self.chat_input.height() - 10
            self.chat_input.show_at(QPoint(ix, iy))

    def say(self, text: str):
        """让魔理沙说话，弹出漫画气泡、同步播放语音并进入 Speaking 状态"""
        self.sm.set_state(PetState.SPEAKING)
        self.bubble.say(text)
        self._sync_bubble_pos()

        if self.tts_enabled:
            TTS_ENGINE.speak(text)

        # 几秒后如果没在拖拽，恢复 Idle
        dur = max(3.5, len(text) * 0.18)
        QTimer.singleShot(int(dur * 1000), lambda: (
            self.sm.set_state(PetState.IDLE) if self.sm.state == PetState.SPEAKING else None
        ))

    def think(self, text: str = "思考中..."):
        """进入思考状态"""
        TTS_ENGINE.stop()
        self.sm.set_state(PetState.THINKING)
        self.bubble.say(f"💭 {text}")
        self._sync_bubble_pos()

    def open_chat_input(self):
        """弹出聊天输入条"""
        TTS_ENGINE.stop()
        ix = self.x() + (self.width() - self.chat_input.width()) // 2
        iy = self.y() - self.chat_input.height() - 10
        self.chat_input.show_at(QPoint(ix, iy))

    def _on_user_message(self, message: str):
        """收到用户输入：启动后台 AgentWorker 异步调用大模型与工具"""
        self.think("让我想想看...")

        # 停止上一个可能还在运行的 Worker
        if self.current_worker and self.current_worker.isRunning():
            self.current_worker.quit()
            self.current_worker.wait()

        self.current_worker = AgentWorker(message)
        self.current_worker.thinking_signal.connect(self.think)
        self.current_worker.reply_signal.connect(self.say)
        self.current_worker.tool_executed_signal.connect(lambda name, res: print(f"[Tool] {name}: {res}"))
        self.current_worker.start()

    # --- 鼠标互动事件 ---
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_dragging = True
            self.drag_start_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self.drag_last_x = event.globalPosition().toPoint().x()
            self.sm.set_state(PetState.DRAGGED)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.is_dragging:
            self.is_dragging = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            # 松开后开始受重力下落
            self.vy = 0.0
            self.sm.set_state(PetState.FALL)
            event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # 摸头互动检测
            patted, fav = MEMORY_MANAGER.try_pat()
            if patted:
                pat_responses = [
                    f"嘿嘿，摸摸头很舒服だぜ！★ (好感度+1 -> {fav})",
                    f"呜哇！突然摸头，本魔法使也是会害羞的啦！(好感度+1 -> {fav})",
                    f"好搭档！今天也一起在桌面大冒险吧！(好感度+1 -> {fav})"
                ]
                self.say(random.choice(pat_responses))
            else:
                self.open_chat_input()
            event.accept()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #ffffff;
                border: 1px solid #dcdcdc;
                border-radius: 8px;
                padding: 6px;
                font-family: "Microsoft YaHei";
                font-size: 12px;
            }
            QMenu::item {
                padding: 6px 20px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #ffe066;
                color: #222222;
            }
        """)
        chat_act = menu.addAction("💬 对话...")
        chat_act.triggered.connect(self.open_chat_input)

        look_screen_act = menu.addAction("👀 观察当前屏幕")
        look_screen_act.triggered.connect(lambda: self._on_user_message("看看我的电脑屏幕，现在有什么？"))

        weather_act = menu.addAction("☀️ 查询实时天气")
        weather_act.triggered.connect(self.show_weather_info)

        balance_act = menu.addAction("💳 查询 DeepSeek 余额")
        balance_act.triggered.connect(self.show_balance_info)

        music_menu = menu.addMenu("🎵 网易云音乐")
        rec_act = music_menu.addAction("✨ 播放魔理推荐的歌单")
        rec_act.triggered.connect(lambda: self._marisa_recommend_playlist())
        play_like_act = music_menu.addAction("❤️ 播放我喜欢的音乐")
        play_like_act.triggered.connect(lambda: self._play_ncm_playlist_by_name(""))
        my_pl_act = music_menu.addAction("🎧 播放我的歌单…")
        my_pl_act.triggered.connect(lambda: self._pick_playlist_play())
        save_song_act = music_menu.addAction("💾 收藏这首歌到宝箱")
        save_song_act.triggered.connect(lambda: self.say(ToolExecutor.save_current_song_to_playlist()))
        play_fav_act = music_menu.addAction("▶ 播放/切换收藏音乐")
        play_fav_act.triggered.connect(lambda: self._handle_music_action("play"))
        ncm_login_act = music_menu.addAction("📱 扫码登录网易云")
        ncm_login_act.triggered.connect(lambda: self._ncm_qrcode_login())
        next_track_act = music_menu.addAction("⏭ 下一首")
        next_track_act.triggered.connect(lambda: self._handle_music_action("next"))
        prev_track_act = music_menu.addAction("⏮ 上一首")
        prev_track_act.triggered.connect(lambda: self._handle_music_action("prev"))

        fav_act = menu.addAction("❤️ 查看好感度与羁绊")
        fav_act.triggered.connect(self.show_favorability_info)

        inspect_menu = menu.addMenu("🔍 桌面主动巡检")
        inspect_toggle_act = inspect_menu.addAction(f"开关状态: {'【已开启】' if self.auto_inspect_enabled else '【已关闭】'}")
        inspect_toggle_act.triggered.connect(lambda: self.toggle_auto_inspect())
        inspect_set_act = inspect_menu.addAction(f"⏱ 修改间隔时间 (当前: {self.inspect_interval_seconds // 60} 分钟)")
        inspect_set_act.triggered.connect(self.set_inspect_interval)

        settings_act = menu.addAction("⚙️ 魔法配置室 (GUI设置)")
        settings_act.triggered.connect(self.open_settings_dialog)

        tts_act = menu.addAction("🔊 切换语音播报")
        tts_act.triggered.connect(self.toggle_tts)

        life_menu = menu.addMenu("⏰ 生活助手")
        add_rem_act = life_menu.addAction("🆕 添加提醒（几分钟后…）")
        add_rem_act.triggered.connect(lambda: self._add_reminder_once())
        add_daily_act = life_menu.addAction("🆕 每日定点提醒…")
        add_daily_act.triggered.connect(lambda: self._add_reminder_daily())
        list_rem_act = life_menu.addAction("📋 查看全部提醒")
        list_rem_act.triggered.connect(self._list_reminders)
        del_rem_act = life_menu.addAction("🗑 删除一条提醒…")
        del_rem_act.triggered.connect(self._delete_reminder)
        life_menu.addSeparator()
        chime_toggle = life_menu.addAction(f"🕐 整点报时: {'开' if self.hourly_chime_enabled else '关'}")
        chime_toggle.triggered.connect(lambda: self._toggle_hourly_chime(chime_toggle))

        clip_act = menu.addAction("📋 解释剪贴板内容")
        clip_act.triggered.connect(lambda: self._on_user_message("看看我的剪贴板里是什么内容，帮我看看/解释一下"))

        mode_menu = menu.addMenu("🎭 状态切换")
        act_walk = mode_menu.addAction("🚶 漫步模式")
        act_walk.triggered.connect(lambda: self.sm.set_state(PetState.WALK))
        act_sit = mode_menu.addAction("🪑 坐下休息")
        act_sit.triggered.connect(lambda: self.sm.set_state(PetState.SIT))
        act_idle = mode_menu.addAction("🧍 站立待机")
        act_idle.triggered.connect(lambda: self.sm.set_state(PetState.IDLE))

        act_spark = menu.addAction("✨ 发射 Master Spark!")
        act_spark.triggered.connect(lambda: self.say("极限火花！！Master Spark 哔哔哔——！！⚡★"))

        menu.addSeparator()
        recall_act = menu.addAction("📍 召回到底部")
        recall_act.triggered.connect(self._init_position)
        
        quit_act = menu.addAction("❌ 退出")
        quit_act.triggered.connect(QApplication.instance().quit)

        menu.exec(event.globalPos())

    def paintEvent(self, event):
        if not self.current_pixmap.isNull():
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawPixmap(0, 0, self.current_pixmap)

    def closeEvent(self, event):
        TTS_ENGINE.stop()
        if self.inspect_timer.isActive():
            self.inspect_timer.stop()
        if self.song_timer.isActive():
            self.song_timer.stop()
        if self.clock_timer.isActive():
            self.clock_timer.stop()
        self.bubble.close()
        self.chat_input.close()
        super().closeEvent(event)
