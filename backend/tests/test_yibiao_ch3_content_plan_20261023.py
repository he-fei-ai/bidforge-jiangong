# -*- coding: utf-8 -*-
"""标书智能体（三）· 正文生成方案引入符合性护栏（2026-10-23）。

参考文档：《标书智能体（三）——生成标书正文代码+提示词》与
《生成正文代码与提示词：文档梳理与引入方案》。

复查核对原则（用户指令，✅ 2026-10-23 裁决更新）：**有不同和冲突，以外部
引用软件（OpenBidKit_Yibiao v2 / yibiao-simple）的工作机制、工作流程、标准
为主** —— 轮次、表格密度、消息序、事实注入、字数机制等一律按外部对齐（C 组
钉住）；内联图表与编号小标题属本软件独有增强，经用户裁决**保留**，与外部
的差异在 B 组列为已知差异锁，防止后人误删。

A 组 —— 参考骨架符合性（行为级 + 接线级）：
  A1 请求参数四件套等价（chapter / parent_chapters / sibling_chapters /
     project_overview → 方案名称+施工内容+项目概述+全局事实）
  A2 同级章节必须排除当前章节（参考文档点名的工程细节）
  A3 上下文字段安全取值，缺字段不抛 KeyError（BUG-2）
  A4 上级章节全部传入（父链完整）
  A5 SystemPrompt 六条要求齐备 + 专项方案适配补充
  A6 UserPrompt 结构顺序（方案名称 → 概述 → 上级 → 同级 → 当前章节 → 事实 → 字数）
  A7 上下文按优先级截断（CONTEXT_PRIORITY + SSE 接线）
  A8 按叶子逐节 + 目录序 DFS 顺序生成（派发序校验同源）
  A9 断点续生成（已有非空正文默认跳过）
  A10 字数四档 + 续写轮次上限 + 三级 300 字下限
  A11 生成后自检与一致性修复接线

B 组 —— 已知差异锁（经裁决保留的本软件独有增强）：
  B1 角色 = 工程项目专项方案编制工程师（非参考的"标书编写专家"，按引入方案§四适配）
  B2 允许 Markdown/GFM 表格与图表代码块（本软件独有：内联图表+导出渲染，裁决保留）
  B3 上下文截断默认关闭（护栏而非默认行为）
  B4 续写轮数以 settings.content_continue_max_rounds 为准（✅ 2026-10-23 对齐
     外部后默认 3 = 外部 MAX_WORD_ADJUSTMENT_ROUNDS）

C 组 —— 外部标准对齐（✅ 2026-10-23 裁决：冲突以外部 v2 为准）：
  C1 五项默认值翻转 + content_table_requirement 新增（默认 heavy 行为保持）
  C2 表格密度四档纯函数公式逐字对齐外部 contentGenerationTask.cjs
  C3 表格四档闸门接线锚点（sse_handlers：闸判/清表/占额/双链路注入）
  C4 SystemPrompt 聚焦条款与表格克制条款（外部 #5/#6 等价增补）
"""
from pathlib import Path

from app.config import settings
from app.services.ai.prompts import render
from app.services.content_plan import (
    max_tables_for_requirement,
    normalize_table_requirement,
    table_slot_allowed,
)
from app.services.content_runtime import (
    build_chapter_layered_messages,
    build_chapter_user_content,
    should_continue_round,
)
from app.services.content_utils import (
    LEAF_MIN_WORD_BUDGET,
    WORD_BUDGET_TIERS,
    build_sibling_context,
    order_sections_dfs,
    resolve_word_budget_tier,
    select_target_leaves,
    verify_generation_order,
)
from app.services.prompt_governance import CONTEXT_PRIORITY

_SSE_SRC = (Path(__file__).parents[1] / "app/routers/sse_handlers.py"
            ).read_text(encoding="utf-8")

