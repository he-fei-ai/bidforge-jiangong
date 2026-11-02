"""R151 引入 · R162（2026-11-01）翻转为「三级最小叶子」标准 · 护栏测试。

用户指令（2026-11-01 方案A）：目录层级只允许 L1~L4（L3 为最小叶子，
L4 仅作兼容层，严禁 L5）、数量 3~5/3~5（L3 下细分 L4 不作数量校验）、
编号子承父系；目录生成强校验 + AI 补齐/精简；正文生成前置闸门 +
字数四档作用于 L3；前端红点 + 导出仅识别 L1~L4。
（历史：R151 原为「四级叶子」标准，R162 按用户指令翻转。）

判据单一事实源 = ``app/services/outline_rules.py``；本文件锁：
  A. outline_rules 树形判据（行为）
  B. DB 行判据 + L5 合并清洗（行为）
  C. 落库钳位四级（normalize_outline / MAX_OUTLINE_DEPTH 单一事实源）
  D. 提示词 SystemPrompt 明文约束（渲染行为）
  E. 生成后合规强制 helper（行为：AI 补齐成功 / 仍失败 / 失败收尾）
  F. 正文前置闸门接线（源码锚 + 消息文案单一事实源）
  G. 字数四档作用于 L3（纯函数行为 + 接线锚）
  H. 标准化修复端点（行为：合规直通 / AI 修复 / 失败 502）
  I. Agent 面（Schema 数量 min/max 行为 + 提示词 + 完成前终检锚）
  J. 导出面（TOC 钳位行为 + 标题封顶源码锚 + 版本锚 + 前端 parity）
"""
import json
import uuid
from pathlib import Path

import pytest
from app.services.outline_rules import (
    CHILDREN_MAX,
    CHILDREN_MIN,
    OUTLINE_MAX_LEVEL,
    V_DEPTH_EXCEEDED,
    V_NUMBERING_MISMATCH,
    V_TOO_FEW_CHILDREN,
    V_TOO_MANY_CHILDREN,
    format_violations_message,
    merge_section_rows_deeper_than_leaf,
    validate_outline_nodes,
    validate_section_rows,
)
from app.services.outline_utils import MAX_OUTLINE_DEPTH, normalize_outline

_BACKEND = Path(__file__).resolve().parent.parent
_FRONTEND = _BACKEND.parent / "frontend" / "src"


def node(title, children=None, description="d"):
    return {"title": title, "description": description,
            "children": (children if children is not None else [])}


def l4s(n):
    return [node(f"四级{i}") for i in range(n)]


def compliant_tree(chapters=2):
    """合规最小树：每章 3 个二级 → 每个 3 个三级 → 每个 4 个四级。"""
    return [node(f"第{i}章", [
        node(f"{i}.{j}", [
            node(f"{i}.{j}.{k}", l4s(4)) for k in range(1, 4)
        ]) for j in range(1, 4)
    ]) for i in range(1, chapters + 1)]


