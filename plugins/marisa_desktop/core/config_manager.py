import json
import sys
from pathlib import Path
from typing import Any, Dict

# 判断运行根目录：打包环境使用 exe 目录，源码环境优先以项目根目录为主
if getattr(sys, "frozen", False):
    ROOT_DIR = Path(sys.executable).resolve().parent
else:
    # plugins/marisa_desktop/core -> 向上三级即为 MaiBot 根目录
    _PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
    if (_PROJECT_ROOT / "config.json").exists() or (_PROJECT_ROOT / "bot.py").exists():
        ROOT_DIR = _PROJECT_ROOT
    else:
        ROOT_DIR = Path(__file__).resolve().parent.parent

if getattr(sys, "frozen", False):
    INTERNAL_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
else:
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
        # 语音只走本地 GPT-SoVITS，下面这些是接入它所需的参数。
        # 缺失时 tts.py 会以空参考音频请求接口，合成结果必然是噪音，所以默认值必须带上。
        "gpt_sovits_api_url": "http://127.0.0.1:9880",
        "ref_audio_path": r"D:\GPT-SoVITS-v4-20250421fix(1)\GPT-SoVITS-v4-20250422fix\【默认】哇，你做点心的手艺很不一般啊！去祭典上摆摊的话，肯定会成为最热门的那一个吧！.wav",
        "prompt_text": "哇，你做点心的手艺很不一般啊！去祭典上摆摊的话，肯定会成为最热门的那一个吧！",
        "prompt_lang": "zh",
        "text_lang": "zh",
        "text_split_method": "cut5",
        "speed_factor": 1.0,
        "top_k": 15,
        "top_p": 1.0,
        "temperature": 1.0,
        "repetition_penalty": 1.35,
        "sample_steps": 32,
        "timeout_sec": 90.0,
        # 服务没起来时自动拉起（模型加载需数十秒），并等待就绪
        "auto_start_api": True,
        "api_start_script": r"D:\GPT-SoVITS-v4-20250421fix(1)\GPT-SoVITS-v4-20250422fix\run_api.bat",
        "api_start_timeout_sec": 180.0
    },
    "pet": {
        "auto_screen_inspect": True,
        "inspect_interval_seconds": 600,
        "scale": 1.0,
        "song_notify": True,
        "hourly_chime": False,
        "marisa_daily_notify": True,
        "presence_watch": True,
        "presence_idle_seconds": 300,
        "magic_delete": True,
        "hotkey_enable": True,
        "hotkey_combo": "ctrl+alt+m",
        "zany_auto": True,
        "zany_level": 1
    },
    # 桌宠只保留「是否连接 MaiBot 大脑」这一组开关；旧 AstrBot/远程桥接配置已退役
    "msg": {
        "maibot_enabled": False,
        "maibot_ws": "ws://127.0.0.1:8400",
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
