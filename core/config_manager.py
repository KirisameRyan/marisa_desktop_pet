import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

# 判断是否打包为 exe
if getattr(sys, "frozen", False):
    # exe 运行模式：配置文件与存档保存在 exe 同级目录下
    ROOT_DIR = Path(sys.executable).resolve().parent
    INTERNAL_DIR = Path(getattr(sys, "_MEIPASS", ROOT_DIR))
else:
    # 源码开发模式
    ROOT_DIR = Path(__file__).resolve().parent.parent
    INTERNAL_DIR = ROOT_DIR

CONFIG_PATH = ROOT_DIR / "config.json"
DATA_DIR = ROOT_DIR / "data"

DEFAULT_CONFIG: Dict[str, Any] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "api_key": "YOUR_DEEPSEEK_API_KEY_HERE",
        "model": "deepseek-chat"
    },
    "vision": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key": "YOUR_ZHIPU_API_KEY_HERE",
        "model": "glm-4.6v-flash"
    },
    "tts": {
        "engine": "edge",
        "voice": "zh-CN-XiaoxiaoNeural",
        "rate": "+15%",
        "pitch": "+10Hz",
        "gpt_sovits_api_url": "http://127.0.0.1:9880"
    },
    "pet": {
        "auto_screen_inspect": True,
        "inspect_interval_seconds": 600,
        "scale": 1.0,
        "song_notify": True,
        "hourly_chime": False,
        "marisa_daily_notify": True
    }
}

class AppConfig:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(AppConfig, cls).__new__(cls)
            cls._instance._load()
        return cls._instance

    def _load(self):
        self.data = DEFAULT_CONFIG.copy()
        if CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    user_conf = json.load(f)
                    for k, v in user_conf.items():
                        if isinstance(v, dict) and k in self.data:
                            self.data[k].update(v)
                        else:
                            self.data[k] = v
            except Exception as e:
                print(f"[Config] Error loading config.json: {e}")
        else:
            self.save()

    def save(self):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Config] Error saving config.json: {e}")

    def get(self, section: str, key: str = None, default: Any = None) -> Any:
        self._load()
        sec = self.data.get(section, {})
        if key is None:
            return sec
        return sec.get(key, default)

    def is_deepseek_configured(self) -> bool:
        key = self.get("deepseek", "api_key", "")
        return bool(key and key != "YOUR_DEEPSEEK_API_KEY_HERE")

    def is_vision_configured(self) -> bool:
        key = self.get("vision", "api_key", "")
        return bool(key and key != "YOUR_ZHIPU_API_KEY_HERE")

APP_CONFIG = AppConfig()
