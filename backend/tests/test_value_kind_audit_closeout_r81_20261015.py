"""全局事实「要求 vs 事实口径」value_kind · 审计/编辑链路收口 · R81（2026-10-15）。

背景：R80（2026-10-15）把 value_kind（''=未区分 / fact=本方案口径 /
requirement=招标·规范要求原文口径）接进了提取、落库、正文/目录注入渲染
与前端徽标，但漏了四条**消费侧**链路，导致「把要求当事实」的混淆沿审计
与编辑通道继续存在：

  B1 跨模块只读桥接 ``load_resolved_facts_for_scope``（预检/审核/合规/
     一致性修复的共同入口）不回传 value_kind；
  B2 ``compliance._facts_prompt_text``（/check、/expert-review 的
     「项目关键事实」装配）不标口径前缀 —— AI 以事实为唯一权威值判冲突，
     requirement 行的要求数字被当本方案既定值 → 误报 + 错误建议；
  B3 ``facts_classification.extract_danger_params`` 把要求阈值（「开挖
     深度 5m 及以上需论证」里的 5m）当项目实际深度 → SAF-08 误报；
  B4 手工单条编辑（_apply_item_updates）无法改口径；AI 调整的
     ``_validate_adjust_ops`` 白名单把 value_kind 剥掉 —— R80 宣称的
     「AI 调整新增透传」实际是死接线（恒为 ''）。

本护栏锁定修复后的契约：
  A 桥接如实回传 value_kind（静态列清单 + 真实 DB 行为）；
  B 合规提示词 requirement 前缀渲染 + fact/''/缺键逐字不变性 +
     单一事实源（不得自建第二份前缀字面量）；
  C 危大参数排除 requirement 行（单测 + SAF-08 端到端 + extra_text 不丢）；
  D 条目级更新/AI 调整校验器的 value_kind 透传与归一；
  E AI 调整提示词声明 value_kind 操作契约；
  F 前后端 parity：前端 FACT_VALUE_KIND_OPTIONS 值域与编辑接线。

运行：python -m pytest tests/test_value_kind_audit_closeout_r81_20261015.py -q
"""
from __future__ import annotations

import ast
import os
import re
import uuid

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_APP = os.path.join(_REPO, "backend", "app")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _src(rel: str) -> str:
    return _read(os.path.join(_APP, rel))


