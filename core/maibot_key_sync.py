# -*- coding: utf-8 -*-
"""MaiBot Key 单源同步：桌宠 config.json 为唯一权威，同步进 MaiBot model_config.toml。

用法：
    python core/maibot_key_sync.py     # 命令行直接同步
    from core.maibot_key_sync import sync_keys

触发点：启动一体化.ps1（启 MaiBot 前）、桌宠设置保存后、marisa-desktop 插件 on_load。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

CANONICAL_CONFIG = Path(r"D:\zhuochong\config.json")
MAIBOT_MODEL_CONFIG = Path(r"D:\MaiBot-1.2.4\config\model_config.toml")


def _read_canonical_keys() -> dict:
    """读取权威钥匙库（桌宠 config.json）。"""
    data = json.loads(CANONICAL_CONFIG.read_text(encoding="utf-8"))
    ds = (data.get("deepseek") or {})
    vis = (data.get("vision") or {})
    emb = (data.get("embedding") or {})
    return {
        "DeepSeek": {"api_key": str(ds.get("api_key") or ""), "base_url": str(ds.get("base_url") or "https://api.deepseek.com")},
        "Zhipu": {"api_key": str(vis.get("api_key") or ""), "base_url": str(vis.get("base_url") or "https://open.bigmodel.cn/api/paas/v4")},
        "SiliconFlow": {"api_key": str(emb.get("api_key") or ""), "base_url": str(emb.get("base_url") or "https://api.siliconflow.cn/v1")},
    }


def _sync_api_keys(keys: dict) -> int:
    """逐 provider 块替换 api_key / base_url，返回改动行数。"""
    lines = MAIBOT_MODEL_CONFIG.read_text(encoding="utf-8").splitlines()
    changed = 0
    current_provider = None
    for i, line in enumerate(lines):
        if line.strip() == "[[api_providers]]":
            current_provider = None
            continue
        if current_provider is None and line.startswith("name = "):
            name = line.split("=", 1)[1].strip().strip('"').strip("'")
            if name in keys:
                current_provider = name
            continue
        if current_provider and line.startswith("api_key = "):
            want = keys[current_provider]["api_key"]
            cur = line.split("=", 1)[1].strip().strip('"').strip("'")
            if cur != want:
                lines[i] = f'api_key = "{want}"'
                changed += 1
        elif current_provider and line.startswith("base_url = "):
            want = keys[current_provider]["base_url"]
            cur = line.split("=", 1)[1].strip().strip('"').strip("'")
            if cur != want:
                lines[i] = f'base_url = "{want}"'
                changed += 1
        elif line.startswith("[") or line.startswith("[[") or line.startswith("#"):
            # 新表头后仍可能跟随 [[api_providers]]（已在上方处理）；嵌套 default_headers 表属当前块，不断开
            pass
    if changed:
        MAIBOT_MODEL_CONFIG.write_text("\n".join(lines), encoding="utf-8")
    return changed


def sync_keys() -> str:
    """执行一次同步，返回状态说明。"""
    if not CANONICAL_CONFIG.exists():
        return "找不到权威钥匙库：D:\\zhuochong\\config.json"
    if not MAIBOT_MODEL_CONFIG.exists():
        return "找不到 MaiBot 配置：model_config.toml"
    try:
        # 首次同步前留一份备份
        backup = MAIBOT_MODEL_CONFIG.with_suffix(".toml.bak_keys")
        if not backup.exists():
            shutil.copy2(MAIBOT_MODEL_CONFIG, backup)
        keys = _read_canonical_keys()
        changed = _sync_api_keys(keys)
        note = "（无变化）" if changed == 0 else f"（更新 {changed} 行）"
        return f"MaiBot key 已与桌宠配置对齐{note}"
    except Exception as exc:
        return f"Key 同步失败：{exc}"


if __name__ == "__main__":
    print(sync_keys())
