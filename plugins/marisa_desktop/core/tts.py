import io
import os
import re
import subprocess
import tempfile
import threading
import time
import unicodedata
import wave
from typing import Optional

import httpx
import pygame

from .config_manager import APP_CONFIG, DATA_DIR

# 语音服务启动输出与跨进程启动锁：与宿主 marisa_voice 插件共用同一份
_VOICE_API_LOG = DATA_DIR / "voice_api.log"
_START_LOCK_PATH = DATA_DIR / "voice_api.start.lock"


def _open_voice_log():
    """打开语音服务输出日志（追加）；打不开时退回丢弃输出。"""

    try:
        _VOICE_API_LOG.parent.mkdir(parents=True, exist_ok=True)
        return open(_VOICE_API_LOG, "ab")
    except OSError:
        return subprocess.DEVNULL


def _try_acquire_start_lock(ttl_sec: float) -> bool:
    """跨进程启动锁：宿主侧已在拉起语音服务时返回 False，避免重复加载抢显存。"""

    try:
        _START_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        if _START_LOCK_PATH.exists() and time.time() - _START_LOCK_PATH.stat().st_mtime < ttl_sec:
            return False
        _START_LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except OSError:
        return True

# 初始化 pygame 音频混音器
try:
    pygame.mixer.init()
except Exception as e:
    print(f"[TTS] Pygame mixer init failed: {e}")


# 日文假名区段（平假名 / 片假名 / 假名扩展 / 半角片假名）。
# 宵宫是纯中文模型，没有日文前端，假名会被当中文音素错误朗读，送合成前一律剔除。
_KANA_RANGES = ((0x3040, 0x309F), (0x30A0, 0x30FF), (0x31F0, 0x31FF), (0xFF66, 0xFF9D))

# 装饰性字符：emoji、星星、音符、零宽字符等，念不出来还会干扰文本前端
_DROP_CATEGORIES = frozenset({"So", "Sk", "Cs", "Co", "Cn", "Cf"})
_DROP_CHARS = frozenset("★☆♪♫♬")

# 生成退化判定：正常中文语速约 0.2-0.3 秒/字。
# 服务进程状态劣化时模型不吐结束符，会一路生成到长度上限，输出超长且是咕噜声。
_DEGENERATE_MIN_SEC = 6.0
_DEGENERATE_SEC_PER_CHAR = 0.75


def sanitize_for_tts(text: str) -> str:
    """把台词收拾成中文模型能正确朗读的形式。

    剔除日文假名、emoji 与装饰符号，换行折叠成停顿，合并重复标点。
    """
    if not text:
        return ""
    kept = []
    for ch in text:
        code = ord(ch)
        if any(lo <= code <= hi for lo, hi in _KANA_RANGES):
            continue
        if ch in _DROP_CHARS or unicodedata.category(ch) in _DROP_CATEGORIES:
            continue
        kept.append(ch)

    cleaned = "".join(kept)
    cleaned = re.sub(r"\s*\n+\s*", "，", cleaned)              # 换行变停顿
    cleaned = re.sub(r"[ \t\u3000]+", " ", cleaned)            # 压缩空白
    cleaned = re.sub(r"([，。！？、,.!?])\1+", r"\1", cleaned)  # 重复标点只留一个
    return cleaned.strip()


