"""第一方插件与桌宠启动面的契约回归测试。

这三类事故在本仓库都**真实发生过**，本文件把它们变成会红的测试：

1. **插件文件只存在于本地、从未入库**。根目录 `.gitignore` 的 ``/plugins/*`` 整体忽略，
   导致 ``plugins/marisa_cv`` 0 文件入库、``plugins/marisa_desktop`` 81 个只入库 24 个，
   连桌宠硬依赖的 ``core/__init__.py`` 都不在版本库里——新 clone 的机器桌宠直接崩。
   （``plugins/MANIFEST.md`` 与 ``plugins/marisa_cv/config.toml`` 也各被忽略规则吃掉过一次。）
2. **插件 config.toml 缺失或缺 ``[plugin].config_version``**。宿主加载插件时这是必填项，
   缺了会在启动阶段直接报错。
3. **``run_pet.py`` 的桌宠引导段退化**。引导段做两件事：固定标准输出为 UTF-8
   （Windows GBK 管道上 ``✓`` 会抛 UnicodeEncodeError，Qt 槽内未处理异常直接终止进程），
   以及按文件路径把插件的 ``core`` 包显式注册进 ``sys.modules``
   （否则 ``src/core`` 会抢走插件 ``core``，导入期即崩，就是 changelog 里的「秒退」）。

本文件不启动 Qt、不联网、不读数据库、不写任何持久化状态，可在 CI 里裸跑。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tomllib

from pathlib import Path
from typing import Any

import pytest

# pytests/plugins_test/<本文件> → 仓库根目录
REPO_ROOT = Path(__file__).resolve().parents[2]

# 主仓库自己维护、缺了就跑不起来的第一方插件（来源与必需性见 plugins/MANIFEST.md）
FIRST_PARTY_PLUGINS: tuple[str, ...] = ("marisa_cv", "marisa_desktop", "marisa_gate", "marisa_voice")

# 宿主 PluginManifest 认得的 plugin_type
KNOWN_PLUGIN_TYPES: frozenset[str] = frozenset({"normal", "adapter", "extension"})

# 每个第一方插件都必须随主仓库入库的关键文件
REQUIRED_TRACKED_FILES: tuple[str, ...] = (
    "plugins/MANIFEST.md",
    "plugins/marisa_desktop/core/__init__.py",
)

_RUN_PET_PATH = REPO_ROOT / "run_pet.py"

# 桌宠引导段与 Qt 导入的分界：只执行它之前的代码，就能在不启动 Qt 的前提下验证引导行为
_RUN_PET_BOOTSTRAP_END_MARKER = "from PyQt6"

_RUN_PET_BOOTSTRAP_PROBE = """
import json
import sys
from pathlib import Path

_run_pet = Path(__RUN_PET_PATH__)
_source = _run_pet.read_text(encoding="utf-8")
_index = _source.find(__BOOTSTRAP_END_MARKER__)
if _index <= 0:
    raise SystemExit("run_pet.py 引导段结构已变：在 Qt 导入之前找不到预期内容")

# 只执行引导段：固定 UTF-8 输出 + 把插件 core 绑定进 sys.modules
exec(
    compile(_source[:_index], str(_run_pet), "exec"),
    {"__file__": str(_run_pet), "__name__": "__main__"},
)