# ============================================================
# A · outline_rules 树形判据
# ============================================================
class TestOutlineRulesTree:
    def test_compliant_tree_has_no_violations(self):
        assert validate_outline_nodes(compliant_tree()) == []

    def test_l1_count_is_not_constrained(self):
        """危大工程 10 章是常态：顶层一级数量不在硬性标准内。"""
        assert validate_outline_nodes(compliant_tree(chapters=10)) == []

    def test_too_few_children_attributed_to_parent(self):
        tree = compliant_tree()
        tree[0]["children"][0]["children"] = tree[0]["children"][0]["children"][:2]
        v = validate_outline_nodes(tree)
        assert len(v) == 1 and v[0].code == V_TOO_FEW_CHILDREN
        assert v[0].level == 2 and v[0].title == "1.1"
        assert v[0].expected == "3~5" and v[0].actual == "2"

    def test_too_many_children(self):
        tree = [node("第一章", [node(f"1.{j}", [
            node(f"1.{j}.{k}", l4s(4)) for k in range(1, 4)
        ]) for j in range(1, 7)])]
        v = validate_outline_nodes(tree)
        assert len(v) == 1 and v[0].code == V_TOO_MANY_CHILDREN
        assert v[0].level == 1 and v[0].actual == "6"

    def test_l3_leaf_is_legal(self):
        """R162 翻转：三级即叶子是合法最小叶子（L1/L2 数量合规即零违规）。"""
        legacy = [node("第一章", [
            node(f"1.{j}", [
                node(f"1.{j}.{k}") for k in range(1, 4)
            ]) for j in range(1, 4)
        ])]
        v = validate_outline_nodes(legacy)
        assert v == [], "三级叶子不再违规（叶子层级必须达到三级即满足）"

    def test_l2_leaf_still_violation(self):
        """R162 保留：只有两级（二级即叶子）仍违规（叶子层级未达三级）。"""
        two_level = [node("第一章", [node("1.1"), node("1.2"), node("1.3")])]
        v = validate_outline_nodes(two_level)
        assert v and all(x.code == V_TOO_FEW_CHILDREN for x in v)
        assert any("叶子层级必须达到三级" in x.detail for x in v)

    def test_l4_with_children_reported_once(self):
        tree = compliant_tree()
        l4 = tree[0]["children"][0]["children"][0]["children"][0]
        l4["children"] = [node("五级A", [node("六级B")]), node("五级C")]
        v = validate_outline_nodes(tree)
        depth_hits = [x for x in v if x.code == V_DEPTH_EXCEEDED]
        assert len(depth_hits) == 1, "深层后代由该条统一承载，不逐个上报"
        assert depth_hits[0].title == "四级0" and depth_hits[0].actual == "2"

    def test_message_contains_standard_head(self):
        msg = format_violations_message(validate_outline_nodes(
            [node("第一章", [node("1.1")])]))
        assert msg.startswith("目录结构不合规，请先修复目录")
        assert "第一章" in msg


# ============================================================
# B · DB 行判据 + L5 合并清洗
# ============================================================
class TestOutlineRulesRows:
    def _rows(self):
        return [
            {"id": "a", "parent_id": "", "level": 1, "title": "第一章",
             "outline_json": json.dumps({"id": "1"})},
            {"id": "b", "parent_id": "a", "level": 2, "title": "1.1",
             "outline_json": json.dumps({"id": "1.1"})},
            {"id": "c", "parent_id": "b", "level": 3, "title": "1.1.1",
             "outline_json": json.dumps({"id": "1.1.1"})},
            {"id": "d", "parent_id": "c", "level": 4, "title": "1.1.1.1",
             "outline_json": json.dumps({"id": "1.1.1.1"})},
        ]

    def test_legacy_three_level_rows_flagged(self):
        rows = self._rows()[:3]  # 三级树：L1/L2/L3 数量全不足
        v = validate_section_rows(rows)
        codes = {x.code for x in v}
        assert V_TOO_FEW_CHILDREN in codes
        assert all(x.level <= 3 for x in v if x.code == V_TOO_FEW_CHILDREN)

    def test_numbering_mismatch_detected(self):
        rows = self._rows()
        rows[3]["outline_json"] = json.dumps({"id": "1.1.1.9"})
        v = validate_section_rows(rows)
        mm = [x for x in v if x.code == V_NUMBERING_MISMATCH]
        assert len(mm) == 1 and mm[0].expected == "1.1.1.1" and mm[0].actual == "1.1.1.9"

    def test_l5_rows_flagged_depth_exceeded(self):
        rows = self._rows() + [
            {"id": "e", "parent_id": "d", "level": 5, "title": "1.1.1.1.1",
             "outline_json": json.dumps({"id": "1.1.1.1.1"})}]
        v = validate_section_rows(rows)
        assert any(x.code == V_DEPTH_EXCEEDED and x.level == 5 for x in v)

    def test_merge_l5_l6_into_l4(self):
        rows = [
            {"id": "p", "parent_id": "", "level": 4, "title": "四级父",
             "content": "父正文"},
            {"id": "s", "parent_id": "p", "level": 5, "title": "五级子",
             "content": "子正文"},
            {"id": "t", "parent_id": "s", "level": 6, "title": "六级孙",
             "content": ""},
        ]
        new_rows, stats = merge_section_rows_deeper_than_leaf(rows)
        assert stats["merged"] == 2 and len(new_rows) == 1
        content = new_rows[0]["content"]
        assert "**五级子**" in content and "子正文" in content
        assert "**六级孙**" in content, "多层深自底向上合并，标题不丢"
        assert "父正文" in content
        # 纯函数：输入行集合未被改动
        assert len(rows) == 3


