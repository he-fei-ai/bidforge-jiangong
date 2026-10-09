"""R38 · 提示词模块横切端到端验证（2026-10-03）。

按真实业务链路逐段走一遍，每段都**跑真代码、留真产物**，并对「实际下发给
模型的提示词」做统一断言：

    解析提取 → 全局事实 → 目录生成 → 目录审核 → 正文生成
             → 图表管线 → 审核与预检 → 导出 DOCX

横切断言（见 ``TestCrossCuttingPromptHygiene``）：

* **不得残留用户变量占位符** ``{xxx}``。判据用「注册表里真实存在的变量名
  全集」做白名单，而不是裸正则 —— 裸正则会把 LaTeX 的 ``{max}`` /
  ``f_{cu,k}`` 误判成漏传变量（AGENTS.md 记录过的既有误报）。
* **不得残留生成期标记** ``<<FUZZY_FILL>>`` / ``<<NO_PLACEHOLDER>>``。

⚠️ 图表段只验证**管线契约**（围栏语言 ↔ 图表类型 ↔ 校验器）与「图表字节
能进 DOCX 且图号连续」；真实 Mermaid 渲染依赖外部 Mermaid HTTP 服务，本机
不可用（实测 ``所有渲染后端均不可用``），属环境限制而非代码缺陷。

运行：``python -m pytest tests/test_prompt_e2e_crosscut_r38.py -v``
"""
from __future__ import annotations

import asyncio
import json
import re
import struct
import sys
import uuid
import zlib

import app.db as _appdb
import app.routers.sse_handlers as sh
import pytest
from app.db import get_conn, init_db

#: 全链路收集到的「实际下发给模型的提示词」，供横切断言消费。
CAPTURED_PROMPTS: list[tuple[str, str]] = []


def _capture(stage: str, prompt: str) -> None:
    CAPTURED_PROMPTS.append((stage, prompt or ""))


def _all_registered_variables() -> set[str]:
    """注册表里出现过的全部用户变量名（跨全部模板）。"""
    from app.services.ai.prompts._registry import (
        PROMPT_VARIABLE_CONTRACTS,
        extract_user_variables,
        get_default_prompt,
        list_prompts,
    )
    names: set[str] = set()
    for item in list_prompts():
        names |= set(extract_user_variables(item.get("default_content") or ""))
    for key in PROMPT_VARIABLE_CONTRACTS:
        names |= set(extract_user_variables(get_default_prompt(key) or ""))
    return names


# ---------------------------------------------------------------------------
# 公共常量 / 夹具
# ---------------------------------------------------------------------------
SCHEME_NAME = "深基坑支护及土方开挖专项施工方案"
SCHEME_TYPE = "深基坑"
STANDARDS = "GB 55032-2022 建筑与市政工程施工质量控制通用规范"


@pytest.fixture
async def ctx(tmp_path, monkeypatch):
    _appdb.DB_PATH = tmp_path / "r38_e2e.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "E2E 项目"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name,type,status) VALUES(?,?,?,?,?)",
        (sid, pid, SCHEME_NAME, SCHEME_TYPE, "目录已确认"))
    await db.commit()
    yield db, pid, sid
    await db.close()


# ===========================================================================
# 1) 解析提取：ExtractionResult → 事实落库
# ===========================================================================
@pytest.mark.asyncio
async def test_stage1_parse_extract_persists_facts(ctx):
    from app.services.facts_extractor import (
        ExtractionResult,
        FactGroup,
        FactItem,
        persist_extraction,
    )
    db, pid, sid = ctx
    result = ExtractionResult(
        groups=[FactGroup(title="基坑参数", category="param", items=[
            FactItem(name="基坑开挖深度", value="5.6m", source="施工图纸",
                     is_safety_critical=True, value_unit="m"),
            FactItem(name="地下水位", value="-1.2m", source="勘察报告"),
        ])],
        total_items=2)
    await persist_extraction(result, db, pid, sid)

    cur = await db.execute(
        "SELECT title, value_unit FROM global_facts WHERE scheme_id=?", (sid,))
    rows = await cur.fetchall()
    assert len(rows) >= 2, f"解析结果未落库：{rows}"
    depth = [r for r in rows if "基坑开挖深度" in (r["title"] or "")]
    assert depth, "开挖深度事实缺失"
    assert depth[0]["value_unit"] == "m", "单位字段未落库（导出/审核侧会丢单位）"


