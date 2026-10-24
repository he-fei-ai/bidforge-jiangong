# -*- coding: utf-8 -*-
"""五段式提取提示词 + 解析质量校验护栏（2026-10-23）

引入《标书智能体——AI解析招标文件代码+提示词》的落地护栏，覆盖本轮全部改动：

一、五段式提示词（todo「五个提取任务适配」）
  ① 五个提取任务模板（projectBasicInfo / schemeBasicInfo / overviewParams /
     compilationBasis / siteConditions）必须含【目标定位】与【输出示例】段，
     且尾部引用 {SHARED_EXTRACT_RULES} + {SHARED_EXTRACT_VERIFY} 两个共享片段；
  ② 两个共享片段必须已注册进统一注册表、变量契约显式空声明 []；
  ③ check_prompt_variables() 无契约漂移（共享片段内联后不得引入新变量）；
  ④ get_item_prompt() 渲染后不得残留 {SHARED_ 字面量（DB 优先链路解析生效），
     且 __CONTEXT__ 占位符保留（由 build_item 注入，不许提前消解）；
  ⑤ 缺失标注统一为「没有提及」——模板与共享片段中不得出现「「未提及」」
     （下游 is_missing_result / _JSON_EMPTY_VALUES / 分段合并只识别
      「没有提及」「未提取到」，出现「未提及」会静默失效）。

二、解析质量校验（todo「文件内容提取增强」）
  ⑥ 结构化格式（docx 等）体积 >1KB 却抽出 <50 字 → diag.warnings 出现
     「过短」告警（信息类，不阻断、不置 truncated）；
  ⑦ 结构损坏的 docx 必须抛 ParseError（失败不静默返回空）。

三、共享片段内容红线（BUG 清单 8/9/10 的最小回归）
  ⑧ 处理规则必须含「单位」纪律与「没有提及」缺失标注；
  ⑨ 验证步骤必须含输出前自检语义。

所有断言对应引入方案第七节 BUG 清单与第八节验证项，改动共享片段或模板
结构而未同步本测试即红。
"""
from __future__ import annotations

import io
import zipfile

import pytest
from app.services.ai.prompts._registry import (
    _ALL_PROMPTS,
    PROMPT_VARIABLE_CONTRACTS,
    check_prompt_variables,
)
from app.services.ai.prompts._shared import (
    SHARED_EXTRACT_RULES,
    SHARED_EXTRACT_VERIFY,
)
from app.services.bid_analysis_service import (
    _ITEM_PROMPT_KEYS,
    _ITEM_PROMPTS,
    get_item_prompt,
)
from app.services.file_parser import ParseError, parse_file_content_ex

#: 引入方案第五节点名的五个提取任务（五段式适配对象）
FIVE_TASKS = (
    "projectBasicInfo",
    "schemeBasicInfo",
    "overviewParams",
    "compilationBasis",
    "siteConditions",
)

#: 两个五段式共享片段键
SHARED_KEYS = ("SHARED_EXTRACT_RULES", "SHARED_EXTRACT_VERIFY")


# ---------------------------------------------------------------------------
# 一、五段式提示词
# ---------------------------------------------------------------------------
def test_five_task_templates_have_five_section_structure():
    """五任务模板必须含【目标定位】【输出示例】段并引用两个共享片段。"""
    for item_id in FIVE_TASKS:
        tpl = _ITEM_PROMPTS[item_id]
        assert "【目标定位】" in tpl, f"{item_id} 缺少【目标定位】段"
        assert "【输出示例】" in tpl, f"{item_id} 缺少【输出示例】段"
        assert "{SHARED_EXTRACT_RULES}" in tpl, f"{item_id} 未引用处理规则片段"
        assert "{SHARED_EXTRACT_VERIFY}" in tpl, f"{item_id} 未引用验证步骤片段"
        # 五段式尾部顺序：处理规则 → 验证步骤 → 资料文本（防段落顺序错乱）
        assert tpl.index("{SHARED_EXTRACT_RULES}") < tpl.index("{SHARED_EXTRACT_VERIFY}") \
            < tpl.index("__CONTEXT__"), f"{item_id} 五段式尾部顺序错误"


def test_shared_fragments_registered_with_empty_contract():
    """两个共享片段已注册且变量契约显式空声明（不得引入待填变量）。"""
    for key in SHARED_KEYS:
        assert key in _ALL_PROMPTS, f"{key} 未注册进统一提示词注册表"
        assert PROMPT_VARIABLE_CONTRACTS.get(key) == [], \
            f"{key} 变量契约必须显式声明为空列表 []"
    # 片段自身不得含任何 {变量} 占位符（会被内联进宿主模板造成混淆）
    assert "{" not in SHARED_EXTRACT_RULES, "SHARED_EXTRACT_RULES 不得含 { 占位符"
    assert "{" not in SHARED_EXTRACT_VERIFY, "SHARED_EXTRACT_VERIFY 不得含 { 占位符"


def test_no_prompt_variable_contract_drift():
    """check_prompt_variables() 返回空 = 注册表无变量契约双向漂移。"""
    issues = check_prompt_variables()
    assert issues == [], f"提示词变量契约漂移: {issues}"