# ============================================================
# C · 落库钳位四级
# ============================================================
class TestClampFourLevels:
    def test_single_source_constant(self):
        from app.services.outline_rules import OUTLINE_MAX_LEVEL
        assert MAX_OUTLINE_DEPTH == 4 == OUTLINE_MAX_LEVEL

    def test_normalize_keeps_four_and_merges_fifth(self):
        deep = [node("L1", [node("L2", [node("L3", [node("L4", [node("L5a"), node("L5b")])])])])]
        out = normalize_outline(deep)
        cur = out[0]
        for _ in range(3):
            assert cur["children"], "前四级必须保留"
            cur = cur["children"][0]
        assert cur["children"] == [], "第五级必须被裁剪"
        assert "L5a" in str(cur.get("description") or "")
        assert "L5b" in str(cur.get("description") or "")


# ============================================================
# D · 提示词 SystemPrompt 明文约束
# ============================================================
class TestPromptsCarryStandard:
    from app.services.ai.prompts._registry import render as _render

    def test_short_system_has_three_level_rules(self):
        from app.services.ai.prompts._registry import render
        text = render("outline_short_system", scheme_name="s", scheme_type="t")
        assert "完整的三级目录树" in text
        assert "3-5 个二级目录" in text
        assert "3-5 个三级目录" in text
        assert "三级目录是最小叶子节点" in text
        assert "严禁生成第四级及更深层级" in text

    def test_review_system_checks_counts(self):
        from app.services.ai.prompts._registry import render
        text = render("outline_review_system", scheme_name="s", scheme_type="t",
                      construction_scope="c", is_dangerous="否", project_facts="",
                      outline_json="[]")
        assert "最多三级" in text
        assert "二级不足 3 个或多于 5 个" in text
        assert "三级下四级不足 4 个或多于 6 个" not in text, \
            "L3→L4 数量不再约束（R162 转正文提示词）"

    def test_four_level_mandate_extinct_from_prompts(self):
        """R162 守卫：四级叶子强制标准（4~6 个四级/四级为最小叶子）必须在
        全部目录提示词中清零（防回退到 R151 契约）。"""
        from app.services.ai.prompts import outline as outline_prompts
        src = Path(outline_prompts.__file__).read_text(encoding="utf-8")
        assert "4-6 个四级目录" not in src
        assert "4~6 个四级" not in src
        assert "四级目录是最小叶子节点" not in src

    def test_sublevel_and_feedback_and_patch_carry_counts(self):
        from app.services.ai.prompts import outline as outline_prompts
        src = Path(outline_prompts.__file__).read_text(encoding="utf-8")
        for key in ("outline_sublevel_system", "outline_sublevel_batch_system",
                    "outline_feedback_system", "outline_patch_system",
                    "outline_adjust_system", "outline_compliance_system"):
            assert f'_reg("{key}"' in src, f"{key} 必须存在"
        assert src.count("3-5 个三级") + src.count("3~5 个三级") >= 3, "子目录/补齐/合规模板必须保留 L2→L3 数量约束"
        assert src.count("严禁生成第四级") >= 3, "生成类模板必须明示三级为最小叶子"