# ===========================================================================
# 2) 全局事实：解析结果 → 提示词事实段
# ===========================================================================
def test_stage2_global_facts_reach_prompt_without_head_truncation():
    from app.routers.compliance import _facts_prompt_text
    facts = [{"name": f"关键参数{i}", "value": "v" * 40,
              "is_safety_critical": i == 0} for i in range(120)]
    text = _facts_prompt_text(facts)
    assert "关键参数0" in text and "关键参数119" in text, (
        "尾部事实被截掉 —— 预算按头部切片而非按比例分配")
    assert "安全关键" in text
    _capture("compliance.facts", text)


@pytest.mark.parametrize("key,var", [
    ("compliance_check_system", "global_facts"),
    ("expert_review_system", "global_facts"),
    # ⚠️ 一致性审计链的变量名是 ``facts``（不是 ``global_facts``）——两条链
    #    各有自己的事实变量，契约里必须都登记，漏一个就是事实不进提示词。
    ("consistency_audit_system", "facts"),
])
def test_stage2_facts_block_is_declared_in_prompt_contract(key, var):
    """事实段必须真的进这些 AI 链路的提示词契约。"""
    from app.services.ai.prompts._registry import (
        PROMPT_VARIABLE_CONTRACTS,
        extract_user_variables,
        get_default_prompt,
    )
    assert var in PROMPT_VARIABLE_CONTRACTS.get(key, []), (
        f"{key} 契约里没有 {var} —— 事实不进提示词")
    assert var in extract_user_variables(get_default_prompt(key) or ""), (
        f"{key} 模板正文里没有 {{{var}}} 占位符")


# ===========================================================================
# 3) 目录生成：四个目录模板的真实渲染
# ===========================================================================
class TestStage3OutlinePrompts:
    BASE = dict(scheme_name=SCHEME_NAME, scheme_type=SCHEME_TYPE,
                construction_scope="基坑支护、土方开挖", scheme_basis="",
                standards_text=STANDARDS, project_facts="基坑开挖深度 5.6m")

    #: 每个目录模板的完整必填变量集（对齐 PROMPT_VARIABLE_CONTRACTS）
    #: 第三个元素 = 是否携带目录侧检查点清单。二三级子目录模板**不**消费
    #: ``outline_checkpoint_block``（那是章级编排约束，子目录阶段不适用），
    #: 拿它断言子层模板属于判据错配。
    CASES = [
        ("outline_short_system", {}, True),
        ("outline_level1_system", {"project_brief": "工程摘要",
                                    "reference_outline": "无"}, True),
        ("outline_sublevel_system", {
            "chapter_id": "1", "chapter_title": "工程概况",
            "chapter_desc": "本章说明工程背景与地质条件。",
            "other_outline": "2 编制依据", "prior_chapters": "无",
            "project_brief": "工程摘要", "requirements": "无"}, False),
        ("outline_sublevel_batch_system", {
            "chapter_count": "2",
            "chapters_text": "1.1 工程概况\n1.2 地质条件",
            "other_outline": "2 编制依据", "prior_chapters": "无",
            "project_brief": "工程摘要", "requirements": "无"}, False),
    ]

    @pytest.mark.parametrize("key,extra,want_checkpoint", CASES)
    def test_standards_reach_every_outline_template(self, key, extra,
                                                    want_checkpoint):
        """四个目录模板都必须拿到编制依据（调用方一直在传，模板必须消费）。"""
        from app.services.ai.prompts import render
        out = render(key, **self.BASE, **extra)
        _capture(key, out)
        assert "GB 55032-2022" in out, f"{key} 丢了编制依据（标准编号未注入）"
        assert "{standards_text}" not in out

    @pytest.mark.parametrize("key,extra,want_checkpoint",
                             [c for c in CASES if c[2]])
    def test_chapter_level_outline_templates_carry_checkpoint(self, key, extra,
                                                              want_checkpoint):
        from app.routers.sse_handlers import _outline_checkpoint_kwargs
        from app.services.ai.prompts import render
        out = render(key, **self.BASE, **extra,
                     **_outline_checkpoint_kwargs(SCHEME_NAME, SCHEME_TYPE, True))
        assert "审核检查点前置要求" in out, f"{key} 丢了目录侧检查点清单"

    @pytest.mark.asyncio
    async def test_review_prompt_dispatch_has_no_dangling_reference(self, monkeypatch):
        """真实下发的审核提示词：开关关闭时不得悬空引用清单。"""
        seen = {}

        async def fake_collect(messages, validate_fn=None, **kw):
            seen["p"] = messages[0]["content"]
            return ({"passed": True, "suggestions": []},
                    json.dumps({"passed": True, "suggestions": []},
                               ensure_ascii=False))

        monkeypatch.setattr(sh.settings, "outline_checkpoint_check", False)
        monkeypatch.setattr(sh, "collect_json_response", fake_collect)
        outline = [{"title": "工程概况", "description": "d", "children": []}]
        await sh._review_and_fix_outline(
            outline, SCHEME_TYPE, True, "简述", scheme_name=SCHEME_NAME,
            construction_scope="基坑支护、土方开挖")
        _capture("outline_review_system(dispatch)", seen["p"])
        assert "审核检查点前置要求" not in seen["p"], "关闭开关后清单不该出现"
        assert "8.1 目录是否覆盖" not in seen["p"], "8.1 指令悬空引用了不存在的清单"


