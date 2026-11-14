# -*- coding: utf-8 -*-
"""R232 · 正文生成模块深度审计增强 —— OMP 阶段 3 正文旁路接缝护栏（2026-11-13）。

本轮定罪与修复（全部有行为探针证据，见 docs/正文生成模块深度审计增强_R232_20261113.md）：

- FIX-1/2（P2 · scan_documents 双误报）：
    ① 图/表混桶 —— ``图 3-1`` 与 ``表 3-1`` 同章同号（工程方案常态）被判
      「图号虚跳 图 3-1→3-1」假发现 ⇒ 按前导字（图|表）分桶；
    ② 编号跨父桶 —— 合法多章编号 ``1.1 → 1.2 → 2.1`` 被判
      「L2 编号跳变 2→1」假发现 ⇒ 桶键改为 ``(深度, 父链)``。
  两类假发现直接污染 ``export_contract.checks.figure_ref/numbering`` 与
  AUDIT_REPORT.md（ready 被误判 False、人工复核被误导）。
- FIX-3（P2 · 叶子原始 id 丢失）：``OmpContentAgent`` 曾把 ``safe_task_id``
  后的安全名同时当回包身份 —— 非 ASCII 叶子 id（如中文标题型 id）被替换成
  下划线后，``ContentDraft.section_id`` / ``documents`` 键 / ``missing_sections``
  比对（expected_ids 用叶子原始 id）三方对不上号 ⇒ 草稿无法落库应用。
- FIX-4（P3 · audit verify 索引键错配）：``OmpAuditAgent._verify`` 用安全名查
  原始 id 键的 ``docs`` ⇒ 非 ASCII id 时 verify 恒失败、任务恒 paused。
- FIX-0（并行 R231 回归代收）：``sse_checkpoint`` partial 写点裸 ``.rowcount``
  绕过 R13 ``safe_rowcount`` 单一出口 —— 由既有 ``test_no_bare_rowcount_
  outside_db_helper`` 护栏覆盖（本轮基线红转绿），此处不再重复锁。
"""

from __future__ import annotations

import json

from app.services.ai import omp_agent_roles as roles
from app.services.ai.omp_audit_agent import OmpAuditAgent
from app.services.ai.omp_content_agent import OmpContentAgent

_UNIT = "本工程采用分段施工工艺并严格控制质量验收。"


def _body_text(min_chars: int) -> str:
    return _UNIT * (min_chars // len(_UNIT) + 2)


# ===================== FIX-1：图/表同号不得误报虚跳 =====================
def test_fig_table_same_number_not_false_skipped():
    doc = "如图 3-1 所示布置\n\n见表 3-1，参数如下\n\n如图 3-2 所示收口"
    found = [f for f in roles.scan_documents({"s1": doc}) if f["type"] == "figure_ref"]
    assert found == [], f"图+表同章同号被误判虚跳：{found}"


def test_real_figure_and_table_skip_still_detected():
    doc = "图 3-1 布置图\n\n图 3-3 收口图\n\n表 2-1 清单\n\n表 2-4 汇总"
    found = [f for f in roles.scan_documents({"s1": doc}) if f["type"] == "figure_ref"]
    assert len(found) == 2, f"真跳变（图 3-1→3-3 / 表 2-1→2-4）漏报：{found}"
    assert any("图 3-1→3-3" in f["detail"] for f in found)
    assert any("表 2-1→2-4" in f["detail"] for f in found)


# ===================== FIX-2：跨父编号不得误报跳变 =====================
def test_cross_parent_numbering_not_false_skipped():
    doc = "# 1 工程概况\n## 1.1 部署\n## 1.2 工艺\n#### 2.1 细则\n"
    found = [f for f in roles.scan_documents({"s1": doc}) if f["type"] == "numbering"]
    assert found == [], f"跨父合法编号（1.1→1.2→2.1）被误判跳变：{found}"


def test_same_parent_numbering_skip_still_detected():
    doc = "# 1 工程概况\n# 3 验收\n## 1.1 部署\n## 1.4 工艺\n"
    found = [f for f in roles.scan_documents({"s1": doc}) if f["type"] == "numbering"]
    assert len(found) == 2, f"同父链真跳变漏报：{found}"
    details = "；".join(f["detail"] for f in found)
    assert "L1 编号跳变 1→3" in details
    assert "L2 编号跳变 1→4" in details


# ===================== FIX-3：草稿回包身份 = 叶子原始 id =====================
async def test_content_draft_keeps_raw_leaf_id():
    # ⚠️ 两叶必须异父：同父会被均分预算（1200/2=600），固定长度正文将触发
    #    「字数超限」→ paused，混淆本护栏要锁的「身份不被 safe_task_id 改写」语义。
    leaves = [
        {"id": "基础工程-1", "title": "基础工程", "parent_id": "p1"},
        {"id": "s2_hex_ok", "title": "主体结构", "parent_id": "p2"},
    ]

    async def exec_fn(spec, prompt, workspace):  # noqa: ANN001
        return _body_text(1000)

    result = await OmpContentAgent().generate_sections(
        leaves=leaves, facts="", tier="standard", executor_fn=exec_fn
    )
    by_sid = {d.section_id: d for d in result.drafts}
    # 非 ASCII id 不得被安全名（____-1）顶掉；ASCII id 行为不变。
    assert "基础工程-1" in by_sid, f"草稿身份被 safe_task_id 改写：{list(by_sid)}"
    assert "s2_hex_ok" in by_sid
    assert all(d.status == "completed" for d in result.drafts)


# ===================== FIX-4：audit verify 索引与回包契约对齐 =====================
async def test_audit_verify_matches_non_ascii_ids():
    docs = {"基础工程-1": "工期 400 天。"}

    async def exec_fn(spec, prompt, workspace):  # noqa: ANN001
        return json.dumps(
            {
                "fixes": [
                    {
                        "section_id": "基础工程-1",
                        "old_text": "400",
                        "new_text": "450",
                        "reason": "与全局事实冲突",
                    }
                ]
            },
            ensure_ascii=False,
        )

    result = await OmpAuditAgent().audit(
        documents=docs, facts="", executor_fn=exec_fn
    )
    assert result.report.totals["paused"] == 0, (
        f"非 ASCII id 章节的修复指令 verify 恒败误判 paused：{result.report.totals}"
    )
    assert "450" in result.revised_documents["基础工程-1"]


# ===================== 既有判据零回归（反向自证护栏非恒真） =====================
def test_placeholder_and_fence_findings_unchanged():
    doc = "# 1 概况\n```mermaid\ngraph TD;\nTODO 待补充"
    found = roles.scan_documents({"s1": doc})
    types = {f["type"] for f in found}
    assert {"fence", "placeholder"} <= types
