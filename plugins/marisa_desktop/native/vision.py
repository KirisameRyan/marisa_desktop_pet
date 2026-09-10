# -*- coding: utf-8 -*-
import io
import base64
from typing import Optional
from pathlib import Path
import mss
from PIL import Image

class ScreenObserver:
    def __init__(self):
        self.sct = mss.mss()

    def capture_screen_base64(self, max_width: int = 1280, quality: int = 70) -> str:
        """毫秒级截取屏幕，等比压缩为 JPEG base64 字符串。"""
        monitor = self.sct.monitors[1]
        sct_img = self.sct.grab(monitor)
        img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
        if img.width > max_width:
            ratio = max_width / float(img.width)
            new_height = int(float(img.height) * ratio)
            img = img.resize((max_width, new_height), Image.Resampling.BILINEAR)

        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=quality)
        b64_str = base64.b64encode(buffer.getvalue()).decode("utf-8")
        return f"data:image/jpeg;base64,{b64_str}"

    def capture_screen_file(self, path: str, max_width: int = 1600) -> str:
        """截屏存为本地图片文件（供远程发图）。返回路径。"""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        monitor = self.sct.monitors[1]
        sct_img = self.sct.grab(monitor)
        img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
        if img.width > max_width:
            ratio = max_width / float(img.width)
            img = img.resize((max_width, int(float(img.height) * ratio)),
                             Image.Resampling.BILINEAR)
        img.save(str(p), format="PNG")
        return str(p)

SCREEN_OBSERVER = ScreenObserver()
