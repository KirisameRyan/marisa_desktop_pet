"""东方知识库插件：把 THBWiki 知识库检索接进 MaiBot。

分工（沿用主人一开始定的架构）：
    **插件做确定性检索，MaiBot 的模型做理解与作答。**
    插件暴露 marisa_touhou_search 工具；模型判断用户意图后调用它，拿到资料再组织回答。
    多跳问题（"红魔馆主人的妹妹的能力"）也交给模型**多次调用**工具，插件不做链式猜测。

检索层的实现在仓库的 scripts/touhou_retrieve.py（单一实现，不复制第二份）：
    ① 别名/俗称归一化（⑨→琪露诺、pad长→十六夜咲夜、东方红魔乡→红魔乡…）
    ② 多路召回：SQL 聚合 · SQL 字段 · 角色档案章节（关键词+语义）· 关系网 · 标签 ·
               RAG 语料段落 · FTS 字面 · 实体向量
    ③ 加权 RRF 融合 + 同人乘区（同人 ×0.25）+ 确定性答案加成
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List

# 复用仓库里的检索层。插件目录是 plugins/marisa_touhou/，仓库根在上两级。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _PROJECT_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from maibot_sdk import Field, MaiBotPlugin, PluginConfigBase, Tool  # noqa: E402
from maibot_sdk.types import ToolParameterInfo, ToolParamType  # noqa: E402

from touhou_retrieve import Hit, TouhouRetriever  # noqa: E402


class PluginSectionConfig(PluginConfigBase):
    """[plugin] 段。"""

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="0.1.0", description="配置版本")


class RetrievalSectionConfig(PluginConfigBase):
    """[retrieval] 段。"""

    db_path: str = Field(default="data\\thwiki\\thwiki.db", description="知识库位置（相对仓库根目录）")
    limit: int = Field(default=5, description="每次返回几条资料给模型")
    max_chars: int = Field(default=900, description="单条资料最多给多少字")
    show_source: bool = Field(default=True, description="是否附出处链接")


class EmbeddingSectionConfig(PluginConfigBase):
    """[embedding] 段。"""

    config_file: str = Field(
        default="config\\model_config.toml",
        description="嵌入服务配置来源（沿用 MaiBot 自己的，插件不另存 API key）",
    )
    enabled: bool = Field(default=True, description="关掉则退化为纯 SQL/标签/关系/FTS 检索")


class TouhouConfig(PluginConfigBase):
    """东方知识库插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    retrieval: RetrievalSectionConfig = Field(default_factory=RetrievalSectionConfig)
    embedding: EmbeddingSectionConfig = Field(default_factory=EmbeddingSectionConfig)


class TouhouPlugin(MaiBotPlugin):
    """东方知识库检索插件。"""

    config_model = TouhouConfig

    def __init__(self) -> None:
        super().__init__()
        self._retriever: TouhouRetriever | None = None
        # 检索跑在工作线程（asyncio.to_thread），而 SQLite 连接不是并发安全的；
        # 用锁把检索串行化，避免两个群同时提问时交叉使用同一个连接。
        self._search_lock = asyncio.Lock()

    # ─────────────────── 生命周期 ───────────────────

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置热更新。

        宿主对每个插件都强制要求实现本方法（见 plugin_loader 的校验），
        所以这里不做"有没有配置变化"的猜测，只把当前生效值如实打出来。
        """

        retrieval = self.config.retrieval
        self.ctx.logger.info(
            "[marisa_touhou] 配置已更新（scope=%s，version=%s）：%s，条数=%s，嵌入=%s",
            scope,
            version,
            "开启" if self.config.plugin.enabled else "关闭",
            retrieval.limit,
            "开启" if self.config.embedding.enabled else "关闭",
        )
        # 配置变了就丢掉缓存的检索器（库路径/嵌入开关都可能变）
        self._retriever = None

    def _get_retriever(self) -> TouhouRetriever:
        """懒加载检索器（首次调用时才读库，避免拖慢插件启动）。"""

        if self._retriever is None:
            db_path = Path(self.config.retrieval.db_path)
            if not db_path.is_absolute():
                db_path = _PROJECT_ROOT / db_path
            if not db_path.exists():
                raise FileNotFoundError(f"知识库不存在：{db_path}")
            config_path = Path(self.config.embedding.config_file)
            if not config_path.is_absolute():
                config_path = _PROJECT_ROOT / config_path
            if not self.config.embedding.enabled:
                config_path = Path("__disabled__")   # 让检索器拿不到嵌入服务
            self._retriever = TouhouRetriever(db_path, config_path)
            self.ctx.logger.info("[marisa_touhou] 知识库已加载：%s", db_path)
        return self._retriever

    # ─────────────────── 工具 ───────────────────

    @Tool(
        "marisa_touhou_search",
        description=(
            "查询东方Project（THBWiki）知识库，返回资料原文与出处。"
            "涵盖：角色（种族/能力/所在地/外貌/人际关系/登场剧情/符卡列表/官方设定文档）、"
            "作品（发售日期/类型/曲目）、符卡（使用者与登场作品）、设定（世界观/符卡规则/幻想乡）、"
            "统计（作品数量/成员数量/按发售时间排序）。"
            "用法：由你判断用户想问什么，把问题原样传进来；"
            "**多跳问题请分多次调用**——例如「红魔馆主人的妹妹的能力」，"
            "先问「红魔馆的主人是谁」，拿到答案后再问「芙兰朵露的能力是什么」。"
        ),
        parameters=[
            ToolParameterInfo(
                name="question",
                param_type=ToolParamType.STRING,
                description="用户关于东方的自然语言问题（可含俗称/简称/日文名）",
                required=True,
            ),
        ],
    )
    async def tool_search(self, question: str = "", **kwargs: Any) -> str:
        del kwargs
        question = (question or "").strip()
        if not question:
            return "没给问题だぜ～把用户想问的话传进来就行。"
        try:
            retriever = self._get_retriever()
            limit = max(1, int(self.config.retrieval.limit))
            # 检索是同步的（SQLite + httpx），丢到线程里避免阻塞事件循环
            async with self._search_lock:
                hits: List[Hit] = await asyncio.to_thread(retriever.search, question, limit)
        except Exception as exc:   # noqa: BLE001 - 工具必须把失败原因如实回给模型
            self.ctx.logger.error("[marisa_touhou] 检索失败: %s", exc, exc_info=True)
            return f"知识库查询失败：{exc}"
        if not hits:
            return "知识库里没有查到相关内容。"
        return self._render(question, hits)

    def _render(self, question: str, hits: List[Hit]) -> str:
        """把检索结果排成模型好读的资料块。"""

        max_chars = max(120, int(self.config.retrieval.max_chars))
        show_source = bool(self.config.retrieval.show_source)
        lines = [f"【东方知识库】问题：{question}", ""]
        for index, hit in enumerate(hits, 1):
            lines.append(f"—— 资料{index}｜{hit.kind}｜{hit.name} ——")
            for key, value in hit.detail.items():
                if value in (None, ""):
                    continue
                text = str(value)
                if len(text) > max_chars:
                    text = text[:max_chars] + "…（截断）"
                lines.append(f"{key}：{text}")
            if show_source and hit.url:
                lines.append(f"出处：{hit.source}（{hit.url}）")
            lines.append("")
        lines.append("请依据以上资料回答；资料不足的部分如实说明，不要编造。")
        return "\n".join(lines)


def create_plugin() -> TouhouPlugin:
    """创建插件实例。"""

    return TouhouPlugin()
