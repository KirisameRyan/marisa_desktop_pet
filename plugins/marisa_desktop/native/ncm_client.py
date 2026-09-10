# -*- coding: utf-8 -*-
"""网易云音乐本地驱动核心（原生版，专供 MaiBot 及其桌宠插件）。
支持动态检测网易云官方安装路径、修补 Windows 注册表 orpheus 协议，
并通过 orpheus:// base64 协议实现无缝本地客户端切歌、点歌与歌单连播。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time
import winreg
from pathlib import Path
from typing import Any, Dict, List, Optional

_NATIVE_DIR = Path(__file__).resolve().parent
if str(_NATIVE_DIR) not in sys.path:
    sys.path.insert(0, str(_NATIVE_DIR))

import third_party.cloud_music_mcp.auth as _auth
import third_party.cloud_music_mcp.api as _api

# 统一数据目录：指向 MaiBot 的 data/ncm
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
NCM_DATA_DIR = PROJECT_ROOT / "data" / "ncm"
NCM_DATA_DIR.mkdir(parents=True, exist_ok=True)

# 重定向 MCP 库的 storage（cookies/二维码/session）到 MaiBot data 目录
_auth.STORAGE_DIR = str(NCM_DATA_DIR)
_auth.COOKIE_FILE = str(NCM_DATA_DIR / "cookies.json")
_auth.SESSION_FILE = str(NCM_DATA_DIR / "session.bin")


def get_cloudmusic_path() -> str:
    """动态获取本机网易云音乐客户端路径（注册表优先，常见路径兜底）。"""
    # 1. 查注册表 HKCU / HKLM 的 orpheus 协议命令
    sub_path = r"Software\Classes\orpheus\shell\open\command"
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, sub_path, 0, winreg.KEY_READ) as k:
                val, _ = winreg.QueryValueEx(k, "")
                if val:
                    # 格式形如 "D:\...\cloudmusic.exe" --webcmd="%1"
                    clean_val = val.split('"')[1] if '"' in val else val.split()[0]
                    if os.path.exists(clean_val):
                        return clean_val
        except Exception:
            pass

    # 2. 常见候选路径
    candidates = [
        r"D:\LenovoSoftstore\Install\wangyiyunyinyue\cloudmusic.exe",
        r"C:\Program Files (x86)\Netease\CloudMusic\cloudmusic.exe",
        r"C:\Program Files\Netease\CloudMusic\cloudmusic.exe",
        r"D:\CloudMusic\cloudmusic.exe",
        r"D:\Program Files\Netease\CloudMusic\cloudmusic.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return "cloudmusic.exe"


CLOUDMUSIC_PATH = get_cloudmusic_path()


def is_cloudmusic_running() -> bool:
    """检测 cloudmusic.exe 是否在运行。"""
    try:
        output = subprocess.check_output(
            'tasklist /FI "IMAGENAME eq cloudmusic.exe"', shell=True, text=True
        )
        return "cloudmusic.exe" in output.lower()
    except Exception:
        return False


def ensure_cloudmusic_running() -> bool:
    """确保网易云客户端已经打开。"""
    try:
        if is_cloudmusic_running():
            return True
        exe_path = get_cloudmusic_path()
        if os.path.exists(exe_path):
            subprocess.Popen([exe_path])
        else:
            os.system("start orpheus://")
        return True
    except Exception as e:
        print(f"[ncm_client] ensure_cloudmusic_running error: {e}")
        return False


def fix_webcmd_protocol() -> str:
    """修补 orpheus 协议注册表（HKCU 覆盖 HKLM，无需管理员）。
    原值常为 `"...exe"--webcmd="%1"` 缺空格，会吞掉参数；
    改为 `"...exe" --webcmd="%1"`，保证能收到完整 URL。
    """
    try:
        exe_path = get_cloudmusic_path()
        sub_path = r"Software\Classes\orpheus\shell\open\command"
        good = f'"{exe_path}" --webcmd="%1"'
        winreg.CreateKey(winreg.HKEY_CURRENT_USER, sub_path)
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, sub_path, 0, winreg.KEY_READ | winreg.KEY_WRITE
        ) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, good)
        return "ok"
    except Exception as e:
        print(f"[ncm_client] fix_webcmd_protocol error: {e}")
        return str(e)


def _orpheus_url(target_id: Any, target_type: str = "song") -> str:
    """构造 orpheus://<base64 JSON> 协议 URL。"""
    cmd = {"type": target_type, "id": str(target_id), "cmd": "play"}
    payload = json.dumps(cmd, separators=(",", ":")).encode("utf-8")
    return "orpheus://" + base64.b64encode(payload).decode("ascii")


