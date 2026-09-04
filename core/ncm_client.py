import base64
import json
import os
import subprocess
import sys
import threading
import time
import winreg
from pathlib import Path

import third_party.cloud_music_mcp.auth as _auth
import third_party.cloud_music_mcp.api as _api

CLOUDMUSIC_PATH = r"D:\LenovoSoftstore\Install\wangyiyunyinyue\cloudmusic.exe"

if getattr(sys, "frozen", False):
    ROOT_DIR = Path(sys.executable).resolve().parent
else:
    ROOT_DIR = Path(__file__).resolve().parent.parent

NCM_DATA_DIR = ROOT_DIR / "data" / "ncm"

# 重定向 MCP 库的 storage（cookies/二维码/session）到桌宠 data 目录
_auth.STORAGE_DIR = str(NCM_DATA_DIR)
_auth.COOKIE_FILE = str(NCM_DATA_DIR / "cookies.json")
_auth.SESSION_FILE = str(NCM_DATA_DIR / "session.bin")


def is_cloudmusic_running() -> bool:
    try:
        output = subprocess.check_output(
            'tasklist /FI "IMAGENAME eq cloudmusic.exe"', shell=True, text=True
        )
        return "cloudmusic.exe" in output.lower()
    except Exception:
        return False


def ensure_cloudmusic_running() -> bool:
    try:
        if is_cloudmusic_running():
            return True
        if os.path.exists(CLOUDMUSIC_PATH):
            subprocess.Popen([CLOUDMUSIC_PATH])
        else:
            os.system("start orpheus://")
        return True
    except Exception as e:
        print(f"[ncm_client] ensure_cloudmusic_running error: {e}")
        return False


def fix_webcmd_protocol() -> str:
    """修补 orpheus 协议注册表（HKCU 覆盖 HKLM，无需管理员）。
    原值常为 `"...exe"--webcmd="%1"` 缺空格，会吞掉参数；
    改为 `"...exe" --webcmd="%1"`，保证 3.1.39 能收到完整 URL。"""
    try:
        sub_path = r"Software\Classes\orpheus\shell\open\command"
        good = f'"{CLOUDMUSIC_PATH}" --webcmd="%1"'
        winreg.CreateKey(winreg.HKEY_CURRENT_USER, sub_path)
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, sub_path, 0, winreg.KEY_READ | winreg.KEY_WRITE
        ) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, good)
        return "ok"
    except Exception as e:
        print(f"[ncm_client] fix_webcmd_protocol error: {e}")
        return str(e)


def _orpheus_url(song_id, song_type="song") -> str:
    """构造 orpheus://<base64 JSON> 协议 URL（3.1.39 实测可播放）。"""
    cmd = {"type": song_type, "id": str(song_id), "cmd": "play"}
    payload = json.dumps(cmd, separators=(",", ":")).encode("utf-8")
    return "orpheus://" + base64.b64encode(payload).decode("ascii")


def play_song(song_id, display_name="", song_type="song") -> str:
    """通过 orpheus base64 协议让本机客户端播放指定 id。"""
    fix_webcmd_protocol()
    ensure_cloudmusic_running()
    time.sleep(1.5)
    url = _orpheus_url(song_id, song_type)
    try:
        os.startfile(url)
    except Exception as e:
        # 兜底：直接以命令行参数唤起
        if os.path.exists(CLOUDMUSIC_PATH):
            subprocess.Popen([CLOUDMUSIC_PATH, f"--webcmd={url}"])
        else:
            os.system(f'start "" "{url}"')
    label = f"《{display_name}》" if display_name else f"(id:{song_id})"
    return label


def play_playlist(playlist_id, display_name="") -> str:
    """通过 orpheus base64 协议让客户端切入歌单并整单连播（3.1.39 实测有效）。
    失败时回退为仅提示；调用方可再降级 play_song 第一首。"""
    try:
        fix_webcmd_protocol()
        ensure_cloudmusic_running()
        time.sleep(1.5)
        url = _orpheus_url(playlist_id, "playlist")
        os.startfile(url)
        ok = True
    except Exception as e:
        print(f"[ncm_client] play_playlist startfile error: {e}")
        ok = False
    label = f"歌单《{display_name}》" if display_name else f"歌单(id:{playlist_id})"
    return label if ok else ""


def _cloudmusic_pids() -> set:
    """返回所有 cloudmusic.exe 的 PID 集合。"""
    try:
        import ctypes
        import struct

        kernel32 = ctypes.windll.kernel32
        snapshot = kernel32.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
        if snapshot == -1:
            return set()
        try:
            # PROCESSENTRY32W (x64 实测): dwSize=568, th32ProcessID@8, szExeFile@44
            DW = ctypes.c_ubyte * 600
            entry = DW()
            struct.pack_into("<I", entry, 0, 568)
            if not kernel32.Process32FirstW(snapshot, entry):
                return set()
            pids = set()
            while True:
                exe = ctypes.create_unicode_buffer(260)
                ctypes.memmove(exe, ctypes.addressof(entry) + 44, 260 * 2)
                pid = struct.unpack_from("<I", entry, 8)[0]
                if exe.value.lower() == "cloudmusic.exe":
                    pids.add(pid)
                if not kernel32.Process32NextW(snapshot, entry):
                    break
            return pids
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception:
        return set()