#: SystemPrompt 渲染最小变量集（与 test_content_checkpoint_prepend 同口径）
_SYS_KW = dict(section_number="1", standards_text="",
               scheme_name="深基坑支护专项施工方案", scheme_type="深基坑",
               subheading_rule="R")


def _leaf(**ov):
    base = {"id": "s1", "title": "土方开挖",
            "description": "基坑土方开挖与运输组织"}
    base.update(ov)
    return base


def _kw(**ov):
    """build_chapter_user_content / layered_messages 共用入参。"""
    kw = {
        "scheme": {"name": "XX项目施工方案", "type": "深基坑",
                   "generation_standard": ""},
        "project_brief": "项目概述文本。",
        "content_scope": "深基坑支护工程",
        "parent_chain": "1 工程概况 > 1.2 总体部署",
        "parent_points": ["- 工程概况：本章概述内容"],
        "sibling_lines": "- 2.1 基坑排水\n- 2.2 井点降水",
        "section_number": "2.3",
        "leaf": _leaf(),
        "word_budget": 1500,
        "word_budget_hint": "",
        "prev_sibling_summary": "",
        "facts_text": "### 工程概况\n- **基坑深度**: 12.5m\n",
        "eff_standard": "precise",
        "knowledge_text": "",
    }
    kw.update(ov)
    return kw


# ============================ A 组：参考骨架符合性 ============================

class TestA1RequestParamsEquivalent:
    """参考 ChapterContentRequest 四参数在本软件中的等价形态。"""

    def test_four_param_families_present(self):
        out = build_chapter_user_content(**_kw())
        # chapter（当前章节）
        assert "【当前章节】：土方开挖" in out
        # parent_chapters（上级章节列表，保持连贯）
        assert "【上级章节链】：1 工程概况 > 1.2 总体部署" in out
        assert "【上级章节要点】：" in out
        # sibling_chapters（同级章节列表，避免重复）
        assert "【同级章节（请避免内容重复）】：" in out
        # project_overview 等价三件套：方案名称 + 施工内容 + 项目概述
        assert "【方案名称】：XX项目施工方案" in out
        assert "【方案名称主要施工内容】" in out
        assert "【项目概述】：项目概述文本。" in out

    def test_layered_messages_same_families(self):
        """分层前缀版（R57）四件套同样齐备。"""
        messages, user_content = build_chapter_layered_messages(
            sys_prompt="SYS", **_kw())
        assert "【方案名称】：XX项目施工方案" in user_content
        assert "【上级章节链】" in user_content
        assert "【同级章节（请避免内容重复）】" in user_content
        assert "【当前章节】：土方开挖" in user_content
        # 分层后至少 1 条 system + 1 条 user
        assert messages[0]["role"] == "system"
        assert any(m["role"] == "user" for m in messages)


class TestA2SiblingExcludesCurrent:
    """参考文档工程细节 ①：同级章节必须排除当前章节（否则 AI 自我参考）。"""

    def _secs(self):
        return [
            {"id": "a", "parent_id": "p", "sort_order": 1,
             "title": "支护设计", "description": "支护形式选型"},
            {"id": "b", "parent_id": "p", "sort_order": 2,
             "title": "开挖工艺", "description": "分层开挖步骤"},
            {"id": "c", "parent_id": "p", "sort_order": 3,
             "title": "监测方案", "description": "监测点布置"},
        ]

    def test_current_sibling_absent(self):
        secs = self._secs()
        lines, _ = build_sibling_context(secs, secs[1])
        assert "开挖工艺" not in lines          # 当前章节自身不得出现
        assert "支护设计" in lines and "监测方案" in lines

    def test_current_absent_even_when_first(self):
        secs = self._secs()
        lines, _ = build_sibling_context(secs, secs[0])
        assert "支护设计" not in lines


