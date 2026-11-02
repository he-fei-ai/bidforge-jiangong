"""R162（2026-11-01）目录层级标准「三级最小叶子」护栏（方案A落地判据）。

背景（取证）：生产 182 节点三级树（9 L1 / 31 L2 / 142 L3）曾因 R151「四级
最小叶子」标准被 142 处违规拒绝、合规修复轮 120s 超时即终局 failed，用户
选择「目录树管到 L3，L3 下 4~6 个四级数量控制转入正文提示词」。新标准：
  - L1 下 3~5 个 L2、L2 下 3~5 个 L3（数量硬约束保留）；
  - L3 为最小叶子 = 正文写作单元；L4 仅作兼容层（不作数量校验）；L5 仍严禁；
  - 「三级下 4~6 个四级」从树校验移除，转为 L3 叶子节正文生成的
    结构要求（sse_handlers 按 level==3 叶子注入，成稿以 Heading 4 呈现）。

判据分 8 组：规则判据 / 闸门行为 / 提示词文案 / 骨架 / 结构要求注入 /
修复轮加固 / 导出 Heading 4 行为锁 / 前端 parity。
"""
from __future__ import annotations

import json
from pathlib import Path

from app.services import outline_rules
from app.services.outline_rules import (
    LEAF_LEVEL,
    OUTLINE_MAX_LEVEL,
    is_compliant_outline,
    validate_outline_nodes,
    validate_section_rows,
)

BACKEND = Path(__file__).resolve().parents[1]
_FRONTEND = BACKEND.parent / "frontend" / "src"
_OUTLINE_PROMPTS = BACKEND / "app" / "services" / "ai" / "prompts" / "outline.py"
_FRAMEWORK = BACKEND / "app" / "services" / "outline_framework.py"
_SCHEMAS = BACKEND / "app" / "services" / "agent" / "outline_schemas.py"
_STAGE_PROMPTS = BACKEND / "app" / "services" / "agent" / "stage_prompts.py"
_COMPLIANCE = BACKEND / "app" / "services" / "outline_compliance.py"


def node(title, children=None, description="d"):
    return {"title": title, "description": description,
            "children": (children if children is not None else [])}


def l3_leaf_tree(n_ch2=3, n_l3=3):
    """三级叶子树（L1→L2→L3 数量合规）——R162 合法形态。"""
    return [node(f"第{i}章", [
        node(f"{i}.{j}", [
            node(f"{i}.{j}.{k}") for k in range(1, n_l3 + 1)
        ]) for j in range(1, n_ch2 + 1)
    ]) for i in range(1, 2)]


# ============================================================
# A · 规则判据（单一事实源 outline_rules）
# ============================================================
class TestR162Rules:
    def test_constants(self):
        assert OUTLINE_MAX_LEVEL == 4
        assert LEAF_LEVEL == 3
        assert outline_rules.CHILDREN_MIN == {1: 3, 2: 3}
        assert outline_rules.CHILDREN_MAX == {1: 5, 2: 5}

    def test_l3_leaf_compliant(self):
        """L3 为最小叶子：L1/L2 数量合规的三级树零违规。"""
        assert validate_outline_nodes(l3_leaf_tree()) == []

    def test_l1_l2_counts_still_enforced(self):
        """L1→L2（3~5）、L2→L3（3~5）数量约束保留。"""
        tree = [
            node("第一章", [
                node("1.1", [node("1.1.1"), node("1.1.2")]),  # L2 下仅 2 个三级
                node("1.2", [node("1.2.1")]),                # 1 个三级
            ])
        ]
        v = validate_outline_nodes(tree)
        assert len(v) == 3 and all(x.code == outline_rules.V_TOO_FEW_CHILDREN for x in v)
        assert {x.level for x in v} == {1, 2}, "L1 数量与 L2 数量均保留校验"
        assert {x.expected for x in v} == {"3~5"}

    def test_l3_to_l4_count_not_validated(self):
        """R162 核心翻转：L3 下是否细分 L4 不再校验（数量不约束）。"""
        tree = l3_leaf_tree(n_l3=3)
        l3 = tree[0]["children"][0]["children"][0]
        l3["children"] = [node("四级1"), node("四级2"),
                          node("四级3"), node("四级4"), node("四级5")]
        assert validate_outline_nodes(tree) == []

    def test_l2_leaf_still_violation_hint(self):
        """叶子层级未达三级（只有两级树）仍违规，hint 为「叶子层级必须达到三级」。"""
        two_level = [node("第一章", [node("1.1"), node("1.2")])]
        v = validate_outline_nodes(two_level)
        assert v and all(x.code == outline_rules.V_TOO_FEW_CHILDREN for x in v)
        assert any("叶子层级必须达到三级" in x.detail for x in v)

    def test_l5_still_illegal(self):
        tree = l3_leaf_tree()
        l3 = tree[0]["children"][0]["children"][0]
        l3["children"] = [node("四级1", [node("五级1")])]
        v = validate_outline_nodes(tree)
        assert any(x.code == outline_rules.V_DEPTH_EXCEEDED for x in v)

    def test_is_compliant_pair(self):
        assert is_compliant_outline(l3_leaf_tree())[0] is True
        assert is_compliant_outline([node("第一章", [node("1.1")])])[0] is False


