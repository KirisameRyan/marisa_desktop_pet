import random
from PyQt6.QtCore import QThread, pyqtSignal
from openai import OpenAI
from .config_manager import APP_CONFIG
from .memory import MEMORY_MANAGER
from .vision import SCREEN_OBSERVER

class ProactiveInspectorWorker(QThread):
    """后台异步视觉巡检与搭话生成 Worker"""
    speak_signal = pyqtSignal(str)
    inspect_done_signal = pyqtSignal()

    def run(self):
        if not APP_CONFIG.is_deepseek_configured() or not APP_CONFIG.is_vision_configured():
            self.inspect_done_signal.emit()
            return

        try:
            # 1. 抓取屏幕并通过智谱视觉模型提取观察报告
            obs_question = "请观察主人当前的电脑屏幕，简要描述他正在进行的活动（例如写代码、看视频、浏览网页、玩游戏、或长时间静止），不超过80字。"
            obs_report = SCREEN_OBSERVER.analyze_screen(obs_question)
            
            if not obs_report or "（提示" in obs_report or "（糟糕" in obs_report:
                self.inspect_done_signal.emit()
                return

            # 2. 将观察报告传递给 DeepSeek 生成魔理沙口吻的关怀/吐槽搭话
            client = OpenAI(
                api_key=APP_CONFIG.get("deepseek", "api_key"),
                base_url=APP_CONFIG.get("deepseek", "base_url")
            )
            model_name = APP_CONFIG.get("deepseek", "model", "deepseek-chat")

            fav = MEMORY_MANAGER.get_favorability()
            rank_name, _ = MEMORY_MANAGER.get_rank()

            system_prompt = (
                "你是「雾雨魔理沙」（Kirisame Marisa），居住在主人电脑桌面的桌宠伙伴。\n"
                "你自称是普通的魔法使，性格自信元气、关心伙伴、热心调皮，口癖是句末加上「～だぜ（daze）」、「★」。\n"
                f"当前你与主人的好感度是 {fav}/100（羁绊：{rank_name}）。\n"
                "刚才你偷偷看了一眼主人的桌面，了解到了他的当前状态。\n"
                "【任务要求】：\n"
                "根据屏幕观察报告，用 1~2 句话主动向主人搭话！\n"
                "- 如果在认真写代码或工作，提醒他端起水杯喝口水、活动一下肩膀、眨眨眼休息；\n"
                "- 如果在看视频或摸鱼，可以带着调皮笑意的口吻吐槽他；\n"
                "- 如果在玩游戏，可以元气满满为他打气加油；\n"
                "- 语言必须极度自然生动，字数控制在 45 字以内，绝对不要出现任何AI助手式说教！"
            )

            user_prompt = f"【当前桌面屏幕视觉侦察报告】：{obs_report}\n请用魔理沙的口吻，现在主动跟主人说一句话吧！"

            resp = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                max_tokens=120,
                temperature=0.85
            )

            chatter = resp.choices[0].message.content.strip()
            if chatter:
                # 记录好感度
                MEMORY_MANAGER.add_favorability(1, "主动巡检关怀")
                self.speak_signal.emit(chatter)

        except Exception as e:
            print(f"[InspectorWorker] Error during proactive inspection: {e}")
        finally:
            self.inspect_done_signal.emit()