class TestA3SafeFieldAccess:
    """参考文档工程细节 ②：字段访问用安全取值，缺字段不抛 KeyError（BUG-2）。"""

    def test_user_content_leaf_without_title(self):
        kw = _kw()
        kw["leaf"] = {"id": "x", "description": "仅描述"}  # 缺 title
        out = build_chapter_user_content(**kw)             # 不得抛 KeyError
        assert "【当前章节】： — 仅描述" in out

    def test_layered_leaf_without_title(self):
        kw = _kw()
        kw["leaf"] = {"id": "x", "description": "仅描述"}
        _, user_content = build_chapter_layered_messages(sys_prompt="SYS", **kw)
        assert "【当前章节】： — 仅描述" in user_content

    def test_sibling_missing_title_and_desc(self):
        secs = [{"id": "a", "parent_id": "p", "sort_order": 0},
                {"id": "b", "parent_id": "p", "sort_order": 1}]
        lines, _ = build_sibling_context(secs, secs[1])     # 不得抛 KeyError
        assert lines.strip() == "-"

    def test_prev_summary_sibling_without_id(self):
        """同级 dict 缺 id 时前序摘要回路也必须安全取值（BUG-2）。"""
        secs = [{"parent_id": "p", "sort_order": 0, "content": "前序正文"},
                {"id": "b", "parent_id": "p", "sort_order": 1}]
        _, prev = build_sibling_context(secs, secs[1])
        assert prev == "前序正文"


class TestA4ParentChainComplete:
    def test_all_ancestors_passed(self):
        import app.routers.sse_handlers as sh
        secs = [
            {"id": "r", "parent_id": "", "sort_order": 0, "title": "工程概况"},
            {"id": "p1", "parent_id": "r", "sort_order": 1, "title": "总体部署"},
            {"id": "p2", "parent_id": "p1", "sort_order": 1,
             "title": "施工准备"},
            {"id": "leaf", "parent_id": "p2", "sort_order": 1, "title": "技术准备"},
        ]
        chain = sh._build_parent_chain(secs, "leaf")
        assert "工程概况" in chain and "总体部署" in chain and "施工准备" in chain


class TestA5SystemSixRequirements:
    """参考 SystemPrompt 六条要求 —— 保留并适配专项施工方案定位。"""

    def test_six_requirements_present(self):
        text = render("content_generation_system", **_SYS_KW)
        # 1 专业准确、与标题描述一致
        assert "与章节标题和描述保持一致" in text
        # 2 技术方案不是宣传报告、朴实无华
        assert "不是宣传报告" in text and "朴实无华" in text
        # 3 语言正式规范、不像 AI 生成（软件口径：AI 表述痕迹/AI 表述禁令）
        assert "AI 表述痕迹" in text
        assert "禁止 AI 表述" in text
        # 4 详细具体、避免空泛
        assert "避免空泛的描述" in text
        # 5 避免与同级章节重复
        assert "避免同级章节内容重复" in text
        # 6 直接返回内容、不生成标题
        assert "不要生成章节标题" in text

    def test_adaptation_additions(self):
        """适配补充：结合方案主要施工内容 / 结合适用规范 / 不编造关键数据。"""
        text = render("content_generation_system", **_SYS_KW)
        assert "【方案名称主要施工内容】" in text   # 结合主要施工内容
        assert "标准与图集引用规范" in text          # 结合适用规范
        assert "数据真实性红线" in text              # 不得编造关键数据
        assert "禁止编造标准" in text


class TestA6UserPromptStructure:
    """参考 UserPrompt 拼接结构在本软件中的顺序（方案名 → … → 生成要求）。"""

    def test_section_order(self):
        out = build_chapter_user_content(**_kw())
        tags = ["【方案名称】", "【项目概述】", "【方案名称主要施工内容】",
                "【上级章节链】", "【同级章节（请避免内容重复）】",
                "【当前章节】", "【目标字数】",
                "【全局事实变量（唯一可信数据源）】"]
        idx = [out.index(t) for t in tags]
        assert idx == sorted(idx), "UserPrompt 段落顺序与参考结构不一致"

    def test_layered_order_and_last_task_segment(self):
        """分层版：稳定段（方案/概述/事实）在前，本章任务差异在最后。"""
        messages, _ = build_chapter_layered_messages(sys_prompt="SYS", **_kw())
        users = [m["content"] for m in messages if m["role"] == "user"]
        assert users, "分层后必须有 user 消息"
        assert "【方案名称】" in users[0]
        assert "【当前章节】" in users[-1]