# ============================================================
# B · 正文闸门行为（三级树放行 + 违规拒绝）
# ============================================================
class TestR162Gate:
    def test_gate_accepts_three_level_tree(self):
        """R162：三级树不再被正文闸门拒绝（用户症状的直接缓解）。"""
        src = (BACKEND / "app" / "routers" / "sse_handlers.py").read_text(encoding="utf-8")
        assert "validate_section_rows(all_sections)" in src
        rules_src = (BACKEND / "app" / "services" / "outline_rules.py") \
            .read_text(encoding="utf-8")
        assert "目录结构不合规，请先修复目录" in rules_src
        # L3 叶结构叶子：validate 零违规 ⇒ 闸门放行（行为与 R162 一致）
        assert validate_outline_nodes(l3_leaf_tree()) == []

    def test_gate_rejects_bad_l1(self):
        """负控：L1 只有 2 个 L2 的树仍被闸门拒绝。"""
        rows = [
            {"id": "a", "parent_id": "", "level": 1, "title": "第一章",
             "outline_json": json.dumps({"id": "1"})},
            {"id": "b", "parent_id": "a", "level": 2, "title": "1.1",
             "outline_json": json.dumps({"id": "1.1"})},
            {"id": "c", "parent_id": "a", "level": 2, "title": "1.2",
             "outline_json": json.dumps({"id": "1.2"})},
        ]
        v = validate_section_rows(rows)
        assert v and any(x.code == outline_rules.V_TOO_FEW_CHILDREN
                         for x in v), "L1 数量不足必须仍判违规"

    def test_gate_order_preserved(self):
        """闸门先于叶子筛选（不合规树不写字数预算）——顺序锚。"""
        src = (BACKEND / "app" / "routers" / "sse_handlers.py").read_text(encoding="utf-8")
        assert src.index("validate_section_rows(all_sections)") < \
            src.index("select_target_leaves(")


# ============================================================
# C · 提示词文案（新标准明文 + 四级强制清零）
# ============================================================
class TestR162Prompts:
    def test_short_three_level(self):
        from app.services.ai.prompts._registry import render
        text = render("outline_short_system", scheme_name="s", scheme_type="t")
        assert "完整的三级目录树" in text
        assert "三级目录是最小叶子节点" in text
        assert "严禁生成第四级及更深层级" in text

    def test_sublevel_three_level(self):
        from app.services.ai.prompts._registry import render
        text = render("outline_sublevel_system", scheme_name="s", scheme_type="t",
                      construction_scope="c", requirements="r", standards_text="",
                      project_brief="b", project_facts="")
        assert "设计二级、三级目录" in text
        assert "3-5 个三级目录" in text
        assert "严禁生成第四级及更深层级" in text

    def test_four_level_mandate_extinct(self):
        """R162：4~6 个四级目录 / 四级最小叶子等 R151 强制措辞清零（防回退）。"""
        src = _OUTLINE_PROMPTS.read_text(encoding="utf-8")
        assert "4-6 个四级目录" not in src
        assert "4~6 个四级" not in src
        assert "四级目录是最小叶子节点" not in src

    def test_compliance_prompt_reflects_new_standard(self):
        src = _OUTLINE_PROMPTS.read_text(encoding="utf-8")
        compliance = src.split('"outline_compliance_system"')[1]
        assert "每个一级目录下必须有 3~5 个二级目录" in compliance
        assert "每个二级目录下必须有 3~5 个三级目录" in compliance
        assert "严禁出现第五级及更深" in compliance
        assert "4~6 个四级" not in compliance