def play_song(song_id: Any, display_name: str = "", song_type: str = "song") -> str:
    """通过 orpheus base64 协议让本机客户端播放指定 id 歌曲。"""
    fix_webcmd_protocol()
    ensure_cloudmusic_running()
    time.sleep(1.2)
    url = _orpheus_url(song_id, song_type)
    try:
        os.startfile(url)
    except Exception:
        exe_path = get_cloudmusic_path()
        if os.path.exists(exe_path):
            subprocess.Popen([exe_path, f"--webcmd={url}"])
        else:
            os.system(f'start "" "{url}"')
    label = f"《{display_name}》" if display_name else f"(id:{song_id})"
    return label


def play_playlist(playlist_id: Any, display_name: str = "") -> str:
    """通过 orpheus base64 协议让客户端切入歌单并整单连播。"""
    fix_webcmd_protocol()
    ensure_cloudmusic_running()
    time.sleep(1.2)
    url = _orpheus_url(playlist_id, "playlist")
    try:
        os.startfile(url)
    except Exception:
        exe_path = get_cloudmusic_path()
        if os.path.exists(exe_path):
            subprocess.Popen([exe_path, f"--webcmd={url}"])
        else:
            os.system(f'start "" "{url}"')
    label = f"歌单《{display_name}》" if display_name else f"歌单 (id:{playlist_id})"
    return label


def search_first_song(keyword: str) -> Optional[Dict[str, Any]]:
    """搜索网易云音乐（无需登录），返回首个匹配的单曲信息。"""
    try:
        res = _api.search(keyword, category="song", limit=5)
        items = res.get("items") or []
        if not res.get("success") or not items:
            return None
        first = items[0]
        name = str(first.get("name") or "")
        artist = str(first.get("artist") or "")
        return {
            "id": first.get("id"),
            "name": name,
            "artist": artist,
            "display": f"{name} - {artist}" if artist else name,
        }
    except Exception as e:
        print(f"[ncm_client] search error: {e}")
    return None


def play_search(keyword: str) -> str:
    """直接根据歌名/歌手搜索并播放。"""
    song = search_first_song(keyword)
    if not song:
        return f"在网易云没搜到关于「{keyword}」的歌曲だぜ……"
    play_song(song["id"], song["display"])
    return f"正在为主人播放：{song['display']} ♪"


def search_playlist(keyword: str, limit: int = 10) -> List[Dict[str, Any]]:
    """搜索公开歌单（无需登录），按收录数降序返回 [{id,name,count}]。"""
    try:
        res = _api.search(keyword, category="playlist", limit=limit)
    except Exception as e:
        print(f"[ncm_client] search_playlist error: {e}")
        return []
    if not res.get("success"):
        return []
    items = res.get("items") or []
    items.sort(key=lambda x: int(x.get("count") or 0), reverse=True)
    return items


# ---------------- 登录态与扫码登录 ----------------

def current_user() -> Optional[Dict[str, Any]]:
    """读取当前登录的网易云用户信息；未登录返回 None。"""
    try:
        if not _auth.load_session()[0]:
            return None
        user = _api.apis.login.GetCurrentLoginStatus()
        profile = (user or {}).get("profile") or {}
        if not profile or not profile.get("userId"):
            return None
        return {
            "userId": profile.get("userId"),
            "nickname": str(profile.get("nickname") or "用户"),
        }
    except Exception as e:
        print(f"[ncm_client] current_user error: {e}")
        return None


def check_login() -> tuple:
    """检查登录状态，返回 (logged_in: bool, nickname: Optional[str])。"""
    user = current_user()
    if user:
        return True, user["nickname"]
    return False, None


def check_login_status() -> Dict[str, Any]:
    """检查网易云登录状态。"""
    user = current_user()
    if user:
        return {"logged_in": True, "nickname": user["nickname"], "userId": user["userId"]}
    return {"logged_in": False}


def qrcode_png_path() -> Path:
    """扫码登录二维码图片保存路径。"""
    return NCM_DATA_DIR / "login_qrcode.png"


def wait_qrcode_login(open_img: bool = True) -> Dict[str, Any]:
    """阻塞式扫码登录（约 2 分钟超时），返回 {"success": bool, "message": str, ...}。"""
    return _auth.login_via_qrcode(open_img=open_img)


def qrcode_login(callback, timeout: int = 120, open_img: bool = True):
    """后台线程执行扫码登录；登录结果经 callback(result) 通知，避免阻塞调用方。"""
    del timeout

    def _work() -> None:
        try:
            result = wait_qrcode_login(open_img=open_img)
        except Exception as e:
            result = {"success": False, "message": f"扫码登录异常: {e}"}
        try:
            callback(result)
        except Exception:
            pass

    thread = threading.Thread(target=_work, daemon=True)
    thread.start()
    return thread


