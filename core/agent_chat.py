"""无 GUI 的对话核心：供 QQ/AstrBot 大脑桥接线程调用。
与桌宠共享同一份 MEMORY_MANAGER（聊天历史/好感度/羁绊），同一只魔理沙。
仅暴露远程安全白名单内的工具。
"""
import json

from openai import OpenAI

from .config_manager import APP_CONFIG
from .memory import MEMORY_MANAGER
from .tools import TOOL_DEFINITIONS, ToolExecutor
from .vision import SCREEN_OBSERVER

# 远程可用的安全工具白名单
REMOTE_ALLOWED_TOOLS = {
    "check_api_balance",
    "get_weather_info",
    "play_music_search",
    "play_cloudmusic_playlist",
    "play_artist_hot",
    "marisa_recommend_playlist",
    "save_current_song_to_playlist",
    "control_cloudmusic",
    "read_clipboard_text",
    "system_uptime_info",
    "inspect_screen",
}


def remote_system_prompt() -> str:
    """远程（QQ）会话的人设提示：同一只魔理沙，附远程说明。"""
    fav = MEMORY_MANAGER.get_favorability()
    rank_name, rank_desc = MEMORY_MANAGER.get_rank()
    memory_summary = MEMORY_MANAGER.get_memory_summary()
    return (
        "【角色设定与行为指导】\n"
        "你叫「雾雨魔理沙」，来自东方Project，是主人的桌宠与伙伴，说话爽朗、带「だぜ」「★」口癖。\n"
        "你此刻正通过 QQ 与主人（或主人在的群）对话——主人可能在外面，你替他守着电脑。\n"
        "\n"
        "【可用能力】\n"
        "- 主人问天气/余额/开机时长/好感度/剪贴板等，用对应工具查了再答；\n"
        "- 音乐：点某首歌用 play_music_search（歌手+歌名）；「放我的歌单/我喜欢的音乐」用 play_cloudmusic_playlist；"
        "「来点推荐/魔理推荐」用 marisa_recommend_playlist；歌手热歌用 play_artist_hot；暂停/切歌用 control_cloudmusic；\n"
        "- 主人让「截图/看看屏幕」用 inspect_screen 汇总屏幕情况；\n"
        "- 你只能使用允许的安全工具；涉及开网页、装软件、删除文件等操作明确告诉主人「这个要回电脑上操作」；\n"
        "\n"
        "【连续记忆与认知要求】\n"
        "- 你拥有连续对话记忆，完全记得刚才和主人聊过什么、主人之前说了什么；\n"
        "- 绝对不要说「我看不到历史记录」或「我没有记忆」，主人询问历史对话时，直接根据上方对话上下文自然回答；\n"
        "- 你和电脑桌面上的桌宠是同一只魔理沙，拥有完全互通的记忆与羁绊；\n"
        "\n"
        f"【好感度与当前关系】\n- 当前羁绊：{rank_name}（好感度：{fav}/100）。\n- 态度指引：{rank_desc}\n\n"
        f"{memory_summary}\n\n"
        "【回复规范】\n"
        "- QQ 消息请精炼（通常 1~3 句，80 字内），第一人称，像真人发消息，别用 AI 套话；"
        "需要分段时用换行即可。"
    )


def run_remote_chat(user_text: str) -> str:
    """执行一轮远程对话，返回回复文本。任何异常都转成可读提示。"""
    text = (user_text or "").strip()
    if not text:
        return "没收到内容だぜ？"
    if not APP_CONFIG.is_deepseek_configured():
        return "主人还没给魔理沙配置 DeepSeek Key，远程大脑没醒だぜ……"

    try:
        client = OpenAI(
            api_key=APP_CONFIG.get("deepseek", "api_key"),
            base_url=APP_CONFIG.get("deepseek", "base_url"),
        )
        model_name = APP_CONFIG.get("deepseek", "model", "deepseek-chat")

        messages = [{"role": "system", "content": remote_system_prompt()}]
        for h in MEMORY_MANAGER.get_recent_history(limit=6):
            messages.append(h)
        messages.append({"role": "user", "content": text})

        response = client.chat.completions.create(
            model=model_name,
            messages=messages,
            tools=TOOL_DEFINITIONS,
            tool_choice="auto",
            temperature=0.75,
            max_tokens=500,
        )
        msg = response.choices[0].message

        if msg.tool_calls:
            messages.append(msg)
            for tool_call in msg.tool_calls:
                fn_name = tool_call.function.name
                fn_args = {}
                try:
                    fn_args = json.loads(tool_call.function.arguments)
                except Exception:
                    pass

                if fn_name not in REMOTE_ALLOWED_TOOLS:
                    tool_result = "该工具在远程（QQ）会话中不可用，请让主人在电脑上操作。"
                elif fn_name == "inspect_screen":
                    q = fn_args.get("question", text)
                    tool_result = f"【屏幕实时观察报告】：{SCREEN_OBSERVER.analyze_screen(q)}"
                else:
                    tool_result = ToolExecutor.execute(fn_name, fn_args)
                    MEMORY_MANAGER.add_favorability(2, f"远程执行工具 {fn_name}")

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": tool_result,
                })

            second = client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=0.75,
                max_tokens=400,
            )
            final_text = (second.choices[0].message.content or "").strip()
        else:
            final_text = (msg.content or "").strip()

        MEMORY_MANAGER.add_chat("user", text)
        MEMORY_MANAGER.add_chat("assistant", final_text)
        MEMORY_MANAGER.add_favorability(1, "远程QQ聊天")
        return final_text or "……（魔理沙陷入了沉默）"
    except Exception as e:
        print(f"[agent_chat] error: {e}")
        return f"哎呀，远程魔法回路出错了：{str(e)[:100]}"
