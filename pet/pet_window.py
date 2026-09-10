import ctypes
import ctypes.wintypes
import os
import random
import time
from pathlib import Path

from PyQt6.QtCore import (
    Qt, QTimer, QPoint, QAbstractNativeEventFilter,
    QCoreApplication, QUrl,
)
from PyQt6.QtGui import QPainter, QPixmap, QAction, QIcon, QCursor, QGuiApplication, QDesktopServices
from PyQt6.QtWidgets import (
    QWidget, QMenu, QSystemTrayIcon, QApplication, QInputDialog, QMessageBox,
)

from .config import CONFIG, ASSETS_DIR
from .sprite_loader import SpriteLoader
from .state_machine import StateMachine, PetState
from .bubble import ComicBubble
from .confirm_dialog import CleanupConfirmDialog
from .input_dialog import ChatInputDialog

# 核心系统引用（桌宠只做表现层：精灵、气泡、语音播放与本机交互，
# 推理、记忆、好感度、桌面能力全部在 MaiBot 侧，唯一入口是 8400 网关）
from core.config_manager import APP_CONFIG, CONFIG_PATH
from core.recorder import VoiceRecorder
from core.tts import TTS_ENGINE
from core.maibot_client import MaiBotClient
from core import windows as winapi

HOTKEY_ID = 0x9A03
WM_HOTKEY = 0x0312

# 摸头冷却（秒）：好感度由宿主记账，这里只防止连续刷同一个互动
PAT_COOLDOWN_SEC = 300.0

