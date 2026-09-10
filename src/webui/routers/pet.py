# -*- coding: utf-8 -*-
"""魔理沙桌宠 WebUI API 路由模块。

提供桌宠状态监控、屏幕快照查看、多媒体播控与桌面远程指令执行能力，
并作为桌宠唯一的配置入口（桌宠自身的 PyQt 设置面板已下线）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.common.logger import get_logger
from src.webui.dependencies import require_auth

logger = get_logger("webui.pet")

router = APIRouter(prefix="/pet", tags=["魔理沙桌宠"])

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_PET_CONFIG_PATH = _PROJECT_ROOT / "config.json"
_FAVORABILITY_PATH = _PROJECT_ROOT / "data" / "marisa" / "favorability.json"

# 桌宠配置里允许 WebUI 读写的字段（API Key 等敏感项不在其中，
# 模型密钥统一由「模型配置」页写入 config/model_config.toml）
_PET_EDITABLE_KEYS = (
    "auto_screen_inspect",
    "inspect_interval_seconds",
    "scale",
    "song_notify",
    "hourly_chime",
    "marisa_daily_notify",
    "presence_watch",
    "presence_idle_seconds",
    "magic_delete",
    "hotkey_enable",
    "hotkey_combo",
    "zany_auto",
    "zany_level",
)
_TTS_EDITABLE_KEYS = (
    "gpt_sovits_api_url",
    "ref_audio_path",
    "prompt_text",
    "speed_factor",
    "auto_start_api",
)

# 尝试导入插件 native actions
_PLUGIN_NATIVE_DIR = _PROJECT_ROOT / "plugins" / "marisa_desktop" / "native"
if str(_PLUGIN_NATIVE_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_NATIVE_DIR))

try:
    from actions import ACTIONS
except Exception as _e:
    ACTIONS = None
    logger.warning(f"未能直接导入桌宠 actions: {_e}")

try:
    import ncm_client as NCM_CLIENT
except Exception as _e:
    NCM_CLIENT = None
    logger.warning(f"未能直接导入网易云客户端: {_e}")


class ActionRequest(BaseModel):
    action: str = Field(..., description="动作类型，如 music_control, volume, play_music, lock_screen 等")
    param: Optional[str] = Field(default="", description="动作参数")


class SayRequest(BaseModel):
    text: str = Field(..., description="气泡展示与播音文本")


class PetConfigPatch(BaseModel):
    """桌宠配置的部分更新请求（只提交需要改动的字段）。"""

    pet: Dict[str, Any] = Field(default_factory=dict, description="桌宠行为与外观字段")
    tts: Dict[str, Any] = Field(default_factory=dict, description="本地 GPT-SoVITS 语音参数")


def _read_pet_config() -> Dict[str, Any]:
    """读取 config.json（缺失或损坏时视为空配置）。"""

    try:
        return json.loads(_PET_CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"config.json 解析失败：{exc}") from exc


def _pick_keys(section: Dict[str, Any], keys: tuple) -> Dict[str, Any]:
    """按白名单挑出可暴露给前端的字段。"""

    return {key: section[key] for key in keys if key in section}


@router.get("/config", dependencies=[Depends(require_auth)])
async def get_pet_config() -> Dict[str, Any]:
    """读取桌宠行为与语音配置（不含任何密钥）。"""

    data = _read_pet_config()
    return {
        "status": "ok",
        "data": {
            "pet": _pick_keys(data.get("pet") or {}, _PET_EDITABLE_KEYS),
            "tts": _pick_keys(data.get("tts") or {}, _TTS_EDITABLE_KEYS),
        },
    }


@router.put("/config", dependencies=[Depends(require_auth)])
async def update_pet_config(patch: PetConfigPatch) -> Dict[str, Any]:
    """更新桌宠配置；桌宠进程会自行检测文件变化并热重载。"""

    unknown = [key for key in patch.pet if key not in _PET_EDITABLE_KEYS]
    unknown += [f"tts.{key}" for key in patch.tts if key not in _TTS_EDITABLE_KEYS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"不支持修改的配置项: {', '.join(unknown)}")

    data = _read_pet_config()
    pet_section = data.setdefault("pet", {})
    tts_section = data.setdefault("tts", {})
    pet_section.update(patch.pet)
    tts_section.update(patch.tts)

    _PET_CONFIG_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"桌宠配置已更新: pet={sorted(patch.pet)} tts={sorted(patch.tts)}")
    return {"status": "ok", "data": {"pet": pet_section, "tts": tts_section}}


@router.get("/favorability", dependencies=[Depends(require_auth)])
async def get_pet_favorability() -> Dict[str, Any]:
    """读取跨端好感度（data/marisa/favorability.json 为唯一来源）。"""

    try:
        data = json.loads(_FAVORABILITY_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"status": "ok", "data": {"favorability": 0, "total_interactions": 0, "available": False}}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"好感度文件解析失败：{exc}") from exc

    favorability = int(data.get("favorability", 0))
    if favorability >= 80:
        rank = "灵魂挚友"
    elif favorability >= 60:
        rank = "亲密搭档"
    elif favorability >= 40:
        rank = "可靠的搭档"
    elif favorability >= 20:
        rank = "熟络的助手"
    else:
        rank = "初识的魔法使"
    return {
        "status": "ok",
        "data": {
            "favorability": favorability,
            "total_interactions": int(data.get("total_interactions", 0)),
            "rank": rank,
            "updated_at": data.get("updated_at", ""),
            "available": True,
        },
    }


@router.get("/ncm/status", dependencies=[Depends(require_auth)])
async def get_ncm_status() -> Dict[str, Any]:
    """查询网易云登录状态与二维码是否已生成。"""

    if NCM_CLIENT is None:
        raise HTTPException(status_code=500, detail="网易云客户端未就绪")

    logged_in, nickname = await asyncio.to_thread(NCM_CLIENT.check_login)
    qr_path = NCM_CLIENT.qrcode_png_path()
    return {
        "status": "ok",
        "data": {
            "logged_in": bool(logged_in),
            "nickname": nickname,
            "has_qrcode": qr_path.exists(),
            "qrcode_updated_at": qr_path.stat().st_mtime if qr_path.exists() else 0,
        },
    }


@router.post("/ncm/login", dependencies=[Depends(require_auth)])
async def start_ncm_login() -> Dict[str, Any]:
    """发起网易云扫码登录并返回二维码图片（供 WebUI 内嵌展示）。"""

    if NCM_CLIENT is None:
        raise HTTPException(status_code=500, detail="网易云客户端未就绪")

    qr_path = NCM_CLIENT.qrcode_png_path()
    qr_path.parent.mkdir(parents=True, exist_ok=True)
    if qr_path.exists():
        qr_path.unlink()

    async def _run_login() -> None:
        await asyncio.to_thread(NCM_CLIENT.qrcode_login, None, 120, False)

    asyncio.create_task(_run_login())

    # 等二维码落盘：手机端扫码窗口只有两分钟，这里最多等 8 秒
    for _ in range(16):
        await asyncio.sleep(0.5)
        if qr_path.exists():
            break

    if not qr_path.exists():
        raise HTTPException(status_code=504, detail="二维码生成超时，请重试")

    image_b64 = base64.b64encode(qr_path.read_bytes()).decode("utf-8")
    return {
        "status": "ok",
        "data": {
            "image_base64": f"data:image/png;base64,{image_b64}",
            "tip": "请用手机网易云 App 扫码登录，两分钟内有效",
        },
    }


def _is_port_open(port: int = 8400, host: str = "127.0.0.1") -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _get_uptime_str() -> str:
    try:
        import ctypes

        lib = ctypes.windll.kernel32
        uptime_ms = lib.GetTickCount64()
        total_sec = int(uptime_ms / 1000)
        h = total_sec // 3600
        m = (total_sec % 3600) // 60
        return f"{h} 小时 {m} 分钟"
    except Exception:
        return "未知"


@router.get("/status", dependencies=[Depends(require_auth)])
async def get_pet_status() -> Dict[str, Any]:
    """获取桌宠运行状态与网关健康度。"""
    ws_online = _is_port_open(8400)

    # 检查桌宠进程
    process_running = False
    try:
        import subprocess

        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV"],
            text=True,
            errors="ignore",
        )
        process_running = "python.exe" in out
    except Exception:
        process_running = False

    uptime = _get_uptime_str()

    return {
        "status": "ok",
        "data": {
            "ws_online": ws_online,
            "ws_port": 8400,
            "process_running": process_running,
            "uptime": uptime,
            "character": "雾雨魔理沙",
            "personality": "普通魔法使 (Da-ze!)",
        },
    }


@router.get("/screenshot", dependencies=[Depends(require_auth)])
async def get_pet_screenshot() -> Dict[str, Any]:
    """截取当前屏幕缩略图并返回 base64。"""
    if ACTIONS is None:
        raise HTTPException(status_code=500, detail="桌宠原生执行引擎未就绪")
    try:
        path, b64 = ACTIONS.capture_screenshot()
        return {
            "status": "ok",
            "data": {
                "image_base64": f"data:image/png;base64,{b64}",
                "timestamp": int(time.time()),
            },
        }
    except Exception as exc:
        logger.error(f"截图失败: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"截图失败: {exc}")


@router.post("/action", dependencies=[Depends(require_auth)])
async def execute_pet_action(req: ActionRequest) -> Dict[str, Any]:
    """执行桌宠本机动作（多媒体、系统音量、锁屏等）。"""
    if ACTIONS is None:
        raise HTTPException(status_code=500, detail="桌宠原生执行引擎未就绪")

    action = req.action.lower().strip()
    param = (req.param or "").strip()
    msg = ""

    try:
        if action == "play_music":
            msg = ACTIONS.play_music(param)
        elif action == "music_control":
            msg = ACTIONS.control_music(param)
        elif action == "play_recommend":
            msg = ACTIONS.play_recommend()
        elif action == "play_my_playlist":
            msg = ACTIONS.play_my_playlist()
        elif action == "volume":
            msg = ACTIONS.control_volume(param)
        elif action == "lock_screen":
            msg = ACTIONS.lock_workstation()
        elif action == "open_app":
            msg = ACTIONS.open_app(param)
        else:
            raise HTTPException(status_code=400, detail=f"未知动作: {action}")

        return {"status": "ok", "message": msg}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"执行动作 {action} 失败: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"执行失败: {exc}")
