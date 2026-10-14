"""✅ D1（2026-10-11 · 离散正文编排阶段）单元测试

覆盖：
A. 配置缺省关闭（零回归护栏：引入前行为逐字不变）
B. ContentPlanData 纯函数校验（候选池过滤 / 类型兜底 / 空输入）
C. to_json / from_json 往返 + 坏 JSON 回退
D. render_content_plan_guidance 纯函数
E. 提示词渲染（全占位符 + 无残留占位符 + JSON 示例已渲染）
F. plan_section_content LLM 步（成功解析 / 失败回退为空，绝不抛）
G. sections 表 DDL 含 plan_json（静态护栏，保证建表与迁移声明一致）
"""
import json
from pathlib import Path

import app.services.ai.prompts.content  # 触发 _reg 注册 content_plan_* 提示词
import app.schema_sql as schema_sql_mod
from app.config import settings
from app.services.ai.prompts._registry import render
from app.services.content_plan import (
    ContentPlanData,
    plan_section_content,
    render_content_plan_guidance,
)


# ── A. 配置缺省关闭 ───────────────────────────────────────────────
def test_config_default_off():
    # ✅ 回归护栏：默认关闭，generate_content 不触发额外 LLM 调用、不改正文提示词
    assert settings.content_plan_stage_enabled is False


# ── B. ContentPlanData 纯函数校验 ─────────────────────────────────
def test_validate_empty():
    p = ContentPlanData.validate({})
    assert p.writing_focus == ""
    assert p.knowledge_names == []
    assert p.fact_titles == []
    assert p.table_needed is False
    assert p.table_purpose == ""


def test_validate_filters_to_pool():
    # ✅ 只保留落在候选池内的素材/事实名，防止模型编造候选外条目
    raw = {
        "writing_focus": "重点写基坑支护",
        "knowledge_names": ["基坑支护规范", "不存在的素材"],
        "fact_titles": ["开挖深度", "瞎编的事实"],
        "table_needed": "true",
        "table_purpose": "罗列参数",
    }
    p = ContentPlanData.validate(
        raw, knowledge_pool=["基坑支护规范"], fact_pool=["开挖深度"]
    )
    assert p.knowledge_names == ["基坑支护规范"]
    assert p.fact_titles == ["开挖深度"]
    assert p.table_needed is True
    assert p.table_purpose == "罗列参数"


def test_validate_bool_coercion():
    assert ContentPlanData.validate({"table_needed": False}).table_needed is False
    assert ContentPlanData.validate({"table_needed": "是"}).table_needed is True
    assert ContentPlanData.validate({"table_needed": "false"}).table_needed is False


def test_validate_non_dict():
    assert ContentPlanData.validate(None).writing_focus == ""
    assert ContentPlanData.validate("garbage").writing_focus == ""


# ── C. JSON 往返 ─────────────────────────────────────────────────
def test_to_from_json_roundtrip():
    p = ContentPlanData(
        writing_focus="x", knowledge_names=["a"], fact_titles=["b"],
        table_needed=True, table_purpose="y",
    )
    assert ContentPlanData.from_json(p.to_json()) == p


def test_from_json_bad_returns_empty():
    assert ContentPlanData.from_json("{not json").writing_focus == ""


# ── D. 软建议渲染 ────────────────────────────────────────────────
def test_guidance_rendering_with_plan():
    p = ContentPlanData(
        writing_focus="聚焦支护", knowledge_names=["基坑规范"],
        fact_titles=["深度"], table_needed=True, table_purpose="参数表",
    )
    g = render_content_plan_guidance(p)
    assert "写作聚焦" in g
    assert "基坑规范" in g
    assert "建议本章使用表格" in g


def test_guidance_none_and_empty():
    assert render_content_plan_guidance(None) == ""
    # 空编排仍产出「可不强制使用表格」提示（供模型决策），不为空串
    assert "可不强制使用表格" in render_content_plan_guidance(ContentPlanData.empty())


