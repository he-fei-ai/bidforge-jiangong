"""R212（2026-11-10）审核与预检模块 · 深度审计增强护栏（后端）

轮次背景
--------
用户指令「聚焦【审核与预检模块】，深入分析现有功能架构与实现现状，识别功能缺口和
技术债务，增强模块功能完整性，优化工作流程，补齐组件级交互测试，完善与其他
模块的数据传递、路由配置和调用链路，排查缺陷并修复所有 BUG」。

本文件锁住 R212 后端落地的 8 项修复，每条护栏以**行为实证**为主、源码棘轮为辅：

  FIX-A P1  查重族基号解析收敛到注册表单一事实源
            （``audit_scoring._base_rule_key`` 委托 ``audit_rules._resolve_base_rule``）——
            旧实现是第二份解析，``CON-SCAN-1`` 解析不出基号 ⇒ 与 ``CON-01``
            不同族 ⇒ 同一处数值冲突被扣两遍分、用户看到两条几乎相同的问题。
  FIX-B P2  评分项覆盖族 ``CMP-SC-1`` 补登记（基规则 ``SCP-01`` + 族前缀别名）——
            注册表「唯一事实源」此前在一个**真实产出点**上失守且无任何告警。
  FIX-C P2  完整性类 finding（「存在章节但正文为空」）必须携带真实 ``section_id``——
            否则前端定位跳转不可达、修复侧只能按标题反查（同名章节命中多章）。
  FIX-D P2  整章型定位的标题分支必须限一片（与 keywords 分支同口径）——
            否则一次「一键修复」把 AI 改写写进多个**无关**同名章节。
  FIX-E P2  一致性维度 finding 必须带 ``section_id``
            （扫描冲突取 occurrences 内既有 id；AI 审计按标题**唯一命中**反查）。
  FIX-F P3  低字数阈值单一事实源（导出侧复用 ``LOW_WORD_THRESHOLD``）。
  FIX-G P2  ``/submit`` 回传 ``review_state_stale``（加法式新键，**只提示不阻断**）。
  FIX-H P3  规则注册表新规则必须可解析 + 别名目标必须真实存在。

禁改原则：这些断言锁的是**意图**（同族只计一次 / 规则可反查 / 定位不扩散 /
阈值同源 / 过期可见但不阻断），不是实现细节；重构实现时请同步更新断言，
但不得删断言。
"""

from __future__ import annotations

import ast
import pathlib
import uuid
from datetime import datetime

import app.routers.compliance as cp
import app.services.audit_rules as ar
import app.services.audit_scoring as asc
import pytest
from app.models import SchemeReviewIn
from app.services import preflight_engine as pe
from app.services.review_autofix import capability_of, locate_targets

_BACKEND = pathlib.Path(__file__).resolve().parent.parent

# ===========================================================================
# FIX-A · 查重族基号解析收敛到注册表（唯一事实源）
# ===========================================================================


