# -*- coding: utf-8 -*-
"""桌宠执行类工具：供 MaiBot（marisa-desktop 网关）反向调用。

所有函数返回 {"text": str, "image_b64": str=""}，由桌宠进程在本机执行，
结果文本/图片回传 MaiBot → 再回到 QQ。
"""
from __future__ import annotations

import base64
import datetime
import time
from pathlib import Path
from typing import Any, Dict

from core.reminders import REMINDER_STORE
from core.tools import ToolExecutor
from core import ncm_client


def _ok(text: str, image_b64: str = "") -> Dict[str, Any]:
    return {"text": text, "image_b64": image_b64}


def run(action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """动作分发主入口。"""
    handler = _HANDLERS.get(action)
    if handler is None:
        return _ok(f"未知动作 {action}，魔理沙没学过这个魔法だぜ……")
    try:
        return handler(payload or {})
    except Exception as exc:
        return _ok(f"执行失败：{str(exc)[:120]}")


# ---------------- 音乐 ----------------

def _music_search(p: Dict[str, Any]) -> Dict[str, Any]:
    return _ok(ToolExecutor.play_music_search(str(p.get("kw") or "")))


def _marisa_recommend(p: Dict[str, Any]) -> Dict[str, Any]:
    return _ok(ToolExecutor.marisa_recommend_playlist())


def _playlist_like(p: Dict[str, Any]) -> Dict[str, Any]:
    return _ok(ToolExecutor.play_cloudmusic_playlist("我喜欢的音乐"))


def _playlist_named(p: Dict[str, Any]) -> Dict[str, Any]:
    return _ok(ToolExecutor.play_cloudmusic_playlist(str(p.get("name") or "")))


def _control(p: Dict[str, Any]) -> Dict[str, Any]:
    return _ok(ToolExecutor.control_cloudmusic(str(p.get("action") or "play")))


def _save_song(p: Dict[str, Any]) -> Dict[str, Any]:
    return _ok(ToolExecutor.save_current_song_to_playlist())


# ---------------- 系统 / 桌面 ----------------

def _open_app(p: Dict[str, Any]) -> Dict[str, Any]:
    return _ok(ToolExecutor.open_application(str(p.get("name") or "")))


def _volume(p: Dict[str, Any]) -> Dict[str, Any]:
    return _ok(ToolExecutor.system_control(str(p.get("action") or "volume_mute")))


def _clipboard(p: Dict[str, Any]) -> Dict[str, Any]:
    text = ToolExecutor.read_clipboard_text()
    if not text or not text.strip():
        return _ok("剪贴板现在是空的だぜ……")
    return _ok(f"剪贴板内容是：\n{text[:400]}{'…' if len(text) > 400 else ''}")


# ---------------- 提醒 ----------------

def _reminder_list(p: Dict[str, Any]) -> Dict[str, Any]:
    items = REMINDER_STORE.list_reminders()
    if not items:
        return _ok("主人还没有设置提醒だぜ。")
    lines = ["⏰ 主人的提醒："]
    for i, r in enumerate(items, 1):
        kind = "每天" if r.get("kind") == "daily" else "单次"
        lines.append(f"{i}. [{kind} {r.get('time')}] {r.get('text')}")
    return _ok("\n".join(lines))


def _reminder_del(p: Dict[str, Any]) -> Dict[str, Any]:
    rid = int(p.get("rid") or 0)
    if REMINDER_STORE.remove(rid):
        return _ok(f"已删除第 {rid} 条提醒だぜ！")
    return _ok(f"没找到第 {rid} 条提醒だぜ……用「查看提醒」看看编号？")


def _reminder_add(p: Dict[str, Any]) -> Dict[str, Any]:
    hm = str(p.get("hm") or "")
    kind = str(p.get("kind") or "once")
    text = str(p.get("text") or "提醒")
    REMINDER_STORE.add(text, hm, kind=kind)
    label = "每天" if kind == "daily" else "单次"
    return _ok(f"好嘞！已设置【{label} {hm}】提醒：{text}だぜ★")


def _reminder_add_min(p: Dict[str, Any]) -> Dict[str, Any]:
    minutes = int(p.get("minutes") or 5)
    text = str(p.get("text") or "提醒")
    hm = (datetime.datetime.now() + datetime.timedelta(minutes=minutes)).strftime("%H:%M")
    REMINDER_STORE.add(text, hm, kind="once")
    return _ok(f"好嘞！{minutes} 分钟后（{hm}）提醒你：{text}だぜ★")


# ---------------- 网易云扫码登录（回图到 QQ） ----------------

def _ncm_login_qr(p: Dict[str, Any]) -> Dict[str, Any]:
    if ncm_client.check_login()[0]:
        return _ok("主人已经登录过网易云了だぜ，无需再扫♪")
    try:
        Path(ncm_client.qrcode_png_path()).unlink(missing_ok=True)
    except Exception:
        pass

    def _on_result(result: Dict[str, Any]):
        if result.get("success"):
            print(f"[ncm] QQ 扫码登录成功: {result.get('nickname')}")

    ncm_client.qrcode_login(_on_result, open_img=False)
    # 等二维码文件生成（最多 6 秒）
    qr_path = ncm_client.qrcode_png_path()
    for _ in range(12):
        if qr_path.exists() and qr_path.stat().st_size > 1000:
            try:
                b64 = base64.b64encode(qr_path.read_bytes()).decode("ascii")
                return _ok(
                    "登录二维码来了だぜ！2 分钟内用手机网易云 App 扫一扫★\n成功后对我说「放歌」就能用啦。",
                    image_b64=b64,
                )
            except Exception:
                break
        time.sleep(0.5)
    return _ok("二维码生成失败了……可以直接在桌宠的「网易云音乐」菜单里扫码だぜ。")



_HANDLERS = {
    "music_search": _music_search,
    "marisa_recommend": _marisa_recommend,
    "playlist_like": _playlist_like,
    "playlist_named": _playlist_named,
    "control": _control,
    "save_song": _save_song,
    "open_app": _open_app,
    "volume": _volume,
    "clipboard": _clipboard,
    "reminder_list": _reminder_list,
    "reminder_del": _reminder_del,
    "reminder_add": _reminder_add,
    "reminder_add_min": _reminder_add_min,
    "ncm_login_qr": _ncm_login_qr,
}