class TestA7PriorityTruncation:
    """参考 BUG-3：上下文过长按优先级截断。"""

    def test_priority_order(self):
        assert (CONTEXT_PRIORITY["全局事实变量"]
                < CONTEXT_PRIORITY["当前章节"]
                < CONTEXT_PRIORITY["项目概述"]
                < CONTEXT_PRIORITY["项目知识库素材"])

    def test_wired_into_content_generation(self):
        assert "_apply_prompt_context_budget(" in _SSE_SRC
        # 分层后对每条 user 消息逐一治理（R57）
        assert '_guard_external_material(_msg.get("content") or "")' in _SSE_SRC


class TestA8LeafwiseOrderedGeneration:
    """参考 BUG-5：按目录序逐节生成（order_sections_dfs + select_target_leaves）。"""

    def _secs(self):
        # 故意乱序给出（扁平行、sort_order 为同级序号）
        return [
            {"id": "c1", "parent_id": "r", "sort_order": 1, "title": "2"},
            {"id": "r", "parent_id": "", "sort_order": 0, "title": "1"},
            {"id": "c2", "parent_id": "r", "sort_order": 2, "title": "3"},
        ]

    def test_dfs_document_order(self):
        ordered = order_sections_dfs(self._secs())
        assert [s["id"] for s in ordered] == ["r", "c1", "c2"]

    def test_select_leaves_only_leaves_in_order(self):
        leaves = select_target_leaves(order_sections_dfs(self._secs()), mode="all")
        assert [s["id"] for s in leaves] == ["c1", "c2"]

    def test_dispatch_order_verifiable(self):
        """派发序与目录序逐位比对的校验器存在且生效（R102 · G1）。"""
        ordered = order_sections_dfs(self._secs())
        leaves = select_target_leaves(ordered, mode="all")
        rep = verify_generation_order([s["id"] for s in leaves],
                                      [s["id"] for s in leaves])
        assert rep["ok"] is True

    def test_wired_into_sse(self):
        assert "order_sections_dfs(" in _SSE_SRC
        assert "select_target_leaves(" in _SSE_SRC


class TestA9ResumeGeneration:
    """参考「断点续生成」：已有正文（content 非空且 word_count>0，软件判据）
    默认跳过，从下一节点继续。"""

    def test_existing_content_skipped(self):
        secs = [
            {"id": "a", "parent_id": "", "sort_order": 0, "title": "已完成",
             "content": "已有正文", "word_count": 10},
            {"id": "b", "parent_id": "", "sort_order": 1, "title": "未完成",
             "content": ""},
        ]
        ordered = order_sections_dfs(secs)
        out = select_target_leaves(ordered, mode="all")
        assert [s["id"] for s in out] == ["b"]
        # force_rewrite 才会重生成已完成章节
        out2 = select_target_leaves(ordered, mode="all", force_rewrite=True)
        assert [s["id"] for s in out2] == ["a", "b"]