class TestDedupeFamilySingleSource:
    def test_con_scan_resolves_to_registered_base(self):
        """CON-SCAN-<n> 必须归一到基规则 CON-07（族前缀别名回落）。"""
        for rid in ("CON-SCAN-1", "CON-SCAN-7", "CON-SCAN"):
            base = ar._resolve_base_rule(rid)
            assert base is not None, f"{rid} 在注册表解析不到"
            assert base.rule_id == "CON-07", f"{rid} 归一到意外规则 {base.rule_id}"

    def test_base_rule_key_delegates_registry(self):
        """``_base_rule_key`` 必须与注册表**同结论**（不再各写一份解析）。"""
        assert asc._base_rule_key("CON-SCAN-1") == "CON-07"
        assert asc._base_rule_key("CON-01") == "CON-01"
        assert asc._base_rule_key("CON-05-2") == "CON-05"
        # 未登记规则退回本地解析（既有行为逐字不变）
        assert asc._base_rule_key("ZZZ-99") == "ZZZ-99"

    def test_same_conflict_dedupes_to_one(self):
        """同一处数值冲突：CON-01 与 CON-SCAN-1 的去重键必须相同。"""
        a = asc._dedup_key({"rule_id": "CON-01", "evidence": ["90天（施工进度计划）"]})
        b = asc._dedup_key({"rule_id": "CON-SCAN-1", "evidence": ["90天（施工进度计划）"]})
        assert a == b, (a, b)

    def test_merge_findings_keeps_single_entry(self):
        """合并后同族同目标只留一条（不得双扣分）。"""
        merged = asc.merge_findings(
            [{"rule_id": "CON-01", "evidence": ["90天（施工进度计划）"], "severity": "high"}],
            [{"rule_id": "CON-SCAN-1", "evidence": ["90天（施工进度计划）"], "severity": "high"}],
        )
        assert len(merged) == 1, merged

    def test_different_conflicts_stay_separate(self):
        """不同目标的冲突仍各自成条（族去重不得过度合并）。"""
        merged = asc.merge_findings(
            [{"rule_id": "CON-01", "evidence": ["90天（施工进度计划）"]}],
            [{"rule_id": "CON-SCAN-1", "evidence": ["C30（混凝土强度）"]}],
        )
        assert len(merged) == 2, merged


# ===========================================================================
# FIX-B · CMP-SC-1 规则登记（注册表唯一事实源）
# ===========================================================================


class TestScoringCoverageRuleRegistered:
    def test_base_rule_registered(self):
        assert ar.get_rule("SCP-01") is not None
        rule = ar.get_rule("SCP-01")
        assert rule.dimension == "completeness"
        assert rule.mode == ar.CHECK_MODE_PROGRAM
        assert rule.title and rule.basis

    def test_emitted_family_alias_resolves(self):
        """生成侧仍产出 CMP-SC-1，但必须能反查到已登记规则。"""
        base = ar._resolve_base_rule("CMP-SC-1")
        assert base is not None, "CMP-SC-1 在注册表反查不到"
        assert base.rule_id == "SCP-01"

    def test_generated_string_unchanged(self):
        """生成侧字符串逐字不变（历史行 / 前端契约零破坏）。"""
        src = (_BACKEND / "app" / "routers" / "compliance.py").read_text(encoding="utf-8")
        assert '"rule_id": "CMP-SC-1"' in src, "生成侧 rule_id 被改动（契约破坏）"

    def test_registry_still_healthy(self):
        assert ar.validate_rule_registry(emitted_rule_ids=ar._PROGRAM_EMITTED_RULE_IDS) == []

    def test_alias_targets_registered(self):
        for fam, target in ar._RULE_ID_ALIASES.items():
            assert target in ar.RULE_MAP, f"别名 {fam} 指向未注册规则 {target}"

    def test_autofix_reason_keeps_actionable_text(self):
        """登记后兜底文案按**解析后基规则前缀**取，SCP 必须有可执行指引，
        不得退化成「该问题暂不支持自动修复」这种无下一步的通用文案。"""
        cap = capability_of("CMP-SC-1")
        assert cap.mode == "manual"
        assert "正文生成" in cap.reason, cap.reason


# ===========================================================================
# FIX-C · 完整性 finding 携带 section_id
# ===========================================================================


def _ctx(sections: list[dict]) -> pe.PreflightContext:
    return pe.PreflightContext(
        scheme_id="x",
        scheme_name="x",
        scheme_type="x",
        word_budget=0,
        sections=sections,
        charts=[],
        facts=[],
    )


