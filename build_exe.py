import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "魔理沙桌宠" + os.environ.get("PET_APP_SUFFIX", "")
ENTRY = ROOT / "run_pet.py"
ICON = ROOT / "assets" / "marisa.ico"
ASSETS_DIR = ROOT / "assets" / "marisa"
DIST_DIR = ROOT / "dist" / APP_NAME


def main():
    if not ICON.exists():
        print("缺少图标 assets/marisa.ico，请先运行图标生成脚本。")
        sys.exit(1)

    # 排除环境中的无关重型库，大幅缩小体积
    exclude = [
        "torch", "torchaudio", "torchvision", "tensorflow", "keras",
        "pandas", "numpy", "scipy", "sympy", "matplotlib", "pyarrow",
        "numba", "llvmlite", "sklearn", "scikit_learn", "statsmodels",
        "seaborn", "plotly", "gradio", "modelscope", "funasr", "librosa",
        "transformers", "torch", "jax", "mxnet", "tflite_runtime", "cv2",
    ]
    exclude_args = []
    for m in exclude:
        exclude_args += ["--exclude-module", m]

    sep = os.pathsep
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",          # 隐藏黑色命令行窗口
        "--onedir",            # 绿色便携目录，秒开
        "--name", APP_NAME,
        "--icon", str(ICON),
        # 将美术素材打包进内置目录（供 MEIPASS 兜底读取）
        "--add-data", f"{ASSETS_DIR}{sep}assets/marisa",
        "--add-data", f"{ICON}{sep}assets",
    ] + exclude_args + [str(ENTRY)]

    print("开始打包，请耐心等待（约 1~3 分钟）...")
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        print("PyInstaller 打包失败！")
        sys.exit(result.returncode)

    print(f"\n打包完成，正在整理发布目录: {DIST_DIR}")

    # 1. 外置 assets：用户可自由替换美术素材
    dist_assets = DIST_DIR / "assets" / "marisa"
    if dist_assets.exists():
        shutil.rmtree(DIST_DIR / "assets")
    shutil.copytree(ASSETS_DIR, dist_assets)
    shutil.copy2(ICON, DIST_DIR / "assets" / "marisa.ico")

    # 2. 外置配置文件模板（真实 config.json 由桌宠首次运行时自动生成）
    shutil.copy2(ROOT / "config.example.json", DIST_DIR / "config.example.json")
    if not (DIST_DIR / "config.json").exists():
        # 首次运行自动创建带默认占位 Key 的配置
        import json
        default_cfg = {
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
                "gpt_sovits_api_url": "http://127.0.0.1:9880",
                "ref_audio_path": r"D:\GPT-SoVITS-v4-20250421fix(1)\GPT-SoVITS-v4-20250422fix\【默认】参考音频.wav",
                "prompt_text": "参考音频对应的台词",
                "prompt_lang": "zh",
                "speed_factor": 1.0,
                "auto_start_api": True,
                "api_start_script": r"D:\GPT-SoVITS-v4-20250421fix(1)\GPT-SoVITS-v4-20250422fix\run_api.bat"
            },
            "pet": {
                "auto_screen_inspect": True,
                "inspect_interval_seconds": 600,
                "scale": 1.0
            }
        }
        with open(DIST_DIR / "config.json", "w", encoding="utf-8") as f:
            json.dump(default_cfg, f, ensure_ascii=False, indent=2)

    # 3. 使用说明
    readme_text = f"""\
★ 魔理沙 AI 桌宠 ★
============================

【启动方式】
双击「{APP_NAME}.exe」即可运行。
魔理沙会出现在桌面右下角，常驻在任务栏托盘（隐藏图标 ^）中。

【托盘图标操作】
· 双击托盘图标：打开「魔法配置室」设置面板
· 单击托盘图标：将魔理沙召回到前台
· 右键托盘图标：完整控制菜单（对话/天气/余额/音乐/巡检/退出）

【首次使用】
打开「魔法配置室」→ 在「API 与指引」页签填入：
  · DeepSeek 密钥（核心大脑）：https://platform.deepseek.com/api_keys
  · 智谱 GLM-V 密钥（视觉眼睛）：https://open.bigmodel.cn/usercenter/apikeys
填好后点击「测试连接并查余额」即可验证。

【QQ 远程（可选，需 NapCat + AstrBot）】
1. 下载并运行 NapCatQQ（github.com/NapNeko/NapCatQQ），用 QQ 号扫码登录。
   ⚠ 第三方协议 + 主号存在风控风险，请自行权衡。
2. 准备 AstrBot（github.com/Soulter/AstrBot 的 4.x 源码）：
   在该目录执行 `uv sync` 或 `pip install -r requirements.txt`，
   运行 `python main.py` 启动（面板 http://127.0.0.1:6185）。
   也可把路径填进桌宠设置 →「🔌 QQ/远程」→ 由桌宠托管启停。
3. AstrBot WebUI「平台管理」添加 OneBot v11：反向 WS 端口填 6199；
   NapCat 侧连接 ws://127.0.0.1:6199/ws。
4. 把 astrbot_plugins/marisa_bridge 插件放进 AstrBot 插件目录并在 WebUI 启用。
5. 桌宠设置「🔌 QQ/远程」：启用大脑、打开远程指令、填白名单 QQ 号、保存后重启桌宠。
6. 私聊魔理沙即可；群里需消息以「魔理沙」开头。

【数据与存档】
· config.json：你的 API 配置（不会出现在 Git 仓库中）
· data/ 文件夹：好感度、长期记忆、聊天历史、网易云登录态与桥接配置
· assets/ 文件夹：魔理沙的动作帧图片，可自行替换皮肤

祝你和魔理沙玩得开心だぜ！★
"""
    (DIST_DIR / "使用说明.txt").write_text(readme_text, encoding="utf-8")

    print("发布目录整理完成！")
    print(f"可执行文件位置: {DIST_DIR / (APP_NAME + '.exe')}")


if __name__ == "__main__":
    main()
