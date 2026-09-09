import json
import socket
import sys
import webbrowser
from pathlib import Path
from PyQt6.QtCore import Qt, pyqtSignal, QThread
from PyQt6.QtGui import QFont, QIcon, QColor, QDesktopServices, QPixmap
from PyQt6.QtWidgets import (
    QDialog, QTabWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QSlider, QCheckBox,
    QComboBox, QTextEdit, QGroupBox, QMessageBox, QProgressBar
)

from core.config_manager import APP_CONFIG
from core.balance import BALANCE_CHECKER
from core.memory import MEMORY_MANAGER, CHAT_HIST_PATH
from core.vision import SCREEN_OBSERVER
from core.tts import TTS_ENGINE
from core import ncm_client

class LoginWorker(QThread):
    """后台跑网易云扫码登录轮询，避免卡住设置窗口。"""
    done = pyqtSignal(dict)

    def run(self):
        try:
            result = ncm_client.wait_qrcode_login()
        except Exception as e:
            result = {"success": False, "message": f"扫码登录异常: {e}"}
        self.done.emit(result)


class TaskWorker(QThread):
    """通用后台任务：返回单条字符串结果。"""
    done = pyqtSignal(str)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            result = str(self._fn())
        except Exception as e:
            result = f"任务出错：{str(e)[:120]}"
        self.done.emit(result)

