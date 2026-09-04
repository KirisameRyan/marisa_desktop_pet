import os
import asyncio
import tempfile
import threading
from pathlib import Path
from typing import Optional
import pygame
import edge_tts
import httpx
from .config_manager import APP_CONFIG

# 初始化 pygame 音频混音器
try:
    pygame.mixer.init()
except Exception as e:
    print(f"[TTS] Pygame mixer init failed: {e}")

class TTSEngine:
    def __init__(self):
        self._is_playing: bool = False
        self._lock = threading.Lock()
        self._current_temp_file: Optional[str] = None

    def stop(self):
        """立即停止当前正在播放的声音"""
        try:
            if pygame.mixer.get_init() and pygame.mixer.music.get_busy():
                pygame.mixer.music.stop()
                pygame.mixer.music.unload()
            self._is_playing = False
            if self._current_temp_file and os.path.exists(self._current_temp_file):
                try:
                    os.remove(self._current_temp_file)
                except Exception:
                    pass
                self._current_temp_file = None
        except Exception as e:
            print(f"[TTS] Stop error: {e}")

    def speak(self, text: str, on_finished=None):
        """异步生成并播放语音（不阻塞主线程）"""
        if not text or not text.strip():
            return
        
        # 清理多余标点与emoji避免TTS卡壳
        clean_text = text.replace("★", "").replace("💭", "").replace("💬", "").replace("⚡", "").strip()
        if not clean_text:
            return

        t = threading.Thread(target=self._speak_worker, args=(clean_text, on_finished), daemon=True)
        t.start()

    def _speak_worker(self, text: str, on_finished):
        with self._lock:
            self.stop()
            engine_choice = APP_CONFIG.get("tts", "engine", "edge")

            # 1. 尝试本地 GPT-SoVITS API (如果用户配置了且服务在线)
            if engine_choice == "gpt_sovits":
                audio_file = self._generate_gpt_sovits(text)
                if audio_file:
                    self._play_file(audio_file, on_finished)
                    return

            # 2. 默认 / 备选: Edge-TTS 高音质活泼中文女声
            audio_file = self._generate_edge_tts(text)
            if audio_file:
                self._play_file(audio_file, on_finished)

    def _generate_edge_tts(self, text: str) -> Optional[str]:
        """使用 edge-tts 生成临时音频"""
        try:
            voice = APP_CONFIG.get("tts", "voice", "zh-CN-XiaoxiaoNeural")
            rate = APP_CONFIG.get("tts", "rate", "+15%")
            pitch = APP_CONFIG.get("tts", "pitch", "+10Hz")

            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as fp:
                tmp_path = fp.name

            async def _run():
                communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
                await communicate.save(tmp_path)

            asyncio.run(_run())
            return tmp_path
        except Exception as e:
            print(f"[TTS] Edge-TTS generation error: {e}")
            return None

    def _generate_gpt_sovits(self, text: str) -> Optional[str]:
        """尝试请求本地运行的 GPT-SoVITS 接口"""
        url = APP_CONFIG.get("tts", "gpt_sovits_api_url", "http://127.0.0.1:9880")
        try:
            # 常见 GPT-SoVITS API endpoint
            resp = httpx.post(f"{url}/tts", json={"text": text, "text_lang": "zh"}, timeout=5.0)
            if resp.status_code == 200:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fp:
                    fp.write(resp.content)
                    return fp.name
        except Exception:
            # 本地未开启，则优雅回退
            pass
        return None

    def _play_file(self, file_path: str, on_finished):
        """通过 pygame.mixer 播放音频"""
        self._current_temp_file = file_path
        try:
            pygame.mixer.music.load(file_path)
            pygame.mixer.music.play()
            self._is_playing = True

            # 轮询等待播放完成
            while pygame.mixer.music.get_busy() and self._is_playing:
                pygame.time.Clock().tick(20)

        except Exception as e:
            print(f"[TTS] Play error: {e}")
        finally:
            self._is_playing = False
            try:
                pygame.mixer.music.unload()
                if os.path.exists(file_path):
                    os.remove(file_path)
            except Exception:
                pass
            if on_finished:
                on_finished()

TTS_ENGINE = TTSEngine()
