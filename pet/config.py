import os
import sys
from dataclasses import dataclass
from pathlib import Path

# 判断是否打包为 exe
if getattr(sys, "frozen", False):
    EXE_DIR = Path(sys.executable).resolve().parent
    MEIPASS_DIR = Path(getattr(sys, "_MEIPASS", EXE_DIR))
    # 优先使用 exe 同级的 assets，若无则使用内置 assets
    if (EXE_DIR / "assets" / "marisa").exists():
        ASSETS_DIR = EXE_DIR / "assets" / "marisa"
    else:
        ASSETS_DIR = MEIPASS_DIR / "assets" / "marisa"
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
    ASSETS_DIR = BASE_DIR / "assets" / "marisa"

@dataclass
class PetConfig:
    # 尺寸与缩放
    sprite_size: int = 128
    scale: float = 1.0
    
    # 动画与物理时钟 (毫秒)
    tick_interval_ms: int = 40
    
    # 物理下落参数
    gravity: float = 1.2
    max_fall_speed: float = 24.0
    ground_margin: int = 4
    
    # 走动与漫步参数
    walk_speed: int = 2
    idle_min_duration: float = 3.0
    idle_max_duration: float = 8.0
    walk_min_duration: float = 4.0
    walk_max_duration: float = 10.0
    
    # 气泡与交互
    typewriter_speed_ms: int = 35
    bubble_auto_dismiss_s: float = 6.0

CONFIG = PetConfig()
