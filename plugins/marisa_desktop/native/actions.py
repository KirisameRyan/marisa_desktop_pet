# -*- coding: utf-8 -*-
"""魔理沙本机动作执行引擎（Native Action Engine）。
MaiBot 核心插件与 QQ 指令的一体化执行中心，直接在 Windows 本机执行，
支持网易云操控、系统音量、锁屏、截图、应用启动与剪贴板读取，彻底消灭中间件。
"""
from __future__ import annotations

import base64
import ctypes
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_NATIVE_DIR = Path(__file__).resolve().parent
if str(_NATIVE_DIR) not in sys.path:
    sys.path.insert(0, str(_NATIVE_DIR))

import ncm_client
import ncm_recommend
import weather
import vision
import balance

# 常用应用名称映射表
COMMON_APPS = {
    "vscode": "code",
    "vs code": "code",
    "代码编辑器": "code",
    "steam": "steam://open/main",
    "qq": "D:\\QQ.exe",
    "微信": "weixin",
    "wechat": "weixin",
    "记事本": "notepad",
    "notepad": "notepad",
    "计算器": "calc",
    "calc": "calc",
    "设置": "ms-settings:",
    "控制面板": "control",
    "资源管理器": "explorer",
    "我的电脑": "explorer",
    "画图": "mspaint",
    "浏览器": "https://www.baidu.com",
    "edge": "msedge",
    "chrome": "chrome",
    "网易云音乐": ncm_client.CLOUDMUSIC_PATH,
    "网易云": ncm_client.CLOUDMUSIC_PATH,
    "cloudmusic": ncm_client.CLOUDMUSIC_PATH,
    "任务管理器": "taskmgr",
    "cmd": "cmd.exe",
    "终端": "wt.exe",
    "powershell": "powershell.exe",
}