# ============================================================
# E · 生成后合规强制 helper
# ============================================================
class TestEnforceCompliance:
    async def test_compliant_tree_skips_ai(self, monkeypatch):
        import app.routers.sse_handlers as sh

        called = {"n": 0}

        async def _boom(*a, **kw):
            called["n"] += 1
            raise AssertionError("合规目录不得触发 AI 修复")

        monkeypatch.setattr(sh, "fix_outline_compliance", _boom)
        outline, violations = await sh._enforce_outline_compliance(
            compliant_tree(), task_id="t1")
        assert violations == [] and called["n"] == 0
        assert outline[0]["title"] == "第1章"

    async def test_ai_fix_success_normalizes_and_rechecks(self, monkeypatch):
        import app.routers.sse_handlers as sh

        async def _fix(outline, violations, **kw):
            return compliant_tree(), {"attempts": 1, "reason": "ok", "ok": True}

        monkeypatch.setattr(sh, "fix_outline_compliance", _fix)
        bad = [node("第一章", [node("1.1")])]
        outline, violations = await sh._enforce_outline_compliance(bad, task_id="t2")
        assert violations == []
        # 修复结果与生成产物同源规范化：编号已重排
        assert outline[0]["children"][0]["id"] == "1.1"

    async def test_ai_fix_still_invalid_returns_violations(self, monkeypatch):
        import app.routers.sse_handlers as sh

        async def _fix(outline, violations, **kw):
            return [node("第一章", [node("1.1"), node("1.2")])], {
                "attempts": 2, "reason": "ai-still-invalid", "ok": False}

        monkeypatch.setattr(sh, "fix_outline_compliance", _fix)
        outline, violations = await sh._enforce_outline_compliance(
            [node("第一章", [node("1.1")])], task_id="t3")
        assert violations, "修复后仍不合规必须返回违规清单"
        assert outline[0]["title"] == "第一章"

    async def test_failure_finalize_writes_checkpoint_and_fails_task(self, monkeypatch):
        import app.routers.sse_handlers as sh

        ckpts, finished = [], []

        async def _ckpt(tid, payload):
            ckpts.append((tid, payload))

        async def _finish(tid, status, message=None):
            finished.append((tid, status, message))

        monkeypatch.setattr(sh, "_save_outline_checkpoint", _ckpt)
        monkeypatch.setattr(sh, "finish_task", _finish)
        holder = {"outline": ["dirty"]}
        violations = validate_outline_nodes([node("第一章", [node("1.1")])])
        evt = await sh._finish_outline_compliance_failure(
            "t4", compliant_tree(), violations, partial_holder=holder)
        assert evt["event"] == "error" and evt["partial"] is True
        assert evt["violations"]
        assert evt["message"].startswith("目录结构不合规，请先修复目录")
        assert finished and finished[0][1] == "failed"
        assert holder["outline"] == [], "失败收尾后断线兜底缓存必须清空"


# ============================================================
# F · 正文前置闸门接线
# ============================================================
class TestContentGateWiring:
    def _src(self, *parts):
        return (_BACKEND.joinpath(*parts)).read_text(encoding="utf-8")

    def test_generate_content_has_gate(self):
        src = self._src("app", "routers", "sse_handlers.py")
        # 锚必须断**完整赋值形态**（R116 教训③）：只断函数名会被三元短路/换名绕过
        assert "_gate_violations = validate_section_rows(all_sections)" in src, \
            "正文生成必须对整树做合规校验（R151 三.2）"
        assert "violations_to_payload(_gate_violations)" in src
        # 闸门必须先于叶子筛选（不合规树不写字数预算）
        assert src.index("validate_section_rows(all_sections)") < \
            src.index("leaves = select_target_leaves(")

    def test_gate_message_single_source(self):
        """标准文案「目录结构不合规，请先修复目录」只允许出现在 outline_rules.py
        （含其 docstring），任何路由/服务不得复制第二份（防口径漂移）。"""
        hits = []
        for path in (_BACKEND / "app").rglob("*.py"):
            if "目录结构不合规，请先修复目录" in path.read_text(encoding="utf-8"):
                hits.append(path.name)
        assert hits == ["outline_rules.py"], f"文案出现面越界：{hits}"