class TestCompletenessCarriesSectionId:
    SECTIONS = [
        {"id": "s4", "title": "施工计划", "content": "", "parent_id": "", "level": 1, "sort_order": 1},
        {"id": "s5", "title": "施工计划", "content": "", "parent_id": "", "level": 1, "sort_order": 2},
    ]

    def test_empty_body_finding_has_section_id(self):
        findings = pe.check_completeness(_ctx(self.SECTIONS))
        hit = [f for f in findings if f["rule_id"] == "CMP-03"]
        assert hit, findings
        assert hit[0].get("section_id") == "s4", hit[0]

    def test_missing_section_finding_has_no_section_id(self):
        """「未找到相关章节」本就没有目标章节 ⇒ section_id 必须为空。"""
        findings = pe.check_completeness(_ctx(self.SECTIONS))
        miss = [f for f in findings if f["rule_id"] == "CMP-02"]
        assert miss
        assert miss[0].get("section_id") == "", miss[0]


# ===========================================================================
# FIX-D · 整章型定位限片（同名章节不扩散）
# ===========================================================================


class TestLocateWholeSectionBounded:
    SECTIONS = [
        {"id": "s4", "title": "施工计划", "content": "正文"},
        {"id": "s5", "title": "施工计划", "content": "正文"},
    ]

    def test_same_title_locate_returns_at_most_one(self):
        targets = locate_targets({"rule_id": "CMP-03", "section_title": "施工计划", "section_id": ""}, self.SECTIONS)
        assert len(targets) <= 1, targets
        assert len(targets) == 1, targets

    def test_section_id_wins(self):
        targets = locate_targets({"rule_id": "CMP-03", "section_title": "施工计划", "section_id": "s5"}, self.SECTIONS)
        assert [t["section_id"] for t in targets] == ["s5"], targets


# ===========================================================================
# FIX-E · 一致性维度 finding 携带 section_id
# ===========================================================================


class TestConsistencySectionId:
    def test_title_lookup_unique_match(self):
        secs = [{"id": "a1", "title": "工程概况"}, {"id": "a2", "title": "施工准备"}]
        assert cp._section_id_by_title(secs, "工程概况") == "a1"

    def test_title_lookup_ambiguous_returns_empty(self):
        """同名多章时**不猜**（宁可空缺、不可错配）。"""
        secs = [{"id": "a1", "title": "施工准备"}, {"id": "a2", "title": "施工准备"}]
        assert cp._section_id_by_title(secs, "施工准备") == ""

    def test_title_lookup_blank(self):
        assert cp._section_id_by_title([{"id": "a1", "title": "x"}], "") == ""
        assert cp._section_id_by_title([], "工程概况") == ""

    def test_scan_occurrences_carry_section_id(self):
        """扫描冲突的 occurrences 里本来就有 section_id（数据在库里，此前不消费）。"""
        src = (_BACKEND / "app" / "routers" / "compliance.py").read_text(encoding="utf-8")
        assert '"section_id": _scan_sid' in src, "一致性扫描 finding 仍未透传 section_id"


# ===========================================================================
# FIX-F · 低字数阈值单一事实源
# ===========================================================================


class TestLowWordThresholdSingleSource:
    def test_export_uses_shared_constant(self):
        src = (_BACKEND / "app" / "routers" / "export.py").read_text(encoding="utf-8")
        assert "0 < wc < 100" not in src, "导出侧仍写死字面量 100（双实现漂移）"
        assert "LOW_WORD_THRESHOLD" in src

    def test_constant_value_unchanged(self):
        """当前取值与旧字面量一致（=100）⇒ 行为零变化。"""
        assert pe.LOW_WORD_THRESHOLD == 100


# ===========================================================================
# FIX-G · /submit 回传 review_state_stale（只提示不阻断）
# ===========================================================================


@pytest.fixture
async def r212_db(tmp_path):
    import app.db as _appdb
    from app.db import close_db, get_conn, init_db

    _appdb.DB_PATH = tmp_path / "r212-review.sqlite"
    await init_db()
    db = await get_conn()
    pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
    await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
    await db.execute(
        "INSERT INTO schemes(id,project_id,name,status,word_budget) VALUES(?,?,?,?,0)",
        (sid, pid, "方案", "目录已确认"),
    )
    await db.commit()
    yield db, sid, pid
    await close_db()


