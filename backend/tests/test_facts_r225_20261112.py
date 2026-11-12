# -*- coding: utf-8 -*-
"""全局事实模块深度探索增强（R225 · 2026-11-12）护栏。

本轮 18 项后端修复由本文件按四层锁定（行为实证 / 源码锁 / 字节体检 / 自证）：

  A. 行为实证
     1.  B1  persist 冲突登记判据：值比较不得再用 normalize_key（名称空间函数）
             —— 「450 天 vs 600 天」必须判异值（登记候选 + 置 stale）、
             「GB 50300-2013 vs GB50300-2013」必须判同值（不再假矛盾）。
     2.  B2  最终整理链改值后强制重派生四维（R214 F1 的同族第三漏改路径）。
     3.  B6  _is_protected 对 list-of-strings 形态 source_ref 不再 AttributeError
             （与 routers 侧 _parse_source_ref 同语义对齐）。
     4.  C1  量纲诊断恢复：merge 折叠后的单例组对 conflict_values 补比对
             （12.5m vs 12.5cm 产出 numeric_scale_suspect）。
     5.  C2  范围值兼容撤销收窄为「全部候选兼容」+ 已撤销条目的冲突记录
             同步剔除（[C25,C35] 不再撤销；[C35] 单兼容仍撤销）。
     6.  C4  XV-SAME-NAME 打标同时登记候选值（裁决面板恢复「选此值」）。
     7.  C5  LCS 规模超闸降档 low（不再是「相似度 0% 属真矛盾 high」）。
     8.  C7  autofix 重试轮用最新正文重建提示词。
     9.  A1  唯一性闸门 peer 侧值剥注释对齐（带注释 peer 与剥注释计划值
             同值不再误判矛盾）。

  B. 源码锁（锁调用点，不锁定义）
    10.  B3/B8  _existing_keys_for_plan：scoped 键剥裸键入集 + 同批 delete 键扣除，
                两个校验调用点均改为传其结果。
    11.  B5  AI 自带 key 不再采信（_parse_fact_dict / legacy 同型）。
    12.  A2  adjust 触及共享行 → 项目级缓存失效（_touched_shared）。
    13.  A3  add 分支锁内库内复检（duplicate_fact_key_db_recheck）。
    14.  A4  复制重算 SELECT 补 conflict_keys 列。
    15.  C3  批内 planned_gate_peers 并入唯一性闸门比对。
    16.  C9  update_fact 裸 UPDATE 路径补分类白名单 + 四维重派生。
    17.  C8  裁决/改值/分组重建后清除跨段矛盾诊断记录（fact_name 前置形态）。

  C. 自证
    18.  触碰文件字节体检（CRLF 保持 / BOM=0 / U+FFFD=0）。
"""

import ast
import asyncio
import io
import json
from pathlib import Path

import pytest
from app.services.facts_cross_validators import run_cross_validations
from app.services.facts_enrich import apply_finalize_result
from app.services.facts_extractor import (
    ExtractionResult,
    FactGroup,
    FactItem,
    _norm_value,
    extract_value_from_markdown_line,
    merge_and_deduplicate,
)

_GF = Path(__file__).resolve().parents[1] / "app" / "routers" / "global_facts.py"
_FE = Path(__file__).resolve().parents[1] / "app" / "services" / "facts_extractor.py"
_EN = Path(__file__).resolve().parents[1] / "app" / "services" / "facts_enrich.py"
_CV = Path(__file__).resolve().parents[1] / "app" / "services" / "facts_cross_validators.py"
_CA = Path(__file__).resolve().parents[1] / "app" / "services" / "content_fact_autofix.py"


def _src(p: Path) -> str:
    with io.open(p, encoding="utf-8") as f:
        return f.read()


def _funcs(src: str) -> dict[str, ast.AST]:
    tree = ast.parse(src)
    out: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(node.name, node)
    return out


