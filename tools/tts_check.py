"""魔理沙语音自检工具。

当桌宠的语音听起来不对（咕噜声、杂音、不出声）时运行这个脚本：

    .venv\\Scripts\\python.exe tools\\tts_check.py

它会检查配置、探活语音服务、合成两句测试语音，并按"时长是否与字数匹配"
判断有没有发生生成退化。正常语速约 0.2-0.3 秒/字。
"""

import io
import json
import math
import struct
import sys
import time
import wave
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
TTS = json.loads((ROOT / "config.json").read_text(encoding="utf-8")).get("tts", {})

API = str(TTS.get("gpt_sovits_api_url", "http://127.0.0.1:9880")).rstrip("/")
REF = str(TTS.get("ref_audio_path", "") or "")
PROMPT = str(TTS.get("prompt_text", "") or "")

# 与 core/tts.py 保持一致的退化判定
DEGENERATE_MIN_SEC = 6.0
DEGENERATE_SEC_PER_CHAR = 0.75

CASES = ["你好啊", "普通魔法使雾雨魔理沙参上！今天也元气满满地大干一场吧！"]


def check_config() -> list[str]:
    problems = []
    if not REF:
        problems.append("未配置 ref_audio_path（参考音频）")
    elif not Path(REF).exists():
        problems.append(f"参考音频不存在: {REF}")
    if not PROMPT:
        problems.append("未配置 prompt_text（参考文本）")
    if not API.startswith("http"):
        problems.append(f"服务地址看起来不对: {API}")
    return problems


def synth(text: str) -> tuple[str, str]:
    payload = {
        "text": text,
        "text_lang": str(TTS.get("text_lang", "zh")),
        "ref_audio_path": REF.replace("\\", "/"),
        "prompt_text": PROMPT,
        "prompt_lang": str(TTS.get("prompt_lang", "zh")),
        "text_split_method": str(TTS.get("text_split_method", "cut5")),
        "batch_size": 1,
        "media_type": "wav",
        "streaming_mode": False,
        "speed_factor": float(TTS.get("speed_factor", 1.0)),
        "top_k": int(TTS.get("top_k", 15)),
        "top_p": float(TTS.get("top_p", 1.0)),
        "temperature": float(TTS.get("temperature", 1.0)),
        "repetition_penalty": float(TTS.get("repetition_penalty", 1.35)),
        "sample_steps": int(TTS.get("sample_steps", 32)),
    }
    try:
        resp = httpx.post(f"{API}/tts", json=payload, timeout=float(TTS.get("timeout_sec", 90.0)))
    except Exception as exc:
        return "fail", f"请求失败: {type(exc).__name__}: {exc}"

    if resp.status_code != 200 or len(resp.content) < 100:
        return "fail", f"HTTP {resp.status_code}: {resp.text[:150]}"

    try:
        with wave.open(io.BytesIO(resp.content), "rb") as wf:
            seconds = wf.getnframes() / float(wf.getframerate() or 1)
            raw = wf.readframes(wf.getnframes())
    except Exception as exc:
        return "fail", f"返回内容不是有效音频: {exc}"

    samples = struct.unpack(f"<{len(raw) // 2}h", raw)
    rms = math.sqrt(sum(s * s for s in samples) / max(len(samples), 1)) / 32768
    rms_db = 20 * math.log10(rms + 1e-12)

    limit = max(DEGENERATE_MIN_SEC, len(text) * DEGENERATE_SEC_PER_CHAR)
    if seconds > limit:
        return "degenerate", (
            f"{len(text)} 字却合成出 {seconds:.2f} 秒（上限 {limit:.1f} 秒），"
            f"RMS {rms_db:.1f}dBFS —— 生成退化，输出会是咕噜声"
        )
    if rms_db < -35:
        return "warn", f"{seconds:.2f} 秒但电平只有 {rms_db:.1f}dBFS，疑似异常"
    return "ok", f"{seconds:.2f} 秒，RMS {rms_db:.1f}dBFS"


def main() -> int:
    print(f"服务地址 : {API}")
    print(f"参考音频 : {REF or '(未配置)'}")
    print(f"参考文本 : {PROMPT[:38]}{'...' if len(PROMPT) > 38 else ''}")
    print()

    problems = check_config()
    if problems:
        print("配置问题：")
        for p in problems:
            print(f"  - {p}")
        print("\n请先在桌宠「设置 → 语音与音乐」里补齐。")
        return 1
    print("配置检查 : 通过")

    try:
        httpx.get(f"{API}/", timeout=3.0)
        print("服务状态 : 在线")
    except Exception:
        print("服务状态 : 未启动（桌宠下次发声时会自动拉起，也可手动运行 run_api.bat）")
        return 1
    print()

    results = []
    for text in CASES:
        t0 = time.time()
        status, detail = synth(text)
        mark = {"ok": "正常", "warn": "可疑", "degenerate": "退化", "fail": "失败"}[status]
        print(f"[{mark}] {text[:24]}")
        print(f"        {detail}（耗时 {time.time() - t0:.0f}s）")
        results.append(status)

    print()
    if all(r == "ok" for r in results):
        print("结论：语音链路正常。")
        return 0
    if any(r == "degenerate" for r in results):
        print("结论：检测到生成退化。桌宠遇到这种情况会自动重启语音服务并重试；")
        print("      若手动处理，重启 run_api.bat 即可恢复（不要同时开多个 GPT-SoVITS 窗口抢显存）。")
        return 2
    print("结论：语音服务异常，请查看它的控制台输出。")
    return 2


if __name__ == "__main__":
    sys.exit(main())
