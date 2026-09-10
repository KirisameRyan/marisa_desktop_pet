"""校验组件注册表对同名跨类型组件的处理。

背景：组件全名 ``plugin_id.name`` 不包含组件类型，而 Runner 按 ``dir()`` 的
字母序收集组件（``cmd_*`` 早于 ``tool_*``）。若同名驱逐不区分类型，后注册的
Tool 会把先注册的 Command 从类型索引里摘掉，命令会静默消失。
"""

from src.plugin_runtime.host.component_registry import ComponentRegistry

PLUGIN_ID = "marisa-team.marisa-desktop"


def _register_balance_command(registry: ComponentRegistry, pattern: str) -> None:
    """注册一条同名命令，pattern 用于区分同一组件的新旧声明。"""

    registry.register_component(
        "marisa_balance",
        "COMMAND",
        PLUGIN_ID,
        {"command_pattern": pattern, "permission": "operator"},
    )


def test_command_survives_same_name_tool_registration() -> None:
    """同名 Tool 注册后，Command 仍应留在命令索引中并可被文本匹配。"""

    registry = ComponentRegistry()
    _register_balance_command(registry, r"/余额(?:\s+[\s\S]*)?\s*$")
    registry.register_component(
        "marisa_balance",
        "TOOL",
        PLUGIN_ID,
        {"description": "查询余额"},
    )

    full_name = f"{PLUGIN_ID}.marisa_balance"
    assert [c.full_name for c in registry.get_components_by_type("COMMAND", enabled_only=False)] == [full_name]
    assert [c.full_name for c in registry.get_components_by_type("TOOL", enabled_only=False)] == [full_name]
    assert registry.find_command_by_text("/余额") is not None
    assert registry.get_component(full_name, "COMMAND") is not None
    assert registry.get_component(full_name, "TOOL") is not None


def test_command_survives_when_tool_registers_first() -> None:
    """注册顺序不应影响结果：先 Tool 后 Command 时两者同样共存。"""

    registry = ComponentRegistry()
    registry.register_component(
        "marisa_balance",
        "TOOL",
        PLUGIN_ID,
        {"description": "查询余额"},
    )
    _register_balance_command(registry, r"/余额(?:\s+[\s\S]*)?\s*$")

    full_name = f"{PLUGIN_ID}.marisa_balance"
    assert [c.full_name for c in registry.get_components_by_type("COMMAND", enabled_only=False)] == [full_name]
    assert [c.full_name for c in registry.get_components_by_type("TOOL", enabled_only=False)] == [full_name]
    assert registry.find_command_by_text("/余额") is not None


def test_same_type_registration_still_overwrites() -> None:
    """同类型同名仍应覆盖，避免重复注册产生多条同名命令。"""

    registry = ComponentRegistry()
    _register_balance_command(registry, r"/余额(?:\s+[\s\S]*)?\s*$")
    _register_balance_command(registry, r"/balance\s*$")

    commands = registry.get_components_by_type("COMMAND", enabled_only=False)
    assert len(commands) == 1
    assert registry.find_command_by_text("/balance") is not None
    assert registry.find_command_by_text("/余额") is None


def test_remove_plugin_components_clears_both_types() -> None:
    """注销插件时，同名的 Command 与 Tool 都应被清理干净。"""

    registry = ComponentRegistry()
    _register_balance_command(registry, r"/余额(?:\s+[\s\S]*)?\s*$")
    registry.register_component(
        "marisa_balance",
        "TOOL",
        PLUGIN_ID,
        {"description": "查询余额"},
    )

    assert registry.remove_components_by_plugin(PLUGIN_ID) == 2
    assert registry.get_components_by_type("COMMAND", enabled_only=False) == []
    assert registry.get_components_by_type("TOOL", enabled_only=False) == []
    assert registry.get_component(f"{PLUGIN_ID}.marisa_balance") is None
