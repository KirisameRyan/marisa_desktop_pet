import os
from pathlib import Path
from typing import Dict, Tuple
from PyQt6.QtGui import QPixmap, QTransform
from .config import ASSETS_DIR, CONFIG

class SpriteLoader:
    def __init__(self, assets_dir: Path = ASSETS_DIR, scale: float = CONFIG.scale):
        self.assets_dir = assets_dir
        self.scale = scale
        # 缓存: (filename, is_flipped) -> QPixmap
        self._cache: Dict[Tuple[str, bool], QPixmap] = {}
        self._load_all()

    def _load_all(self):
        target_size = int(CONFIG.sprite_size * self.scale)
        for i in range(1, 56):
            filename = f"shime{i}.png"
            path = self.assets_dir / filename
            if not path.exists():
                continue
            
            orig = QPixmap(str(path))
            if target_size != CONFIG.sprite_size:
                orig = orig.scaled(target_size, target_size)
            
            # 原图朝向为面向左侧 (facing left)
            self._cache[(filename, False)] = orig
            
            # 水平翻转版本 (facing right)
            transform = QTransform().scale(-1, 1)
            flipped = orig.transformed(transform)
            self._cache[(filename, True)] = flipped

    def get_frame(self, filename: str, flipped: bool = False) -> QPixmap:
        key = (filename, flipped)
        if key in self._cache:
            return self._cache[key]
        path = self.assets_dir / filename
        if path.exists():
            orig = QPixmap(str(path))
            target_size = int(CONFIG.sprite_size * self.scale)
            if target_size != CONFIG.sprite_size:
                orig = orig.scaled(target_size, target_size)
            self._cache[(filename, False)] = orig
            transform = QTransform().scale(-1, 1)
            flipped_pix = orig.transformed(transform)
            self._cache[(filename, True)] = flipped_pix
            return self._cache[key]
        return QPixmap()