# ============================================================
# G · 字数四档作用于 L3
# ============================================================
class TestTierOnL3:
    def test_apply_tier_stamps_l3_and_leaves(self):
        from app.services.content_utils import apply_word_budget_tier_to_l3
        sections = [
            {"id": "l1", "level": 1, "word_budget": 1200},
            {"id": "l2", "level": 2, "word_budget": 1200},
            {"id": "l3a", "level": 3, "word_budget": 1200},
            {"id": "l3b", "level": 3, "word_budget": 1200},
            {"id": "l4a", "level": 4, "word_budget": 1200},
            {"id": "l4b", "level": 4, "word_budget": 1200},
        ]
        leaves = [s for s in sections if s["level"] == 4]
        targets = apply_word_budget_tier_to_l3(sections, leaves, 2000)
        assert set(targets) == {"l3a", "l3b", "l4a", "l4b"}
        by_id = {s["id"]: s for s in sections}
        assert by_id["l3a"]["word_budget"] == 2000
        assert by_id["l3b"]["word_budget"] == 2000
        assert by_id["l4a"]["word_budget"] == 2000
        assert by_id["l2"]["word_budget"] == 1200, "一级/二级不写字数档位"
        assert by_id["l1"]["word_budget"] == 1200

    def test_generation_branch_uses_helper(self):
        src = (_BACKEND / "app" / "routers" / "sse_handlers.py").read_text(encoding="utf-8")
        assert "apply_word_budget_tier_to_l3(" in src
        assert "UPDATE sections SET word_budget=? WHERE id=?" in src


# ============================================================
# H · 标准化修复端点
# ============================================================
@pytest.mark.asyncio
class TestStandardizeEndpoint:
    async def _mk_scheme(self, db):
        pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
        await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
        await db.execute(
            "INSERT INTO schemes(id, project_id, name) VALUES(?,?,?)",
            (sid, pid, "旧三级方案"))
        rows = [
            ("a", "", 1, "第一章 工程概况", "1"),
            ("b", "a", 2, "1.1 编制依据", "1.1"),
            ("c", "b", 3, "1.1.1 规范清单", "1.1.1"),
        ]
        for _id, _parent, _lv, _title, _oid in rows:
            await db.execute(
                "INSERT INTO sections(id, scheme_id, project_id, parent_id,"
                " title, level, sort_order, outline_json)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (_id, sid, pid, _parent or None, _title, _lv, 1,
                 json.dumps({"id": _oid, "level": _lv, "confidence": 1.0})))
        await db.commit()
        return sid

    async def test_compliant_tree_short_circuits(self, db_conn, monkeypatch):
        from app.routers import sections as sec_router
        sid = await self._mk_scheme(db_conn)
        # 覆盖为合规树：直接把行改多（简化：塞满 3-5/3-5/4-6 太重，
        # 改用 monkeypatch validate_section_rows 返回空）
        monkeypatch.setattr(sec_router, "validate_section_rows", lambda rows: [])
        res = await sec_router.standardize_outline(sid, db=db_conn)
        assert res["compliant"] is True and res["outline"] is None

    async def test_ai_fix_returns_confirmable_tree(self, db_conn, monkeypatch):
        from app.routers import sections as sec_router
        sid = await self._mk_scheme(db_conn)

        async def _fix(outline, violations, **kw):
            assert violations, "必须把违规清单交给 AI"
            return compliant_tree(), {"attempts": 1, "reason": "ok", "ok": True}

        monkeypatch.setattr(sec_router, "fix_outline_compliance", _fix)
        res = await sec_router.standardize_outline(sid, db=db_conn)
        assert res["ok"] is True and res["compliant"] is True
        assert res["violations"], "响应必须回带修复前违规清单（DB 行口径）"
        assert res["violations_after"] == []
        assert res["outline"][0]["children"], "返回四级合规树"
        # 不落库：DB 行集合不变
        cur = await db_conn.execute(
            "SELECT COUNT(*) AS n FROM sections WHERE scheme_id=?", (sid,))
        assert (await cur.fetchone())["n"] == 3

    async def test_ai_failure_raises_502(self, db_conn, monkeypatch):
        from app.routers import sections as sec_router
        from fastapi import HTTPException
        sid = await self._mk_scheme(db_conn)

        async def _fix(outline, violations, **kw):
            return None, {"attempts": 2, "reason": "ai-timeout", "ok": False}

        monkeypatch.setattr(sec_router, "fix_outline_compliance", _fix)
        with pytest.raises(HTTPException) as ei:
            await sec_router.standardize_outline(sid, db=db_conn)
        assert ei.value.status_code == 502
        assert "ai-timeout" in str(ei.value.detail)