class TTSEngine:
    def __init__(self):
        self._is_playing: bool = False
        self._lock = threading.Lock()
        self._current_temp_file: Optional[str] = None
        # 语音服务的启动节流：模型加载慢，短时间内重复拉起会多实例抢显存
        self._start_lock = threading.Lock()
        self._last_start_ts: float = 0.0

    # ---------------- 配置读取 ----------------

    def _cfg(self, key: str, default):
        try:
            return APP_CONFIG.get("tts", key, default)
        except Exception:
            return default

    @property
    def _base_url(self) -> str:
        return str(self._cfg("gpt_sovits_api_url", "http://127.0.0.1:9880")).rstrip("/")

    # ---------------- 播放控制 ----------------

    def stop(self):
        """立即停止当前正在播放的声音（临时文件由播放线程自己清理）。"""
        try:
            if pygame.mixer.get_init() and pygame.mixer.music.get_busy():
                pygame.mixer.music.stop()
                pygame.mixer.music.unload()
        except Exception as e:
            print(f"[TTS] Stop error: {e}")
        finally:
            self._is_playing = False

    def speak(self, text: str, on_finished=None):
        """异步生成并播放语音（不阻塞主线程）"""
        if not text or not text.strip():
            return

        clean_text = sanitize_for_tts(text)
        if not clean_text:
            print("[TTS] 清洗后没有可朗读内容，跳过本次发声")
            return

        t = threading.Thread(target=self._speak_worker, args=(clean_text, on_finished), daemon=True)
        t.start()

    def _speak_worker(self, text: str, on_finished):
        with self._lock:
            self.stop()
            # 仅使用 GPT-SoVITS 一个引擎，没有任何备用音色
            audio_file = self._generate_gpt_sovits(text)
            if audio_file:
                self._play_file(audio_file, on_finished)
            else:
                print(f"[TTS] 本次未能合成，保持静默: {text[:30]}")

    # ---------------- 语音服务健康检查 ----------------

    def _api_alive(self, timeout: float = 2.0) -> bool:
        try:
            httpx.get(f"{self._base_url}/", timeout=timeout)
            return True
        except Exception:
            return False

    def _start_api(self) -> bool:
        script = str(self._cfg("api_start_script", "") or "").strip()
        if not script or not os.path.exists(script):
            print(f"[TTS] 未找到语音服务启动脚本: {script or '(未配置)'}")
            return False

        with self._start_lock:
            if time.time() - self._last_start_ts < 120:
                return False
            self._last_start_ts = time.time()

        # 宿主侧可能也在预热：拿不到锁就只等它，不重复拉起第二个实例
        if not _try_acquire_start_lock(float(self._cfg("api_start_timeout_sec", 180.0))):
            print("[TTS] 语音服务已在拉起中，等待就绪…")
            return True

        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            subprocess.Popen(
                # cmd /d /s /c 双引号写法：路径含 (1) 这类括号时，普通 cmd /c 会截断命令秒退
                f'cmd /d /s /c ""{script}""',
                cwd=os.path.dirname(script),
                creationflags=flags,
                close_fds=True,
                stdout=_open_voice_log(),
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
            print(f"[TTS] 正在拉起语音服务: {script}（输出见 {_VOICE_API_LOG}）")
            return True
        except Exception as e:
            print(f"[TTS] 拉起语音服务失败: {e}")
            return False

    def _wait_api(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(3)
            if self._api_alive():
                return True
        return False

    def _ensure_api_ready(self) -> bool:
        if self._api_alive():
            return True

        if not bool(self._cfg("auto_start_api", True)):
            print("[TTS] 语音服务未启动，且未开启自动拉起")
            return False

        if not self._start_api():
            return False

        print("[TTS] 等待语音服务加载模型（首次约需一分钟）...")
        if self._wait_api(float(self._cfg("api_start_timeout_sec", 180.0))):
            print("[TTS] 语音服务已就绪")
            return True
        print("[TTS] 语音服务超时未就绪")
        return False

    def _restart_api(self) -> bool:
        try:
            httpx.get(f"{self._base_url}/control", params={"command": "restart"}, timeout=5.0)
        except Exception:
            # 进程会被 os.execl 替换，连接中断属正常现象
            pass
        return self._wait_api(float(self._cfg("api_start_timeout_sec", 180.0)))

    # ---------------- 合成 ----------------

    def _build_payload(self, text: str) -> dict:
        ref_audio = str(self._cfg("ref_audio_path", "") or "")
        if ref_audio:
            ref_audio = ref_audio.replace("\\", "/")  # 统一正斜杠，避免转义歧义
        return {
            "text": text,
            "text_lang": str(self._cfg("text_lang", "zh")),
            "ref_audio_path": ref_audio,
            "prompt_text": str(self._cfg("prompt_text", "") or ""),
            "prompt_lang": str(self._cfg("prompt_lang", "zh")),
            "text_split_method": str(self._cfg("text_split_method", "cut5")),
            "batch_size": 1,
            "media_type": "wav",
            "streaming_mode": False,
            "speed_factor": float(self._cfg("speed_factor", 1.0)),
            # 显式给出采样参数，不依赖服务端默认值
            "top_k": int(self._cfg("top_k", 15)),
            "top_p": float(self._cfg("top_p", 1.0)),
            "temperature": float(self._cfg("temperature", 1.0)),
            "repetition_penalty": float(self._cfg("repetition_penalty", 1.35)),
            "sample_steps": int(self._cfg("sample_steps", 32)),
        }

    def _request_tts(self, text: str) -> Optional[bytes]:
        payload = self._build_payload(text)
        if not payload["ref_audio_path"] or not payload["prompt_text"]:
            print("[TTS] 参考音频或参考文本未配置，无法合成（请在设置里填写）")
            return None

        timeout = float(self._cfg("timeout_sec", 90.0))
        try:
            resp = httpx.post(f"{self._base_url}/tts", json=payload, timeout=timeout)
        except Exception as e:
            print(f"[TTS] 请求语音服务失败: {type(e).__name__}: {e}")
            return None

        if resp.status_code != 200 or len(resp.content) < 100:
            print(f"[TTS] 语音服务返回异常: HTTP {resp.status_code} {resp.text[:200]}")
            return None
        return resp.content

    @staticmethod
    def _wav_seconds(data: bytes) -> float:
        with wave.open(io.BytesIO(data), "rb") as wf:
            return wf.getnframes() / float(wf.getframerate() or 1)

    @staticmethod
    def _is_degenerate(text: str, seconds: float) -> bool:
        limit = max(_DEGENERATE_MIN_SEC, len(text) * _DEGENERATE_SEC_PER_CHAR)
        return seconds > limit

    def _generate_gpt_sovits(self, text: str) -> Optional[str]:
        """请求本地 GPT-SoVITS 合成，返回临时音频路径。"""
        if not self._ensure_api_ready():
            return None

        data = self._request_tts(text)
        if data is not None:
            try:
                seconds = self._wav_seconds(data)
            except Exception as e:
                print(f"[TTS] 返回内容不是有效音频: {e}")
            else:
                if not self._is_degenerate(text, seconds):
                    return self._save_temp(data)
                print(f"[TTS] 输出异常：{len(text)} 字却合成出 {seconds:.1f} 秒，判定为生成退化")

        # 服务跑久后状态劣化会导致持续退化输出，重启一次再试
        print("[TTS] 尝试重启语音服务后重试")
        if not self._restart_api():
            return None

        data = self._request_tts(text)
        if data is None:
            return None
        try:
            seconds = self._wav_seconds(data)
        except Exception as e:
            print(f"[TTS] 重试返回内容不是有效音频: {e}")
            return None
        if self._is_degenerate(text, seconds):
            print(f"[TTS] 重试后仍然异常（{seconds:.1f} 秒），放弃本次发声")
            return None
        return self._save_temp(data)

    @staticmethod
    def _save_temp(data: bytes) -> str:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fp:
            fp.write(data)
            return fp.name

    # ---------------- 播放 ----------------

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
            self._current_temp_file = None
            try:
                pygame.mixer.music.unload()
                if os.path.exists(file_path):
                    os.remove(file_path)
            except Exception:
                pass
            if on_finished:
                on_finished()


TTS_ENGINE = TTSEngine()