# 桌宠唯一设置入口：一体化 WebUI 的桌宠页
WEBUI_PORT = 8001
WEBUI_PET_URL = f"http://127.0.0.1:{WEBUI_PORT}/pet"


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    """探测本机端口是否已监听（判断 WebUI 是否在跑）。"""

    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class HotkeyFilter(QAbstractNativeEventFilter):
    """监听全局 WM_HOTKEY，触发回调。"""

    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def nativeEventFilter(self, event_type, message):
        if event_type == b"windows_generic_MSG":
            try:
                msg = ctypes.wintypes.MSG.from_address(int(message))
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    self._callback()
            except Exception as e:
                print(f"[Hotkey] filter error: {e}")
        return False, 0


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

        # 清理方案确认弹窗（宿主下发 confirm_request 时弹出）
        self.confirm_dialog = CleanupConfirmDialog()
        self.confirm_dialog.confirmed.connect(self._on_cleanup_confirmed)
        self.confirm_dialog.cancelled.connect(self._on_cleanup_cancelled)

        # 3. MaiBot 一体化大脑客户端（P2：桌面 ↔ QQ 同一颗大脑）
        self.maibot_enabled: bool = bool(APP_CONFIG.get("msg", "maibot_enabled", False))
        self.maibot_ws: str = str(APP_CONFIG.get("msg", "maibot_ws", "ws://127.0.0.1:8400"))
        self._maibot_connected: bool = False
        self.maibot = MaiBotClient(self.maibot_ws)
        if self.maibot_enabled:
            self.maibot.start(
                on_reply=self._on_maibot_reply,
                on_status=self._on_maibot_status,
                on_connected=self._on_maibot_connected,
                on_confirm=self._on_maibot_confirm_request,
                on_confirm_closed=self._on_maibot_confirm_closed,
            )

        # 2.1 语音输入（耳朵）：录音在本机，识别与理解全在 MaiBot 主链
        self.recorder = VoiceRecorder(self)
        self.recorder.finished.connect(self._on_voice_recorded)
        self.recorder.failed.connect(self._on_voice_failed)
        self._last_pat_ts: float = 0.0

        # 3. 物理与鼠标交互状态
        self.is_dragging: bool = False
        self.drag_start_pos: QPoint = QPoint()
        self.drag_last_x: int = 0
        self.vy: float = 0.0  # 下落纵向速度
        self.tts_enabled: bool = True

        # 3.1 爬窗状态（RIDING）
        self.ride_hwnd: int = 0

        # 3.15 窗口目标快照（菜单弹出前捕获，避免焦点被抢）
        self._last_good_app: int = 0
        self._last_good_checked: float = 0.0
        self._menu_fg: int = 0

        # 3.2 拖放魔法删除
        self.setAcceptDrops(True)
        self.magic_delete_enabled: bool = APP_CONFIG.get("pet", "magic_delete", True)
        
        # 当前帧图像缓存
        self.current_pixmap: QPixmap = self.loader.get_frame("shime1.png")

        # 4. 摆放到屏幕右下角任务栏上沿
        self._init_position()

        # 5. 主动画与物理时钟
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._update_loop)
        self.timer.start(CONFIG.tick_interval_ms)

        # 6. 主动视觉巡检与切歌播报均已上移到 MaiBot 侧，桌宠只负责播报推送来的文本
        self.song_notify_enabled: bool = APP_CONFIG.get("pet", "song_notify", True)

        # 6.2 整点报时（每 15 秒对一次表；到点提醒由宿主推送）
        self.hourly_chime_enabled: bool = APP_CONFIG.get("pet", "hourly_chime", False)
        self._last_minute_mark: str = ""
        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self._tick_clock)
        self.clock_timer.start(15000)

        # 6.2.1 配置热重载：WebUI 改完 config.json 后桌宠自动生效，无需重启
        self._config_mtime: float = self._config_file_mtime()
        self.config_watch_timer = QTimer(self)
        self.config_watch_timer.timeout.connect(self._watch_config_file)
        self.config_watch_timer.start(3000)

        # 6.3 每日魔理推荐歌单由宿主侧生成并推送，桌宠只保留提示开关
        self.marisa_daily_notify_enabled: bool = APP_CONFIG.get("pet", "marisa_daily_notify", True)

        # 6.4 主人离开检测（键鼠空闲判定）
        self.presence_watch_enabled: bool = APP_CONFIG.get("pet", "presence_watch", True)
        self.presence_idle_seconds: int = int(APP_CONFIG.get("pet", "presence_idle_seconds", 300))
        self._was_away: bool = False
        self.presence_timer = QTimer(self)
        self.presence_timer.timeout.connect(self._check_presence)
        self.presence_timer.start(5000)

        # 6.5 全局热键（呼叫对话）
        self.hotkey_combo: str = str(APP_CONFIG.get("pet", "hotkey_combo", "ctrl+alt+m"))
        self.hotkey_enabled: bool = bool(APP_CONFIG.get("pet", "hotkey_enable", True))
        self._hotkey_filter = HotkeyFilter(self._on_hotkey_triggered)
        QCoreApplication.instance().installNativeEventFilter(self._hotkey_filter)
        self._apply_hotkey()

        # 6.6 行为导演（Shimeji 化）：地面小动作/看鼠标/贴近窗口整活
        self.zany_auto: bool = bool(APP_CONFIG.get("pet", "zany_auto", True))
        self.zany_level: int = int(APP_CONFIG.get("pet", "zany_level", 1))
        self.ride_kind: str = "top"          # top / bottom / ceiling / side_l / side_r
        self._ride_started_ts: float = 0.0
        self._hang_moving: bool = False
        self._hang_dir: int = 1
        self._action_until: float = 0.0
        self._last_zany_window: float = 0.0
        self._cursor_looking: bool = False
        self._carry_hwnd: int = 0
        self._push_hwnd: int = 0
        self._carry_phase: int = 0
        self._carry_to_floor: bool = False
        self._carry_settled: bool = False
        self._carry_anchor_x: int = 0
        self._carry_corner_left: bool = True
        self._carry_dir: int = 1
        self._carry_finish_at: float = 0.0
        self._push_frames_left: int = 0
        self._carry_until: float = 0.0
        self._hang_move_until: float = 0.0
        self.director_timer = QTimer(self)
        self.director_timer.timeout.connect(self._tick_director)
        self.director_timer.start(600)

        # 6.7 MaiBot 一体化大脑（P2）：连接配置在设置页/托盘开关控制，启动前不再自启旧大脑
        self._managed_astrbot: bool = False

        # 7. 系统托盘与右键菜单
        self._init_tray()

        # 初始打招呼（羁绊与陪伴天数由宿主在回复里给出，这里只报家门）
        QTimer.singleShot(600, lambda: self.say("哟！我是雾雨魔理沙，普通的魔法使！★ 随时喊我だぜ～"))

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

        self.voice_action = QAction("🎤 语音输入（点一下说话）", self)
        self.voice_action.triggered.connect(lambda: self._toggle_voice_input(self.voice_action))

        look_screen_act = QAction("👀 观察当前屏幕", self)
        look_screen_act.triggered.connect(lambda: self._on_user_message("看看我的屏幕上有什么？"))

        music_menu = tray_menu.addMenu("🎵 网易云音乐")
        rec_act = music_menu.addAction("✨ 播放魔理推荐的歌单")
        rec_act.triggered.connect(lambda: self._marisa_recommend_playlist())
        play_like_act = music_menu.addAction("❤️ 播放我喜欢的音乐")
        play_like_act.triggered.connect(lambda: self._play_ncm_playlist_by_name(""))
        my_pl_act = music_menu.addAction("🎧 播放我的歌单…")
        my_pl_act.triggered.connect(lambda: self._play_ncm_playlist_by_name(""))
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
        tray_menu.addAction(self.voice_action)
        tray_menu.addAction(look_screen_act)

        weather_action = QAction("☀️ 实时天气播报", self)
        weather_action.triggered.connect(self.show_weather_info)
        tray_menu.addAction(weather_action)

        balance_action = QAction("💳 查询 DeepSeek 余额", self)
        balance_action.triggered.connect(self.show_balance_info)
        tray_menu.addAction(balance_action)

        tray_menu.addMenu(music_menu)
        tray_menu.addAction(fav_action)
        tray_menu.addAction(tts_toggle_act)

        # MaiBot 一体化大脑开关（P2：桌面 ↔ QQ 同源）
        self.maibot_act = tray_menu.addAction(
            f"🧠 MaiBot 一体化大脑: {'开' if self.maibot_enabled else '关'}"
        )
        self.maibot_act.triggered.connect(self._toggle_maibot)

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

        zany_menu = tray_menu.addMenu("🎭 魔理沙整活")
        self._fill_zany_menu(zany_menu)

        settings_action = QAction("⚙️ 打开 WebUI 设置", self)
        settings_action.triggered.connect(self.open_webui_settings)
        tray_menu.addAction(clip_act)
        tray_menu.addAction(settings_action)

        tray_menu.addSeparator()
        tray_menu.addAction(walk_action)
        tray_menu.addAction(sit_action)
        tray_menu.addAction(reset_pos_action)
        tray_menu.addSeparator()
        tray_menu.addAction(quit_action)

        self.tray_menu = tray_menu
        tray_menu.aboutToShow.connect(self._snapshot_foreground)
        self.tray.setContextMenu(tray_menu)
        self.tray.show()

    def _on_tray_activated(self, reason):
        """托盘图标点击：双击呼出魔法配置室，单击召回桌宠"""
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.open_webui_settings()
        elif reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._bring_to_front()

    def _bring_to_front(self):
        """将魔理沙带到前台，并让她打个招呼"""
        self.show()
        self.raise_()
        self.activateWindow()
        self.say("喂！本魔法使在这里呢！★")

    def _ask_host(self, slash: str) -> None:
        """把一条斜杠指令交给 MaiBot 执行（结果经 WS 回推成气泡 + 语音）。

        桌宠不再自己执行桌面能力：所有功能都在宿主侧实现一份，
        这里只负责把主人的意图送过去。
        """

        if not (self.maibot_enabled and self._maibot_connected):
            self.say("大脑还没连上，先启动一体化だぜ！")
            return
        self.maibot.send(slash)

    def _report_favor(self, reason: str, delta: int = 1) -> None:
        """把本机互动上报给宿主记账（好感度只有宿主那一份）。"""

        if self.maibot_enabled and self._maibot_connected:
            self.maibot.send_favor(delta, reason)

    def _try_pat(self) -> bool:
        """摸头：本机只做冷却判断，好感度交给宿主记账。"""

        now = time.time()
        if now - self._last_pat_ts < PAT_COOLDOWN_SEC:
            return False
        self._last_pat_ts = now
        self._report_favor("摸头互动")
        return True

    def _toggle_voice_input(self, action_item: QAction = None):
        """托盘/菜单：开始或结束语音输入。"""

        if self.recorder.is_recording:
            self.recorder.stop()
            if action_item:
                action_item.setText("🎤 语音输入（点一下说话）")
            return
        if not (self.maibot_enabled and self._maibot_connected):
            self.say("大脑还没连上，等我连上再说话だぜ！")
            return
        if self.recorder.start():
            if action_item:
                action_item.setText("⏹ 正在听…（点一下结束）")
            self.think("在听主人说话…")

    def _on_voice_recorded(self, audio_base64: str, seconds: float) -> None:
        """录音结束：上行给宿主识别，识别结果直接进主链。"""

        self.think("让我听听主人说了什么…")
        self.maibot.send_voice(audio_base64, seconds)

    def _on_voice_failed(self, message: str) -> None:
        self.say(message)

    def _handle_music_action(self, action: str):
        """播放控制：转成斜杠指令交给宿主。"""
        slash_map = {"play": "/播放", "pause": "/暂停", "next": "/下一首", "prev": "/上一首"}
        self._ask_host(slash_map.get(action, "/播放"))
        # 播放音乐时魔理沙坐下打节拍
        if action in ("play", "next", "prev"):
            self.sm.set_state(PetState.SIT)

    def _ncm_qrcode_login(self):
        self._ask_host("/网易云登录")

    def _marisa_recommend_playlist(self):
        self.think("正在切到今天的魔理推荐歌单...")
        self._ask_host("/推荐歌曲")
        self.sm.set_state(PetState.SIT)

    def _play_ncm_playlist_by_name(self, name: str):
        # 歌单播放统一走宿主的「我喜欢的音乐」入口，不再由桌宠列举歌单
        self.think("正在翻开你的歌单...")
        self._ask_host("/我的歌单")
        self.sm.set_state(PetState.SIT)

    def _toggle_song_notify(self, action_item: QAction = None):
        self.song_notify_enabled = not self.song_notify_enabled
        APP_CONFIG.data["pet"]["song_notify"] = self.song_notify_enabled
        APP_CONFIG.save()
        if action_item:
            action_item.setText(f"🔔 切歌播报: {'开' if self.song_notify_enabled else '关'}")
        self.say(f"切歌播报已{'开启' if self.song_notify_enabled else '关闭'}！")

    def _toggle_hourly_chime(self, action_item: QAction = None):
        self.hourly_chime_enabled = not self.hourly_chime_enabled
        APP_CONFIG.data["pet"]["hourly_chime"] = self.hourly_chime_enabled
        APP_CONFIG.save()
        if action_item:
            action_item.setText(f"🕐 整点报时: {'开' if self.hourly_chime_enabled else '关'}")
        self.say(f"整点报时已{'开启' if self.hourly_chime_enabled else '关闭'}！")

    # ---- 切歌播报 ----
    # ---- 提醒 / 整点报时调度 ----
    # 到点提醒由宿主侧扫描并推送（提醒数据在 MaiBot 侧），桌宠只负责播报
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

    def _hourly_chime_line(self, hour: int):
        hour_cn = f"{hour:02d}:00"
        if 5 <= hour < 9:
            tail = "新的一天，元气满满地开始吧！"
        elif 9 <= hour < 12:
            tail = "上午好！要不要泡杯茶继续干活？"
        elif 12 <= hour < 14:
            tail = "该吃午饭了，别饿着肚子施法！"
        elif 14 <= hour < 18:
            tail = "下午时光，加油吧搭档！"
        elif 18 <= hour < 22:
            tail = "晚上好！注意别熬太晚哦。"
        else:
            tail = "夜深了，该休息了……"
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
        self._ask_host(f"/提醒 {minutes_map[mins]} {text.strip()}")

    def _list_reminders(self):
        self._ask_host("/提醒列表")

    def _delete_reminder(self):
        index, ok = QInputDialog.getInt(self, "删除提醒", "要删除第几条提醒？（用 /提醒列表 查看编号）", 1, 1, 99, 1)
        if ok:
            self._ask_host(f"/提醒删除 {index}")

    def open_webui_settings(self):
        """打开 WebUI 桌宠页（桌宠唯一设置入口）。"""
        TTS_ENGINE.stop()
        url = WEBUI_PET_URL
        if not _port_open(WEBUI_PORT):
            self.say("先启动一体化（启动一体化.bat），我才能带主人去 WebUI 配置だぜ！")
            return
        QDesktopServices.openUrl(QUrl(url))
        self.say("配置都挪到 WebUI 啦，主人去那边改就行だぜ★")

    def show_balance_info(self):
        """让宿主查一次 DeepSeek 余额"""
        self.think("正在连接 DeepSeek 查询余额...")
        self._ask_host("/余额")

    def show_weather_info(self):
        """问主人要城市后交给宿主查天气"""
        city, ok = QInputDialog.getText(self, "实时天气", "要查哪座城市？（如 上海 / 东京）")
        if not ok or not city.strip():
            return
        self.think("正在观察天空气象...")
        self._ask_host(f"/天气 {city.strip()}")

    @staticmethod
    def _config_file_mtime() -> float:
        """读取 config.json 的修改时间，缺失时返回 0。"""

        try:
            return CONFIG_PATH.stat().st_mtime
        except OSError:
            return 0.0

    def _watch_config_file(self):
        """配置文件被外部（WebUI）改写时即时热重载。

        桌宠不再是配置入口，WebUI 编辑 config.json 后靠这里生效，
        因此重载保持静默，不弹气泡也不播音。
        """
        current = self._config_file_mtime()
        if current and current != self._config_mtime:
            self._config_mtime = current
            self._on_settings_reloaded(announce=False)

    def _on_settings_reloaded(self, announce: bool = True):
        """当配置被修改后即时热重载（announce=False 时静默生效）"""
        self.song_notify_enabled = bool(APP_CONFIG.get("pet", "song_notify", True))
        self.hourly_chime_enabled = bool(APP_CONFIG.get("pet", "hourly_chime", False))
        self.marisa_daily_notify_enabled = bool(APP_CONFIG.get("pet", "marisa_daily_notify", True))
        self.presence_watch_enabled = bool(APP_CONFIG.get("pet", "presence_watch", True))
        self.presence_idle_seconds = int(APP_CONFIG.get("pet", "presence_idle_seconds", 300))
        self.magic_delete_enabled = bool(APP_CONFIG.get("pet", "magic_delete", True))
        self.hotkey_combo = str(APP_CONFIG.get("pet", "hotkey_combo", "ctrl+alt+m"))
        self.hotkey_enabled = bool(APP_CONFIG.get("pet", "hotkey_enable", True))
        self._apply_hotkey()
        self.zany_auto = bool(APP_CONFIG.get("pet", "zany_auto", True))
        self.zany_level = int(APP_CONFIG.get("pet", "zany_level", 1))
        # 模型密钥不在这里同步：密钥只有 config/model_config.toml 一份，由 WebUI 模型配置页维护
        # MaiBot 一体化大脑配置热重载
        maibot_new_state = bool(APP_CONFIG.get("msg", "maibot_enabled", False))
        if maibot_new_state != self.maibot_enabled:
            self.maibot_enabled = maibot_new_state
            if self.maibot_enabled:
                self.maibot.start(
                    on_reply=self._on_maibot_reply,
                    on_status=self._on_maibot_status,
                    on_connected=self._on_maibot_connected,
                    on_confirm=self._on_maibot_confirm_request,
                    on_confirm_closed=self._on_maibot_confirm_closed,
                )
            else:
                self.maibot.stop()
                self._maibot_connected = False
            if hasattr(self, "maibot_act"):
                self.maibot_act.setText(f"🧠 MaiBot 一体化大脑: {'开' if self.maibot_enabled else '关'}")
        self._apply_scale(float(APP_CONFIG.get("pet", "scale", 1.0)))
        if announce:
            self.say("魔法配置已即时更新并生效！★")

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
        self.say(f"语音播报已{status}！")

    def show_favorability_info(self):
        """好感度、羁绊与陪伴天数都由宿主回答（数据只有那一份）。"""
        self._ask_host("/好感度")

    def _update_loop(self):
        dt = CONFIG.tick_interval_ms / 1000.0
        ground_y = self._get_ground_y()
        curr_x = self.x()
        curr_y = self.y()

        # 0. 搬运窗口 / 推窗 每帧执行
        if self._carry_hwnd and not self.is_dragging:
            self._tick_carry()
        if self._push_hwnd:
            self._tick_push()

        # A. 鼠标拖拽中
        if self.is_dragging:
            cursor_pos = QCursor.pos()
            new_pos = cursor_pos - self.drag_start_pos
            dx = cursor_pos.x() - self.drag_last_x
            self.drag_last_x = cursor_pos.x()
            self.sm.update_drag_velocity(dx)
            self.move(new_pos)
            self._sync_bubble_pos()
            self.current_pixmap, _dxn, _dyn = self.sm.tick(dt)
            self.update()
            return

        # B. 悬空受重力下落（骑乘/吊挂中不触发，交由 B1 跟随）
        if curr_y < ground_y - 2 and not self.ride_hwnd and self.ride_kind != "ceiling":
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
            self.current_pixmap, _dxn, _dyn = self.sm.tick(dt)
            self.update()
            return

        # B1. 骑乘窗口：跟随窗口移动（在拖拽/下落处理之后尽早返回）
        if self.ride_hwnd or self.ride_kind == "ceiling":
            self._sync_ride_position()
            self.current_pixmap, _dxn, _dyn = self.sm.tick(dt)
            self.update()
            return

        # 保证不陷到任务栏下方
        if curr_y > ground_y:
            self.move(curr_x, ground_y)
            self.vy = 0.0

        # C. 正常地面状态
        self.current_pixmap, dx, _dyn = self.sm.tick(dt)

        if dx != 0 and not self._carry_hwnd:
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

        # 几秒后如果没在拖拽，恢复站姿
        dur = max(3.5, len(text) * 0.18)
        QTimer.singleShot(int(dur * 1000), lambda: (
            self.sm.set_idle_pose("stand") if self.sm.state == PetState.SPEAKING else None
        ))

    def _quiet_bubble(self, text: str):
        """只出气泡+语音，不切换状态（骑乘/攀爬/吊挂期间用，避免顶掉动作帧）。"""
        self.bubble.say(text)
        self._sync_bubble_pos()
        if self.tts_enabled:
            TTS_ENGINE.speak(text)
        QTimer.singleShot(6000, lambda: (self.bubble.hide(), self._sync_bubble_pos()))

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

    # ---------------- 主人离开检测 ----------------
    def _check_presence(self):
        if not self.presence_watch_enabled:
            self._was_away = False
            return
        if self.is_dragging or self.ride_hwnd:
            return
        if self.chat_input.isVisible():
            return
        idle = winapi.idle_seconds()
        if not self._was_away and idle >= self.presence_idle_seconds:
            # 刚发现主人离开
            self._was_away = True
            self.sm.set_state(PetState.SIT)
            QTimer.singleShot(2500, self._sleepy_talk)
        elif self._was_away and idle < 3:
            # 主人回来了
            self._was_away = False
            self.sm.set_idle_pose("stand")
            self._greet_return()

    def _sleepy_talk(self):
        if not self._was_away:
            return
        if random.random() < 0.5:
            self.bubble.say("……zZ zZ")
            self._sync_bubble_pos()

    def _greet_return(self):
        from datetime import datetime
        hour = datetime.now().hour
        if hour < 5:
            time_word = "半夜"
        elif hour < 12:
            time_word = "上午"
        elif hour < 14:
            time_word = "中午"
        elif hour < 18:
            time_word = "下午"
        else:
            time_word = "晚上"
        # 好感度越高，回来时的口吻越亲近（具体数值由宿主记账，这里只读一次用于选台词）
        fav = self._cached_favorability()
        if fav >= 80:
            lines = [
                f"欢迎回来！{time_word}好，我刚刚梦见和你去雾之湖钓星星了～★",
                "回来啦！刚才我小憩了一下，梦里的星星比现实还亮！",
            ]
        elif fav >= 50:
            lines = [
                f"回来啦？{time_word}好！我刚刚偷懒眯了一会儿……",
                "欢迎回来！刚打完盹，正好有精神陪你！",
            ]
        else:
            lines = [
                "哦，回来了啊。我……才没有等你呢！",
                f"{time_word}好。你不在的时候我可没偷懒哦！",
            ]
        self._report_favor("主人回来啦")
        self.say(random.choice(lines))

    def _cached_favorability(self) -> int:
        """读一次好感度文件用于选台词（只读；写入一律由宿主负责）。"""

        try:
            import json as _json

            path = Path(__file__).resolve().parent.parent / "data" / "marisa" / "favorability.json"
            return int(_json.loads(path.read_text(encoding="utf-8")).get("favorability", 0))
        except Exception:
            return 0

    # ---------------- 全局热键 ----------------
    HOTKEY_PRESETS = [
        "ctrl+alt+m", "ctrl+alt+space", "ctrl+shift+space", "ctrl+alt+z",
    ]

    def _apply_hotkey(self):
        winapi.unregister_hotkey(int(self.winId()), HOTKEY_ID)
        self._hotkey_registered = False
        self._effective_combo = ""
        self._hotkey_fallback = False
        if not self.hotkey_enabled:
            return
        candidates = [self.hotkey_combo] + [
            p for p in self.HOTKEY_PRESETS if p != self.hotkey_combo
        ]
        for combo in candidates:
            try:
                if winapi.register_hotkey(int(self.winId()), HOTKEY_ID, combo):
                    self._hotkey_registered = True
                    self._effective_combo = combo
                    self._hotkey_fallback = combo != self.hotkey_combo
                    break
            except Exception as e:
                print(f"[Hotkey] register {combo} failed: {e}")
        if not self._hotkey_registered:
            print("[Hotkey] all presets occupied")
        elif self._hotkey_fallback:
            QTimer.singleShot(8000, lambda: self._notify_hotkey_fallback())

    def _notify_hotkey_fallback(self):
        if not self._hotkey_fallback or not self._effective_combo:
            return
        label = self._effective_combo.upper().replace("+", "+")
        self.say(f"你设置的呼叫我热键被别的软件占了，我改用 {label} 啦！可在设置里换一个♪")

    def _maybe_start_astrbot(self):
        """（已退役）旧 AstrBot 托管入口：不再使用。"""
        return

    def _on_hotkey_triggered(self):
        QTimer.singleShot(0, self._hotkey_action)

    def _hotkey_action(self):
        self._bring_to_front()
        self.open_chat_input()

    # ---------------- 爬窗互动 ----------------
    def _climb_foreground(self):
        hwnd = winapi.foreground_window()
        if not hwnd or hwnd == int(self.winId()):
            self.say("前面没有可以爬的窗口？点一下你想让我爬上去的窗口再喊我♪")
            return
        self._attach_ride(hwnd, by_menu=True)

    def _attach_ride(self, hwnd: int, by_menu: bool = False, kind: str = "top"):
        if not hwnd or hwnd == int(self.winId()):
            return
        cls = winapi.window_class_name(hwnd)
        if cls in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            self.say("那可是桌面/任务栏，才不是窗口！")
            return
        if not winapi.is_window_visible(hwnd) or winapi.is_window_minimized(hwnd):
            self.say("那个窗口看不见也爬不了……")
            return
        if winapi.is_fullscreen_primary(hwnd):
            self.say("全屏画面我可不能挡着主人，先老实待在下面！")
            return
        self.ride_hwnd = hwnd
        self.ride_kind = kind
        self._ride_started_ts = time.time()
        self._hang_moving = False
        if kind.startswith("side"):
            # 帧 12-14 原图=趴在窗口右边缘；左边缘用镜像
            self.sm.facing_right = kind == "side_l"
        if kind == "bottom":
            self.sm.set_state(PetState.HANG)
            self.sm.change_hang_motion(False)
        elif kind in ("side_l", "side_r"):
            self.sm.set_state(PetState.CLIMB)
        else:
            self.sm.set_state(PetState.RIDING)
        self._sync_ride_position()
        if by_menu:
            if kind == "bottom":
                self._quiet_bubble("倒吊金钩！本魔法使吊在窗口下面喽！🦇")
            elif kind.startswith("side"):
                self._quiet_bubble("沿墙往上爬，可别手滑！🧗")
            else:
                self._quiet_bubble("哼哼，看本魔法使飞上去！🧗")

    def _detach_ride(self, speak: bool = True):
        if not self.ride_hwnd and not self.ride_kind:
            return
        self.ride_hwnd = 0
        self.ride_kind = "top"
        self._carry_hwnd = 0
        self._push_hwnd = 0
        self.sm.set_idle_pose("stand")
        # 落回地面
        ground_y = self._get_ground_y()
        min_x, max_x = self._get_screen_bounds()
        self.move(max(min_x, min(max_x, self.x())), ground_y)
        self._sync_bubble_pos()
        if speak:
            self.say("跳下来啦！还是地面踏实！")

    def _sync_ride_position(self):
        # 吊天花板：不依赖宿主窗口
        if self.ride_kind == "ceiling":
            screen = QGuiApplication.primaryScreen()
            geo = screen.availableGeometry()
            win_w = self.width()
            nx = self.x()
            if getattr(self, "_hang_moving", False):
                step = 2 if self._hang_dir > 0 else -2
                nx += step
                if nx <= geo.left():
                    nx, self._hang_dir = geo.left(), 1
                elif nx >= geo.right() - win_w:
                    nx, self._hang_dir = geo.right() - win_w, -1
            self.move(nx, geo.top() + 2)
            self._sync_bubble_pos()
            return True
        if not self.ride_hwnd:
            return False
        rect = winapi.window_rect(self.ride_hwnd)
        if not rect or winapi.is_window_minimized(self.ride_hwnd) \
                or not winapi.is_window_visible(self.ride_hwnd):
            self.ride_hwnd = 0
            self.ride_kind = "top"
            self.sm.set_state(PetState.FALL)
            self._quiet_bubble("哦豁，窗口不见了！")
            return False
        if winapi.is_fullscreen_primary(self.ride_hwnd):
            self.ride_hwnd = 0
            self.ride_kind = "top"
            ground_y = self._get_ground_y()
            self.move(self.x(), ground_y)
            self.sm.set_idle_pose("stand")
            self._quiet_bubble("它变全屏了，我先下来了！")
            return False
        left, top, right, bottom = rect
        win_w, win_h = self.width(), self.height()
        screen = QGuiApplication.screenAt(self.geometry().center()) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()

        if self.ride_kind == "bottom":
            # 吊在窗口下沿：手抓窗底，身体荡在下面，可沿边横移
            base_x = self.x()
            if getattr(self, "_hang_moving", False):
                step = 2 if self._hang_dir > 0 else -2
                nx = base_x + step
                if nx <= geo.left():
                    nx, self._hang_dir = geo.left(), 1
                elif nx >= geo.right() - win_w:
                    nx, self._hang_dir = geo.right() - win_w, -1
                self.move(nx, max(bottom - 12, geo.top()))
            else:
                self.move(max(geo.left(), min(geo.right() - win_w,
                                              left + (right - left - win_w) // 2)),
                          max(bottom - 12, geo.top()))
            self._sync_bubble_pos()
            return True

        if self.ride_kind in ("side_l", "side_r"):
            # 趴在窗口右/左边缘（身体横跨窗缘，帧 12-14）
            if self.ride_kind == "side_l":
                target_x = left - win_w // 2
            else:
                target_x = right - win_w // 2
            target_y = self.y() - 2 if self.y() - 2 >= geo.top() else geo.top()
            target_x = max(geo.left(), min(geo.right() - win_w, target_x))
            # 到顶后翻身骑上去
            if self.y() <= top + 4:
                self.ride_kind = "top"
                self.sm.set_state(PetState.RIDING)
                self._quiet_bubble("到顶了！翻身上窗！")
            self.move(target_x, target_y)
            self._sync_bubble_pos()
            return True

        # 默认骑顶：站上窗口上沿并水平居中
        target_x = left + (right - left - win_w) // 2
        target_y = max(top - win_h + 8, geo.top())
        ground = self._get_ground_y()
        if target_y > ground:
            target_y = ground
        target_x = max(geo.left(), min(geo.right() - win_w, target_x))
        self.move(target_x, target_y)
        self._sync_bubble_pos()
        return True

    # ---------------- 行为导演（Shimeji 化地面日常/看鼠标/贴近窗口整活） ----------------
    def _floor_idle_ok(self) -> bool:
        if self.is_dragging or self.ride_hwnd or self.chat_input.isVisible():
            return False
        return self.sm.state in (
            PetState.IDLE, PetState.WALK, PetState.RUN, PetState.CRAWL,
            PetState.SIT, PetState.RELAX, PetState.LIE, PetState.DANGLE,
            PetState.BOUNCE, PetState.TUMBLE,
        )

    def _tick_director(self):
        # 0) 后台记录最近合法应用窗口（整活兜底目标，不受 zany 开关影响）
        try:
            self._refresh_last_good_app()
        except Exception:
            pass
        if not self.zany_auto and self.sm.state in (PetState.IDLE, PetState.SIT):
            return
        try:
            now = time.time()

            # 1) 看鼠标（反应式，不算概率）
            self._handle_cursor_look()

            # 2) 骑乘/吊挂微行为（横移小动作 + 偶发松手）
            if not self._floor_idle_ok():
                if self.sm.state in (PetState.HANG, PetState.RIDING, PetState.CLIMB):
                    self._tick_ride_micro(now)
                return

            # 2) 动作到期收尾 → 回到站姿（含手动模式没限时的移动态）
            if self.sm.state in (PetState.WALK, PetState.RUN, PetState.CRAWL, PetState.DANGLE):
                if not self._action_until or now >= self._action_until:
                    self._action_until = 0.0
                    self.sm.stop_motion()
            elif self.sm.state in (PetState.LIE, PetState.RELAX, PetState.SIT):
                if self._action_until and now >= self._action_until:
                    self._action_until = 0.0
                    self.sm.set_idle_pose("stand")

            if self.sm.state != PetState.IDLE or self._action_until:
                return

            # 3) 克制风地面小动作
            self._roll_floor_action(now)

            # 4) 偶发：贴近窗口时整活（吊/爬/推/扛），有冷却
            if now - self._last_zany_window > 40 and random.random() < 0.4:
                self._last_zany_window = now
                self._maybe_window_zany()
        except Exception as e:
            print(f"[director] error: {e}")

    def _tick_ride_micro(self, now: float):
        """骑乘/吊挂期间的自主小动作。"""
        try:
            # 吊挂：随机开始/停止沿边横移
            if self.sm.state == PetState.HANG:
                if self._hang_moving:
                    if not self._hang_move_until:
                        self._hang_move_until = now + random.uniform(1.2, 3.0)
                    elif now >= self._hang_move_until:
                        self._hang_moving = False
                        self._hang_move_until = 0.0
                        self.sm.change_hang_motion(False)
                elif random.random() < 0.3:
                    self._hang_moving = True
                    self._hang_dir = random.choice([-1, 1])
                    self.sm.change_hang_motion(True)

            # 偶发松手掉落（真吊/骑 20 秒后，2% 几率/次，极低打扰）
            duration = now - self._ride_started_ts
            if duration > 20 and random.random() < 0.02:
                self._release_ride_fall()
        except Exception as e:
            print(f"[ride_micro] error: {e}")

    def _release_ride_fall(self):
        """骑乘/吊挂随机松手 → 自然下坠。"""
        if not self.ride_hwnd and self.ride_kind == "ceiling":
            pass
        self.ride_hwnd = 0
        self.ride_kind = "top"
        self._hang_moving = False
        self._carry_hwnd = 0
        self.vy = 0.0
        self.sm.set_state(PetState.FALL)
        if random.random() < 0.5:
            self._quiet_bubble("呜哇——手滑了！！")

    def _roll_floor_action(self, now: float):
        level = self.zany_level if self.zany_auto else 0
        if level <= 0:
            # 基本待机：站/坐轮换即可
            if random.random() < 0.05:
                self.sm.set_idle_pose(random.choice(["sit", "relax"]))
            return
        mult = 1.0 if level == 1 else 1.8
        r = random.random()
        if r < 0.03 * mult:
            self._action_until = now + random.uniform(3.0, 4.5)
            self.sm.facing_right = random.random() < 0.5
            self.sm.set_state(PetState.RUN)
        elif r < 0.055 * mult:
            self._action_until = now + random.uniform(6.0, 10.0)
            self.sm.set_state(PetState.LIE)
        elif r < 0.08 * mult:
            self._action_until = now + random.uniform(5.0, 8.0)
            self.sm.set_state(PetState.RELAX)
        elif r < 0.105 * mult:
            self._action_until = now + random.uniform(3.5, 5.0)
            self.sm.set_idle_pose("sit")
            self.sm.set_state(PetState.DANGLE)
        elif r < 0.125 * mult:
            self._action_until = now + random.uniform(2.5, 4.0)
            self.sm.set_state(PetState.CRAWL)
        elif r < 0.145 * mult:
            self.sm.play_raw_cycles("跳ねる", cycles=2)
        elif r < 0.2 * mult:
            if random.random() < 0.6:
                self._action_until = now + random.uniform(4.0, 8.0)
                self.sm.facing_right = random.random() < 0.5
                self.sm.set_state(PetState.WALK)
            else:
                self.sm.set_idle_pose(random.choice(["sit", "relax", "stand"]))

    def _handle_cursor_look(self):
        """光标靠近感应圈时转头看她。"""
        if self.is_dragging or self.ride_hwnd or self._carry_hwnd:
            return
        if self.sm.state not in (PetState.IDLE, PetState.SIT, PetState.RELAX,
                                 PetState.LIE, PetState.LOOK):
            return
        cursor = QCursor.pos()
        cx = self.frameGeometry().center().x()
        dist = abs(cursor.x() - cx)
        near = dist < 260 and abs(cursor.y() - (self.y() - 30)) < 220
        if near:
            # 面向光标一侧
            if cursor.x() > cx and not self.sm.facing_right:
                self.sm.facing_right = True
            elif cursor.x() <= cx and self.sm.facing_right:
                self.sm.facing_right = False
            if self.sm.state != PetState.LOOK:
                self._cursor_looking = True
                self.sm.set_state(PetState.LOOK)
        elif self._cursor_looking:
            self._cursor_looking = False
            if self.sm.state == PetState.LOOK:
                self.sm.stop_script_to_idle()

    # ---------------- 窗口整活（自动触发） ----------------
    def _maybe_window_zany(self):
        w = winapi.nearest_window_center(
            self.frameGeometry().center().x(),
            self.frameGeometry().center().y(),
            own_pid=os.getpid(),
        )
        if not w:
            return
        pet = self.frameGeometry()
        cx = pet.center().x()
        side = winapi.window_relative_side(cx, pet.top(), pet.bottom(), w, margin=70)
        roll = random.random()
        if side == "left" and roll < 0.5:
            self._do_push(w["hwnd"], -1, speak=False)
        elif side == "right" and roll < 0.5:
            self._do_push(w["hwnd"], 1, speak=False)
        elif side == "overlap" and roll < 0.3:
            self._do_carry(w["hwnd"], speak=False)
        elif side in ("above", "overlap") and roll < 0.6:
            self._do_hang_bottom(w["hwnd"], speak=False)
        elif side in ("left", "right") and roll < 0.85:
            self._do_side_climb(w["hwnd"], "side_r" if side == "right" else "side_l", speak=False)

    # ---------------- 吊挂/侧爬（手动入口） ----------------
    def _do_hang_bottom(self, hwnd: int, speak: bool = True):
        if not hwnd:
            return
        self._attach_ride(hwnd, kind="bottom")
        if speak and self.ride_hwnd:
            self._quiet_bubble("吊住啦！窗口下面凉快！🦇")

    def _do_hang_ceiling(self, speak: bool = True):
        self.ride_hwnd = 0
        self.ride_kind = "ceiling"
        self._ride_started_ts = time.time()
        self._hang_moving = False
        self.sm.set_state(PetState.HANG)
        self.sm.change_hang_motion(False)
        screen = QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        self.move(self.x(), geo.top() + 2)
        if speak:
            self._quiet_bubble("挂在天花板上偷个懒！🦇")

    def _do_hang_move_toggle(self, moving: bool):
        self.sm.change_hang_motion(moving)

    def _do_side_climb(self, hwnd: int, side: str, speak: bool = True):
        if not hwnd:
            return
        self._attach_ride(hwnd, kind=side)
        if speak and self.ride_hwnd:
            self._quiet_bubble("爬墙特训开始！！")

    # ---------------- 窗口推搡 / 扛窗 ----------------
    def _do_push(self, hwnd: int, direction: int, speak: bool = True):
        if not hwnd:
            return
        self.ride_hwnd = 0
        self._push_hwnd = hwnd
        self._push_frames_left = 50 // 6 + 2
        self.sm.set_idle_pose("stand")
        self.sm.facing_right = direction > 0
        self.sm._set_script(self.sm._poses("push"), loop=False)
        self._pending_push_dir = direction
        if speak:
            lines = ["让让让！这窗户挡着本魔法使看星星了！", "嘿咻——给我挪开点！"]
            self.say(random.choice(lines))

    # ---------------- 小动作/窗口整活 菜单 ----------------
    def _is_good_window(self, hwnd: int) -> bool:
        """校验 hwnd 是否是“可整活的合法窗口”（排除自己进程/桌面/菜单/全屏/过小）。"""
        if not hwnd:
            return False
        try:
            if hwnd == int(self.winId()):
                return False
            if winapi.pid_of_hwnd(hwnd) == os.getpid():
                return False
            cls = winapi.window_class_name(hwnd)
            if cls in winapi.DESKTOP_CLASSES:
                return False
            if not winapi.is_window_visible(hwnd) or winapi.is_window_minimized(hwnd):
                return False
            if winapi.is_fullscreen_primary(hwnd):
                return False
            rect = winapi.window_rect(hwnd)
            if not rect:
                return False
            if (rect[2] - rect[0]) < 60 or (rect[3] - rect[1]) < 40:
                return False
            return True
        except Exception:
            return False

    def _snapshot_foreground(self):
        """菜单弹出前快照当前合法前台窗口。"""
        try:
            fg = winapi.foreground_window()
            if self._is_good_window(fg):
                self._menu_fg = fg
        except Exception:
            pass

    def _refresh_last_good_app(self, force: bool = False):
        """导演静默记录最近一个合法应用窗口（兜底用）。"""
        now = time.time()
        if not force and now - self._last_good_checked < 1.5:
            return
        self._last_good_checked = now
        try:
            fg = winapi.foreground_window()
            if self._is_good_window(fg):
                self._last_good_app = fg
        except Exception:
            pass

    def _current_target_window(self) -> int:
        """整活目标窗口：当前前台 → 菜单快照 → 后台记录，全无效才返回 0。"""
        try:
            fg = winapi.foreground_window()
            if self._is_good_window(fg):
                return fg
        except Exception:
            pass
        if self._is_good_window(self._menu_fg):
            return self._menu_fg
        self._refresh_last_good_app(force=True)
        if self._is_good_window(self._last_good_app):
            return self._last_good_app
        return 0

    def _need_target(self) -> int:
        hwnd = self._current_target_window()
        if not hwnd:
            self.say("附近没找到能整活的窗口……先点一下目标窗口，再让魔理沙干活！")
        return hwnd

    def _fill_zany_menu(self, menu: QMenu):
        a_run = menu.addAction("🏃 跑一会儿")
        a_run.triggered.connect(lambda: self._manual_act(PetState.RUN, 3.5))
        a_lie = menu.addAction("😴 躺平发呆")
        a_lie.triggered.connect(lambda: self._manual_pose("lie", 7.0))
        a_relax = menu.addAction("🪑 放松坐")
        a_relax.triggered.connect(lambda: self._manual_pose("relax", 5.0))
        a_dangle = menu.addAction("🦵 坐着晃脚")
        a_dangle.triggered.connect(lambda: self._manual_pose("sit", 4.0))
        a_bounce = menu.addAction("🤸 蹦跶两下")
        a_bounce.triggered.connect(lambda: self.sm.play_raw_cycles("跳ねる", cycles=2))
        menu.addSeparator()

        if self.ride_hwnd or self.ride_kind == "ceiling":
            a_down = menu.addAction("🧚 跳下来")
            a_down.triggered.connect(lambda: self._detach_ride())
            menu.addSeparator()

        # 窗口动作：点击时才现取前台窗口
        a_top = menu.addAction("🧗 爬到前台窗口顶上")
        a_top.triggered.connect(lambda: self._do_need(self._manual_top))
        a_hang = menu.addAction("🦇 吊到前台窗口下面")
        a_hang.triggered.connect(lambda: self._do_need(self._do_hang_bottom))
        a_climb = menu.addAction("🧗 趴在前台窗口边缘上爬")
        a_climb.triggered.connect(lambda: self._do_need(self._manual_side_climb))
        a_push = menu.addAction("💢 推开前台窗口")
        a_push.triggered.connect(lambda: self._do_need(self._manual_push))
        a_carry = menu.addAction("🧳 扛着前台窗口走")
        a_carry.triggered.connect(lambda: self._do_need(lambda h: self._do_carry(h)))
        a_carry_down = menu.addAction("🪂 抱着前台窗口落地")
        a_carry_down.triggered.connect(lambda: self._do_need(self._manual_carry_down))
        a_ceil = menu.addAction("🦇 吊到天花板")
        a_ceil.triggered.connect(lambda: self._do_hang_ceiling())

    def _do_need(self, fn):
        hwnd = self._need_target()
        if hwnd:
            fn(hwnd)

    def _manual_side_climb(self, hwnd: int):
        rect = winapi.window_rect(hwnd)
        if not rect:
            return
        cx = self.frameGeometry().center().x()
        side = "side_r" if cx >= (rect[0] + rect[2]) // 2 else "side_l"
        self._do_side_climb(hwnd, side)

    def _manual_top(self, hwnd: int):
        self._attach_ride(hwnd, by_menu=True, kind="top")

    def _do_master_spark(self):
        """播放 42-46 帧 Master Spark 施放演出（不打断自己状态机恢复）。"""
        self._ensure_floor()
        self.sm.play_raw_cycles("分裂1", cycles=2)
        self.bubble.say("极限火花！！Master Spark 哔哔哔——！！⚡★")
        self._sync_bubble_pos()
        if self.tts_enabled:
            TTS_ENGINE.speak("极限火花！！Master Spark 哔哔哔——！")
        QTimer.singleShot(5200, lambda: (self.bubble.hide(), self._sync_bubble_pos()))

    def _manual_act(self, state, seconds):
        self._ensure_floor()
        self._action_until = time.time() + seconds
        self.sm.facing_right = random.random() < 0.5
        self.sm.set_state(state)

    def _manual_pose(self, pose: str, seconds: float):
        self._ensure_floor()
        self._action_until = time.time() + seconds
        self.sm.set_idle_pose(pose)

    def _pick_side(self, w: dict) -> str:
        if isinstance(w, int):
            rect = winapi.window_rect(w)
            w = {"hwnd": w, "left": rect[0], "right": rect[2]} if rect else None
        cx = self.frameGeometry().center().x()
        if w and cx < w["left"]:
            return "side_l"
        return "side_r"

    def _manual_push(self, hwnd: int):
        rect = winapi.window_rect(hwnd)
        if not rect:
            self.say("那个窗口已经不见了……")
            return
        cx = self.frameGeometry().center().x()
        direction = 1 if cx < (rect[0] + rect[2]) // 2 else -1
        self._do_push(hwnd, direction)

    def _ensure_floor(self):
        """终止骑乘/抱窗/推窗，回到地面站姿（动作开始前调用）。"""
        if self.ride_hwnd or self.ride_kind in ("ceiling", "bottom", "side_l", "side_r", "top"):
            self._detach_ride(speak=False)
        if self._carry_hwnd:
            self._cancel_carry()
        self._push_hwnd = 0
        self.sm.set_idle_pose("stand")

    def _manual_carry_down(self, hwnd: int):
        if not self._do_carry(hwnd, speak=False, to_floor=True):
            return
        self.say("抱稳了！跳！🪂")

    def _do_carry(self, hwnd: int, speak: bool = True, to_floor: bool = False):
        """扛窗/抱窗落地：先跑到窗口下角→抱持角锚平移→47 帧收尾。
        普通搬运要求窗口底边够得着；落地模式允许窗口悬高（把它拽下来）。"""
        if not hwnd:
            return False
        if winapi.is_fullscreen_primary(hwnd) or winapi.is_window_minimized(hwnd):
            return False
        rect = winapi.window_rect(hwnd)
        if not rect:
            return False
        self._ensure_floor()

        ground_y = self._get_ground_y()
        gap = ground_y - rect[3]
        if not to_floor and gap > 260:
            to_floor = True  # 够不着角角 → 自动改成把它拽下来

        wleft, wtop, wright, wbottom = rect
        pet_cx = self.frameGeometry().center().x()
        # 抱离自己最近的底角
        self._carry_corner_left = (pet_cx < (wleft + wright) // 2)
        corner_x = wleft if self._carry_corner_left else wright
        geo = QGuiApplication.primaryScreen().availableGeometry()
        # 窗口角能跑到才去贴角；否则原地起手用“偏移搬运”（跨屏窗口也能拖）
        if geo.left() + 66 <= corner_x <= geo.right() - 66:
            self._carry_phase = 0
            self._carry_anchor_x = corner_x
        else:
            self._carry_phase = 1
            self._carry_dir = -1 if self.x() > (geo.left() + geo.right()) // 2 else 1
            self.sm.set_state(PetState.CARRY)
            self.sm.set_carry_fast(True)
            self.sm.facing_right = self._carry_dir > 0
            self._carry_until = time.time() + random.uniform(3.2, 4.6)
            self._carry_offset = wleft - self.x()
        self._carry_hwnd = hwnd
        self._carry_to_floor = to_floor
        self._carry_settled = False
        if speak:
            self.say("跑到窗边……准备扛起它！")
        return True

    def _tick_carry(self):
        """抱窗帧执行：跑向角落→抱持快速平移→47 收尾（落地可选）。"""
        if not self._carry_hwnd:
            return
        hwnd = self._carry_hwnd
        if not winapi.is_window_visible(hwnd) or winapi.is_window_minimized(hwnd):
            self._cancel_carry()
            return
        rect = winapi.window_rect(hwnd)
        if not rect:
            self._cancel_carry()
            return

        pet_cx = self.frameGeometry().center().x()
        win_w = self.width()
        ground_y = self._get_ground_y()

        # 阶段0：水平跑到目标角下方
        if self._carry_phase == 0:
            geo = QGuiApplication.primaryScreen().availableGeometry()
            left_max = geo.left()
            right_max = geo.right() - win_w
            if abs(pet_cx - self._carry_anchor_x) <= 10:
                self._carry_phase = 1
                self._carry_dir = -1 if self.x() > (geo.left() + geo.right()) // 2 else 1
                self.sm.set_state(PetState.CARRY)
                self.sm.set_carry_fast(True)
                self.sm.facing_right = self._carry_dir > 0
                self._carry_until = time.time() + random.uniform(3.2, 4.6)
                self._carry_offset = winapi.window_rect(self._carry_hwnd)[0] - self.x()
                return
            step = 10 if pet_cx < self._carry_anchor_x else -10
            if self.sm.state != PetState.WALK:
                self.sm.facing_right = step > 0
                self.sm.set_state(PetState.WALK)
            nx = max(left_max, min(right_max, self.x() + step))
            self.move(nx, self.y())
            return

        # 阶段1：抱持角锚快速平移，窗口角始终在“怀里”
        if self._carry_phase == 1:
            geo = QGuiApplication.primaryScreen().availableGeometry()
            left_max = geo.left()
            right_max = geo.right() - win_w
            nx = self.x() + self._carry_dir * 12
            if nx <= left_max or nx >= right_max:
                self._carry_phase = 2          # 到屏边→收尾
            else:
                self.move(nx, self.y())
                self.sm.facing_right = self._carry_dir > 0
                if self.sm.state != PetState.CARRY:
                    self.sm.set_state(PetState.CARRY)
                self.sm.set_carry_fast(True)
                # 窗口保持与魔理沙的固定偏移一起平移
                winapi.move_window_instantly(hwnd, self._carry_offset + nx, rect[1])
                if time.time() >= self._carry_until:
                    self._carry_phase = 2
            return

        # 阶段2：收尾（47 帧 + 可选落地，时间驱动，不依赖 QTimer）
        if not self._carry_settled:
            self._carry_settled = True
            self._carry_finish_at = time.time()
            if self._carry_to_floor:
                dy = ground_y - rect[3]
                if dy > 0:
                    winapi.animate_move_window(hwnd, 0, dy, steps=10, interval_ms=24)
            self.sm.pose_blip("shime47.png", 16, back_pose="stand")
        elif time.time() - self._carry_finish_at > 1.4:
            self._finish_carry()

    def _finish_carry(self):
        if not self._carry_hwnd:
            return
        self._carry_hwnd = 0
        self._carry_phase = 0
        self.sm.set_idle_pose("stand")
        self.say("放好啦！搬运完成！")

    def _cancel_carry(self):
        self._carry_hwnd = 0
        self._carry_phase = 0
        self.sm.set_idle_pose("stand")

    def _end_carry(self, speak: bool = False):
        """兼容旧调用：立即中止（双击放下）。"""
        self._cancel_carry()
        if speak:
            self.say("放下啦！")

    def _tick_push(self):
        if not self._push_hwnd:
            return
        hwnd = self._push_hwnd
        if self._push_frames_left <= 0:
            dist = random.choice([120, 160, 200, 260])
            winapi.animate_move_window(hwnd, self._pending_push_dir * dist, 0, steps=8)
            self._push_hwnd = 0
            self.sm.set_idle_pose("stand")
            return
        self._push_frames_left -= 1
        # 推姿期间微顶
        step = 6 if self._push_frames_left % 8 == 0 else 0
        if step:
            rect = winapi.window_rect(hwnd)
            if rect:
                winapi.move_window_instantly(hwnd, rect[0] + self._pending_push_dir * step, rect[1])

    # ---------------- 拖放魔法删除 ----------------
    def dragEnterEvent(self, event):
        if not self.magic_delete_enabled:
            return
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        if not self.magic_delete_enabled:
            return
        paths = []
        for url in event.mimeData().urls():
            local = url.toLocalFile()
            if local and os.path.exists(local):
                paths.append(local)
        if not paths:
            return
        names = [p.split("\\")[-1].split("/")[-1] for p in paths]
        shown = "、".join(names[:3]) + (f" 等{len(names)}个" if len(names) > 3 else "")
        reply = QMessageBox.question(
            self, "🧙 魔理沙的魔法",
            f"主人要把《{shown}》交给本魔法使用魔法消灭吗？\n"
            "（只会送进回收站，反悔还来得及哦）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            self.say("哼，胆小鬼！我早就知道你会反悔！")
            return
        ok = winapi.trash_files(paths)
        if ok:
            self._report_favor("魔法删除")
            self.sm.set_state(PetState.SIT)
            self.say(f"限！界！火！花！《{shown}》已被魔法送进回收站，想救它还来得及！★")
        else:
            self.say("呜……那个东西好像有结界保护，我没删动它！")

    def _on_user_message(self, message: str):
        """收到用户输入：交给 MaiBot（QQ 与桌面共用同一颗大脑）。

        桌宠不再有本地大脑：没连上就明确告诉主人，而不是偷偷换一个模型回答。
        """
        if not (self.maibot_enabled and self._maibot_connected):
            self.say("大脑还没连上呢，先启动一体化だぜ！")
            return
        self.think("让我想想看...")
        self.maibot.send(message)

    # ---------------- MaiBot 一体化大脑接入 ----------------
    def _on_maibot_reply(self, text: str):
        """收到 MaiBot（QQ 同源大脑）的回复：气泡 + 语音演出。"""
        self.say(text)

    def _on_maibot_status(self, status: str):
        """MaiBot 连接状态（仅调试打印，避免打扰语音）。"""
        print(f"[MaiBot] {status}")

    def _on_maibot_connected(self, ok: bool):
        """连接状态变化。"""
        self._maibot_connected = bool(ok)
        if ok and not hasattr(self, "_maibot_greeted"):
            self._maibot_greeted = True
            self._quiet_bubble("和 MaiBot 大脑连上了！现在桌面和 QQ 是同一只魔理沙★")
        print(f"[MaiBot] connected={ok}")

    # ---------------- 清理方案确认弹窗 ----------------
    def _on_maibot_confirm_request(self, payload):
        """宿主下发清理方案：在魔理沙头顶弹出勾选确认框。"""
        if not isinstance(payload, dict):
            return
        anchor = QPoint(
            self.x() + (self.width() - self.confirm_dialog.width()) // 2,
            max(20, self.y() - self.confirm_dialog.height() - 10),
        )
        self.confirm_dialog.show_request(payload, anchor)
        self._quiet_bubble("清理方案来了，看看弹窗要清哪些だぜ！")

    def _on_maibot_confirm_closed(self, req_id: str):
        """宿主通知关闭弹窗（QQ 端已确认/取消，或方案被替换）。"""
        self.confirm_dialog.close_request(str(req_id or ""))

    def _on_cleanup_confirmed(self, selected, permanent):
        """弹窗点「执行清理」：把勾选结果回传宿主执行。"""
        if not (self.maibot_enabled and self._maibot_connected):
            self.say("大脑还没连上，方案没法确认だぜ…")
            return
        self.maibot.send_confirm_result(self.confirm_dialog.req_id, True, selected, permanent)
        self.think("收到，开始执行清理…")

    def _on_cleanup_cancelled(self):
        """弹窗点「取消」：把取消结果回传宿主。"""
        if not (self.maibot_enabled and self._maibot_connected):
            return
        self.maibot.send_confirm_result(self.confirm_dialog.req_id, False, [], False)
        self.say("好，方案取消だぜ。")

    def _toggle_maibot(self):
        """托盘菜单：开关 MaiBot 一体化大脑。"""
        self.maibot_enabled = not self.maibot_enabled
        APP_CONFIG.data["msg"]["maibot_enabled"] = self.maibot_enabled
        APP_CONFIG.save()
        if self.maibot_enabled:
            self.maibot.start(
                on_reply=self._on_maibot_reply,
                on_status=self._on_maibot_status,
                on_connected=self._on_maibot_connected,
                on_confirm=self._on_maibot_confirm_request,
                on_confirm_closed=self._on_maibot_confirm_closed,
            )
        else:
            self.maibot.stop()
            self._maibot_connected = False
        self.maibot_act.setText(f"🧠 MaiBot 大脑: {'开' if self.maibot_enabled else '关'}")
        self._quiet_bubble(f"MaiBot 一体化大脑已{'开启' if self.maibot_enabled else '关闭'}！")

    def _try_attach_under_cursor(self) -> bool:
        """拖拽松手时，若魔理沙正贴近某个窗口顶部，则吸附骑上去。"""
        try:
            cx = self.frameGeometry().center().x()
            hwnd = winapi.window_at(cx, self.y() - 6)
            if not hwnd or hwnd == int(self.winId()):
                return False
            if winapi.pid_of_hwnd(hwnd) == os.getpid():
                return False
            cls = winapi.window_class_name(hwnd)
            if cls in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
                return False
            if not winapi.is_window_visible(hwnd) or winapi.is_window_minimized(hwnd):
                return False
            if winapi.is_fullscreen_primary(hwnd):
                return False
            self._attach_ride(hwnd)
            return self.ride_hwnd != 0
        except Exception as e:
            print(f"[ride] attach error: {e}")
            return False

    # --- 鼠标互动事件 ---
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self.ride_hwnd or self.ride_kind == "ceiling":
                self._detach_ride(speak=False)
            if self._carry_hwnd:
                self._end_carry()
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
            # 松手时若贴近窗口顶部则爬上去，否则照常下落
            if not self._try_attach_under_cursor():
                self.vy = 0.0
                self.sm.set_state(PetState.FALL)
            event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self._carry_hwnd or self._push_hwnd:
                self._end_carry()
                self._push_hwnd = 0
                self.sm.set_idle_pose("stand")
                self.say("放下啦！")
                event.accept()
                return
            if self.ride_hwnd or self.ride_kind == "ceiling":
                self._detach_ride()
                event.accept()
                return
            # 摸头互动检测
            patted = self._try_pat()
            if patted:
                pat_responses = [
                    "嘿嘿，摸摸头很舒服！★",
                    "呜哇！突然摸头，本魔法使也是会害羞的啦！",
                    "好搭档！今天也一起在桌面大冒险吧！",
                ]
                self.say(random.choice(pat_responses))
            else:
                self.open_chat_input()
            event.accept()

    def contextMenuEvent(self, event):
        # 菜单弹出前快照合法前台窗口（避免弹出后焦点被抢导致检测不到）
        self._snapshot_foreground()
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

        voice_act = menu.addAction("🎤 语音输入（点一下说话）")
        voice_act.triggered.connect(lambda: self._toggle_voice_input(voice_act))

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
        my_pl_act.triggered.connect(lambda: self._play_ncm_playlist_by_name(""))
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

        inspect_menu = menu.addMenu("🔍 桌面主动巡检（已上移到 WebUI 设置）")
        inspect_toggle_act = inspect_menu.addAction("打开桌宠行为设置…")
        inspect_toggle_act.triggered.connect(self.open_webui_settings)

        settings_act = menu.addAction("⚙️ 打开 WebUI 设置")
        settings_act.triggered.connect(self.open_webui_settings)

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
        act_spark.triggered.connect(self._do_master_spark)

        zany_menu = menu.addMenu("🎭 魔理沙整活")
        self._fill_zany_menu(zany_menu)

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
        try:
            if getattr(self, "maibot", None) is not None:
                self.maibot.stop()
        except Exception:
            pass
        if self.clock_timer.isActive():
            self.clock_timer.stop()
        if self.presence_timer.isActive():
            self.presence_timer.stop()
        if self.director_timer.isActive():
            self.director_timer.stop()
        winapi.unregister_hotkey(int(self.winId()), HOTKEY_ID)
        self.bubble.close()
        self.chat_input.close()
        self.confirm_dialog.close()
        super().closeEvent(event)