# ===========================================================================
# 4) 正文生成：两个正文模板的真实渲染
# ===========================================================================
class TestStage4ContentPrompts:
    def _render(self, key, **extra):
        from app.services.ai.prompts import _cache
        out = _cache.get_prompt(key, **extra)
        _capture(key, out)
        return out

    def test_first_round_carries_redline_and_standards(self):
        text = self._render(
            "content_generation_system",
            scheme_name=SCHEME_NAME, scheme_type=SCHEME_TYPE, section_number="1",
            standards_text=STANDARDS)
        assert "GB 55032-2022" in text, "正文侧丢编制依据"
        assert "文件性质红线" in text, "正文侧红线缺失 —— 共享简档未被解析"
        assert "{SHARED_SCOPE_RULES_BRIEF}" not in text, "共享简档占位符残留"
        assert "<<FUZZY_FILL>>" not in text, "生成期标记未在注册期消解"

    def test_continue_round_carries_redline(self):
        text = self._render(
            "content_continue_system",
            scheme_name=SCHEME_NAME, scheme_type=SCHEME_TYPE,
            standards_text=STANDARDS)
        assert "文件性质红线" in text, "续写轮缺红线（与首轮口径不一致）"
        assert "<<NO_PLACEHOLDER>>" not in text, "生成期标记未在注册期消解"

    def test_redline_is_editable_through_runtime_cache(self):
        """用户改简档 → 正文两个模板都跟着变（本轮修复的核心行为）。"""
        from app.services.ai.prompts import _cache
        old = (_cache._prompt_cache, _cache._loaded_db_path, _cache._loaded_db_mtime)
        try:
            mark = "ZZ_E2E_REDLINE_ZZ"
            _cache._prompt_cache = {"SHARED_SCOPE_RULES_BRIEF": "### 探针\n" + mark}
            _cache._loaded_db_path = _cache._current_db_path()
            _cache._loaded_db_mtime = None
            for key in ("content_generation_system", "content_continue_system"):
                body = _cache.get_prompt(key, scheme_name="S", scheme_type="T",
                                         standards_text="X")
                assert mark in body, f"{key} 未消费用户对共享红线简档的修改"
        finally:
            (_cache._prompt_cache, _cache._loaded_db_path,
             _cache._loaded_db_mtime) = old


# ===========================================================================
# 5) 图表管线：围栏 ↔ 类型 ↔ 校验器 ↔ 导出插入
# ===========================================================================
def test_stage5_chart_fence_language_is_shared_with_pipeline():
    from app.routers._chart_pipeline import (
        INLINE_CHART_FENCE_LANGS,
        is_chart_fence_lang,
    )
    assert "mermaid" in INLINE_CHART_FENCE_LANGS
    assert is_chart_fence_lang("mermaid"), "mermaid 围栏未被识别为图表围栏"
    assert not is_chart_fence_lang("python"), "非图表围栏被误判"


def test_stage5_inline_chart_is_extracted_with_type():
    from app.routers._chart_pipeline import (
        detect_mermaid_chart_type,
        extract_inline_charts,
        has_inline_charts,
    )
    md = ("正文。\n\n```mermaid\nflowchart TD\n  A[开挖] --> B[支护]\n```\n\n"
          "如图所示。\n")
    assert has_inline_charts(md)
    found = extract_inline_charts(md)
    assert len(found) == 1, f"内联图表提取数量异常：{found}"
    ctype, code = found[0]
    assert ctype == detect_mermaid_chart_type(code) == "flowchart"


def _png_bytes(w: int = 64, h: int = 64) -> bytes:
    """最小合法 PNG（真 zlib 流 + CRC），供导出插图路径使用。

    尺寸必须让产物体积 **> 100 字节** —— 导出层的可用性判据是
    ``len(img_bytes.getvalue()) > 100``，纯色 8×8 会被判为「字节过小」而
    静默跳过（返回 False），测的就不是插入路径了。故用逐行渐变图案。
    """
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    rows = []
    for y in range(h):
        row = bytearray(b"\x00")
        for x in range(w):
            row += bytes(((x * 4) % 256, (y * 4) % 256, ((x + y) * 2) % 256))
        rows.append(bytes(row))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 6))
            + chunk(b"IEND", b""))