_core = sys.modules.get("core")
print(json.dumps({
    "core_file": getattr(_core, "__file__", None),
    "stdout_encoding": sys.stdout.encoding,
}, ensure_ascii=False))
"""


def _plugin_dir(plugin: str) -> Path:
    """返回插件目录。"""
    return REPO_ROOT / "plugins" / plugin


def _load_manifest(plugin: str) -> dict[str, Any]:
    """读取插件 _manifest.json。"""
    manifest_path = _plugin_dir(plugin) / "_manifest.json"
    assert manifest_path.is_file(), f"{plugin}: 缺少 _manifest.json"
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    return manifest


def _load_plugin_config(plugin: str) -> dict[str, Any]:
    """读取插件 config.toml。"""
    config_path = _plugin_dir(plugin) / "config.toml"
    assert config_path.is_file(), f"{plugin}: 缺少 config.toml"
    with config_path.open("rb") as handle:
        config: dict[str, Any] = tomllib.load(handle)
    return config


def _git_tracked_files(patterns: tuple[str, ...]) -> set[str]:
    """返回 git 已跟踪的文件集合（路径用 / 分隔，相对仓库根）。"""
    completed = subprocess.run(
        ["git", "ls-files", *patterns],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, f"git ls-files 执行失败: {completed.stderr}"
    return {line.strip() for line in completed.stdout.splitlines() if line.strip()}


@pytest.mark.parametrize("plugin", FIRST_PARTY_PLUGINS)
def test_first_party_plugin_has_required_layout(plugin: str) -> None:
    """第一方插件的四个必需文件必须都在磁盘上。"""
    plugin_dir = _plugin_dir(plugin)
    assert plugin_dir.is_dir(), f"缺少插件目录: plugins/{plugin}"

    for name in ("_manifest.json", "plugin.py", "config.toml"):
        assert (plugin_dir / name).is_file(), f"plugins/{plugin}/{name} 不存在"


@pytest.mark.parametrize("plugin", FIRST_PARTY_PLUGINS)
def test_first_party_plugin_manifest_is_v2_and_complete(plugin: str) -> None:
    """manifest 必须是 v2 且 id / version / plugin_type / host_application 齐全。"""
    manifest = _load_manifest(plugin)

    assert manifest.get("manifest_version") == 2, f"{plugin}: manifest_version 必须是 2"
    assert isinstance(manifest.get("id"), str) and manifest["id"].strip(), f"{plugin}: id 缺失"
    assert isinstance(manifest.get("version"), str) and manifest["version"].strip(), f"{plugin}: version 缺失"
    assert manifest.get("plugin_type") in KNOWN_PLUGIN_TYPES, (
        f"{plugin}: plugin_type={manifest.get('plugin_type')!r} 不在 {sorted(KNOWN_PLUGIN_TYPES)} 内"
    )

    host_app = manifest.get("host_application")
    assert isinstance(host_app, dict), f"{plugin}: 缺少 host_application"
    assert host_app.get("min_version") and host_app.get("max_version"), (
        f"{plugin}: host_application 必须同时声明 min_version 与 max_version"
    )


@pytest.mark.parametrize("plugin", FIRST_PARTY_PLUGINS)
def test_first_party_plugin_config_declares_config_version(plugin: str) -> None:
    """config.toml 必须有 [plugin].config_version——宿主加载插件时这是必填项。"""
    config = _load_plugin_config(plugin)

    plugin_section = config.get("plugin")
    assert isinstance(plugin_section, dict), f"{plugin}: config.toml 缺少 [plugin] 段"
    config_version = plugin_section.get("config_version")
    assert isinstance(config_version, str) and config_version.strip(), (
        f"{plugin}: [plugin].config_version 缺失或为空，宿主加载该插件会直接报错"
    )
    assert isinstance(plugin_section.get("enabled"), bool), f"{plugin}: [plugin].enabled 必须是布尔值"


def test_first_party_plugin_files_are_tracked_by_git() -> None:
    """第一方插件的关键文件必须已入库——磁盘上有、git 里没有，等于新 clone 缺文件。"""
    if shutil.which("git") is None:
        pytest.skip("环境里没有 git，跳过入库完整性检查")
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("不是 git 工作区（可能是导出的源码包），跳过入库完整性检查")

    required = list(REQUIRED_TRACKED_FILES)
    for plugin in FIRST_PARTY_PLUGINS:
        required.extend(
            (
                f"plugins/{plugin}/_manifest.json",
                f"plugins/{plugin}/plugin.py",
                f"plugins/{plugin}/config.toml",
            )
        )

    tracked = _git_tracked_files(tuple(required))
    missing = sorted(path for path in required if path not in tracked)

    assert not missing, (
        "以下第一方插件文件存在于磁盘但没有入库，新 clone 会缺失：\n"
        + "\n".join(f"  - {path}" for path in missing)
        + "\n提示：/plugins/* 默认整体忽略；检查 .gitignore 的反白名单，"
        "并注意 gitignore 是「最后匹配者胜」（裸 config.toml 之类的规则会覆盖靠前的反白名单）。"
    )


def test_run_pet_bootstrap_binds_plugin_core_and_forces_utf8_stdout() -> None:
    """桌宠引导段必须把插件的 core 绑定到 sys.modules，并把标准输出固定为 UTF-8。

    只执行 run_pet.py 里 Qt 导入之前的引导段：既验证了真实代码，又不需要显示环境。
    """
    assert _RUN_PET_PATH.is_file(), "run_pet.py 不存在"

    probe = _RUN_PET_BOOTSTRAP_PROBE.replace(
        "__RUN_PET_PATH__", repr(str(_RUN_PET_PATH))
    ).replace("__BOOTSTRAP_END_MARKER__", repr(_RUN_PET_BOOTSTRAP_END_MARKER))

    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, (
        f"桌宠引导段执行失败（returncode={completed.returncode}）:"
        f"\nstdout: {completed.stdout}\nstderr: {completed.stderr}"
    )

    result = json.loads(completed.stdout.strip().splitlines()[-1])

    core_file = result["core_file"]
    assert core_file, "引导段执行后 sys.modules 里没有 core 包"
    core_path = Path(core_file).resolve()
    expected_dir = (_plugin_dir("marisa_desktop") / "core").resolve()
    assert core_path.parent == expected_dir, (
        f"core 绑定到了错误的包: {core_path}\n期望落在插件目录: {expected_dir}\n"
        "（这正是 src/core 抢占插件 core 导致的桌宠「秒退」）"
    )

    assert (result["stdout_encoding"] or "").lower().replace("_", "-") == "utf-8", (
        f"标准输出未固定为 UTF-8，实际是 {result['stdout_encoding']!r}；"
        "GBK 管道上状态文案里的 ✓ 会抛 UnicodeEncodeError 直接终止桌宠进程"
    )