def test_get_item_prompt_resolves_shared_without_residue():
    """18 项模板经 DB 优先链路渲染后：无 {SHARED_ 残留、__CONTEXT__ 保留。"""
    for item_id, key in _ITEM_PROMPT_KEYS.items():
        rendered = get_item_prompt(item_id)
        assert rendered, f"{item_id} 渲染结果为空"
        assert "{SHARED_" not in rendered, \
            f"{item_id}（{key}）渲染后残留 {{SHARED_ 字面量，共享片段未解析"
        assert "__CONTEXT__" in rendered, \
            f"{item_id}（{key}）__CONTEXT__ 被提前消解（应由 build_item 注入）"


def test_missing_label_is_unified_as_meiyouTijian():
    """缺失标注统一为「没有提及」：模板与共享片段不得出现「「未提及」」。

    下游唯一口径：PARTIAL_MISSING_TEXT="没有提及" / MARKDOWN_MISSING_RESULT=
    "未提取到"；_JSON_EMPTY_VALUES 与 routers 分段合并均不识别「未提及」，
    一旦回潮，全字段「未提及」的 JSON 会被判「已完成」造成假绿。
    """
    banned = "「未提及」"
    for item_id, tpl in _ITEM_PROMPTS.items():
        assert banned not in tpl, f"{item_id} 残留非标缺失标注 {banned}"
    assert banned not in SHARED_EXTRACT_RULES
    assert banned not in SHARED_EXTRACT_VERIFY
    # 正向：共享片段必须使用统一口径
    assert "「没有提及」" in SHARED_EXTRACT_RULES
    assert "「没有提及」" in SHARED_EXTRACT_VERIFY


def test_shared_rules_carry_unit_and_no_fabrication_discipline():
    """BUG 清单 9/10 回归：处理规则必须含单位纪律 + 缺失不编造纪律。"""
    assert "单位" in SHARED_EXTRACT_RULES, "处理规则缺少单位统一纪律（BUG-10）"
    assert "严禁推测" in SHARED_EXTRACT_RULES, "处理规则缺少禁止编造纪律（BUG-9）"
    assert "[表格数据]" in SHARED_EXTRACT_RULES, "处理规则缺少表格标注要求"


def test_shared_verify_requires_output_self_check():
    """BUG 清单 8 回归：验证步骤必须要求返回前自检（五段式最有价值段）。"""
    assert "自检" in SHARED_EXTRACT_VERIFY
    assert "返回前" in SHARED_EXTRACT_VERIFY


def test_facts_extract_system_has_self_check_section():
    """全局事实提取（最重要提取任务）必须带输出前自检段。

    刻意**内联**而非复用 SHARED_EXTRACT_VERIFY：事实任务的缺失语义是
    「资料没有的事实直接不输出」，与字段提取任务的「缺失填没有提及」相反，
    共享片段的缺失标注条款会与 facts 规则正面冲突。
    """
    from app.services.ai.prompts import get_prompt
    prompt = get_prompt("facts_extract_system")
    assert "【输出前自检" in prompt, "全局事实提取缺少输出前自检段"
    assert "is_simulated" in prompt, "自检段未覆盖禁止模拟红线"
    # 自检不得改变「缺失事实不输出」的既有语义
    assert "原文没有的事实不要生成" in prompt


# ---------------------------------------------------------------------------
# 二、解析质量校验
# ---------------------------------------------------------------------------
def _minimal_docx(padding: int = 0, with_r_ns: bool = True) -> bytes:
    """构造最小合法 docx；padding 控制 zip 体积（>1024 触发过短告警）；
    with_r_ns=False 时去掉 document.xml 的 xmlns:r 声明（构造结构损坏件）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        z.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>",
        )
        z.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
            + (' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
               if with_r_ns else "")
            + ">"
            "<w:body><w:p><w:r><w:t> </w:t></w:r></w:p></w:body></w:document>",
        )
        if padding:
            z.writestr(
                "word/styles.xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<!-- " + "padding " * padding + "-->"
                "</w:styles>",
            )
    return buf.getvalue()


def test_structured_file_with_tiny_text_raises_short_warning():
    """BUG 清单 4 补强：解析成功但内容不可用（>1KB 只抽出 <50 字）必须告警。"""
    data = _minimal_docx(padding=200)
    assert len(data) > 1024, "测试夹具须超过 1KB 阈值"
    text, diag = parse_file_content_ex(data, "empty.docx")
    assert len(text.strip()) < 50
    assert any("过短" in w for w in diag["warnings"]), \
        "未产生『解析文本过短』诊断告警"
    # 信息类告警不置 truncated、不阻断
    assert diag["truncated"] is False


def test_broken_docx_raises_parse_error_not_silent_empty():
    """BUG 清单 4：结构损坏 docx 必须抛 ParseError，不得静默返回空串。"""
    # 缺 xmlns:r 命名空间的 document.xml —— docx2python 必然 KeyError
    data = _minimal_docx(padding=0, with_r_ns=False)
    with pytest.raises(ParseError):
        parse_file_content_ex(data, "broken.docx")