# ============================================================
# I · Agent 面
# ============================================================
class TestAgentStandard:
    def _agent_tree(self):
        """Agent 契约形态的合规树：id=None、一级带 attr、三级叶子带 content_mode。"""
        def al3(i):
            return {"id": None, "title": f"细目{i}", "description": "d",
                    "content_mode": "ai-generate"}
        return [{
            "id": None, "number": "", "title": "第一章", "description": "d",
            "attr": "技术",
            "children": [{
                "id": None, "number": "", "title": f"二级{j}", "description": "d",
                "children": [al3(m) for m in range(4)],
            } for j in range(3)],
        }]

    def test_schema_enforces_counts_and_depth(self):
        from app.services.agent.outline_schemas import ROOT_NODE_SCHEMA
        from jsonschema import Draft7Validator

        def validate(inst):
            errors = sorted(Draft7Validator(ROOT_NODE_SCHEMA).iter_errors(inst),
                            key=lambda e: list(e.path))
            assert not errors, [e.message for e in errors[:3]]

        # 合规树（3/4，三级为叶子）通过（ROOT_NODE_SCHEMA 是单节点 Schema，校验根对象）
        validate(self._agent_tree()[0])
        # 一级下只有 2 个二级 → Schema 拒绝（minItems 3）
        bad = self._agent_tree()
        bad[0]["children"] = bad[0]["children"][:2]
        with pytest.raises(Exception):
            validate(bad[0])
        # 三级下挂四级子节点 → Schema 拒绝（R162：三级为最小叶子，仅 leaf_schema）
        bad2 = self._agent_tree()
        bad2[0]["children"][0]["children"][0]["children"] = [
            {"id": None, "title": "四级", "description": "d",
             "content_mode": "ai-generate"}]
        with pytest.raises(Exception):
            validate(bad2[0])

    def test_stage_prompts_no_six_level(self):
        from app.services.agent import stage_prompts
        src = Path(stage_prompts.__file__).read_text(encoding="utf-8")
        assert "最多六级" not in src, "旧六级契约必须清零"
        assert "最多三级" in src
        assert "4~6 个四级" not in src

    def test_completion_gate_wired(self):
        from app.services.agent import outline_agent_flow
        src = Path(outline_agent_flow.__file__).read_text(encoding="utf-8")
        assert "validate_outline_nodes(nodes)" in src
        assert "OutlineAgentReportedFailure" in src