class TestSubmitReviewStateStale:
    async def _seed_run(self, db, sid: str, *, released=1, review_hash="") -> None:
        await db.execute(
            "INSERT INTO preflight_runs (id, scheme_id, total, blocked, released,"
            " content_fingerprint, review_state_hash, stats, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex,
                sid,
                95,
                0,
                released,
                "",
                review_hash,
                "{}",
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        await db.commit()

    async def test_response_contains_stale_flag(self, r212_db):
        """加法式新键必须恒存在（False = 未过期）。"""
        from app.routers.review import submit_scheme_review

        db, sid, _pid = r212_db
        await self._seed_run(db, sid)
        out = await submit_scheme_review(
            sid,
            SchemeReviewIn(to_status="approved", reviewer="张三", comment="ok"),
            db,
        )
        assert "review_state_stale" in out, out
        assert out["review_state_stale"] is False, out

    async def test_stale_flag_true_when_section_review_changed(self, r212_db):
        """章节审核状态变更后，最近一次总检的审核维度结论已过期 ⇒ 标记为 True
        （**不阻断**：强制提交语义不变）。"""
        from app.routers.review import submit_scheme_review
        from app.services.scheme_fingerprint import review_state_hash

        db, sid, _pid = r212_db
        await db.execute(
            "INSERT INTO sections (id, scheme_id, parent_id, title, description, level,"
            " status, word_count, word_budget, content, review_status, sort_order)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, sid, "", "章", "", 1, "generated", 0, 0, "正文", "approved", 0),
        )
        await db.commit()
        h0 = await review_state_hash(db, sid)
        await self._seed_run(db, sid, review_hash=h0)
        # 审核状态变更（章节审核状态由 approved 改为 rejected）
        await db.execute("UPDATE sections SET review_status='rejected' WHERE scheme_id=?", (sid,))
        await db.commit()

        out = await submit_scheme_review(
            sid,
            SchemeReviewIn(to_status="approved", reviewer="张三", comment="ok"),
            db,
        )
        assert out["review_state_stale"] is True, out


# ===========================================================================
# FIX-H · 注册表结构自检（防止再出现「产出点未登记」）
# ===========================================================================


class TestRuleRegistryStructure:
    def test_all_registered_rules_resolvable(self):
        for r in ar.ALL_RULES:
            assert ar.get_rule(r.rule_id) is not None

    def test_program_rules_are_resolvable_by_emitted_set(self):
        problems = ar.validate_rule_registry(emitted_rule_ids=ar._PROGRAM_EMITTED_RULE_IDS)
        assert problems == [], problems

    def test_no_duplicate_rule_id(self):
        ids = [r.rule_id for r in ar.ALL_RULES]
        assert len(ids) == len(set(ids)), "rule_id 重复注册"

    def test_rule_version_bumped(self):
        assert tuple(int(x) for x in ar.RULE_VERSION.split(".")[:2]) >= (1, 12)


# ===========================================================================
# 源码棘轮：/submit 必须「只标记、不阻断」
# ===========================================================================


def test_submit_does_not_raise_on_review_state_mismatch():
    """审核状态过期**不得**抛 422（会堵死文档化的强制提交逃生门）。"""
    src = (_BACKEND / "app" / "routers" / "review.py").read_text(encoding="utf-8")
    fn = next(
        n
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "submit_scheme_review"
    )
    assert "review_state_stale" in ast.dump(fn), "/submit 未回传 review_state_stale"
    # 过期判定分支**内部**不得出现 raise HTTPException
    seg = src.split("if _cur_rh and _cur_rh != _stored_rh:", 1)[1]
    block = seg.split("logger.warning(", 1)[0]
    assert "raise HTTPException" not in block, "审核状态过期被改成硬阻断"
    # 标记位赋值必须与「只记 WARNING」同段
    assert "review_state_stale = True" in seg