# ---------------- 歌单与曲库管理（登录后可用） ----------------

def _api_apis():
    """获取 pyncm apis 模块（供内部封装 weapi 请求）。"""
    from pyncm import apis
    return apis


def _current_uid() -> int:
    """返回当前登录用户的 userId；未登录抛异常。"""
    info = _api_apis().login.GetCurrentLoginStatus()
    if not info.get("profile"):
        raise RuntimeError("未登录")
    return info["profile"].get("userId") or 0


def user_playlists() -> List[Dict[str, Any]]:
    """返回账号下的歌单列表 [{id,name,count,is_mine}]；未登录返回 []。"""
    try:
        res = _api.get_user_playlists()
    except Exception as e:
        print(f"[ncm_client] user_playlists error: {e}")
        return []
    if not res.get("success"):
        return []
    out = []
    for pl in res.get("playlists") or []:
        out.append({
            "id": pl.get("id"),
            "name": pl.get("name"),
            "count": pl.get("count", 0),
            "is_mine": bool(pl.get("is_mine")),
        })
    return out


def liked_playlist() -> Dict[str, Any]:
    """找「我喜欢的音乐」歌单；找不到返回 {}。"""
    for pl in user_playlists():
        name = (pl.get("name") or "").strip()
        if name == "我喜欢的音乐" or name.endswith("喜欢的音乐"):
            return pl
    return {}


def get_user_liked_playlist_id() -> Optional[int]:
    """获取用户「我喜欢的音乐」歌单 ID。"""
    pl = liked_playlist()
    return pl.get("id") if pl else None


def playlist_songs(playlist_id: Any) -> List[Dict[str, Any]]:
    """取歌单内歌曲 [{id,name,artist,album}]（上限约 1000）。"""
    try:
        res = _api.get_playlist_detail(playlist_id)
    except Exception as e:
        print(f"[ncm_client] playlist_songs error: {e}")
        return []
    if not res.get("success"):
        return []
    return res.get("songs") or []


def create_playlist(name: str) -> Optional[int]:
    """新建歌单，返回 playlist_id；失败返回 None。"""
    try:
        res = _api.create_playlist(name)
        if res.get("success"):
            return res.get("playlist_id")
    except Exception as e:
        print(f"[ncm_client] create_playlist error: {e}")
    return None


def add_songs_to_playlist(playlist_id: Any, track_ids: List[int]) -> bool:
    """向歌单追加歌曲。"""
    try:
        res = _api.add_to_playlist(playlist_id, track_ids)
        return bool(res.get("success"))
    except Exception as e:
        print(f"[ncm_client] add_songs error: {e}")
        return False


def remove_songs_from_playlist(playlist_id: Any, track_ids: List[int]) -> bool:
    """从歌单批量移除（用于覆写歌单时清空）。"""
    if not track_ids:
        return True
    try:
        result = _api_apis().playlist.SetManipulatePlaylistTracks(
            trackIds=list(track_ids), playlistId=playlist_id, op="del"
        )
        return result.get("code") == 200
    except Exception as e:
        print(f"[ncm_client] remove_songs error: {e}")
        return False


def daily_recommendations() -> Dict[str, Any]:
    """获取网易云每日推荐（需登录）。"""
    try:
        return _api.get_daily_recommendations()
    except Exception as e:
        return {"success": False, "error": str(e)}


def frequent_songs(limit: int = 50) -> Optional[List[Dict[str, Any]]]:
    """常听排行（全部时间 type=1）。成功返回 [{id,name,artist}]；失败/未登录返回 None。"""
    try:
        uid = _current_uid()
        apis = _api_apis()

        @apis.WeapiCryptoRequest
        def GetPlayRecordInternal():
            return "/weapi/v1/play/record", {
                "uid": str(uid), "type": "1",
                "limit": str(limit), "offset": "0", "total": "true",
            }

        result = GetPlayRecordInternal()
        if result.get("code") != 200:
            return None
        # /weapi/v1/play/record 返回 weekData(每周)/allData(全部时间)
        entries = (result.get("allData") or result.get("weekData")
                   or (result.get("data") or {}).get("list") or [])
        songs = []
        for entry in entries:
            s = entry.get("song") or entry
            song_id = s.get("id")
            name = s.get("name")
            if not song_id or not name:
                continue
            ar = s.get("ar") or s.get("artists") or []
            artist = ar[0].get("name", "") if ar else ""
            songs.append({"id": song_id, "name": name, "artist": artist})
            if len(songs) >= limit:
                break
        return songs or None
    except Exception as e:
        print(f"[ncm_client] frequent_songs error: {e}")
        return None