# ============================================================
# D · 骨架（三级形态 + 自身合规不变量 + 不私藏 L4）
# ============================================================
class TestR162Framework:
    def test_skeleton_three_level(self):
        from app.services import outline_framework as of
        for idx in range(1, 8):
            fw = of.build_sublevel_framework(idx, f"第{idx}章")
            for j, l2 in enumerate(fw["outline"], start=1):
                assert l2["id"] == f"{idx}.{j}"
                for k, l3 in enumerate(l2["children"], start=1):
                    assert l3["id"] == f"{idx}.{j}.{k}"
                    assert l3["children"] == [], "三级叶子（R162）不得挂子级"

    def test_skeleton_compliant_invariant(self):
        from app.services import outline_framework as of
        from app.services.outline_rules import is_compliant_outline
        for idx in range(1, 13):
            fw = of.build_sublevel_framework(idx)
            ok, violations = is_compliant_outline(
                [{"title": f"第{idx}章", "description": "d",
                  "children": fw["outline"]}])
            assert ok, f"第{idx}章骨架不合规: {[v.code for v in violations]}"

    def test_no_private_l4_constraints(self):
        from app.services import outline_framework as of
        assert not hasattr(of, "L4_MIN") and not hasattr(of, "L4_MAX")


# ============================================================
# E · L3 叶子正文结构要求注入（提示词层 + 装配层行为锚）
# ============================================================
class TestR162StructInjection:
    def test_injection_block_for_leaf(self):
        from app.routers.sse_handlers import _l3_leaf_structure_requirement
        leaf3 = {"level": 3, "id": "1.1.1"}
        block = _l3_leaf_structure_requirement(leaf3)
        assert "4~6 个四级要点" in block
        assert "Markdown 二级标题" in block
        assert "Heading 4" in block
        # L4 兼容叶子/其他层级不注入
        for leaf in ({"level": 4, "id": "x"}, {"level": 2, "id": "y"},
                     {"level": None, "id": "z"}):
            assert _l3_leaf_structure_requirement(leaf) == ""

    def test_injection_wired_in_both_branches(self):
        """装配层（R169 D2 收口）：L3 结构要求恰一次进单一装配点，
        双分支同源消费（stable 经 _incs_stable_text、非 stable 经
        _incs_plain_text），不再双口径。"""
        src = (BACKEND / "app" / "routers" / "sse_handlers.py").read_text(encoding="utf-8")
        assert "_l3_leaf_struct = _l3_leaf_structure_requirement(leaf)" in src
        assert '_chapter_increments.append(("\\n", _l3_leaf_struct))' in src
        assert "_incs_stable_text" in src, "stable 分支消费点丢失"
        assert "_incs_plain_text" in src, "非 stable 分支消费点丢失"


# ============================================================
# F · 修复轮加固（超时进下一轮 + 截断上限）
# ============================================================
class TestR162ComplianceHardening:
    def test_fix_routine_structure_validate_failsoft(self):
        """fail-soft：不抛异常打断调用方（结构校验/复检不抛）。"""
        src = _COMPLIANCE.read_text(encoding="utf-8")
        assert "except asyncio.TimeoutError" in src
        assert "except Exception as e:  # noqa: BLE001" in src
        assert "fail-soft" in src

    def test_timeout_continues_to_second_attempt(self):
        """行为级：第 1 轮超时后进入第 2 轮（用尽 MAX 才终止）—— 修复轮不再一击即溃。"""
        import asyncio

        from app.services import outline_compliance as oc

        calls = []

        async def fake_collect(messages, checker, **kw):
            calls.append(1)
            raise asyncio.TimeoutError("slow provider")

        outline = l3_leaf_tree()
        fixed, info = asyncio.run(oc.fix_outline_compliance(
            outline, [], scheme_name="s", collect=fake_collect))
        assert len(calls) == 2, f"两轮均超时也应进第 2 轮（实测调用 {len(calls)} 次）"
        assert info["attempts"] == 2
        assert info["reason"] == "ai-timeout"
        assert fixed is None


