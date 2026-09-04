import os
import time
import shutil
import subprocess
import webbrowser
import urllib.parse
import ctypes
import ctypes.wintypes
import httpx
from typing import Dict, Any, List, Optional, Tuple

from .balance import BALANCE_CHECKER
from .weather import WEATHER_SERVICE
from . import ncm_client
from .ncm_client import CLOUDMUSIC_PATH

# 常见应用程序匹配表
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
    "网易云音乐": CLOUDMUSIC_PATH,
    "网易云": CLOUDMUSIC_PATH,
    "cloudmusic": CLOUDMUSIC_PATH,
    "抖音": "D:\\douyin\\douyin.exe",
    "任务管理器": "taskmgr",
    "cmd": "cmd.exe",
    "终端": "wt.exe",
    "powershell": "powershell.exe"
}

# OpenAI 工具定义列表
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "check_api_balance",
            "description": "查询主人当前 DeepSeek API 账户的可用余额与充值金额。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather_info",
            "description": "获取实时天气预报（支持自动基于网络 IP 定位当前城市，或查询指定城市）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名称（如 '北京', '上海', '广州'），留空则自动定位当前所在城市"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "play_music_search",
            "description": "按歌名在主人本机的网易云音乐客户端里直接搜索并播放该歌曲（自动定位到本机客户端内播放）。用户点歌或你推荐歌曲后要播放时都调用它。",
            "parameters": {
                "type": "object",
                "properties": {
                    "song_name": {
                        "type": "string",
                        "description": "要播放的歌曲，建议带上歌手，例如 '恋色Master Spark', 'Bad Apple!! 巡音ルカ', '周杰伦 晴天'"
                    }
                },
                "required": ["song_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "control_cloudmusic",
            "description": "控制网易云音乐播放（在后台打开网易云、播放收藏的音乐/我喜欢的音乐、暂停、上一曲、下一曲）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["play", "pause", "toggle", "next", "prev"],
                        "description": "音乐动作：play(播放音乐/播收藏的歌), pause(暂停), toggle(播放/暂停切换), next(下一首), prev(上一首)"
                    }
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "marisa_recommend_playlist",
            "description": "综合主人的网易云口味做「魔理推荐」：拉取常听排行、我喜欢的音乐、收藏歌单，再拌入今日推荐，合成约30首刷新到固定歌单「魔理推荐的歌单」并直接开播第一首。主人说「推荐歌/推荐歌单/不知道听什么/帮我找点歌」时优先调用（需已扫码登录；未登录会提示）。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "play_cloudmusic_playlist",
            "description": "播放主人网易云里的歌单或「我喜欢的音乐」：留空默认播放我喜欢的音乐；指定名称则模糊匹配主人家歌单/收藏歌单并播放其第一首（需已扫码登录）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "歌单名关键词（如 '东方'、'纯音乐'），留空表示我喜欢的音乐"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "play_artist_hot",
            "description": "按歌手名搜索并播放其热门歌曲第一首，如「放一首周杰伦的歌」（需已扫码登录，更准）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "artist": {
                        "type": "string",
                        "description": "歌手名，如 '周杰伦'、'久石让'、'ZUN'"
                    }
                },
                "required": ["artist"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "save_current_song_to_playlist",
            "description": "把网易云客户端「当前正在播放」的歌曲收藏进固定收集歌单「魔理沙の宝箱」（不存在会自动创建）。主人说「收藏这首歌/存起来/收进歌单」时调用。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "login_cloudmusic_qrcode",
            "description": "弹窗显示网易云音乐扫码登录二维码，等待主人在 2 分钟内用手机 App 扫码（登录成功后可解锁魔理推荐、歌单、歌手热歌等私人内容）。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "open_application",
            "description": "在用户的 Windows 电脑上启动指定的应用程序、游戏或系统工具（如 vscode, steam, 记事本, 计算器, 浏览器等）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "要打开的应用名称或别名，例如 'vscode', 'steam', 'calc', 'notepad', '浏览器' 等"
                    }
                },
                "required": ["app_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "open_url",
            "description": "在用户的默认浏览器中打开指定网址，或者执行关键词搜索。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "网页地址 (http/https 开头) 或者搜索关键词"
                    }
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "system_control",
            "description": "控制用户的电脑系统状态，如调节音量、静音、锁屏或清空回收站。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["volume_up", "volume_down", "volume_mute", "lock_screen", "open_recycle_bin"],
                        "description": "系统动作：volume_up(调大音量), volume_down(调小音量), volume_mute(静音开关), lock_screen(锁屏), open_recycle_bin(打开回收站)"
                    }
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_screen",
            "description": "截取并查看用户当前的电脑屏幕，分析屏幕上正在显示的内容、代码错误、网页或游戏画面。",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "你想在屏幕上观察或确认的问题，例如 '看看代码报错是什么' 或 '看看正在玩什么游戏'"
                    }
                },
                "required": ["question"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_clipboard_text",
            "description": "读取主人剪贴板中的纯文本（最多 2000 字）。主人说「看看/解释/翻译/总结剪贴板」或想让你处理刚复制的内容时调用。剪贴板若是图片/文件则返回相应提示。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "system_uptime_info",
            "description": "查询电脑本次开机已运行多久（用于关怀主人：久坐提醒喝水休息）。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    }
]