# ===========================================================================
# 6) 审核与预检
# ===========================================================================
def test_stage6_preflight_findings_use_registered_rule_ids():
    from app.services import audit_rules
    from app.services.preflight_engine import PreflightContext, run_preflight
    sections = [
        {"id": "c1", "title": "工程概况", "content": "本章说明工程背景。"},
        {"id": "c2", "title": "编制依据", "content": "依据相关法律法规。"},
    ]
    findings = run_preflight(PreflightContext(
        scheme_id="s1", scheme_name=SCHEME_NAME, scheme_type=SCHEME_TYPE,
        word_budget=4000, sections=sections))
    assert findings, "预检零 findings（正文明显不合格，不应有这种结果）"
    known = {r["rule_id"] for r in audit_rules.rule_catalog()}
    for f in findings:
        rid = f.get("rule_id") or ""
        resolved = audit_rules._resolve_base_rule(rid) or audit_rules.get_rule(rid)
        assert resolved is not None or rid in known, (
            f"预检产出未登记的规则编号：{rid!r}")


def test_stage6_validators_cover_consumed_fields():
    """/check 与一致性审计：校验字段集必须覆盖消费字段集。"""
    from app.routers.compliance import (
        _validate_check_results,
        _validate_consistency_audit,
    )
    assert _validate_consistency_audit({"score": 88, "issues": []}) == []
    assert _validate_consistency_audit({"score": 100}), (
        "缺 issues 竟然通过 —— 旧行为会落库空壳并返回 100 分")
    assert _validate_consistency_audit({"score": "x", "issues": []}), (
        "非数值 score 竟然通过")
    # 元素级校验器收**整个响应对象**（内部取 results），不是 results 数组本身
    assert _validate_check_results(
        {"results": [{"item": "GB 55032", "hit": True}]}) == []
    assert _validate_check_results({"results": [{"hit": True}]}), (
        "空壳行竟然通过 —— 旧行为会把空壳落库")
    assert _validate_check_results(
        {"results": [{"item": "x", "hit": "yes"}]}), "非布尔 hit 竟然通过"
    assert _validate_check_results({}), "缺 results 竟然通过"


# ===========================================================================
# 7) 导出 DOCX（真实产物）
# ===========================================================================
def test_stage7_export_produces_readable_docx(tmp_path):
    from app.routers.export import _build_docx_sync, _load_heading_styles
    from app.services.content_blocks import _parse_content_blocks

    sections = [
        {"id": "c1", "parent_id": "", "level": 1, "sort_order": 0,
         "title": "1 工程概况",
         "content": ("## 1.1 工程背景\n本工程位于城区，基坑开挖深度 5.6m，"
                     "施工期间对周边建筑进行实时监测，确保基坑安全。" * 3 + "\n")},
        {"id": "c2", "parent_id": "", "level": 1, "sort_order": 1,
         "title": "2 编制依据",
         "content": ("## 2.1 法律法规\n《建设工程安全生产管理条例》。\n"
                     "## 2.2 标准规范\n《建筑与市政工程施工质量控制通用规范》"
                     "（GB 55032-2022）。\n")},
    ]
    blocks = {s["id"]: _parse_content_blocks(s["content"]) for s in sections}
    out = str(tmp_path / "r38_e2e.docx")
    _build_docx_sync(
        out, {"id": "s1", "project_id": "p1", "name": SCHEME_NAME},
        sections, {}, {}, {}, blocks,
        "宋体", 12, "", "", False, False, False,
        bidder_name="", heading_styles=_load_heading_styles({}),
        line_spacing=1.5, page_number_style="simple", toc_depth=3)

    import docx
    d = docx.Document(out)
    texts = [p.text for p in d.paragraphs]
    joined = "\n".join(texts)
    assert "工程概况" in joined, "导出件丢失章节标题"
    assert "GB 55032-2022" in joined, "导出件丢失编制依据正文"
    assert len(texts) >= 8, f"导出件内容过少：{len(texts)} 段"