def _called(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for c in ast.walk(node):
        if isinstance(c, ast.Call):
            t = c.func
            if isinstance(t, ast.Name):
                out.add(t.id)
            elif isinstance(t, ast.Attribute):
                out.add(t.attr)
    return out


# ---------------------------------------------------------------------------
# A. 行为实证
# ---------------------------------------------------------------------------


class TestB1PersistValueCompare:
    """B1：persist 冲突登记的「值变化」判据。"""

    def test_true_value_change_is_detected(self):
        """「工期 450 → 600」必须判异值 —— normalize_key 对值恒同键的吞噬已根除。"""
        assert _norm_value("工期要求 450 日历天") != _norm_value("工期要求 600 日历天")

    def test_notation_variant_is_same_value(self):
        """「GB 50300-2013 vs GB50300-2013」空白记法变体必须判同值（不再假矛盾）。"""
        assert _norm_value("GB 50300-2013") == _norm_value("GB50300-2013")

    def test_production_judgment_uses_norm_value(self):
        """源码锁：persist_extraction 判据用 _norm_value，normalize_key 比值不得回流。"""
        node = _funcs(_src(_FE))["persist_extraction"]
        text = ast.get_source_segment(_src(_FE), node) or ""
        assert "_norm_value(prior_value) != _norm_value(new_value)" in text
        assert "normalize_key(prior_value)" not in text

    def test_item_updates_judgment_family_aligned(self):
        """B1 同族第二份：_apply_item_updates 的 value_changed 同步 _norm_value。"""
        src = _src(_GF)
        # 侧写：old_value 侧必须过 _norm_value（源码为跨行写法，new_value 侧
        # 不构成连续子串，故只锚 old_value 侧 + 归一函数在场）。
        assert "_norm_value(old_value)" in src
        assert "value_changed = old_value.strip() != new_value.strip() and _norm_value(old_value) != _norm_value(" in src
        # 旧判据（normalize_key 比值）不得以任何形态回流
        assert "normalize_key(old_value)" not in src


class TestB2FinalizeRederive:
    """B2：最终整理链改值后重派生四维。"""

    def test_finalize_rederives_dimensions(self):
        it = FactItem(
            name="质量验收",
            value="满足验收规范要求",
            chapter="acceptance",
            fact_attr="qualitative",
        )
        items, changed = apply_finalize_result(
            [it],
            {"facts": [{"fact_key": it.key, "name": it.name, "value": "GB 50300-2021"}]},
        )
        assert changed == 1
        assert items[0].value == "GB 50300-2021"
        # 改写后 fact_attr 与值语义一致（norm 类），不再停在提取期 qualitative
        assert items[0].fact_attr != "qualitative"

    def test_finalize_uses_classification_exit(self):
        """源码锁：整理链与补丁链同用 facts_classification 单一出口。"""
        text = _src(_EN)
        assert "force_rederive_fact_dimensions" in text


class TestB6SourceRefCompat:
    """B6：_is_protected 对 list-of-strings source_ref 不再炸整轮落库。"""

    def test_persist_survives_list_of_strings_source_ref(self):
        async def _run():
            import aiosqlite

            db = await aiosqlite.connect(":memory:")
            db.row_factory = aiosqlite.Row
            try:
                await db.execute(
                    "CREATE TABLE global_facts (id TEXT PRIMARY KEY, scheme_id TEXT DEFAULT '',"
                    " project_id TEXT DEFAULT '', title TEXT DEFAULT '', content TEXT DEFAULT '',"
                    " category TEXT DEFAULT '', fact_key TEXT DEFAULT '', is_simulated INTEGER DEFAULT 0,"
                    " is_resolved INTEGER DEFAULT 0, has_conflict INTEGER DEFAULT 0, is_stale INTEGER DEFAULT 0,"
                    " is_shared INTEGER DEFAULT 0, confidence REAL DEFAULT 0.9, source_ref TEXT DEFAULT '',"
                    " source_kind TEXT DEFAULT '', chapter TEXT DEFAULT '', fact_attr TEXT DEFAULT '',"
                    " value_kind TEXT DEFAULT '', created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '',"
                    " group_id TEXT DEFAULT '', value TEXT DEFAULT '', word_count INTEGER DEFAULT 0,"
                    " conflict_keys TEXT DEFAULT '', source_category TEXT DEFAULT '', page_ref TEXT DEFAULT '',"
                    " fact_type TEXT DEFAULT '', chunk_hash TEXT DEFAULT '')"
                )
                await db.execute(
                    "INSERT INTO global_facts (id, scheme_id, title, content, fact_key, is_resolved, source_ref)"
                    " VALUES ('r1', 's1', '执行标准', '- **执行标准**: GB50300', 'execution_standard',"
                    " 1, '[\"手动录入\"]')"
                )
                await db.commit()
                result = ExtractionResult(groups=[], total_items=0)
                # 修复前：list-of-strings 元素走 (ref or {}).get → AttributeError 炸整轮
                from app.services.facts_extractor import persist_extraction

                await persist_extraction(result, db, "p1", "s1")
            finally:
                await db.close()

        asyncio.run(_run())


class TestC1QuantityDiagnosisRestored:
    """C1：merge 折叠后的单例组量纲诊断恢复。"""

    def test_scale_suspect_on_merged_singleton(self):
        a = FactItem(name="基坑深度", value="12.5m", key="foundation_depth", source="地勘", confidence=0.9)
        b = FactItem(name="基坑深度", value="12.5cm", key="foundation_depth", source="方案", confidence=0.9)
        merged = merge_and_deduplicate([a, b])
        assert len(merged) == 1
        confs = run_cross_validations(merged)
        scale = [c for c in confs if c["rule_id"] == "XV-NUM-UNIT" and c["conflict_type"] == "numeric_scale_suspect"]
        assert scale, "merge 后单例组的量纲诊断不得空转"
        assert scale[0]["side_b"]["value"] == "12.5cm"

    def test_multi_member_group_does_not_duplicate(self):
        """组内 ≥2 真实条目时不展开候选 —— C4 登记的候选不得经 C1 产出重复记录。"""
        items = [
            FactItem(name="开挖深度", value="12.5m", key="excavation_depth", source="d1", confidence=0.9),
            FactItem(name="开挖深度", value="1250cm", key="excavation_depth", source="d2", confidence=0.9),
        ]
        confs = run_cross_validations(items)
        num = [c for c in confs if c["rule_id"] == "XV-NUM-UNIT"]
        assert len(num) == 1, f"同一物理对只产出一份诊断（实测 {len(num)}）"


class TestC2RangeRevertNarrowed:
    """C2：范围值兼容撤销收窄 + 冲突记录同步剔除。"""

    def _merged(self, values):
        items = [
            FactItem(name="混凝土强度等级", value=v, key="hnt", source=f"s{i}", confidence=0.9)
            for i, v in enumerate(values)
        ]
        return merge_and_deduplicate(items)

    def test_below_floor_candidate_blocks_revert(self):
        """[C25(低于下限), C35(兼容)]：不再整体撤销 —— 记录与行标记一致。"""
        merged = self._merged(["C30及以上", "C25", "C35"])
        confs = run_cross_validations(merged)
        below = [c for c in confs if c["conflict_type"] == "material_below_range_floor"]
        assert below, "低于下限的候选必须保留 high 诊断"
        assert merged[0].has_conflict, "存在真矛盾候选时行标记不得被撤销"

    def test_single_compatible_candidate_still_reverts(self):
        """[C35] 单一兼容候选：撤销语义保留（回归面）。"""
        merged = self._merged(["C30及以上", "C35"])
        confs = run_cross_validations(merged)
        assert not confs
        assert not merged[0].has_conflict
        assert merged[0].conflict_values == []


class TestC4SameNameCandidates:
    """C4：XV-SAME-NAME 打标同时登记候选值。"""

    def test_marks_register_candidates(self):
        c1 = FactItem(name="备注参数", value="方案A", key="fact_abc111", source="s1", confidence=0.9)
        c2 = FactItem(name="备注参数", value="方案B", key="fact_abc111", source="s2", confidence=0.9)
        run_cross_validations([c1, c2])
        assert c1.has_conflict and c2.has_conflict
        vals1 = {c.get("value") for c in c1.conflict_values}
        assert "方案B" in vals1, "打标条目必须登记组内其他 distinct 值为候选"
        assert c1.conflict_values[0]["is_simulated"] is False


class TestC5LcsCapDowngrade:
    """C5：LCS 规模超闸降档 low。"""

    def test_over_cap_is_low_not_high(self):
        fa = " → ".join(f"步骤{i}" for i in range(600))
        fb = " → ".join(f"步骤{i}" for i in range(600)).replace("步骤300", "第300步")
        f1 = FactItem(name="总体流程", value=fa, key="flow_x", source="s1", confidence=0.9)
        f2 = FactItem(name="总体流程", value=fb, key="flow_x", source="s2", confidence=0.9)
        merged = merge_and_deduplicate([f1, f2])
        confs = run_cross_validations(merged)
        div = [c for c in confs if c["conflict_type"] == "flow_sequence_divergence"]
        assert div, "超闸仍需产出需人工复核的记录（不得静默）"
        assert all(c["severity"] == "low" for c in div), "超闸是性能保护不是真矛盾判据"
        assert "未做全量比对" in div[0]["resolution_hint"]


class TestC7AutofixRetryPrompt:
    """C7：autofix 重试轮用最新正文重建提示词。"""

    def test_round2_sees_updated_body(self):
        from app.services.content_fact_autofix import autofix_section_fact_conflicts

        captured: list[list[dict]] = []
        call_count = {"n": 0}

        async def fake_chat(messages, **kwargs):
            # chat_fn 契约：返回**原始模型文本**（collect_json_response_with_chat
            # 内部负责解析/修复轮），不是 (obj, raw) 元组。
            captured.append(json.loads(json.dumps(messages, ensure_ascii=False, default=str)))
            call_count["n"] += 1
            if call_count["n"] == 1:
                # 第 1 轮：一个可应用的 edit + 一个必然 not_found 的 edit
                # （部分成功/部分拒绝 → 触发第 2 轮重试）
                return json.dumps(
                    {
                        "edits": [
                            {"old_text": "基坑开挖工期为90天", "new_text": "基坑开挖工期为120天"},
                            {"old_text": "不存在的文本xyz", "new_text": "y"},
                        ]
                    },
                    ensure_ascii=False,
                )
            # 第 2 轮：AI 继续返回一个必然 not_found 的 edit（模拟未收敛）
            return json.dumps({"edits": [{"old_text": "不存在的文本xyz", "new_text": "y"}]}, ensure_ascii=False)

        conflicts = [{"rule_id": "XV-TEST", "side_a": {}, "side_b": {}}]

        async def _run():
            new_body, _res = await autofix_section_fact_conflicts(
                content="基坑开挖工期为90天。",
                standard="precise",
                fact_rows=[{"title": "基坑深度", "content": "- **基坑深度**: 12.5m"}],
                conflicts=conflicts,
                section_id="sec1",
                section_title="施工工期",
                chat_fn=fake_chat,
            )
            return new_body

        asyncio.run(_run())
        assert len(captured) >= 2, "至少两轮调用"
        round2_text = json.dumps(captured[1], ensure_ascii=False)
        # 第 2 轮的 user 消息必须包含**改写后**正文，不得再含修复前旧文
        assert "基坑开挖工期为120天" in round2_text, "第 2 轮提示词必须用最新正文"
        assert "基坑开挖工期为90天" not in round2_text, "第 2 轮不得再内嵌修复前旧文"


class TestA1GatePeerNoteStripping:
    """A1：唯一性闸门 peer 侧值剥注释对齐。"""

    def test_annotated_peer_same_value_not_conflict(self):
        from app.routers.global_facts import _collect_duplicate_injectable_conflicts

        peers = [
            {
                "id": "p1",
                "title": "基坑深度",
                # 带行内注释的历史行
                "content": "- **基坑深度**: 12.5 *(设计值)*",
                "scheme_id": "s1",
            }
        ]
        # 计划值是解析剥注释后的「12.5」—— 同值，不得误判矛盾
        conflicts = _collect_duplicate_injectable_conflicts(
            peers, [("new1", "基坑深度", "12.5")], exclude_ids=set()
        )
        assert conflicts == {}, f"同值（注释差异）不得判矛盾：{conflicts}"

    def test_truly_different_value_still_conflict(self):
        from app.routers.global_facts import _collect_duplicate_injectable_conflicts

        peers = [
            {"id": "p1", "title": "基坑深度", "content": "- **基坑深度**: 12.5 *(设计值)*", "scheme_id": "s1"}
        ]
        conflicts = _collect_duplicate_injectable_conflicts(
            peers, [("new1", "基坑深度", "14.0")], exclude_ids=set()
        )
        assert "new1" in conflicts

    def test_group_rebuild_compare_is_semantic(self):
        """分组重建内容对比的语义等价 round-trip：格式差异不得清矛盾。"""
        from app.routers.global_facts import _build_fact_content, _split_fact_lines

        old_content = "- **基坑深度**: 12.5 *(设计值)*"
        parsed = _split_fact_lines("分组", old_content)
        nm, val, is_sim = parsed[0]
        rebuilt = _build_fact_content(nm, val, is_sim)
        # 逐字不等（注释被剥）—— 修复后必须靠语义判据保留矛盾
        assert rebuilt.strip() != old_content.strip()
        old_plain = extract_value_from_markdown_line("x: 12.5 *(设计值)*")[1]
        assert _norm_value(old_plain) == _norm_value(val)


# ---------------------------------------------------------------------------
# B. 源码锁
# ---------------------------------------------------------------------------


class TestSourceLocks:
    def test_b3_b8_existing_keys_for_plan(self):
        """B3/B8：scoped 键剥裸键 + 同批 delete 扣除，两个调用点都改传其结果。"""
        src = _src(_GF)
        assert "def _existing_keys_for_plan" in src
        assert 'k.split("::", 1)[0].strip()' in src
        assert "_validate_adjust_ops(\n            {\"operations\": confirmed_ops}, valid_ids, existing_keys=_existing_keys_for_plan(confirmed_ops)" in src
        assert "existing_keys=_existing_keys_for_plan(_ai_plan_ops)" in src

    def test_b5_model_key_not_trusted(self):
        """B5：两处解析器不采信模型自带 key。"""
        src = _src(_FE)
        node = _funcs(src)["_parse_fact_dict"]
        text = ast.get_source_segment(src, node) or ""
        assert 'normalize_key(name)' in text
        assert 'f.get("key")' not in text
        # legacy 同型（非空 name/value 校验后的 key 派生）
        assert src.count('key = normalize_key(name)') >= 1 or src.count("key = normalize_key(name)") >= 1

    def test_a2_touched_shared_cache_invalidation(self):
        """A2：adjust 触及共享行 → 项目级缓存失效。"""
        src = _src(_GF)
        assert "_touched_shared" in src
        assert 'await _invalidate_fact_scope_cache(db, "", str(real_pid))' in src

    def test_a3_add_db_recheck(self):
        """A3：add 分支锁内库内复检 + 留痕。"""
        src = _src(_GF)
        assert "duplicate_fact_key_db_recheck" in src
        assert "fact_key=? OR fact_key LIKE ?" in src

    def test_a4_conflict_keys_selected(self):
        """A4：复制重算 SELECT 补 conflict_keys。"""
        src = _src(_GF)
        assert "has_conflict, is_stale, is_shared, confidence, source_ref, conflict_keys " in src

    def test_c3_planned_gate_peers(self):
        """C3：批内 planned peers 声明、记录、并入比对三件套。"""
        src = _src(_GF)
        assert "planned_gate_peers: dict[tuple[str, str], list[dict]] = {}" in src
        assert 'planned_gate_peers.setdefault(_gate_key_now, []).append' in src
        assert "peers=_gate_peers + planned_gate_peers.get(_gate_key, [])" in src

    def test_c9_bare_update_whitelist_and_rederive(self):
        """C9：裸 UPDATE 路径分类白名单 + 四维重派生。"""
        src = _src(_GF)
        node = _funcs(src)["update_fact"]
        text = ast.get_source_segment(src, node) or ""
        assert 'fields["category"] = _normalize_fact_category' in text
        assert "_rederive_dimension_columns(" in text

    def test_c8_cross_conflicts_cleanup_fact_name_first(self):
        """C8：三处诊断清理，fact_name 前置（不撞 r54 作用域谓词字面量）。"""
        src = _src(_GF)
        assert src.count("DELETE FROM facts_cross_conflicts WHERE fact_name=? AND project_id=? AND scheme_id=?") == 2
        assert "DELETE FROM facts_cross_conflicts WHERE fact_name IN ({_ph}) AND project_id=? AND scheme_id=?" in src
        # r54 棘轮同口径：只查 SQL 语句形态的字符串常量（docstring 对历史
        # 缺陷的引述不参与 —— _fact_scope_where 的注释里就有该字面量）。
        tree = ast.parse(src)
        sql_consts = [
            n.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and n.value.lstrip().upper().startswith(("DELETE", "UPDATE", "SELECT", "INSERT"))
        ]
        offenders = [s for s in sql_consts if "WHERE project_id=? AND scheme_id=?" in s]
        assert not offenders, f"r54 作用域谓词字面量不得以 SQL 常量形态回流：{offenders}"


# ---------------------------------------------------------------------------
# C. 自证
# ---------------------------------------------------------------------------


class TestByteIntegrity:
    TOUCHED = [_GF, _FE, _EN, _CV, _CA]

    def test_line_endings_and_bytes(self):
        for p in self.TOUCHED:
            raw = p.read_bytes()
            assert b"\r\n" in raw, f"{p.name} 应保持 CRLF"
            lf_only = raw.replace(b"\r\n", b"")
            assert b"\n" not in lf_only, f"{p.name} 混入裸 LF"
            assert not raw.startswith(b"\xef\xbb\xbf"), f"{p.name} BOM=0"
            assert b"\xef\xbf\xbd" not in raw, f"{p.name} U+FFFD=0"


def test_touched_files_exist():
    """锚点真实存在（防测试自身假绿）。"""
    for p in [_GF, _FE, _EN, _CV, _CA]:
        assert p.exists(), f"{p} 不存在"