class SettingsDialog(QDialog):
    config_saved = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("⭐ 雾雨魔理沙的魔法配置室")
        self.resize(580, 520)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        self._init_ui()
        self._load_values()

    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        # 标题栏卡片
        header = QLabel("🌟 雾雨魔理沙 · AI 桌宠控制与指引中心")
        header.setFont(QFont("Microsoft YaHei", 12, QFont.Weight.Bold))
        header.setStyleSheet("color: #b38600; padding: 2px;")
        main_layout.addWidget(header)

        # Tab 主分页
        self.tabs = QTabWidget(self)
        self.tabs.setFont(QFont("Microsoft YaHei", 9))

        self.tab_api = QWidget()
        self.tab_pet = QWidget()
        self.tab_voice = QWidget()
        self.tab_memory = QWidget()
        self.tab_msg = QWidget()

        self.tabs.addTab(self.tab_api, "🔑 API 与指引")
        self.tabs.addTab(self.tab_pet, "🎛️ 巡检与桌宠")
        self.tabs.addTab(self.tab_voice, "🗣️ 语音与音乐")
        self.tabs.addTab(self.tab_memory, "❤️ 羁绊与记忆")
        self.tabs.addTab(self.tab_msg, "🧠 MaiBot 一体化")

        self._build_tab_api()
        self._build_tab_pet()
        self._build_tab_voice()
        self._build_tab_memory()
        self._build_tab_msg()

        main_layout.addWidget(self.tabs)

        # 底部按钮栏
        btn_layout = QHBoxLayout()
        self.status_lbl = QLabel("")
        self.status_lbl.setFont(QFont("Microsoft YaHei", 9))
        
        self.btn_save = QPushButton("⭐ 保存并应用配置")
        self.btn_save.setFont(QFont("Microsoft YaHei", 9, QFont.Weight.Bold))
        self.btn_save.setStyleSheet("""
            QPushButton {
                background-color: #ffcc00;
                color: #222222;
                border: 1px solid #cca300;
                border-radius: 6px;
                padding: 6px 18px;
            }
            QPushButton:hover { background-color: #ffe066; }
        """)
        self.btn_save.clicked.connect(self._save_config)

        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setFont(QFont("Microsoft YaHei", 9))
        self.btn_cancel.clicked.connect(self.reject)

        btn_layout.addWidget(self.status_lbl)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addWidget(self.btn_save)

        main_layout.addLayout(btn_layout)

    # --- Tab 1: API 与指引 ---
    def _build_tab_api(self):
        layout = QVBoxLayout(self.tab_api)
        layout.setSpacing(12)

        # DeepSeek 卡片
        grp_ds = QGroupBox("🧠 DeepSeek 核心大脑配置")
        ds_layout = QVBoxLayout(grp_ds)

        # Key 行
        key_layout = QHBoxLayout()
        key_layout.addWidget(QLabel("API Key:"))
        self.edit_ds_key = QLineEdit()
        self.edit_ds_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.btn_ds_toggle = QPushButton("👁")
        self.btn_ds_toggle.setFixedWidth(30)
        self.btn_ds_toggle.clicked.connect(lambda: self._toggle_echo(self.edit_ds_key, self.btn_ds_toggle))
        key_layout.addWidget(self.edit_ds_key)
        key_layout.addWidget(self.btn_ds_toggle)
        ds_layout.addLayout(key_layout)

        # 引导超链接与测试按钮
        ds_sub = QHBoxLayout()
        link_ds = QLabel('<a href="https://platform.deepseek.com/api_keys">👉 点击前往 DeepSeek 官方控制台申请 API Key</a>')
        link_ds.setOpenExternalLinks(True)
        self.btn_test_ds = QPushButton("🔍 测试连接并查余额")
        self.btn_test_ds.clicked.connect(self._test_deepseek)
        ds_sub.addWidget(link_ds)
        ds_sub.addStretch()
        ds_sub.addWidget(self.btn_test_ds)
        ds_layout.addLayout(ds_sub)

        self.lbl_ds_res = QLabel("未测试")
        self.lbl_ds_res.setStyleSheet("color: #666; font-size: 11px;")
        ds_layout.addWidget(self.lbl_ds_res)

        layout.addWidget(grp_ds)

        # 智谱视觉卡片
        grp_v = QGroupBox("👀 智谱 GLM-V 视觉模型配置")
        v_layout = QVBoxLayout(grp_v)

        v_key_layout = QHBoxLayout()
        v_key_layout.addWidget(QLabel("API Key:"))
        self.edit_v_key = QLineEdit()
        self.edit_v_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.btn_v_toggle = QPushButton("👁")
        self.btn_v_toggle.setFixedWidth(30)
        self.btn_v_toggle.clicked.connect(lambda: self._toggle_echo(self.edit_v_key, self.btn_v_toggle))
        v_key_layout.addWidget(self.edit_v_key)
        v_key_layout.addWidget(self.btn_v_toggle)
        v_layout.addLayout(v_key_layout)

        v_sub = QHBoxLayout()
        link_v = QLabel('<a href="https://open.bigmodel.cn/usercenter/apikeys">👉 点击前往智谱 AI 开放平台申请 API Key</a>')
        link_v.setOpenExternalLinks(True)
        self.btn_test_v = QPushButton("📸 测试截屏视觉识别")
        self.btn_test_v.clicked.connect(self._test_vision)
        v_sub.addWidget(link_v)
        v_sub.addStretch()
        v_sub.addWidget(self.btn_test_v)
        v_layout.addLayout(v_sub)

        self.lbl_v_res = QLabel("未测试")
        self.lbl_v_res.setStyleSheet("color: #666; font-size: 11px;")
        v_layout.addWidget(self.lbl_v_res)

        layout.addWidget(grp_v)
        layout.addStretch()

    # --- Tab 2: 巡检与桌宠 ---
    def _build_tab_pet(self):
        layout = QVBoxLayout(self.tab_pet)
        layout.setSpacing(14)

        # 主动巡检
        grp_insp = QGroupBox("🔍 桌面定时主动巡检 (关怀与摸鱼搭话)")
        insp_layout = QVBoxLayout(grp_insp)

        self.chk_auto_inspect = QCheckBox("开启后台定时视觉巡检（自动观察屏幕并关心主人）")
        insp_layout.addWidget(self.chk_auto_inspect)

        slider_layout = QHBoxLayout()
        slider_layout.addWidget(QLabel("巡检间隔:"))
        self.slider_inspect = QSlider(Qt.Orientation.Horizontal)
        self.slider_inspect.setRange(2, 60) # 2~60 分钟
        self.lbl_inspect_val = QLabel("10 分钟")
        self.slider_inspect.valueChanged.connect(lambda val: self.lbl_inspect_val.setText(f"{val} 分钟"))
        slider_layout.addWidget(self.slider_inspect)
        slider_layout.addWidget(self.lbl_inspect_val)
        insp_layout.addLayout(slider_layout)

        layout.addWidget(grp_insp)

        # 尺寸比例
        grp_scale = QGroupBox("📏 桌宠外观尺寸")
        sc_layout = QHBoxLayout(grp_scale)
        sc_layout.addWidget(QLabel("缩放倍率:"))
        self.combo_scale = QComboBox()
        self.combo_scale.addItems(["0.8x (精致小巧)", "1.0x (原生默认)", "1.25x (稍大更清晰)", "1.5x (元气巨萌)"])
        sc_layout.addWidget(self.combo_scale)
        sc_layout.addStretch()
        layout.addWidget(grp_scale)

        # 互动与陪伴
        grp_inter = QGroupBox("🧚 互动与陪伴")
        inter_layout = QVBoxLayout(grp_inter)
        self.chk_presence = QCheckBox("主人离开时打瞌睡，回来后打招呼（离开检测）")
        inter_layout.addWidget(self.chk_presence)
        presence_row = QHBoxLayout()
        presence_row.addWidget(QLabel("判定离开的时长:"))
        self.slider_presence = QSlider(Qt.Orientation.Horizontal)
        self.slider_presence.setRange(1, 30)
        self.lbl_presence_val = QLabel("5 分钟")
        self.slider_presence.valueChanged.connect(
            lambda val: self.lbl_presence_val.setText(f"{val} 分钟")
        )
        presence_row.addWidget(self.slider_presence)
        presence_row.addWidget(self.lbl_presence_val)
        inter_layout.addLayout(presence_row)

        self.chk_magic_delete = QCheckBox("允许把文件/快捷方式拖到魔理沙身上，用魔法送进回收站")
        inter_layout.addWidget(self.chk_magic_delete)

        hotkey_row = QHBoxLayout()
        self.chk_hotkey = QCheckBox("启用全局呼叫热键")
        self.combo_hotkey = QComboBox()
        self.combo_hotkey.addItems(["Ctrl+Alt+M", "Ctrl+Alt+Space", "Ctrl+Shift+Space", "Ctrl+Alt+Z"])
        hotkey_row.addWidget(self.chk_hotkey)
        hotkey_row.addWidget(self.combo_hotkey)
        hotkey_row.addStretch()
        inter_layout.addLayout(hotkey_row)

        self.chk_zany = QCheckBox("整活行为自动触发（跑/躺/吊窗/爬窗/推窗等）")
        inter_layout.addWidget(self.chk_zany)
        zany_row = QHBoxLayout()
        zany_row.addWidget(QLabel("整活频率:"))
        self.combo_zany = QComboBox()
        self.combo_zany.addItems(["克制（安静陪伴为主）", "热闹（偶尔到处皮）"])
        zany_row.addWidget(self.combo_zany)
        zany_row.addStretch()
        inter_layout.addLayout(zany_row)
        layout.addWidget(grp_inter)

        layout.addStretch()

    # --- Tab 3: 语音与音乐 ---
    def _build_tab_voice(self):
        layout = QVBoxLayout(self.tab_voice)
        layout.setSpacing(14)

        # TTS 语音
        grp_tts = QGroupBox("🗣️ TTS 语音合成设置")
        tts_layout = QVBoxLayout(grp_tts)

        # 语速
        rate_layout = QHBoxLayout()
        rate_layout.addWidget(QLabel("语速微调:"))
        self.slider_rate = QSlider(Qt.Orientation.Horizontal)
        self.slider_rate.setRange(-20, 50)
        self.lbl_rate_val = QLabel("+15%")
        self.slider_rate.valueChanged.connect(lambda val: self.lbl_rate_val.setText(f"{val:+d}%"))
        rate_layout.addWidget(self.slider_rate)
        rate_layout.addWidget(self.lbl_rate_val)
        tts_layout.addLayout(rate_layout)

        # 音调
        pitch_layout = QHBoxLayout()
        pitch_layout.addWidget(QLabel("音调微调:"))
        self.slider_pitch = QSlider(Qt.Orientation.Horizontal)
        self.slider_pitch.setRange(-20, 30)
        self.lbl_pitch_val = QLabel("+10Hz")
        self.slider_pitch.valueChanged.connect(lambda val: self.lbl_pitch_val.setText(f"{val:+d}Hz"))
        pitch_layout.addWidget(self.slider_pitch)
        pitch_layout.addWidget(self.lbl_pitch_val)
        tts_layout.addLayout(pitch_layout)

        # 试听按钮
        btn_listen = QPushButton("▶ 试听魔理沙元气台词")
        btn_listen.clicked.connect(self._test_tts)
        tts_layout.addWidget(btn_listen)

        layout.addWidget(grp_tts)

        # 网易云音乐
        grp_music = QGroupBox("🎵 网易云音乐（扫码登录后可放每日推荐）")
        m_layout = QVBoxLayout(grp_music)

        # 登录状态行
        self.lbl_ncm_status = QLabel("正在检查网易云登录状态...")
        self.lbl_ncm_status.setStyleSheet("color: #555;")
        m_layout.addWidget(self.lbl_ncm_status)

        # 二维码预览
        self.lbl_qr = QLabel()
        self.lbl_qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_qr.hide()
        m_layout.addWidget(self.lbl_qr)

        # 操作按钮
        btn_row = QHBoxLayout()
        self.btn_ncm_login = QPushButton("📱 扫码登录网易云")
        self.btn_ncm_login.clicked.connect(self._ncm_qrcode_login)
        self.btn_marisa_recommend = QPushButton("✨ 魔理推荐：合成歌单并开播")
        self.btn_marisa_recommend.clicked.connect(self._ncm_marisa_recommend)
        btn_test_music = QPushButton("🎶 播放《Bad Apple!!》测试")
        btn_test_music.clicked.connect(self._ncm_play_test)
        btn_row.addWidget(self.btn_ncm_login)
        btn_row.addWidget(self.btn_marisa_recommend)
        btn_row.addWidget(btn_test_music)
        btn_row.addStretch()
        m_layout.addLayout(btn_row)

        # 音乐小功能开关
        self.chk_song_notify = QCheckBox("切歌时播报（气泡+语音报歌名）")
        self.chk_hourly_chime = QCheckBox("整点报时")
        self.chk_marisa_daily = QCheckBox("每天自动更新魔理推荐歌单并语音提醒")
        m_layout.addWidget(self.chk_song_notify)
        m_layout.addWidget(self.chk_hourly_chime)
        m_layout.addWidget(self.chk_marisa_daily)

        # 结果提示
        self.lbl_music_res = QLabel("")
        self.lbl_music_res.setWordWrap(True)
        self.lbl_music_res.setStyleSheet("color: #666; font-size: 11px;")
        m_layout.addWidget(self.lbl_music_res)

        layout.addWidget(grp_music)

        layout.addStretch()

        # 延迟初始化登录状态（等窗口就绪）
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(0, self._refresh_ncm_status)

    # --- Tab 4: 羁绊与记忆 ---
    def _build_tab_memory(self):
        layout = QVBoxLayout(self.tab_memory)
        layout.setSpacing(12)

        # 好感度进度
        grp_fav = QGroupBox("❤️ 好感度与羁绊称号")
        fav_layout = QVBoxLayout(grp_fav)
        self.lbl_fav_rank = QLabel("")
        self.lbl_fav_rank.setFont(QFont("Microsoft YaHei", 10, QFont.Weight.Bold))
        self.bar_fav = QProgressBar()
        self.bar_fav.setRange(0, 100)
        self.bar_fav.setStyleSheet("""
            QProgressBar {
                border: 1px solid #cca300;
                border-radius: 5px;
                text-align: center;
                height: 18px;
            }
            QProgressBar::chunk {
                background-color: #ffcc00;
                border-radius: 4px;
            }
        """)
        fav_layout.addWidget(self.lbl_fav_rank)
        fav_layout.addWidget(self.bar_fav)
        layout.addWidget(grp_fav)

        # 备忘档案
        grp_mem = QGroupBox("📝 魔理沙记下的主人备忘档案")
        mem_layout = QVBoxLayout(grp_mem)
        self.edit_notes = QTextEdit()
        self.edit_notes.setMaximumHeight(90)
        mem_layout.addWidget(self.edit_notes)
        layout.addWidget(grp_mem)

        # 清除上下文
        btn_reset_chat = QPushButton("🧹 清空多轮对话历史上下文")
        btn_reset_chat.clicked.connect(self._reset_chat_history)
        layout.addWidget(btn_reset_chat)

        layout.addStretch()

    # --- 数据装载与保存 ---
    # --- Tab 5: MaiBot 一体化大脑 ---
    def _build_tab_msg(self):
        layout = QVBoxLayout(self.tab_msg)
        layout.setSpacing(12)

        grp_brain = QGroupBox("🧠 MaiBot 一体化大脑（QQ 与桌面同一颗魔理沙）")
        b_layout = QVBoxLayout(grp_brain)
        self.chk_maibot = QCheckBox("启用：桌面聊天走 MaiBot 大脑（记忆/人格/好感与 QQ 同源）")
        b_layout.addWidget(self.chk_maibot)
        row = QHBoxLayout()
        row.addWidget(QLabel("MaiBot 桌面网关地址:"))
        self.edit_maibot_ws = QLineEdit()
        self.edit_maibot_ws.setPlaceholderText("ws://127.0.0.1:8400")
        row.addWidget(self.edit_maibot_ws)
        self.btn_maibot_refresh = QPushButton("🔄 检测连接")
        self.btn_maibot_refresh.clicked.connect(self._maibot_refresh)
        row.addWidget(self.btn_maibot_refresh)
        b_layout.addLayout(row)
        self.lbl_maibot_state = QLabel("")
        self.lbl_maibot_state.setStyleSheet("color: #555; font-size: 11px;")
        self.lbl_maibot_state.setWordWrap(True)
        b_layout.addWidget(self.lbl_maibot_state)
        tip = QLabel(
            "说明：MaiBot 是本桌宠的统一大脑（通过 NapCat 上 QQ，通过本机网关连桌面）。\n"
            "桌面与 QQ 的聊天记忆、好感度、羁绊档案全部同源，改这里即可即时启停。"
        )
        tip.setStyleSheet("color: #888; font-size: 11px;")
        tip.setWordWrap(True)
        b_layout.addWidget(tip)
        layout.addWidget(grp_brain)

        grp_fav = QGroupBox("❤️ 好感度（MaiBot 单源数据）")
        f_layout = QVBoxLayout(grp_fav)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("数据文件:"))
        self.edit_maibot_fav_path = QLineEdit()
        self.edit_maibot_fav_path.setPlaceholderText(
            r"D:\MaiBot-1.2.4\data\marisa\favorability.json"
        )
        row2.addWidget(self.edit_maibot_fav_path)
        self.btn_fav_refresh = QPushButton("🔄 刷新")
        self.btn_fav_refresh.clicked.connect(self._maibot_fav_refresh)
        row2.addWidget(self.btn_fav_refresh)
        f_layout.addLayout(row2)
        self.lbl_maibot_fav = QLabel("")
        self.lbl_maibot_fav.setStyleSheet("color: #b38600; font-size: 12px; font-weight: bold;")
        self.lbl_maibot_fav.setWordWrap(True)
        f_layout.addWidget(self.lbl_maibot_fav)
        layout.addWidget(grp_fav)
        layout.addStretch()

    @staticmethod
    def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.4) -> bool:
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            s.close()
            return True
        except Exception:
            return False

    def _maibot_refresh(self):
        """探测 MaiBot 桌面网关(8400) 与 WebUI(8001)。"""
        ws_up = self._port_open(8400)
        web_up = self._port_open(8001)
        parts = []
        if self.chk_maibot.isChecked():
            parts.append("本端开关: 开")
        else:
            parts.append("本端开关: 关")
        parts.append("桌面网关8400:" + ("通" if ws_up else "不通"))
        parts.append("MaiBot WebUI8001:" + ("通" if web_up else "不通"))
        if not ws_up:
            parts.append("（请先启动 MaiBot：D:\\MaiBot-1.2.4 下 python bot.py）")
        self.lbl_maibot_state.setText(" · ".join(parts))
        self._maibot_fav_refresh()

    def _maibot_fav_refresh(self):
        """读取 MaiBot 单源好感度文件并展示。"""
        path = self.edit_maibot_fav_path.text().strip()
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            fav = int(data.get("favorability", 0))
            if fav >= 80:
                rank = "灵魂挚友"
            elif fav >= 60:
                rank = "亲密搭档"
            elif fav >= 40:
                rank = "可靠的搭档"
            elif fav >= 20:
                rank = "熟络的助手"
            else:
                rank = "初识的魔法使"
            total = int(data.get("total_interactions", 0))
            self.lbl_maibot_fav.setText(f"{fav}/100（{rank}） · 累计互动 {total} 次\nQQ 与桌面端看到的都是这个数字だぜ★")
        except Exception as e:
            self.lbl_maibot_fav.setText(f"（暂未读到 MaiBot 好感文件：{str(e)[:70]}）")
            self.lbl_maibot_fav.setStyleSheet("color: #999; font-size: 11px;")

    def _load_values(self):
        # API Keys
        self.edit_ds_key.setText(APP_CONFIG.get("deepseek", "api_key", ""))
        self.edit_v_key.setText(APP_CONFIG.get("vision", "api_key", ""))

        # 音乐通知
        self.chk_song_notify.setChecked(bool(APP_CONFIG.get("pet", "song_notify", True)))
        self.chk_hourly_chime.setChecked(bool(APP_CONFIG.get("pet", "hourly_chime", False)))
        self.chk_marisa_daily.setChecked(bool(APP_CONFIG.get("pet", "marisa_daily_notify", True)))

        # 巡检
        auto_insp = APP_CONFIG.get("pet", "auto_screen_inspect", True)
        self.chk_auto_inspect.setChecked(auto_insp)
        interval = APP_CONFIG.get("pet", "inspect_interval_seconds", 600) // 60
        self.slider_inspect.setValue(max(2, min(60, interval)))

        # 缩放
        sc = APP_CONFIG.get("pet", "scale", 1.0)
        scale_map = {0.8: 0, 1.0: 1, 1.25: 2, 1.5: 3}
        self.combo_scale.setCurrentIndex(scale_map.get(sc, 1))

        # 互动与陪伴
        self.chk_presence.setChecked(bool(APP_CONFIG.get("pet", "presence_watch", True)))
        idle_min = max(1, min(30, int(APP_CONFIG.get("pet", "presence_idle_seconds", 300)) // 60))
        self.slider_presence.setValue(idle_min)
        self.lbl_presence_val.setText(f"{idle_min} 分钟")
        self.chk_magic_delete.setChecked(bool(APP_CONFIG.get("pet", "magic_delete", True)))
        self.chk_hotkey.setChecked(bool(APP_CONFIG.get("pet", "hotkey_enable", True)))
        combo_stored = str(APP_CONFIG.get("pet", "hotkey_combo", "ctrl+alt+m"))
        combo_map = {"ctrl+alt+m": 0, "ctrl+alt+space": 1, "ctrl+shift+space": 2, "ctrl+alt+z": 3}
        self.combo_hotkey.setCurrentIndex(combo_map.get(combo_stored, 0))
        self.chk_zany.setChecked(bool(APP_CONFIG.get("pet", "zany_auto", True)))
        self.combo_zany.setCurrentIndex(max(0, int(APP_CONFIG.get("pet", "zany_level", 1)) - 1))

        # MaiBot 一体化大脑
        self.chk_maibot.setChecked(bool(APP_CONFIG.get("msg", "maibot_enabled", False)))
        self.edit_maibot_ws.setText(str(APP_CONFIG.get("msg", "maibot_ws", "ws://127.0.0.1:8400")))
        self.edit_maibot_fav_path.setText(
            str(APP_CONFIG.get("msg", "maibot_fav_path", r"D:\MaiBot-1.2.4\data\marisa\favorability.json"))
        )
        self._maibot_refresh()

        # 声音
        rate_str = APP_CONFIG.get("tts", "rate", "+15%").replace("%", "")
        pitch_str = APP_CONFIG.get("tts", "pitch", "+10Hz").replace("Hz", "")
        try:
            self.slider_rate.setValue(int(rate_str))
        except Exception:
            self.slider_rate.setValue(15)
        try:
            self.slider_pitch.setValue(int(pitch_str))
        except Exception:
            self.slider_pitch.setValue(10)

        # 记忆与好感度
        fav = MEMORY_MANAGER.get_favorability()
        rank_name, rank_desc = MEMORY_MANAGER.get_rank()
        self.bar_fav.setValue(fav)
        self.lbl_fav_rank.setText(f"当前羁绊: {rank_name} ({fav}/100) — {rank_desc}")
        self.edit_notes.setPlainText(MEMORY_MANAGER.memory.get("notes", ""))

    def _save_config(self):
        # 写回配置
        ds_k = self.edit_ds_key.text().strip()
        v_k = self.edit_v_key.text().strip()
        if ds_k:
            APP_CONFIG.data["deepseek"]["api_key"] = ds_k
        if v_k:
            APP_CONFIG.data["vision"]["api_key"] = v_k

        APP_CONFIG.data["pet"]["auto_screen_inspect"] = self.chk_auto_inspect.isChecked()
        APP_CONFIG.data["pet"]["inspect_interval_seconds"] = self.slider_inspect.value() * 60
        APP_CONFIG.data["pet"]["song_notify"] = self.chk_song_notify.isChecked()
        APP_CONFIG.data["pet"]["hourly_chime"] = self.chk_hourly_chime.isChecked()
        APP_CONFIG.data["pet"]["marisa_daily_notify"] = self.chk_marisa_daily.isChecked()

        scale_values = [0.8, 1.0, 1.25, 1.5]
        APP_CONFIG.data["pet"]["scale"] = scale_values[self.combo_scale.currentIndex()]

        APP_CONFIG.data["pet"]["presence_watch"] = self.chk_presence.isChecked()
        APP_CONFIG.data["pet"]["presence_idle_seconds"] = self.slider_presence.value() * 60
        APP_CONFIG.data["pet"]["magic_delete"] = self.chk_magic_delete.isChecked()
        APP_CONFIG.data["pet"]["hotkey_enable"] = self.chk_hotkey.isChecked()
        combo_values = ["ctrl+alt+m", "ctrl+alt+space", "ctrl+shift+space", "ctrl+alt+z"]
        APP_CONFIG.data["pet"]["hotkey_combo"] = combo_values[self.combo_hotkey.currentIndex()]
        APP_CONFIG.data["pet"]["zany_auto"] = self.chk_zany.isChecked()
        APP_CONFIG.data["pet"]["zany_level"] = self.combo_zany.currentIndex() + 1

        # MaiBot 一体化大脑
        APP_CONFIG.data["msg"]["maibot_enabled"] = self.chk_maibot.isChecked()
        APP_CONFIG.data["msg"]["maibot_ws"] = self.edit_maibot_ws.text().strip() or "ws://127.0.0.1:8400"
        fav_p = self.edit_maibot_fav_path.text().strip()
        if fav_p:
            APP_CONFIG.data["msg"]["maibot_fav_path"] = fav_p

        APP_CONFIG.data["tts"]["rate"] = f"{self.slider_rate.value():+d}%"
        APP_CONFIG.data["tts"]["pitch"] = f"{self.slider_pitch.value():+d}Hz"

        APP_CONFIG.save()

        # 更新记忆档案
        MEMORY_MANAGER.update_notes(self.edit_notes.toPlainText().strip())

        self.config_saved.emit()
        QMessageBox.information(self, "保存成功", "魔理沙的配置已成功保存并立即生效だぜ！★")
        self.accept()

    def _toggle_echo(self, edit: QLineEdit, btn: QPushButton):
        if edit.echoMode() == QLineEdit.EchoMode.Password:
            edit.setEchoMode(QLineEdit.EchoMode.Normal)
            btn.setText("🔒")
        else:
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            btn.setText("👁")

    def _test_deepseek(self):
        key = self.edit_ds_key.text().strip()
        self.lbl_ds_res.setText("正在连接 DeepSeek 官方验证并查询余额...")
        ok, msg, _ = BALANCE_CHECKER.query_balance(key)
        if ok:
            self.lbl_ds_res.setText(f"🟢 验证成功！{msg}")
            self.lbl_ds_res.setStyleSheet("color: #008800; font-size: 11px;")
        else:
            self.lbl_ds_res.setText(f"🔴 验证失败：{msg}")
            self.lbl_ds_res.setStyleSheet("color: #cc0000; font-size: 11px;")

    def _test_vision(self):
        key = self.edit_v_key.text().strip()
        if not key:
            self.lbl_v_res.setText("🔴 请先输入智谱 API Key")
            return
        self.lbl_v_res.setText("正在截屏并请求智谱 GLM-V 识别...")
        APP_CONFIG.data["vision"]["api_key"] = key
        res = SCREEN_OBSERVER.analyze_screen("快速测试屏幕正在显示什么")
        if "（" not in res[:3]:
            self.lbl_v_res.setText(f"🟢 识别成功：{res[:70]}...")
            self.lbl_v_res.setStyleSheet("color: #008800; font-size: 11px;")
        else:
            self.lbl_v_res.setText(f"🔴 识别遇到问题：{res}")
            self.lbl_v_res.setStyleSheet("color: #cc0000; font-size: 11px;")

    def _test_tts(self):
        rate = f"{self.slider_rate.value():+d}%"
        pitch = f"{self.slider_pitch.value():+d}Hz"
        APP_CONFIG.data["tts"]["rate"] = rate
        APP_CONFIG.data["tts"]["pitch"] = pitch
        TTS_ENGINE.speak("普通魔法使雾雨魔理沙参上！今天也元气满满地大干一场吧だぜ！★")

    def _refresh_ncm_status(self):
        logged_in, nickname = ncm_client.check_login()
        if logged_in:
            self.lbl_ncm_status.setText(f"🟢 已登录网易云：{nickname}")
            self.lbl_ncm_status.setStyleSheet("color: #008800;")
            self.btn_ncm_login.setText("🔄 重新扫码登录")
        else:
            self.lbl_ncm_status.setText("🔴 尚未登录网易云（登录后可解锁每日推荐播放）")
            self.lbl_ncm_status.setStyleSheet("color: #cc0000;")

    def _ncm_qrcode_login(self):
        from PyQt6.QtGui import QPixmap
        logged_in, _ = ncm_client.check_login()
        if logged_in:
            self.btn_ncm_login.setEnabled(False)
        self.lbl_music_res.setText("正在获取二维码，请用手机网易云 App 扫码（2 分钟内有效）...")

        def _on_done(result):
            self.btn_ncm_login.setEnabled(True)
            self.lbl_qr.hide()
            try:
                if result.get("success"):
                    self.lbl_music_res.setText(f"🟢 {result.get('message')}")
                    self.lbl_music_res.setStyleSheet("color: #008800;")
                    TTS_ENGINE.speak(f"网易云登录成功だぜ！欢迎{result.get('nickname', '主人')}")
                else:
                    self.lbl_music_res.setText(f"🔴 {result.get('message')}")
                    self.lbl_music_res.setStyleSheet("color: #cc0000;")
                self._refresh_ncm_status()
            except Exception:
                pass

        # 二维码生成后立刻内嵌预览（自动弹出改成内嵌，不另开看图软件）
        qr_path = ncm_client.qrcode_png_path()
        qr_path.parent.mkdir(parents=True, exist_ok=True)
        prev = qr_path
        if prev.exists():
            prev.unlink()

        self.worker_login = LoginWorker()
        self.worker_login.done.connect(_on_done)
        self.worker_login.finished.connect(self.worker_login.deleteLater)
        self.worker_login.start()

        # 轮询二维码文件生成后展示（worker 里 save 很快，这里简单延时兜底）
        from PyQt6.QtCore import QTimer
        import os

        def _poll_qr(tries=8):
            if not self.worker_login.isRunning() and tries <= 0:
                return
            if qr_path.exists():
                pix = QPixmap(str(qr_path))
                if not pix.isNull():
                    self.lbl_qr.setPixmap(pix.scaledToWidth(190))
                    self.lbl_qr.show()
                    self.lbl_music_res.setText("二维码已生成，请用手机网易云 App 扫码登录...")
                    return
            if tries > 0:
                QTimer.singleShot(300, lambda: _poll_qr(tries - 1))

        _poll_qr()

    def _ncm_play_test(self):
        label = ncm_client.play_song("687506", "Bad Apple!! - のみこ")
        self.lbl_music_res.setText(f"🎶 已调起本机网易云播放{label}")
        TTS_ENGINE.speak("正在为你播放测试歌曲 Bad Apple だぜ！")

    def _ncm_marisa_recommend(self):
        """后台生成魔理推荐歌单并开播。"""
        self.btn_marisa_recommend.setEnabled(False)
        self.lbl_music_res.setText("正在翻主人的常听与收藏，拌今日推荐...")
        from core.tools import ToolExecutor
        self.worker_task = TaskWorker(lambda: ToolExecutor.marisa_recommend_playlist())
        self.worker_task.done.connect(self._on_marisa_recommend_done)
        self.worker_task.finished.connect(self.worker_task.deleteLater)
        self.worker_task.start()

    def _on_marisa_recommend_done(self, text: str):
        self.btn_marisa_recommend.setEnabled(True)
        self.lbl_music_res.setText(text[:180])
        if "开播" in text:
            TTS_ENGINE.speak("魔理推荐做好了，第一首已开播，去歌单看看だぜ！")
        else:
            TTS_ENGINE.speak("魔理推荐没做成，看看设置里的提示だぜ")

    def _reset_chat_history(self):
        MEMORY_MANAGER.chat_history = []
        MEMORY_MANAGER._save_json(CHAT_HIST_PATH, [])
        QMessageBox.information(self, "完成", "与魔理沙的多轮对话历史已重置清空！")