def test_stage7_chart_bytes_are_inserted_when_available():
    """图表字节可用时必须真的插入并占用图号（不经 Mermaid 服务，纯本地判据）。"""
    import io as _io

    import docx
    from app.routers.export import _add_inline_chart_from_bytes

    d = docx.Document()
    d.add_paragraph("如下图所示：")
    # 导出层图表字节口径是 file-like（内部取 .getvalue()），不是裸 bytes
    ok = _add_inline_chart_from_bytes(d, "flowchart", _io.BytesIO(_png_bytes()),
                                      "1-1", "施工流程图")
    assert ok is True, "合法 PNG 未被插入"
    texts = [p.text for p in d.paragraphs]
    assert any("图 1-1" in t for t in texts), f"图号缺失：{texts}"
    assert len(d.inline_shapes) == 1, "图片未真正进入文档"


def test_stage7_unrenderable_chart_does_not_consume_figure_number(tmp_path):
    """Mermaid 不可用时图表被跳过，**不得占号虚跳**（R35 回退纪律）。

    本机无 Mermaid HTTP 服务（实测「所有渲染后端均不可用」），正好是该
    降级路径的确定性复现条件。
    """
    from app.routers.export import _build_docx_sync, _load_heading_styles
    from app.services.content_blocks import _parse_content_blocks

    code = "flowchart TD\n  A[开挖] --> B[支护]"
    sections = [
        {"id": "c1", "parent_id": "", "level": 1, "sort_order": 0,
         "title": "1 工程概况",
         "content": ("## 1.1 流程\n如下图所示：\n\n```mermaid\n" + code
                     + "\n```\n")},
        {"id": "c2", "parent_id": "", "level": 1, "sort_order": 1,
         "title": "2 编制依据",
         "content": "## 2.1 法律法规\n《建设工程安全生产管理条例》。\n"},
    ]
    blocks = {s["id"]: _parse_content_blocks(s["content"]) for s in sections}
    out = str(tmp_path / "r38_chart_skip.docx")
    _build_docx_sync(
        out, {"id": "s1", "project_id": "p1", "name": SCHEME_NAME},
        sections, {}, {}, {}, blocks,
        "宋体", 12, "", "", False, False, False,
        bidder_name="", heading_styles=_load_heading_styles({}),
        line_spacing=1.5, page_number_style="simple", toc_depth=3)

    import docx
    texts = [p.text for p in docx.Document(out).paragraphs]
    assert not any("图 1-1" in t for t in texts), (
        f"图表未渲染却占了图号（虚跳）：{texts}")
    assert not any("插入失败" in t for t in texts), (
        f"默认模式不得写红字占位：{texts}")


# ===========================================================================
# 横切断言
# ===========================================================================
def _contract_vars_for(stage: str) -> set[str]:
    """该阶段提示词**自己**声明的变量集（空集 = 非模板文本，不参与判据）。"""
    from app.services.ai.prompts._registry import PROMPT_VARIABLE_CONTRACTS
    key = stage.split("(")[0].split(".")[0]
    return set(PROMPT_VARIABLE_CONTRACTS.get(key, []))


class TestCrossCuttingPromptHygiene:
    def test_capture_populated(self):
        """防空转：前面的用例必须真的把提示词收集进来了。"""
        assert len(CAPTURED_PROMPTS) >= 6, (
            f"提示词捕获样本过少（{len(CAPTURED_PROMPTS)}），横切断言会空转；"
            f"请整文件运行而不是只跑本类")

    def test_no_user_variable_placeholder_leaks(self):
        """下发提示词里不得残留 ``{xxx}`` 用户变量占位符。

        判据按**该模板自己的契约变量集**取白名单，而不是全局并集也不是裸
        正则：全局并集会把模板正文里的 LaTeX 示例 ``{max}`` 误判成漏传变量
        （``max`` 恰好也是别处的一个合法变量名），裸正则则把 ``{cu,k}``
        这类公式片段一并误报。
        """
        rx = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
        leaks = set()
        checked = 0
        for stage, text in CAPTURED_PROMPTS:
            declared = _contract_vars_for(stage)
            if not declared:
                continue
            checked += 1
            for name in rx.findall(text):
                if name in declared:
                    leaks.add((stage, name))
        assert checked >= 6, f"参与判据的提示词样本过少（{checked}），会空转"
        assert not leaks, f"提示词残留用户变量占位符：{sorted(leaks)}"

    def test_no_generation_marker_leaks(self):
        marks = ("<<FUZZY_FILL>>", "<<NO_PLACEHOLDER>>", "<<SCOPE_RULES>>")
        bad = [(s, m) for s, t in CAPTURED_PROMPTS for m in marks if m in t]
        assert not bad, f"生成期标记泄漏到下发提示词：{bad}"

    def test_contract_registry_healthy_after_everything(self):
        from app.services.ai.prompts import check_prompt_variables
        assert check_prompt_variables() == []


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))