# ============================================================
# J · 导出面
# ============================================================
class TestExportStandard:
    def test_toc_field_clamped_to_four(self):
        from app.routers.export import _add_toc_field
        from docx import Document

        doc = Document()
        p = doc.add_paragraph()
        _add_toc_field(p, 9)
        # 域指令在 w:instrText 里（R127 五-run 标准写法），run.text 不含它
        instr = "".join(
            (t.text or "") for t in p._p.xpath('.//w:instrText'))
        assert 'TOC \\o "1-4"' in instr, "TOC 域深度必须封顶 4"
        assert "1-9" not in instr

    def test_options_clamp_toc_depth(self):
        from app.routers.export import _build_docx_options
        cfg = {"toc_depth": 9}
        opts = _build_docx_options(
            cfg,
            lambda k, d: float(cfg.get(k, d)),
            lambda k, d: int(cfg.get(k, d)))
        assert opts["toc_depth"] == 4
        empty = {}
        assert _build_docx_options(
            empty,
            lambda k, d: float(empty.get(k, d)),
            lambda k, d: int(empty.get(k, d)))["toc_depth"] == 3

    def test_heading_cap_wired_in_source(self):
        src = (_BACKEND / "app" / "routers" / "export.py").read_text(encoding="utf-8")
        assert "if level <= 4:" in src, "DB 章节 L5+ 必须降级为非标题渲染"
        assert "_want_lv" in src, "正文子标题超 4 级必须降级加粗正文"
        assert 'min(_int("toc_depth", 3), 4)' in src
        assert '_EXPORTER_VERSION = "33"' in src

    def test_docx_renders_only_heading_1_to_4(self, tmp_path):
        """行为锁：L1→L5 五级章节链导出后，文档只出现 Heading 1~4 样式；
        L5 标题降级为加粗正文段落（文本保留）。"""
        import os
        import tempfile

        from app.routers.export import (
            _build_docx_sync,
            _load_heading_styles,
        )
        from app.services.content_blocks import _parse_content_blocks
        from docx import Document

        secs = []
        cmap = {}
        prev = ""
        for lv in range(1, 6):
            sid = f"s{lv}"
            secs.append({"id": sid, "parent_id": prev, "sort_order": lv,
                         "level": lv, "title": f"第{lv}级标题", "content": ""})
            if prev:
                cmap.setdefault(prev, []).append(secs[-1])
            prev = sid
        blocks = {s["id"]: _parse_content_blocks(s["content"]) for s in secs}
        out = os.path.join(tempfile.gettempdir(), "r151_cap.docx")
        _build_docx_sync(
            out, {"id": "s1", "project_id": "p1", "name": "R151封顶测试"},
            secs[:1], cmap, {}, {}, blocks,
            "宋体", 12, "", "", False, False, False,
            bidder_name="",
            heading_styles=_load_heading_styles({}),
            page_break_before_chapter=False,
            line_spacing=1.5,
            page_number_style="simple",
            toc_depth=4, margins=None, cover_info=None,
        )
        doc = Document(out)
        heading_styles = {p.style.name for p in doc.paragraphs
                          if p.style.name.startswith("Heading")}
        nums = {int(s.split()[-1]) for s in heading_styles}
        assert nums <= {1, 2, 3, 4}, f"文档大纲只允许 1~4 级，实测 {heading_styles}"
        # L5 标题文本仍在（降级为加粗正文段落），不丢数据
        texts = [p.text for p in doc.paragraphs]
        assert any("第5级标题" in t for t in texts)
        l5_para = next(p for p in doc.paragraphs if "第5级标题" in p.text)
        assert not l5_para.style.name.startswith("Heading")
        assert any(r.bold for r in l5_para.runs), "降级标题须以加粗呈现"

    def test_frontend_parity_mirror(self):
        """前端 outlineRules.ts 数值必须与后端逐字一致（双向锚的后端侧）。
        R162 翻转后为三级叶子标准：L3 不作叶子数量校验（L3 下不挂子节点即合法）。"""
        ts = (_FRONTEND / "utils" / "outlineRules.ts").read_text(encoding="utf-8")
        assert "export const OUTLINE_MAX_LEVEL = 4;" in ts
        assert "export const CHILDREN_MIN: Record<number, number> = { 1: 3, 2: 3 };" in ts
        assert "export const CHILDREN_MAX: Record<number, number> = { 1: 5, 2: 5 };" in ts

    def test_frontend_page_wiring(self):
        """页面接线源码锚（原在前端测试，因 tsc 环境无 node:fs 移至后端侧）：
        两棵树真实消费 outlineRules（红点）+ 渲染层 L4 截断 + 一键标准化入口。"""
        page = (_FRONTEND / "pages" / "SchemeWorkbenchPage.tsx").read_text(encoding="utf-8")
        assert 'from "../utils/outlineRules"' in page
        assert "outlineViolationsByKey(tree)" in page
        # 渲染层 L4 截断（编辑树与左树各一处 filter）
        assert len(__import__("re").findall(
            r"c\.level <= OUTLINE_MAX_LEVEL", page)) >= 2
        assert "standardizeOutline" in page and "handleStandardizeOutline" in page
        constants = (_FRONTEND / "utils" / "outlineConstants.ts").read_text(encoding="utf-8")
        assert "export const MAX_OUTLINE_DEPTH = 4;" in constants

    def test_node_budget_caps_cover_compliant_trees(self):
        """节点上限默认值必须容纳合规树理论上限（10×5×5×6=1500）。"""
        import app.routers.sse_handlers as sh
        assert sh.OUTLINE_GENERATE_MAX_NODES >= 1500
        assert sh.OUTLINE_FIX_MAX_NODES >= 1500