class ToolExecutor:
    @staticmethod
    def is_cloudmusic_running() -> bool:
        return ncm_client.is_cloudmusic_running()

    @classmethod
    def control_cloudmusic(cls, action: str = "play") -> str:
        try:
            running = cls.is_cloudmusic_running()
            if not running:
                if os.path.exists(CLOUDMUSIC_PATH):
                    subprocess.Popen([CLOUDMUSIC_PATH])
                else:
                    os.system("start orpheus://")
                time.sleep(1.8)

            if action in ("play", "pause", "toggle"):
                ctypes.windll.user32.keybd_event(0xB3, 0, 0, 0)
                ctypes.windll.user32.keybd_event(0xB3, 0, 2, 0)
                return "已在网易云音乐中为你切换播放收藏歌曲だぜ！♪"
            elif action == "next":
                ctypes.windll.user32.keybd_event(0xB0, 0, 0, 0)
                ctypes.windll.user32.keybd_event(0xB0, 0, 2, 0)
                return "已为你切换到下一首音乐だぜ！♪"
            elif action == "prev":
                ctypes.windll.user32.keybd_event(0xB1, 0, 0, 0)
                ctypes.windll.user32.keybd_event(0xB1, 0, 2, 0)
                return "已为你倒回上一首音乐だぜ！♪"
            return f"已处理音乐动作：{action}"
        except Exception as e:
            return f"控制网易云音乐失败：{str(e)}"

    @staticmethod
    def ncm_search_song(song_name: str) -> Optional[Tuple[str, str]]:
        """调用网易云 cloudsearch 接口，返回 (song_id, "歌名 - 艺术家") 或 None。免登录免密钥。"""
        try:
            resp = httpx.post(
                "https://music.163.com/api/cloudsearch/pc",
                data={"s": song_name, "type": 1, "limit": 5},
                headers={
                    "Referer": "https://music.163.com/",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                },
                timeout=8.0,
            )
            songs = (resp.json().get("result") or {}).get("songs") or []
            if not songs:
                return None
            top = songs[0]
            song_id = str(top.get("id"))
            name = top.get("name", "未知歌曲")
            artists = top.get("ar") or []
            artist = artists[0].get("name", "") if artists else ""
            display = f"{name} - {artist}" if artist else name
            return song_id, display
        except Exception as e:
            print(f"[tools] ncm_search_song error: {e}")
            return None

    @staticmethod
    def fix_cloudmusic_protocol() -> str:
        """修补 orpheus 协议注册表命令串（委托 ncm_client，确保 3.1.39 能收到 URL）。"""
        return ncm_client.fix_webcmd_protocol()

    @classmethod
    def play_music_search(cls, song_name: str) -> str:
        """搜歌 → 用 orpheus base64 JSON 协议让本机客户端直接播放（3.1.39 实测有效）。"""
        query = song_name.strip()
        if not query:
            return "还没有告诉魔理沙想听什么歌名呢だぜ！"

        # 智能补全歌手名：先把原词搜一次，从 top1 取出歌手，再用"歌手 歌名"再搜一次（命中率更高）
        hit = cls.ncm_search_song(query)
        enriched_query = query
        if hit:
            song_id_rough, display = hit
            try:
                resp = httpx.post(
                    "https://music.163.com/api/cloudsearch/pc",
                    data={"s": query, "type": 1, "limit": 1},
                    headers={"Referer": "https://music.163.com/"},
                    timeout=6.0,
                )
                songs = (resp.json().get("result") or {}).get("songs") or []
                if songs:
                    ar = songs[0].get("ar") or []
                    if ar:
                        artist = ar[0].get("name", "").strip()
                        name = songs[0].get("name", "").strip()
                        if artist and artist not in query:
                            enriched_query = f"{artist} {name}"
            except Exception:
                pass

        hit = cls.ncm_search_song(enriched_query) or hit
        if not hit:
            # 全失败 → 直接打开浏览器搜索页
            return cls._fallback_browser(query)

        song_id, display = hit

        label = ncm_client.play_song(song_id, display)
        return f"已在本机网易云为你播放{label}，尽情享受だぜ！♪"

    @classmethod
    def marisa_recommend_playlist(cls) -> str:
        """切到当日魔理推荐歌单连播（当天未生成则先合成刷新）。"""
        cls.fix_cloudmusic_protocol()
        try:
            from .ncm_recommend import marisa_recommend_now
            return marisa_recommend_now()
        except Exception as e:
            return f"魔理推荐出岔子了：{str(e)[:80]}"

    @classmethod
    def play_cloudmusic_playlist(cls, name: str = "") -> str:
        """播放我喜欢的音乐或指定歌单第一首。"""
        logged_in, _ = ncm_client.check_login()
        if not logged_in:
            return "主人还没登录网易云だぜ！说「扫码登录网易云」，之后就能播你的歌单了♪"
        name = (name or "").strip()
        if name:
            pls = ncm_client.user_playlists()
            best = None
            for pl in pls:
                pl_name = pl.get("name") or ""
                if not pl_name:
                    continue
                if pl_name == name:
                    best = pl
                    break
                if best is None and name in pl_name:
                    best = pl
            if best is None:
                return f"没找到名字带「{name}」的歌单だぜ（看一眼你的歌单名对不对？）"
            playlist_label = best["name"]
        else:
            liked = ncm_client.liked_playlist()
            if not liked:
                return "没找到「我喜欢的音乐」歌单だぜ，换个歌单名试试？"
            best = liked
            playlist_label = best["name"]
        songs = ncm_client.playlist_songs(best["id"])
        if not songs:
            return f"歌单「{playlist_label}」现在是空的だぜ……"
        top = songs[0]
        display = f"{top.get('name')} - {top.get('artist', '')}".rstrip(" -")
        ncm_client.play_song(top["id"], display)
        return f"已开始播放你的歌单「{playlist_label}」（共 {len(songs)} 首）第一首《{display}》だぜ！♪"

    @classmethod
    def play_artist_hot(cls, artist: str) -> str:
        """搜歌手 → 播放热门歌曲第一首。"""
        artist = artist.strip()
        if not artist:
            return "还没说想听哪位歌手呢だぜ！"
        logged_in, _ = ncm_client.check_login()
        if not logged_in:
            return f"主人还没登录网易云だぜ！说「扫码登录网易云」后我就能放 {artist} 的热歌了♪"
        try:
            hit = ncm_client.search_artist(artist)
        except Exception as e:
            hit = None
            print(f"[tools] search_artist error: {e}")
        if not hit:
            return f"没搜到歌手「{artist}」だぜ……"
        artist_name = hit.get("name") or artist
        artist_id = hit["id"]
        try:
            from third_party.cloud_music_mcp import api as _mcp_api
            res = _mcp_api.get_artist_info(artist_id)
        except Exception as e:
            return f"拉取 {artist_name} 的热歌失败了：{str(e)[:80]}"
        if not res.get("success"):
            return f"拉取 {artist_name} 的热歌失败了：{res.get('error', '')}"
        songs = res.get("songs") or []
        if not songs:
            return f"{artist_name} 好像还没有热门歌曲だぜ……"
        top = songs[0]
        display = f"{top.get('name')} - {artist_name}"
        ncm_client.play_song(top["id"], display)
        return f"正在为你播放 {artist_name} 的热门歌曲《{top.get('name')}》だぜ！♪"

    @classmethod
    def save_current_song_to_playlist(cls) -> str:
        """把客户端当前播放的歌收进「魔理沙の宝箱」。"""
        title = ncm_client.current_title()
        if not title:
            return "主人现在好像没在网易云里放歌だぜ（先放一首，再说「收藏这首歌」）"
        if not ncm_client.is_cloudmusic_running():
            return "网易云客户端没开だぜ，先放首歌再来收~"
        hit = cls.ncm_search_song(title)
        if not hit:
            return f"没找到当前播放《{title}》对应的歌曲 id だぜ……"
        song_id, display = hit
        # 固定收集歌单：魔理沙の宝箱
        box_name = "魔理沙の宝箱"
        pls = ncm_client.user_playlists()
        target = next((p for p in pls if (p.get("name") or "").strip() == box_name), None)
        if target is None:
            pid = ncm_client.create_playlist(box_name)
            if not pid:
                return f"创建收集歌单「{box_name}」失败了だぜ"
        else:
            pid = target["id"]
        if not ncm_client.add_songs_to_playlist(pid, [int(song_id)]):
            return f"收藏《{display}》时写入失败だぜ（可能已经收过了？）"
        return f"已把《{display}》收进「{box_name}」だぜ！♪（喜欢的话多说「收藏」）"

    @staticmethod
    def read_clipboard_text() -> str:
        try:
            from .clipboard import get_clipboard_text
            text, kind = get_clipboard_text()
            if kind != "text" or not text:
                return "主人剪贴板里现在不是文字（是图片/文件或空的），换成文字再让我看吧"
            return f"【剪贴板内容，共 {len(text)} 字】：\n{text}"
        except Exception as e:
            return f"读取剪贴板失败：{str(e)[:80]}"

    @staticmethod
    def system_uptime_info() -> str:
        try:
            import ctypes
            ms = ctypes.windll.kernel32.GetTickCount64()
            secs = int(ms // 1000)
            hours, rem = divmod(secs, 3600)
            minutes = rem // 60
            if hours > 0:
                return f"本次开机已运行 {hours} 小时 {minutes} 分（提醒主人：起来喝口水、活动一下再战）"
            return f"本次开机才 {minutes} 分钟，元气满满だぜ！"
        except Exception as e:
            return f"查询开机时长失败：{str(e)[:80]}"

    @classmethod
    def login_cloudmusic_qrcode(cls) -> str:
        """弹二维码并异步等待扫码，登录态自动保存到 data/ncm/cookies.json。"""
        if ncm_client.check_login()[0]:
            return "主人已经登录过网易云了だぜ，无需再扫♪"

        def _on_result(result):
            if result.get("success"):
                print(f"[ncm] 扫码登录成功: {result.get('nickname')}")
            else:
                print(f"[ncm] 扫码登录失败/取消: {result.get('message')}")

        ncm_client.qrcode_login(_on_result)
        return (
            "二维码已弹出（会自动用看图软件打开），请主人 2 分钟内用手机网易云 App 扫一扫；"
            "成功后登录态会自动保存，对我说「来点推荐」就能做魔理推荐了だぜ♪"
        )

    @staticmethod
    def _fallback_browser(query: str) -> str:
        """浏览器网页兜底播放（不会调用本机客户端）"""
        try:
            web_url = f"https://music.163.com/#/search/m/?s={urllib.parse.quote(query)}&type=1"
            webbrowser.open(web_url)
            return f"本机直达没成功，已在浏览器打开「{query}」的搜索结果，点一下就能听だぜ！♪"
        except Exception as e:
            return f"搜索歌曲失败：{str(e)}"

    @staticmethod
    def check_api_balance() -> str:
        ok, msg, _ = BALANCE_CHECKER.query_balance()
        if ok:
            return f"【DeepSeek账户余额检测报告】：{msg}。魔法能源充足，可以继续愉快聊天だぜ！★"
        return f"【DeepSeek余额查询结果】：{msg}"

    @staticmethod
    def get_weather_info(city: str = "") -> str:
        ok, summary, _ = WEATHER_SERVICE.get_weather(city)
        return summary

    @staticmethod
    def open_application(app_name: str) -> str:
        name_clean = app_name.strip().lower()
        target = COMMON_APPS.get(name_clean)
        
        if not target:
            for k, v in COMMON_APPS.items():
                if k in name_clean or name_clean in k:
                    target = v
                    break

        if target:
            try:
                if target.startswith("http") or "://" in target or target.endswith(":"):
                    webbrowser.open(target)
                elif os.path.exists(target):
                    subprocess.Popen([target], shell=True)
                else:
                    os.system(f"start {target}")
                return f"已成功为你打开 {app_name} だぜ！"
            except Exception as e:
                return f"尝试打开 {app_name} 时施法失败：{str(e)}"

        try:
            os.system(f"start {app_name}")
            return f"已尝试在系统中唤起应用「{app_name}」だぜ！"
        except Exception as e:
            return f"未能找到应用「{app_name}」：{str(e)}"

    @staticmethod
    def open_url(url: str) -> str:
        url_clean = url.strip()
        if not (url_clean.startswith("http://") or url_clean.startswith("https://")):
            url_clean = f"https://www.baidu.com/s?wd={url_clean}"
        try:
            webbrowser.open(url_clean)
            return f"已在浏览器中为你打开链接：{url_clean} だぜ！"
        except Exception as e:
            return f"打开网页失败了：{str(e)}"

    @staticmethod
    def system_control(action: str) -> str:
        try:
            if action == "lock_screen":
                ctypes.windll.user32.LockWorkStation()
                return "已帮你锁定屏幕だぜ！回来见！"
            elif action == "volume_mute":
                ctypes.windll.user32.keybd_event(0xAD, 0, 0, 0)
                ctypes.windll.user32.keybd_event(0xAD, 0, 2, 0)
                return "音量静音开关已切换だぜ！"
            elif action == "volume_up":
                for _ in range(5):
                    ctypes.windll.user32.keybd_event(0xAF, 0, 0, 0)
                    ctypes.windll.user32.keybd_event(0xAF, 0, 2, 0)
                return "音量已经调大一点了だぜ！"
            elif action == "volume_down":
                for _ in range(5):
                    ctypes.windll.user32.keybd_event(0xAE, 0, 0, 0)
                    ctypes.windll.user32.keybd_event(0xAE, 0, 2, 0)
                return "音量已经调小一点了だぜ！"
            elif action == "open_recycle_bin":
                os.system("start shell:RecycleBinFolder")
                return "回收站已打开だぜ！"
            return f"未知的系统操作指令：{action}"
        except Exception as e:
            return f"执行系统操作失败：{str(e)}"

    @classmethod
    def execute(cls, tool_name: str, arguments: Dict[str, Any]) -> str:
        if tool_name == "check_api_balance":
            return cls.check_api_balance()
        elif tool_name == "get_weather_info":
            return cls.get_weather_info(arguments.get("city", ""))
        elif tool_name == "play_music_search":
            # 触发点歌前先确保注册表空格已修复
            cls.fix_cloudmusic_protocol()
            return cls.play_music_search(arguments.get("song_name", ""))
        elif tool_name == "marisa_recommend_playlist":
            return cls.marisa_recommend_playlist()
        elif tool_name == "play_cloudmusic_playlist":
            cls.fix_cloudmusic_protocol()
            return cls.play_cloudmusic_playlist(arguments.get("name", ""))
        elif tool_name == "play_artist_hot":
            cls.fix_cloudmusic_protocol()
            return cls.play_artist_hot(arguments.get("artist", ""))
        elif tool_name == "save_current_song_to_playlist":
            return cls.save_current_song_to_playlist()
        elif tool_name == "login_cloudmusic_qrcode":
            return cls.login_cloudmusic_qrcode()
        elif tool_name == "control_cloudmusic":
            return cls.control_cloudmusic(arguments.get("action", "play"))
        elif tool_name == "open_application":
            return cls.open_application(arguments.get("app_name", ""))
        elif tool_name == "open_url":
            return cls.open_url(arguments.get("url", ""))
        elif tool_name == "system_control":
            return cls.system_control(arguments.get("action", ""))
        elif tool_name == "inspect_screen":
            return "__INSPECT_SCREEN__"
        elif tool_name == "read_clipboard_text":
            return cls.read_clipboard_text()
        elif tool_name == "system_uptime_info":
            return cls.system_uptime_info()
        return f"未定义的魔法工具：{tool_name}"