class NativeActionEngine:
    """本机动作执行引擎。"""

    # ---------------- 音乐播放与控制 ----------------

    @staticmethod
    def play_music(song_name: str) -> str:
        """搜歌并直接在本机官方网易云客户端中播放。"""
        q = (song_name or "").strip()
        if not q:
            return "还没有告诉我歌名だぜ！"
        return ncm_client.play_search(q)

    @staticmethod
    def control_music(action: str) -> str:
        """控制网易云多媒体播放（play/pause/next/prev）。"""
        act = (action or "play").lower().strip()
        ncm_client.ensure_cloudmusic_running()
        try:
            if act in ("play", "pause", "toggle"):
                ctypes.windll.user32.keybd_event(0xB3, 0, 0, 0)
                ctypes.windll.user32.keybd_event(0xB3, 0, 2, 0)
                return "已在网易云音乐中为你切换播放/暂停だぜ！♪"
            elif act == "next":
                ctypes.windll.user32.keybd_event(0xB0, 0, 0, 0)
                ctypes.windll.user32.keybd_event(0xB0, 0, 2, 0)
                return "已为你切到下一首音乐だぜ！♪"
            elif act == "prev":
                ctypes.windll.user32.keybd_event(0xB1, 0, 0, 0)
                ctypes.windll.user32.keybd_event(0xB1, 0, 2, 0)
                return "已为你倒回上一首音乐だぜ！♪"
            return f"已处理音乐动作：{act}"
        except Exception as exc:
            return f"控制网易云音乐失败：{exc}"

    @staticmethod
    def play_my_playlist() -> str:
        """播放用户「我喜欢的音乐」。"""
        pid = ncm_client.get_user_liked_playlist_id()
        if not pid:
            return "未找到你的「我喜欢的音乐」歌单，请确认是否已通过 /网易云登录 扫码绑定だぜ！"
        ncm_client.play_playlist(pid, "我喜欢的音乐")
        return "正在为主人播放「我喜欢的音乐」歌单♪"

    @staticmethod
    def _touhou_playlist() -> Optional[Dict[str, Any]]:
        """搜一个东方主题的公开歌单（无需登录），找不到返回 None。"""
        for kw in ("东方project", "东方 纯音乐", "东方project 精选"):
            for pl in ncm_client.search_playlist(kw, limit=10):
                name = str(pl.get("name") or "")
                if ("东方" in name or "touhou" in name.lower()) and int(pl.get("count") or 0) >= 100:
                    return pl
        return None

    @staticmethod
    def play_recommend() -> str:
        """播放今日魔理推荐歌单。

        已登录：合成/刷新专属日推并开播；
        未登录：不做静默兜底，明确说明并转放一首东方精选公开歌单（无需登录）。
        """
        logged_in, _ = ncm_client.check_login()
        if not logged_in:
            pl = NativeActionEngine._touhou_playlist()
            if pl is None:
                return "还没绑定网易云账号，做不了今日推荐。对我说「网易云登录」扫码绑定后就能听魔理专属日推了だぜ。"
            ncm_client.play_playlist(pl["id"], pl["name"])
            return (
                "还没绑定网易云账号，专属日推做不了（对我说「网易云登录」扫码即可）。"
                f"先放一首东方精选公开歌单《{pl['name']}》顶一下だぜ♪"
            )
        try:
            res = ncm_recommend.ensure_daily()
        except Exception as exc:
            return f"生成今日魔理推荐时出错了：{str(exc)[:120]}"
        if not res.get("success") or not res.get("playlist_id"):
            return str(res.get("message") or "今日魔理推荐生成失败了，稍后再试だぜ。")
        ncm_client.play_playlist(res["playlist_id"], "魔理推荐的歌单")
        count = res.get("count", 30)
        detail = ""
        if res.get("source_note"):
            detail += f"（{res['source_note']}）"
        first = res.get("first") or {}
        if first.get("name"):
            detail += f" 首播：{first['name']}"
        return f"已为主人开播今日魔理推荐（{count} 首）{detail}♪"

    @staticmethod
    def get_ncm_login_qr() -> Dict[str, Any]:
        """网易云扫码登录：已登录直接说明；否则弹出二维码并返回 base64 供聊天回图。

        返回 {"ok": bool, "text": str, "b64": str="base64二维码"}。
        弹出后由后台线程轮询扫码结果，成功后自动保存登录态。
        """
        logged_in, nickname = ncm_client.check_login()
        if logged_in:
            return {"ok": False, "text": f"主人已经登录过网易云了（{nickname}），无需再扫♪"}
        qr_path = ncm_client.qrcode_png_path()
        try:
            if qr_path.exists():
                qr_path.unlink()
        except Exception:
            pass

        def _on_result(result: Dict[str, Any]) -> None:
            if result.get("success"):
                print(f"[ncm] 扫码登录成功: {result.get('nickname')}")
            else:
                print(f"[ncm] 扫码登录失败/取消: {result.get('message')}")

        # 后台线程负责轮询扫码并保存 session，这里只等二维码文件生成后回图
        ncm_client.qrcode_login(_on_result, open_img=True)
        for _ in range(12):
            if qr_path.exists() and qr_path.stat().st_size > 1000:
                try:
                    b64 = base64.b64encode(qr_path.read_bytes()).decode("ascii")
                    return {
                        "ok": True,
                        "b64": b64,
                        "text": "登录二维码だぜ！2 分钟内用手机网易云 App 扫一扫，绑定后就能听魔理专属日推了★",
                    }
                except Exception:
                    break
            time.sleep(0.5)
        return {"ok": False, "text": "二维码生成失败了……也可以在桌宠的「网易云音乐」菜单里扫码だぜ。"}

    # ---------------- 屏幕截图与视觉 ----------------

    @staticmethod
    def capture_screenshot(save_path: Optional[str] = None) -> Tuple[str, str]:
        """截取屏幕，返回 (文件路径, base64图片字符串)。"""
        if not save_path:
            tmp_dir = _NATIVE_DIR.parent.parent.parent / "data" / "remote"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            save_path = str(tmp_dir / f"shot_{int(time.time()*1000)}.png")
        
        path = vision.SCREEN_OBSERVER.capture_screen_file(save_path)
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return path, b64

    # ---------------- 系统控制 ----------------

    @staticmethod
    def control_volume(action: str) -> str:
        """调节系统音量。"""
        act = (action or "volume_mute").lower().strip()
        try:
            if act in ("volume_up", "up", "大"):
                for _ in range(3):
                    ctypes.windll.user32.keybd_event(0xAF, 0, 0, 0)
                    ctypes.windll.user32.keybd_event(0xAF, 0, 2, 0)
                return "已为主人的电脑调大音量だぜ！"
            elif act in ("volume_down", "down", "小"):
                for _ in range(3):
                    ctypes.windll.user32.keybd_event(0xAE, 0, 0, 0)
                    ctypes.windll.user32.keybd_event(0xAE, 0, 2, 0)
                return "已为主人的电脑调小音量だぜ！"
            elif act in ("volume_mute", "mute", "静音"):
                ctypes.windll.user32.keybd_event(0xAD, 0, 0, 0)
                ctypes.windll.user32.keybd_event(0xAD, 0, 2, 0)
                return "已切换主人电脑的静音状态だぜ！"
            return f"未知音量操作：{act}"
        except Exception as e:
            return f"调节音量失败：{e}"

    @staticmethod
    def lock_workstation() -> str:
        """锁定电脑屏幕。"""
        try:
            ctypes.windll.user32.LockWorkStation()
            return "已为主人锁定电脑屏幕だぜ！外出辛苦啦★"
        except Exception as e:
            return f"锁屏失败：{e}"

    @staticmethod
    def open_app(app_name: str) -> str:
        """启动本机常见软件或网址。"""
        name = (app_name or "").strip().lower()
        if not name:
            return "想让我打开什么呀？"
        target = COMMON_APPS.get(name, name)
        try:
            if target.startswith("http://") or target.startswith("https://"):
                import webbrowser
                webbrowser.open(target)
                return f"已在浏览器打开：{target} だぜ！"
            elif os.path.exists(target):
                subprocess.Popen([target])
                return f"已启动：{app_name} だぜ！"
            else:
                subprocess.Popen(target, shell=True)
                return f"已尝试启动：{app_name} だぜ！"
        except Exception as exc:
            return f"打开 {app_name} 失败：{exc}"

    @staticmethod
    def get_clipboard() -> str:
        """读取系统剪贴板。"""
        try:
            import win32clipboard
            import win32con
            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                    data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                    if data and data.strip():
                        return f"剪贴板内容是：\n{data[:400]}{'…' if len(data) > 400 else ''}"
                    return "剪贴板现在是空的だぜ……"
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            pass
        return "未能读取到剪贴板文本だぜ……"

    @staticmethod
    def get_system_uptime() -> str:
        """获取开机运行时长。"""
        try:
            uptime_ms = ctypes.windll.kernel32.GetTickCount64()
            total_sec = uptime_ms // 1000
            hours = total_sec // 3600
            minutes = (total_sec % 3600) // 60
            return f"这台电脑已经持续开机运行了 {hours} 小时 {minutes} 分钟だぜ！注意劳逸结合★"
        except Exception:
            return "开机时间统计失败だぜ。"

    @staticmethod
    def query_weather(city: str = "") -> str:
        """查询天气。"""
        ok, summary, _ = weather.WEATHER_SERVICE.get_weather(city)
        return summary

ACTIONS = NativeActionEngine()