# ---------------------------------------------------------------------------
# A 组 · 桥接（load_resolved_facts_for_scope）回传 value_kind
# ---------------------------------------------------------------------------
class TestBridgeCarriesValueKind:
    def test_bridge_columns_include_value_kind(self):
        src = _src("services/facts_extractor.py")
        m = re.search(r"_BRIDGE_COLUMNS = \((.*?)\n\)", src, re.S)
        assert m, "_BRIDGE_COLUMNS 定义未找到"
        # 先剥注释行再拆列（注释与末行字面量之间无逗号，直接 split 会把
        # 注释尾部与 "value_kind" 粘成一个元素 → 假阴性）
        body = "\n".join(ln for ln in m.group(1).splitlines()
                         if not ln.strip().startswith("#"))
        blob = body.replace('"', "").replace("'", "")
        cols = [c.strip() for c in blob.split(",") if c.strip()]
        assert "value_kind" in cols, (
            "桥接列清单必须含 value_kind（审核/预检/合规/修复四链路的共同入口）")

    def test_bridge_return_dict_has_value_kind(self):
        """返回体按键名取 value_kind（锚调用形态，防只加 SELECT 不加返回）。"""
        src = _src("services/facts_extractor.py")
        assert '"value_kind": r.get("value_kind") or ""' in src, (
            "桥接返回 dict 必须回传 value_kind（缺列旧库 fail-open 为 ''）")

    @pytest.mark.asyncio
    async def test_bridge_returns_kinds_from_real_db(self, tmp_path):
        import app.db as appdb
        appdb.DB_PATH = tmp_path / "r81bridge.sqlite"
        await appdb.init_db()
        db = await appdb.get_conn()
        pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
        await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
        await db.execute(
            "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
        rows = [
            ("建设工期", "- **建设工期**: 不超过450日历天", "requirement"),
            ("项目经理", "- **项目经理**: 张伟", "fact"),
            ("塔吊型号", "- **塔吊型号**: QTZ80", ""),
        ]
        for nm, content, vk in rows:
            await db.execute(
                "INSERT INTO global_facts(id,project_id,scheme_id,group_id,"
                "group_title,title,content,category,is_resolved,value_kind) "
                "VALUES(?,?,?,?,?,?,?,?,1,?)",
                (uuid.uuid4().hex, pid, sid, "g1", "工期", nm, content,
                 "schedule", vk))
        await db.commit()
        try:
            from app.services.facts_extractor import load_resolved_facts_for_scope
            out = await load_resolved_facts_for_scope(db, scheme_id=sid)
            by_name = {o["name"]: o for o in out}
            assert set(by_name) == {"建设工期", "项目经理", "塔吊型号"}
            assert by_name["建设工期"]["value_kind"] == "requirement"
            assert by_name["项目经理"]["value_kind"] == "fact"
            assert by_name["塔吊型号"]["value_kind"] == ""
        finally:
            await appdb.close_db()

    def test_bridge_docstring_declares_value_kind(self):
        src = _src("services/facts_extractor.py")
        seg = src[src.index("async def load_resolved_facts_for_scope"):]
        head = seg[:seg.index('"""', seg.index('"""') + 3) + 3]
        assert "value_kind" in head, "桥接 docstring 的返回字段清单必须声明 value_kind"


# ---------------------------------------------------------------------------
# B 组 · 合规提示词渲染（/check、/expert-review）
# ---------------------------------------------------------------------------
class TestComplianceFactsPromptText:
    def _rows(self):
        return [
            {"name": "监测报警值", "value": "30mm", "value_kind": "requirement"},
            {"name": "开挖深度", "value": "6.5", "value_unit": "m",
             "is_safety_critical": True, "value_kind": "fact"},
            {"name": "legacy", "value": "x"},
            {"name": "blank", "value": "y", "value_kind": ""},
        ]

    def test_requirement_row_gets_prefix(self):
        from app.routers.compliance import _facts_prompt_text
        from app.services.facts_builder import _REQUIREMENT_KIND_PREFIX
        text = _facts_prompt_text(self._rows())
        line = next(ln for ln in text.splitlines() if "监测报警值" in ln)
        assert _REQUIREMENT_KIND_PREFIX in line, "requirement 行必须带口径前缀"

    def test_fact_empty_missing_rows_verbatim_unchanged(self):
        """行为不变性锚：fact/''/缺键三形态与「无 value_kind 键」逐字一致。

        ⚠️ requirement 行**不在**本 parity 内 —— 它正是本功能要改变的行
        （由 test_requirement_row_gets_prefix 单独锁定）。把三形态与
        requirement 混在一起比全文，会因「唯一想变的那行变了」而恒失败。
        """
        from app.routers.compliance import _facts_prompt_text
        rows = [r for r in self._rows() if r.get("value_kind") != "requirement"]
        assert rows, "夹具必须仍含 fact/''/缺键三形态"
        stripped = [{k: v for k, v in r.items() if k != "value_kind"}
                    for r in rows]
        assert _facts_prompt_text(rows) == _facts_prompt_text(stripped), (
            "fact/''/缺键行不得因本功能改变一个字符")

    def test_prefix_sits_before_safety_mark(self):
        """顺序契约：口径前缀在单位之后、安全关键标记之前（标记仍是
        最后一个可抢救后缀——_keep_suffixes 只对它生效）。"""
        from app.routers.compliance import _facts_prompt_text, SAFETY_MARK
        rows = [{"name": "双重标记", "value": "1",
                 "is_safety_critical": True, "value_kind": "requirement"}]
        line = next(ln for ln in _facts_prompt_text(rows).splitlines()
                    if "双重标记" in ln)
        assert line.index("（要求口径") < line.index(SAFETY_MARK)

    def test_single_source_prefix_no_second_literal(self):
        """静态锁：compliance 不得自建第二份前缀字面量（只 import 单一事实源）。"""
        src = _src("routers/compliance.py")
        tree = ast.parse(src)
        literals: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.append(node.value)
        assert not any("要求口径" in s and "满足/响应招标要求" in s
                       for s in literals), (
            "compliance 不得自建口径前缀副本——必须 import facts_builder 单一常量")
        assert "_REQUIREMENT_KIND_PREFIX" in src, (
            "compliance 必须 import 单一事实源常量")

    def test_import_is_from_facts_builder(self):
        src = _src("routers/compliance.py")
        assert re.search(
            r"from app\.services\.facts_builder import _REQUIREMENT_KIND_PREFIX",
            src), "前缀常量必须来自 facts_builder（与正文/目录注入渲染同源）"


# ---------------------------------------------------------------------------
# C 组 · 危大参数排除 requirement 行（SAF-08 误报修复）
# ---------------------------------------------------------------------------
class TestDangerParamsSkipRequirement:
    def test_unit_three_forms(self):
        from app.services.facts_classification import extract_danger_params
        assert extract_danger_params(
            [{"name": "基坑开挖深度", "value": "5m 及以上需组织专家论证",
              "value_kind": "requirement"}]) == {}, (
            "要求行的阈值数字不得当项目参数提取")
        assert extract_danger_params(
            [{"name": "基坑开挖深度", "value": "6.5m",
              "value_kind": "fact"}]) == {"depth": 6.5}
        assert extract_danger_params(
            [{"name": "基坑开挖深度", "value": "7m"}]) == {"depth": 7.0}, (
            "缺 value_kind 字段的历史行/外部调用方按 '' 处理（fail-open）")

    def test_object_rows_supported(self):
        """FactItem 对象行同样尊重 value_kind（R80 起对象行带该属性）。"""
        from app.services.facts_classification import extract_danger_params
        from app.services.facts_extractor import FactItem
        req = FactItem(name="基坑开挖深度", value="5m", value_kind="requirement")
        fact = FactItem(name="基坑开挖深度", value="9m", value_kind="fact")
        assert extract_danger_params([req]) == {}
        assert extract_danger_params([fact]) == {"depth": 9.0}

    @staticmethod
    def _ctx(facts, name="基础工程施工方案"):
        from app.services.preflight_engine import PreflightContext
        return PreflightContext(
            scheme_id="s1", scheme_name=name, scheme_type="",
            word_budget=0, sections=[], charts=[], facts=facts)

    def test_saf08_not_fired_by_requirement_threshold(self):
        """端到端：要求阈值行（5m 是部文阈值不是项目深度）不得触发 SAF-08。"""
        from app.services.preflight_engine import check_hazard_params
        ctx = self._ctx([{
            "name": "危大工程判定标准",
            "value": "开挖深度 5m 及以上需组织专家论证",
            "value_kind": "requirement",
        }])
        assert check_hazard_params(ctx) == []

    def test_saf08_still_fired_by_real_fact(self):
        """反向锁：真实事实参数（depth=6.5m）照旧触发 SAF-08（不放松安全判定）。"""
        from app.services.preflight_engine import check_hazard_params
        ctx = self._ctx([{
            "name": "基坑开挖深度", "value": "6.5m", "value_kind": "fact",
        }])
        findings = check_hazard_params(ctx)
        assert findings and findings[0]["rule_id"] == "SAF-08"

    def test_extra_text_keeps_requirement_rows(self):
        """分类通道不丢：要求行仍进 extra_text（子类识别可用），只排除参数通道。"""
        from app.services.preflight_engine import check_hazard_params
        ctx = self._ctx([
            {"name": "基坑开挖深度", "value": "6.5m", "value_kind": "fact"},
            {"name": "危大工程判定标准", "value": "开挖深度 5m 及以上需论证",
             "value_kind": "requirement"},
        ])
        findings = check_hazard_params(ctx)
        assert findings, "事实行参数必须仍能触发（要求行不影响它）"
        ev = " ".join(findings[0].get("evidence") or [])
        assert "6.5" in ev and "5m" not in ev, (
            "证据只能来自事实行参数，不得混入要求行阈值")


# ---------------------------------------------------------------------------
# D 组 · 条目级更新 + AI 调整校验器透传
# ---------------------------------------------------------------------------
class TestItemUpdatesAndAdjustOps:
    @pytest.mark.asyncio
    async def test_apply_item_updates_sets_kind(self, tmp_path):
        import app.db as appdb
        appdb.DB_PATH = tmp_path / "r81item.sqlite"
        await appdb.init_db()
        db = await appdb.get_conn()
        pid, sid = uuid.uuid4().hex, uuid.uuid4().hex
        await db.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
        await db.execute(
            "INSERT INTO schemes(id,project_id,name) VALUES(?,?,?)", (sid, pid, "s"))
        fid = uuid.uuid4().hex
        await db.execute(
            "INSERT INTO global_facts(id,project_id,scheme_id,group_id,"
            "group_title,title,content,category,is_resolved) "
            "VALUES(?,?,?,?,?,?,?,?,1)",
            (fid, pid, sid, "g1", "工期", "建设工期", "- **建设工期**: 400天",
             "schedule"))
        await db.commit()
        try:
            from app.routers.global_facts import _apply_item_updates
            n, _ = await _apply_item_updates(
                db, [{"fact_id": fid, "value_kind": "requirement"}])
            assert n == 1
            cur = await db.execute(
                "SELECT value_kind FROM global_facts WHERE id=?", (fid,))
            row = await cur.fetchone()
            assert row["value_kind"] == "requirement"
            # 非法值归一 ''
            await _apply_item_updates(db, [{"fact_id": fid, "value_kind": "要求"}])
            cur = await db.execute(
                "SELECT value_kind FROM global_facts WHERE id=?", (fid,))
            assert (await cur.fetchone())["value_kind"] == ""
            # 不传该键 → 列不被写（保持原值）
            await _apply_item_updates(db, [{"fact_id": fid, "value_kind": "fact"}])
            await _apply_item_updates(db, [{"fact_id": fid, "name": "总工期"}])
            cur = await db.execute(
                "SELECT value_kind FROM global_facts WHERE id=?", (fid,))
            assert (await cur.fetchone())["value_kind"] == "fact", (
                "未传 value_kind 的更新不得清空已有口径")
        finally:
            await appdb.close_db()

    def test_validate_adjust_ops_passes_kind(self):
        from app.routers.global_facts import _validate_adjust_ops
        fid = "f1"
        obj = {"operations": [
            {"op": "update", "fact_id": fid, "value": "90天",
             "value_kind": "requirement"},
            {"op": "update", "fact_id": fid, "value_kind": "bogus"},
            {"op": "add", "name": "质保期", "value": "2年",
             "value_kind": "requirement"},
            {"op": "add", "name": "响应时间", "value": "2小时"},
        ], "summary": "s"}
        ops, _ = _validate_adjust_ops(obj, {fid})
        by_op = {}
        for o in ops:
            by_op.setdefault(o["op"], []).append(o)
        assert by_op["update"][0]["value_kind"] == "requirement"
        assert by_op["update"][1]["value_kind"] == "", "非法值归一 ''"
        assert by_op["add"][0]["value_kind"] == "requirement", (
            "R80 死接线修复：add 的 value_kind 不得再被白名单剥掉")
        assert "value_kind" in by_op["add"][1] and by_op["add"][1]["value_kind"] == ""
        # 只改 value_kind 的 update 算实质变更（len>2 → 不被判 no_effective_change）
        assert len([o for o in ops if o["op"] == "update"]) == 2

    def test_adjust_apply_update_op_forwards_kind(self):
        """静态锁：adjust 应用路径的 update op 必须把 value_kind 喂给
        _apply_item_updates（R80 只在 add 路径读过，update 从未接）。"""
        src = _src("routers/global_facts.py")
        assert re.search(
            r'"value_kind": op\.get\("value_kind"\),', src), (
            "adjust 的 update op 必须透传 value_kind")


# ---------------------------------------------------------------------------
# E 组 · AI 调整提示词契约
# ---------------------------------------------------------------------------
class TestAdjustPromptContract:
    def test_prompt_declares_value_kind(self):
        from app.services.ai.prompts._registry import get_prompt
        text = get_prompt("global_facts_adjust_system")
        assert "value_kind" in text and "requirement" in text, (
            "AI 调整提示词必须声明 value_kind 操作契约（与提取提示词 :309 对齐）")


# ---------------------------------------------------------------------------
# F 组 · 前后端 parity（前端值域 + 编辑接线）
# ---------------------------------------------------------------------------
_FE_PAGE = os.path.join(_REPO, "frontend", "src", "pages", "SchemeWorkbenchPage.tsx")


class TestFrontendParity:
    def test_options_values_match_backend_whitelist(self):
        from app.services.facts_extractor import VALUE_KIND_ALLOWED
        src = _read(_FE_PAGE)
        m = re.search(
            r"export const FACT_VALUE_KIND_OPTIONS[^\[]*\[(.*?)\];", src, re.S)
        assert m, "前端 FACT_VALUE_KIND_OPTIONS 未找到"
        vals = re.findall(r'value:\s*"([^"]+)"', m.group(1))
        assert sorted(vals) == sorted(VALUE_KIND_ALLOWED), (
            f"前端选项值域必须与后端白名单逐项一致：{vals} vs {VALUE_KIND_ALLOWED}")

    def test_item_modal_wiring(self):
        """静态接线锁（调用形态）：打开回填 / 弹窗控件 / 提交透传 / 徽标。"""
        src = _read(_FE_PAGE)
        assert "value_kind: item.value_kind || \"\"," in src, (
            "openFactItemEdit 必须回填 value_kind")
        assert "value_kind: values.value_kind ?? \"\"," in src, (
            "submitFactItem 的 item_updates 必须透传 value_kind")
        assert 'name="value_kind"' in src, "编辑弹窗必须有 value_kind 表单项"
        assert "factValueKindLabel(it.value_kind)" in src, (
            "徽标文案必须走单一导出函数（与测试同源）")
