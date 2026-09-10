# -*- coding: utf-8 -*-
"""桌宠侧录音：点一次开始说话，说完自动收尾。

只负责采集与编码（16kHz / 单声道 / 16bit PCM → WAV base64），
识别、理解与回复全部交给 MaiBot 主链，这里不做任何判断。
"""
from __future__ import annotations

import base64
import io
import wave
from typing import Optional

import numpy as np
from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices

# 16kHz 单声道是语音识别的通用输入规格，也能显著压小上行数据量
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2            # 16bit PCM
BYTES_PER_SEC = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH
PULL_INTERVAL_MS = 60

# 静音判定：16bit 满量程 32768，环境底噪通常远低于该阈值
SILENCE_RMS = 320
SILENCE_HOLD_SEC = 1.2      # 连续静音多久认为一句话说完了
MIN_RECORD_SEC = 0.4        # 短于该时长视为误触，不发送
MAX_RECORD_SEC = 15.0       # 单次录音上限


class VoiceRecorder(QObject):
    """麦克风录音器（Qt 多媒体拉模式 + numpy 做静音检测）。"""

    finished = pyqtSignal(str, float)   # (WAV base64, 时长秒数)
    failed = pyqtSignal(str)            # 失败原因（面向主人的中文提示）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._source: Optional[QAudioSource] = None
        self._stream = None
        self._device = None
        self._buffer = bytearray()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._pull)
        self._silence_bytes = 0
        self._heard_voice = False
        self._stopping = False

    @property
    def is_recording(self) -> bool:
        return self._source is not None

    @property
    def elapsed_sec(self) -> float:
        """已录时长（按实际收到的 PCM 字节数换算，与采集回调频率无关）。"""

        return len(self._buffer) / BYTES_PER_SEC

    def start(self) -> bool:
        """开始录音；返回是否成功启动。"""

        if self.is_recording:
            return True

        fmt = QAudioFormat()
        fmt.setSampleRate(SAMPLE_RATE)
        fmt.setChannelCount(CHANNELS)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)

        device = QMediaDevices.defaultAudioInput()
        if device is None or device.isNull():
            self.failed.emit("没找到麦克风，主人检查一下设备だぜ")
            return False
        if not device.isFormatSupported(fmt):
            self.failed.emit("麦克风不支持 16kHz 单声道录音，换个设备试试だぜ")
            return False

        self._device = device
        self._buffer = bytearray()
        self._silence_bytes = 0
        self._heard_voice = False

        self._source = QAudioSource(device, fmt, self)
        self._stream = self._source.start()
        if self._stream is None:
            self._source = None
            self.failed.emit("麦克风打不开，可能被别的程序占用了だぜ")
            return False
        self._stream.readyRead.connect(self._pull)
        self._timer.start(PULL_INTERVAL_MS)
        return True

    def stop(self) -> None:
        """停止录音并上报结果（太短则直接丢弃）。

        ``_pull`` 在静音或超长时会自动调用本方法，所以这里必须先置
        ``_stopping`` 并把剩余数据直接抽干，避免重入后重复取设备导致崩溃。
        """

        if not self.is_recording or self._stopping:
            return
        self._stopping = True
        try:
            stream = self._stream
            if stream is not None:
                self._buffer.extend(bytes(stream.readAll()))
            self._timer.stop()
            source, self._source = self._source, None
            self._stream = None
            if source is not None:
                source.stop()
        finally:
            self._stopping = False

        seconds = self.elapsed_sec
        if seconds < MIN_RECORD_SEC:
            self.failed.emit("这次太短啦，我什么都没听到だぜ")
            return

        wav_bytes = self._encode_wav(bytes(self._buffer))
        if not wav_bytes:
            self.failed.emit("录音编码失败，主人再试一次だぜ")
            return
        self.finished.emit(base64.b64encode(wav_bytes).decode("ascii"), seconds)

    def _pull(self) -> None:
        """拉取已采集的音频，并据此更新静音计时。"""

        if not self.is_recording or self._stopping:
            return
        if self.elapsed_sec >= MAX_RECORD_SEC:
            self.stop()
            return

        stream = self._stream
        if stream is None:
            return
        chunk = bytes(stream.readAll())
        if not chunk:
            return

        self._buffer.extend(chunk)

        samples = np.frombuffer(chunk, dtype=np.int16)
        if samples.size == 0:
            return
        rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float32)))))
        if rms >= SILENCE_RMS:
            self._heard_voice = True
            self._silence_bytes = 0
            return

        # 还没听到人声前不计静音，避免刚点开就因为没有声音而立刻结束
        if not self._heard_voice:
            return
        # 按实际收到的静音音频长度计时：_pull 同时挂在 readyRead 与定时器上，
        # 用调用次数累加会让 1 秒被算成十几秒
        self._silence_bytes += len(chunk)
        if self._silence_bytes >= int(SILENCE_HOLD_SEC * BYTES_PER_SEC):
            self.stop()

    @staticmethod
    def _encode_wav(pcm: bytes) -> bytes:
        """把裸 PCM 包成 WAV（识别接口需要带头的音频文件）。"""

        if not pcm:
            return b""
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as writer:
            writer.setnchannels(CHANNELS)
            writer.setsampwidth(SAMPLE_WIDTH)
            writer.setframerate(SAMPLE_RATE)
            writer.writeframes(pcm)
        return buffer.getvalue()
