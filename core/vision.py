import io
import base64
from typing import Optional
import mss
from PIL import Image
from openai import OpenAI
from .config_manager import APP_CONFIG

class ScreenObserver:
    def __init__(self):
        self.sct = mss.mss()

    def capture_screen_base64(self, max_width: int = 1280, quality: int = 70) -> str:
        """毫秒级截取屏幕，等比压缩为 JPEG base64 字符串"""
        # 截取主显示器
        monitor = self.sct.monitors[1]
        sct_img = self.sct.grab(monitor)
        
        # 转换为 PIL Image
        img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
        
        # 等比缩放，降低 Token 消耗并提升速度
        if img.width > max_width:
            ratio = max_width / float(img.width)
            new_height = int(float(img.height) * ratio)
            img = img.resize((max_width, new_height), Image.Resampling.BILINEAR)

        # 压缩为 JPEG 字节流
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=quality)
        b64_str = base64.b64encode(buffer.getvalue()).decode("utf-8")
        return f"data:image/jpeg;base64,{b64_str}"

    def analyze_screen(self, question: str = "请简要描述屏幕上正在展示的关键内容与软件") -> str:
        """调用智谱视觉模型 (glm-4.6v-flash) 分析屏幕内容"""
        if not APP_CONFIG.is_vision_configured():
            return "（提示：尚未在 config.json 中配置智谱视觉模型的 api_key，魔理沙现在还看不清屏幕だぜ！）"

        try:
            b64_image = self.capture_screen_base64()
            
            client = OpenAI(
                api_key=APP_CONFIG.get("vision", "api_key"),
                base_url=APP_CONFIG.get("vision", "base_url")
            )
            model_name = APP_CONFIG.get("vision", "model", "glm-4.6v-flash")

            prompt = (
                f"你是视觉观察助手。用户的问题是：【{question}】。\n"
                f"请查看所附电脑屏幕截图，用简明、客观、精炼的中文总结关键观察结果（不超过 120 字）。"
                f"包括：主要打开的软件/网页、正在进行的活动、明显的代码报错或画面特征等。"
            )

            # 优先使用配置的模型，若遇到繁忙则自动降级使用 glm-4v-flash 冗余
            candidates = [model_name]
            if "glm-4v-flash" not in candidates:
                candidates.append("glm-4v-flash")

            for m in candidates:
                try:
                    response = client.chat.completions.create(
                        model=m,
                        messages=[
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": prompt},
                                    {
                                        "type": "image_url",
                                        "image_url": {"url": b64_image}
                                    }
                                ]
                            }
                        ],
                        max_tokens=250,
                        temperature=0.3
                    )
                    content = response.choices[0].message.content or ""
                    if content.strip():
                        return content.strip()
                except Exception as ex:
                    print(f"[Vision] Candidate {m} failed: {ex}, trying fallback...")
                    continue

            return "（未能成功识别屏幕内容）"

        except Exception as e:
            print(f"[Vision] Error during GLM vision analysis: {e}")
            return f"（糟糕，魔理沙的远见魔法被干扰了：{str(e)}）"

SCREEN_OBSERVER = ScreenObserver()