def current_title() -> str:
    """读取 cloudmusic 主窗口标题（通常形如「歌名 - 歌手」），失败返回空串。"""
    try:
        import ctypes

        EnumProc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
        )
        cloud_pids = _cloudmusic_pids()
        titles = []

        def cb(hwnd, lparam):
            if not ctypes.windll.user32.IsWindowVisible(hwnd):
                return True
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            pid = ctypes.c_ulong()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in cloud_pids:
                titles.append(buf.value)
            return True

        ctypes.windll.user32.EnumWindows(EnumProc(cb), 0)
        # 主窗口标题通常是第一个且带 " - "
        for t in titles:
            if " - " in t:
                return t
        return titles[0] if titles else ""
    except Exception:
        return ""


# ---------------- 登录 ----------------

def check_login():
    """返回 (logged_in: bool, nickname: str|None)。"""
    try:
        status = _auth.check_login_status()
        return bool(status.get("logged_in")), status.get("nickname")
    except Exception as e:
        print(f"[ncm_client] check_login error: {e}")
        return False, None


def wait_qrcode_login(open_img=True):
    """阻塞式扫码登录（最多约 2 分钟），返回 result dict。"""
    return _auth.login_via_qrcode(open_img=open_img)


def qrcode_login(callback, timeout=120, open_img=True):
    """后台线程跑扫码登录，完成后回调 result dict。"""
    def work():
        try:
            result = wait_qrcode_login(open_img=open_img)
        except Exception as e:
            result = {"success": False, "message": f"扫码登录异常: {e}"}
        try:
            callback(result)
        except Exception:
            pass

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread


def qrcode_png_path() -> Path:
    return NCM_DATA_DIR / "login_qrcode.png"


# ---------------- 歌单/推荐/搜索 ----------------

def search_songs(keyword, limit=5):
    return _api.search(keyword, category="song", limit=limit)


def search_artist(keyword, limit=5):
    """搜歌手，返回第一个 {id,name}（优先精确匹配名）。"""
    try:
        res = _api.search(keyword, category="artist", limit=limit)
    except Exception:
        return None
    if not res.get("success"):
        return None
    items = res.get("items") or []
    if not items:
        return None
    kw = keyword.strip().lower()
    for it in items:
        if (it.get("name") or "").strip().lower() == kw:
            return it
    return items[0]


def daily_recommendations():
    return _api.get_daily_recommendations()


# ---------------- 歌单与曲库管理（登录后可用） ----------------

def _current_uid() -> int:
    """返回当前登录用户的 userId；未登录抛异常。"""
    info = _api_apis().login.GetCurrentLoginStatus()
    if not info.get("profile"):
        raise RuntimeError("未登录")
    return info["profile"].get("userId") or 0


def _api_apis():
    from pyncm import apis
    return apis


def user_playlists() -> list:
    """返回我的歌单+收藏歌单列表 [{id,name,count,is_mine}]；未登录返回 []。"""
    try:
        res = _api.get_user_playlists()
    except Exception:
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


def liked_playlist() -> dict:
    """找「我喜欢的音乐」歌单；找不到返回 {}。"""
    for pl in user_playlists():
        name = (pl.get("name") or "").strip()
        if name == "我喜欢的音乐" or name.endswith("喜欢的音乐"):
            return pl
    return {}


def playlist_songs(playlist_id) -> list:
    """取歌单内歌曲 [{id,name,artist,album}]（上限约 1000）。"""
    try:
        res = _api.get_playlist_detail(playlist_id)
    except Exception:
        return []
    if not res.get("success"):
        return []
    return res.get("songs") or []


def create_playlist(name: str):
    """新建歌单，返回 playlist_id；失败返回 None。"""
    try:
        res = _api.create_playlist(name)
        if res.get("success"):
            return res.get("playlist_id")
    except Exception as e:
        print(f"[ncm_client] create_playlist error: {e}")
    return None


def add_songs_to_playlist(playlist_id, track_ids) -> bool:
    try:
        res = _api.add_to_playlist(playlist_id, track_ids)
        return bool(res.get("success"))
    except Exception as e:
        print(f"[ncm_client] add_songs error: {e}")
        return False


def remove_songs_from_playlist(playlist_id, track_ids) -> bool:
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


def frequent_songs(limit: int = 50):
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