class TestA10WordControl:
    """参考「字数控制」：四档 + 扩展循环有上限 + L3 约 300 字。"""

    def test_four_tiers(self):
        assert WORD_BUDGET_TIERS == {"brief": 800, "standard": 1200,
                                     "detailed": 2000, "ultra": 2500}
        assert resolve_word_budget_tier("brief") == 800
        assert resolve_word_budget_tier("ultra") == 2500
        assert resolve_word_budget_tier(1200) == 1200     # 档位数值形态
        assert resolve_word_budget_tier("非法档位") is None  # 非法回落旧链路

    def test_l3_floor_300(self):
        assert LEAF_MIN_WORD_BUDGET == 300
        # 极小预算脏数据章：90 字 < max(100×0.8, 300) 仍触发续写
        assert should_continue_round(wc=90, word_budget=100, continue_count=0,
                                     min_passes=0, max_rounds=2,
                                     min_floor_chars=300) is True

    def test_expansion_loop_has_cap(self):
        """扩展循环单节有上限（参考 BUG-6：循环无上限）。"""
        count = 0
        while should_continue_round(wc=100, word_budget=1200,
                                    continue_count=count, min_passes=0,
                                    max_rounds=2):
            count += 1
        assert count == 2

    def test_wired_into_sse(self):
        assert "resolve_word_budget_tier(" in _SSE_SRC
        assert "CONTENT_CONTINUE_MAX_ROUNDS" in _SSE_SRC


class TestA11SelfCheckAndConsistency:
    """参考 BUG-8/9：生成后自检 + 一致性审计修复接线。"""

    def test_post_generation_selfcheck_wired(self):
        assert "standard_report(" in _SSE_SRC            # 事实冲突检出
        assert "格式契约自检" in _SSE_SRC                 # 三端格式契约第一层
        assert "content_selfcheck" in _SSE_SRC           # 生成后程序化自检开关

    def test_selfcheck_defaults_on(self):
        assert settings.content_selfcheck is True
        assert settings.content_selfcheck_autofix is True

    def test_consistency_repair_wired_default_on(self):
        assert 'body.get("auto_consistency_repair"), default=True' in _SSE_SRC

    def test_fact_autofix_unique_hit_only(self):
        """参考「普通修复：old_text/new_text 唯一命中才替换」。"""
        src = (Path(__file__).parents[1] / "app/services/consistency_edits.py"
               ).read_text(encoding="utf-8")
        assert "唯一" in src


# ======================= B 组：冲突以软件为主（差异锁） =======================

class TestBConflictsResolveToSoftware:
    def test_b1_role_is_scheme_specialist(self):
        """参考角色「标书编写专家」→ 本软件为专项方案编制专家（定位冲突）。"""
        text = render("content_generation_system", **_SYS_KW)
        assert text.startswith("你是资深的工程项目专项方案编制工程师")
        assert "标书编写专家" not in text

    def test_b2_markdown_and_charts_allowed(self):
        """参考「不要任何额外说明或格式标记」与本软件图表一体生成机制冲突
        → 以软件为准：允许 Markdown/GFM 表格与三种图表围栏。"""
        text = render("content_generation_system", **_SYS_KW)
        assert "GFM 语法" in text
        assert "严禁输出 HTML 标签" in text
        assert "图表同步生成规范" in text
        assert "```mermaid" in text

    def test_b3_truncation_default_off(self):
        """截断是护栏而非默认行为：开启必须由用户显式配置（软件标准）。"""
        assert settings.prompt_context_budget == 0
        assert settings.context_length_limit == 0

    def test_b4_continue_rounds_follow_software_setting(self):
        """续写轮数以本软件配置为准（✅ 2026-10-23 对齐外部：默认 3 =
        外部 MAX_WORD_ADJUSTMENT_ROUNDS，见 C 组）。"""
        assert settings.content_continue_max_rounds == 3


# ============ C 组：外部标准对齐（✅ 2026-10-23 裁决：冲突以外部 v2 为准） ============

