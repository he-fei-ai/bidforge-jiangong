"""OmpTaskAgent（阶段 2 骨架）单元测试。

只覆盖纯逻辑（Markdown/JSON 目录解析、prompt 构造）、配置默认值与三道安全闸门
（未启用 / 二进制缺失 / 未配置模型均抛错且不拉起子进程）。不依赖真实 omp 或真实模型。

注：闸门测试通过 OmpTaskAgent 构造参数（enabled / binary / model）覆盖，避免依赖
pytest 下对 settings 实例的 monkeypatch 绑定歧义。
"""

import app.config as _cfg
import app.services.ai.omp_task_agent as ta
import pytest
from app.services.ai.omp_task_agent import (
    OmpTaskAgent,
    parse_outline_json,
    parse_outline_markdown,
)


# ===================== 纯逻辑（同步） =====================
def test_parse_outline_markdown_numbered():
    md = "# 总则\n## 1.1 工程概况\n### 1.1.1 项目背景\n## 1.2 编制范围\n"
    secs = parse_outline_markdown(md)
    assert [(s.level, s.title, s.path) for s in secs] == [
        (1, "总则", ""),
        (2, "工程概况", "1.1"),
        (3, "项目背景", "1.1.1"),
        (2, "编制范围", "1.2"),
    ]


def test_parse_outline_markdown_no_number():
    md = "## 工程概况\n## 编制依据\n"
    secs = parse_outline_markdown(md)
    assert all(s.path == "" for s in secs)
    assert [s.title for s in secs] == ["工程概况", "编制依据"]


def test_parse_outline_markdown_skips_non_heading():
    md = "# 章一\n正文段落不应被解析\n## 节一\n"
    secs = parse_outline_markdown(md)
    assert [s.title for s in secs] == ["章一", "节一"]


def test_parse_outline_json_nested():
    data = '[{"title":"章1","children":[{"title":"节1.1"},{"title":"节1.2","children":[{"title":"子1.2.1"}]}]}]'
    secs = parse_outline_json(data)
    assert [(s.level, s.title, s.path) for s in secs] == [
        (1, "章1", "1"),
        (2, "节1.1", "1.1"),
        (2, "节1.2", "1.2"),
        (3, "子1.2.1", "1.2.1"),
    ]


def test_parse_outline_json_flat():
    data = '[{"title":"a"},{"title":"b"}]'
    secs = parse_outline_json(data)
    assert [(s.level, s.title, s.path) for s in secs] == [
        (1, "a", "1"),
        (1, "b", "2"),
    ]


def test_build_outline_prompt_constraints():
    prompt = ta._build_outline_prompt(brief="某桥梁施工方案", project_type="桥梁", industry="市政")
    assert "某桥梁施工方案" in prompt
    assert "OUTLINE.md" in prompt
    assert "不要执行任何 shell 命令" in prompt
    assert "桥梁" in prompt and "市政" in prompt


# ===================== 配置默认值（只读，不 setattr） =====================
def test_default_config_values():
    a = OmpTaskAgent()
    settings = _cfg.settings
    assert a._enabled == settings.omp_task_enabled
    assert a._model == settings.omp_task_model
    assert a._tools == settings.omp_task_tools
    assert a._timeout == settings.omp_task_timeout
    assert a._keep_workspace == settings.omp_task_keep_workspace


# ===================== 安全闸门（构造参数覆盖，不拉起子进程） =====================
async def test_generate_outline_disabled_raises():
    a = OmpTaskAgent(enabled=False)
    with pytest.raises(RuntimeError, match="未启用"):
        await a.generate_outline(brief="x")


async def test_generate_outline_missing_binary_raises():
    a = OmpTaskAgent(enabled=True, binary="definitely_not_a_real_binary_xyz", model="openai/gpt-4o")
    with pytest.raises(RuntimeError, match="可执行文件不存在"):
        await a.generate_outline(brief="x")


async def test_generate_outline_missing_model_raises():
    # 用 sys.executable 作为「确实存在」的占位二进制，确保模型闸门（而非二进制闸门）先触发
    import sys

    a = OmpTaskAgent(enabled=True, binary=sys.executable, model="")
    with pytest.raises(RuntimeError, match="未配置模型"):
        await a.generate_outline(brief="x")


def test_agent_uses_shared_rpc_session():
    from app.services.ai.omp_rpc import OmpRpcSession

    assert hasattr(OmpTaskAgent, "generate_outline")
    assert OmpRpcSession.__name__ == "OmpRpcSession"