# ── E. 提示词渲染 ────────────────────────────────────────────────
def test_prompts_render_with_all_kwargs():
    out = render(
        "content_plan_user",
        scheme_name="方案", scheme_type="基坑", section_number="3.1",
        section_title="支护", section_description="说明", word_budget=1500,
        parent_chain="上级", sibling_lines="同级", project_brief="概述",
        knowledge_names_text="- 基坑规范", fact_titles_text="- 深度",
    )
    assert "知识库素材候选" in out
    assert "全局事实候选标题" in out
    assert "{scheme_" not in out          # 无残留占位符
    assert "{writing_focus" not in out    # JSON 示例已渲染为 {..}
    assert "{" in out and "}" in out      # JSON 示例块已正确输出
    assert render("content_plan_system").strip() != ""


# ── F. plan_section_content LLM 步 ───────────────────────────────
async def _fake_chat_ok(messages, **kwargs):
    return json.dumps({
        "writing_focus": "聚焦", "knowledge_names": ["基坑规范"],
        "fact_titles": ["深度"], "table_needed": True, "table_purpose": "参数",
    })


async def test_plan_section_content_ok():
    p = await plan_section_content(
        _fake_chat_ok,
        leaf={"title": "支护", "description": "说明"},
        word_budget=1500, parent_chain="上级", sibling_lines="同级",
        project_brief="概述", knowledge_names=["基坑规范"], fact_titles=["深度"],
        scheme_name="方案", scheme_type="基坑",
    )
    assert p.knowledge_names == ["基坑规范"]
    assert p.fact_titles == ["深度"]
    assert p.table_needed is True


async def test_plan_section_content_failure_falls_back():
    # ✅ 编排失败（网络/JSON 解析）绝不阻断正文生成：回退为「无编排」
    async def _bad(messages, **kwargs):
        raise RuntimeError("boom")

    p = await plan_section_content(
        _bad, leaf={"title": "x"}, word_budget=100, parent_chain="",
        sibling_lines="", project_brief="", knowledge_names=[], fact_titles=[],
    )
    assert p.writing_focus == ""
    assert p.knowledge_names == []


# ── G. sections 表 DDL 静态护栏 ───────────────────────────────────
def test_schema_has_plan_json():
    # ✅ 建表 DDL 与迁移声明必须同时含 plan_json，否则新建库缺列 → 运行期 no such column
    assert "plan_json" in schema_sql_mod.SCHEMA_SQL


def _sse_src() -> str:
    """读 sse_handlers.py 源码（集成点护栏的唯一取源出口）。"""
    return (
        Path(__file__).resolve().parents[1]
        / "app" / "routers" / "sse_handlers.py"
    ).read_text(encoding="utf-8")


# ── H. sse_handlers 集成点：两类候选池的行形态 ────────────────────
def test_facts_pool_uses_tuple_index_not_get():
    """D1 事实候选池必须按 ``facts_rows`` 的 5 元组契约按下标取 title。

    ``_load_facts_rows`` 的行契约是 5 元组 ``(gt, title, content, confidence,
    chapter)``（facts_builder._load_facts_rows docstring 明写「必须投影回 5 元组，
    不得直接外泄」），不是 dict。曾写成 ``r.get("title")`` → AttributeError，
    被 D1 外层 fail-soft 吞掉后，编排阶段在**每一章**都静默空转：功能全灭、
    日志只有一条 WARNING，用户看不出任何异常。本护栏锁死该行形态防回写。
    """
    block = _sse_src().partition("_f_pool =")[2][:420]
    assert 'r.get("title")' not in block, "facts_rows 是 5 元组，不能按 dict 取值"
    assert "r[1]" in block, "必须按下标 1 取 title"


def test_knowledge_pool_uses_dict_name_key():
    """知识库候选池按 dict 的 name 键取值（``_load_knowledge_rows`` 返回 dict 行）。"""
    src = _sse_src()
    assert '_k_pool = [str(r.get("name") or "") for r in knowledge_rows]' in src
    assert '_f_pool = [str(r.get("title")' not in src