class TestC1ExternalDefaultAlignment:
    """C1 —— 五项默认翻转 + 表格需求配置新增（外部机制恒执行 ⇒ 默认开启）。"""

    def test_five_defaults_aligned_to_external(self):
        # D1/G1/G2/G3：外部无此开关、对应机制恒执行 → 本机默认改开
        assert settings.content_plan_stage_enabled is True
        assert settings.content_plan_selected_facts_inject is True
        assert settings.consistency_fullbook_repair_enabled is True
        assert settings.word_adjust_batch_enabled is True
        # 外部 MAX_WORD_ADJUSTMENT_ROUNDS = 3（04_content.md §6.2）
        assert settings.content_continue_max_rounds == 3

    def test_table_requirement_added_default_heavy(self):
        """外部前后端 tableRequirement 缺省 'heavy'（§8.3/§8.5）；本机新增同名
        配置保持同缺省 ⇒ 引入前无闸门，缺省行为零回归。"""
        assert settings.content_table_requirement == "heavy"

    def test_off_switches_preserved_for_compatibility(self):
        """向后兼容：所有翻转项仍可通过配置关闭回旧行为（纯加法式扩展）。"""
        s = settings.__class__(
            content_plan_stage_enabled=False,
            content_plan_selected_facts_inject=False,
            consistency_fullbook_repair_enabled=False,
            word_adjust_batch_enabled=False,
            content_continue_max_rounds=2,
            content_table_requirement="none",
        )
        assert s.content_plan_stage_enabled is False
        assert s.consistency_fullbook_repair_enabled is False
        assert s.word_adjust_batch_enabled is False
        assert s.content_continue_max_rounds == 2
        assert s.content_table_requirement == "none"


class TestC2TableQuadrantFormulas:
    """C2 —— 表格密度四档纯函数公式逐字对齐外部 contentGenerationTask.cjs。"""

    def test_normalize_matches_external(self):
        """对齐 :379-389（含中文别名分支与 heavy 兜底）。"""
        assert normalize_table_requirement("none") == "none"
        assert normalize_table_requirement("light") == "light"
        assert normalize_table_requirement("moderate") == "moderate"
        assert normalize_table_requirement("heavy") == "heavy"
        assert normalize_table_requirement(" 不要 ") == "none"
        assert normalize_table_requirement("少量") == "light"
        assert normalize_table_requirement("适中") == "moderate"
        assert normalize_table_requirement("大量") == "heavy"
        assert normalize_table_requirement(None) == "heavy"
        assert normalize_table_requirement("") == "heavy"
        assert normalize_table_requirement("bogus") == "heavy"

    def test_max_tables_matches_external_formulas(self):
        """对齐 :520-525：none→0；light→floor(n*0.2)；moderate→floor(n*0.4)；
        heavy→None（无上限哨兵，不是 0、不是数字）。"""
        assert max_tables_for_requirement("none", 100) == 0
        assert max_tables_for_requirement("light", 10) == 2   # floor(2.0)
        assert max_tables_for_requirement("light", 7) == 1    # floor(1.4)
        assert max_tables_for_requirement("moderate", 10) == 4  # floor(4.0)
        assert max_tables_for_requirement("moderate", 7) == 2   # floor(2.8)
        assert max_tables_for_requirement("heavy", 10) is None
        assert max_tables_for_requirement("none", -5) == 0
        assert max_tables_for_requirement("light", 0) == 0

    def test_slot_allowed_cumulative_quota_semantics(self):
        """顺序流累计配额（外部 pickDistributedTableTargets 分槽思想的等价形态）。"""
        # heavy（None）恒放行
        assert table_slot_allowed(None, 0, 0, 10) is True
        assert table_slot_allowed(None, 99, 9, 10) is True
        # none（0）恒拒绝
        assert table_slot_allowed(0, 0, 0, 10) is False
        # cap=4 / total=10：首章额度 ceil(0.4)=1 → 不许囤积
        assert table_slot_allowed(4, 0, 0, 10) is True
        assert table_slot_allowed(4, 1, 0, 10) is False
        # 中段 pos=4 → ceil(4*5/10)=2
        assert table_slot_allowed(4, 1, 4, 10) is True
        assert table_slot_allowed(4, 2, 4, 10) is False
        # 末章额度=4 → 总量恒 ≤ cap
        assert table_slot_allowed(4, 1, 9, 10) is True
        assert table_slot_allowed(4, 4, 9, 10) is False
        # 异常入参防御：负数钳到 0；total<=0 不额外限制
        assert table_slot_allowed(2, -1, -5, 10) is True
        assert table_slot_allowed(2, 0, 0, 0) is True


