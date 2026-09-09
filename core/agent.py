import json
from typing import Optional
from PyQt6.QtCore import QThread, pyqtSignal
from openai import OpenAI
from .config_manager import APP_CONFIG
from .memory import MEMORY_MANAGER
from .tools import TOOL_DEFINITIONS, ToolExecutor
from .vision import SCREEN_OBSERVER

class AgentWorker(QThread):
    # 异步发出的信号：思考开始、输出回复、执行工具通知、结束
    thinking_signal = pyqtSignal(str)
    reply_signal = pyqtSignal(str)
    tool_executed_signal = pyqtSignal(str, str) # (工具名, 执行结果)
    finished_signal = pyqtSignal()

    def __init__(self, user_message: str):
        super().__init__()
        self.user_message = user_message

    def run(self):
        # 1. 检查配置
        if not APP_CONFIG.is_deepseek_configured():
            self.reply_signal.emit(
                "主人，你还没有在 config.json 里面填入 DeepSeek 的 API Key 呢！填好后保存，我就能正式启动思考魔法了だぜ！★"
            )
            self.finished_signal.emit()
            return

        self.thinking_signal.emit("让本魔法使推演一下...")

        try:
            client = OpenAI(
                api_key=APP_CONFIG.get("deepseek", "api_key"),
                base_url=APP_CONFIG.get("deepseek", "base_url")
            )
            model_name = APP_CONFIG.get("deepseek", "model", "deepseek-chat")

            # 2. 组装魔理沙人设与记忆
            system_prompt = self._build_system_prompt()
            
            # 构建消息列表
            messages = [{"role": "system", "content": system_prompt}]
            
            # 添加历史滑动窗口
            history = MEMORY_MANAGER.get_recent_history(limit=6)
            for h in history:
                messages.append(h)
                
            messages.append({"role": "user", "content": self.user_message})

            # 3. 首次请求：允许 Function Calling
            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                tools=TOOL_DEFINITIONS,
                tool_choice="auto",
                temperature=0.75,
                max_tokens=600
            )

            msg = response.choices[0].message

            # 4. 判断是否触发了 Tool 调用
            if msg.tool_calls:
                messages.append(msg)
                for tool_call in msg.tool_calls:
                    fn_name = tool_call.function.name
                    fn_args = {}
                    try:
                        fn_args = json.loads(tool_call.function.arguments)
                    except Exception:
                        pass

                    # 特殊拦截：屏幕视觉检测
                    if fn_name == "inspect_screen":
                        self.thinking_signal.emit("正在施展魔法观察屏幕...")
                        q = fn_args.get("question", self.user_message)
                        obs_result = SCREEN_OBSERVER.analyze_screen(q)
                        tool_result = f"【屏幕实时观察报告】：{obs_result}"
                    else:
                        tool_result = ToolExecutor.execute(fn_name, fn_args)
                        self.tool_executed_signal.emit(fn_name, tool_result)
                        # 好感度奖励：成功调用工具为主人服务
                        MEMORY_MANAGER.add_favorability(2, f"成功执行工具 {fn_name}")

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_result
                    })

                # 工具执行完毕后，将结果交给模型做终轮回答
                second_response = client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    temperature=0.75,
                    max_tokens=500
                )
                final_text = second_response.choices[0].message.content.strip()
            else:
                final_text = msg.content.strip()

            # 5. 更新记忆与好感度
            MEMORY_MANAGER.add_chat("user", self.user_message)
            MEMORY_MANAGER.add_chat("assistant", final_text)
            MEMORY_MANAGER.add_favorability(1, "日常聊天")

            self.reply_signal.emit(final_text)

        except Exception as e:
            print(f"[Agent] Error during DeepSeek invocation: {e}")
            err_msg = f"哎呀，魔法回路出错了：{str(e)[:120]}だぜ！"
            self.reply_signal.emit(err_msg)
        finally:
            self.finished_signal.emit()

    def _build_system_prompt(self) -> str:
        fav = MEMORY_MANAGER.get_favorability()
        rank_name, rank_desc = MEMORY_MANAGER.get_rank()
        memory_summary = MEMORY_MANAGER.get_memory_summary()

        return (
            "【角色设定与行为指导】\n"
            "你叫「雾雨魔理沙」（Kirisame Marisa），来自东方Project。你自称是「普通的魔法使」，性格自信、热心、充满元气与好奇心，有些男孩子气，行动力超强。\n"
            "你习惯在句末加上经典的「～だぜ（daze）」、「★」口癖，说话口吻爽朗自然。\n"
            "你现在生活在用户的电脑桌面上，作为桌宠与桌面伙伴陪伴着面前的主人。\n"
            "\n"
            "【关于工具与眼睛】\n"
            "1. 你拥有操作用户电脑的各种工具：\n"
            "   - 查询天气：主人问天气时调用 get_weather_info 工具；\n"
            "   - 查API余额：主人问及大模型余额/花费时调用 check_api_balance 工具；\n"
            "   - 音乐播放与推荐：主人让你「推荐歌曲/歌单/不知道听什么/放推荐」时，调用 marisa_recommend_playlist（会切到魔理沙每天自动更新的「魔理推荐的歌单」整单连播，当天没生成会自动先做好）；主人点某首具体的歌时调用 play_music_search（传入「歌手 歌名」）；主人说「放我歌单/我喜欢的音乐」时调用 play_cloudmusic_playlist；「放某歌手的歌」用 play_artist_hot；「收藏这首歌/存进歌单」用 save_current_song_to_playlist；只说'放首歌/暂停/切歌'（没指定）时调用 control_cloudmusic。以上私人歌单/推荐相关需网易云已扫码登录；\n"
            "   - 剪贴板：主人问「看看剪贴板/解释剪贴板/翻译我刚复制的内容」时，先调用 read_clipboard_text 拿到内容再加工回答；\n"
            "   - 关怀：主人工作很久或聊到开机时长时，可调用 system_uptime_info 了解开机多久并提醒喝水休息；\n"
            "   - 系统操作：打开常用软件（vscode/steam/qq等）、控制音量、锁屏；\n"
            "2. 当主人问及「看看我的屏幕」、「帮我看看这个报错/代码」、「屏幕上是什么」或相关桌面内容时，请务必调用 inspect_screen 工具去捕获屏幕画面！\n"
            "3. 【连续记忆要求】：你清晰感知与主人的所有互动历史，包括桌面互动与手机/QQ远程对话（两者记忆完全互通）。当主人问到之前的聊天内容时，根据历史对话自然回答，绝对不要说「我看不到历史记录」或「我没有记忆」！\n"
            "\n"
            f"【好感度与当前关系】\n"
            f"- 当前羁绊：{rank_name}（好感度：{fav}/100）。\n"
            f"- 态度指引：{rank_desc}\n"
            "\n"
            f"{memory_summary}\n"
            "\n"
            "【回复规范】\n"
            "- 因为你的台词会在头顶的气泡中以打字机形式逐字展示并伴随语音，请尽量保持回答精炼、有趣、直奔主题（通常在 1~3 句话以内，不超过 80 字，除非主人要求详细讲解）。\n"
            "- 严禁出现机械刻板的AI助手套话，完全以魔理沙的第一人称自然交流！"
        )