# ============================================================
# G · 导出 Heading 4 行为锁（L3 节内「N）」要点以 Heading 4 呈现）
# ============================================================
class TestR162ExportHeading4:
    def test_three_level_tree_with_subpoints_exports_heading4(self, tmp_path):
        """行为锁：三级树、L3 节内容含「1）要点」→ 导出文档出现 Heading 4。"""
        import os
        import tempfile

        from app.routers.export import (
            _build_docx_sync,
            _load_heading_styles,
        )
        from app.services.content_blocks import _parse_content_blocks

        secs = [
            {"id": "s1", "parent_id": "", "sort_order": 1, "level": 1,
             "title": "第一章 工程概况", "content": ""},
            {"id": "s2", "parent_id": "s1", "sort_order": 2, "level": 2,
             "title": "1 项目基本信息", "content": ""},
            {"id": "s3", "parent_id": "s2", "sort_order": 3, "level": 3,
             "title": "1.1 工程概况", "content":
                 "## 工程概况概述\n## 建设范围与目标\n## 主要工程量概况"},
        ]
        cmap = {}
        for s in secs:
            if s["parent_id"]:
                cmap.setdefault(s["parent_id"], []).append(s)
        blocks = {s["id"]: _parse_content_blocks(s["content"]) for s in secs}
        out = os.path.join(tempfile.gettempdir(), "r162_heading4.docx")
        _build_docx_sync(
            out, {"id": "s1", "project_id": "p1", "name": "R162三级树导出"},
            secs, cmap, {}, {}, blocks,
            "宋体", 12, "", "", False, False, False,
            bidder_name="",
            heading_styles=_load_heading_styles({}),
            page_break_before_chapter=False,
            line_spacing=1.5,
            page_number_style="simple",
            toc_depth=4, margins=None, cover_info=None,
        )
        doc = __import__("docx").Document(out)
        heading_styles = {p.style.name for p in doc.paragraphs
                          if p.style.name.startswith("Heading")}
        nums = {int(s.split()[-1]) for s in heading_styles}
        assert 4 in nums, f"三级树内的「1）」要点须以 Heading 4 呈现，实测 {heading_styles}"


# ============================================================
# H · 前端 parity（规则镜像 + 页面接线 + 文案）
# ============================================================
class TestR162FrontParity:
    def test_frontend_rules_mirror(self):
        ts = (_FRONTEND / "utils" / "outlineRules.ts").read_text(encoding="utf-8")
        assert "export const OUTLINE_MAX_LEVEL = 4;" in ts
        assert "export const CHILDREN_MIN: Record<number, number> = { 1: 3, 2: 3 };" in ts
        assert "export const CHILDREN_MAX: Record<number, number> = { 1: 5, 2: 5 };" in ts

    def test_frontend_banner_standard_text(self):
        page = (_FRONTEND / "pages" / "SchemeWorkbenchPage.tsx").read_text(encoding="utf-8")
        assert "目录结构不合规（" in page, "横幅仍存在（违规时提示正文生成被拒）"
        # 标准文案与后端 rules 同源翻转后同步（R162）
        assert "三级为最小叶子" in page or "三级目录是最小叶子" in page

    def test_frontend_depth_cap_unchanged(self):
        """层级上限不动：MAX_OUTLINE_DEPTH=4（L4 兼容层）。"""
        constants = (_FRONTEND / "utils" / "outlineConstants.ts").read_text(encoding="utf-8")
        assert "export const MAX_OUTLINE_DEPTH = 4;" in constants