class TestC3TableGateWiring:
    """C3 —— 表格四档闸门在 sse_handlers 的接线锚点。"""

    def test_gate_block_and_helpers_wired(self):
        assert "normalize_table_requirement(" in _SSE_SRC
        assert "max_tables_for_requirement(" in _SSE_SRC
        assert "table_slot_allowed(" in _SSE_SRC
        assert "nonlocal _table_slot_used" in _SSE_SRC
        assert "_table_gate_line" in _SSE_SRC

    def test_plan_cleared_and_quota_reconciled(self):
        """对齐外部 :3989-3999：未中选 → clearContentPlanTable 清表 +
        占额对账释放（预占不漏账）。"""
        anchor = "if _plan.table_needed and not _table_allowed:"
        assert anchor in _SSE_SRC
        seg = _SSE_SRC[_SSE_SRC.index(anchor):]
        seg = seg[:1200]
        assert '_plan.table_needed = False' in seg
        assert '_plan.table_purpose = ""' in seg
        assert "_table_slot_used -= 1" in seg

    def test_gate_line_injected_into_both_message_paths(self):
        """表格闸门行必须双链路可达（防 R74 式静默丢弃）。

        R169 D2 收口后：闸门行恰一次 append 进 ``_chapter_increments``
        单一装配点，双分支同源消费（stable 经 ``_incs_stable_text``、
        非 stable 经 ``_incs_plain_text``），旧双口径补拼已铲除。
        """
        assert '_chapter_increments.append(("\\n", _table_gate_line))' in _SSE_SRC
        assert "_incs_stable_text" in _SSE_SRC, "stable 分支消费点丢失"
        assert "_incs_plain_text" in _SSE_SRC, "非 stable 分支消费点丢失"

    def test_d1_failure_releases_reservation(self):
        """编排 fail-soft 时必须回滚预占（否则配额越用越紧）。"""
        anchor = "离散编排阶段失败（回退为无编排）"
        assert anchor in _SSE_SRC
        seg = _SSE_SRC[_SSE_SRC.index(anchor):]
        seg = seg[:600]
        assert "_table_slot_used -= 1" in seg

    def test_default_heavy_gate_is_inert(self):
        """缺省 'heavy' ⇒ cap 恒 None ⇒ 闸恒放行（缺省零回归的函数层证明）。"""
        assert max_tables_for_requirement(
            settings.content_table_requirement, 1000) is None


class TestC4PromptAlignmentClauses:
    """C4 —— SystemPrompt 聚焦条款与表格克制条款（外部 #5/#6 等价增补）。"""

    def test_focus_clause_present(self):
        """外部 #5「围绕标题与编排重点聚焦」→ 要求 15（加法式，不重排旧序号）。"""
        text = render("content_generation_system", **_SYS_KW)
        assert "15. 若下文给出【本章离散编排要点" in text
        assert "写作聚焦" in text
        assert "围绕参考软件" not in text  # 不得出现占位式引用残留写法
        # 旧 1..14 条序不被破坏（防重排引入回归）
        for n in ("1. 内容要专业", "7. ✅ 图表与正文同步生成",
                  "10. 直接返回章节正文内容", "14. 严格按目录顺序编写"):
            assert n in text

    def test_table_restraint_clause_present(self):
        """外部 #6 表格克制（条件允许才用）→ 写作风格规范表格条增补。"""
        text = render("content_generation_system", **_SYS_KW)
        assert "不得为凑形式硬插表格" in text
        assert "【表格需求】" in text
        assert "以闸门指令为最高口径" in text
