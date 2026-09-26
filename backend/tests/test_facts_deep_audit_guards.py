"""事实提取模块 2026-09-19 深度审查回归守卫

覆盖三处：
1. SSE 进度回退：generate_facts 的 _stage 必须过 _monotonic_progress 护栏
   （管线内部 0.03/0.06 与 SSE 层 0.05~0.09 口径交错，旧实现进度条会倒退）
2. 超长表格行丢失：_split_table_rows 遇单行超过 chunk_size 时旧实现把该行
   静默丢弃（每轮 _flush+重建 cur，该行永远无法入段），现强制独立成段
3. 交叉校验器零覆盖缺口：为 run_cross_validations 三组规则补最小契约守卫
"""
import inspect

from app.routers import sse_handlers as sh
from app.services.facts_extractor import _split_table_rows, Chunk, FactItem
from app.services.facts_cross_validators import run_cross_validations


# ---------------------------------------------------------------------------
# 1. 进度不可回退护栏
# ---------------------------------------------------------------------------

class TestFactsProgressMonotonicGuard:

    def test_stage_uses_monotonic_guard(self):
        """源码级护栏：_stage 必须把进度抬到历史最大值后再广播/落库。"""
        src = inspect.getsource(sh.generate_facts)
        assert "_monotonic_progress(_max_pushed, p)" in src, \
            "事实提取 _stage 未接进度不可回退护栏（0.08→0.03 会让进度条倒退）"
        # 护栏值必须同时用于 update_progress 与 SSE 事件（同一口径）
        assert "await update_progress(task_id, p, m)" in src

    def test_guard_behavior_on_regress_sequence(self):
        """行为级护栏：模拟事实链路的真实进度序列，折算结果必须单调不减。"""
        # SSE 层与管线内部交错后的实际发送顺序（修复前 0.03 会直接透传）
        seq = [0.05, 0.08, 0.09, 0.03, 0.06, 0.10, 0.72, 0.96, 1.0]
        guarded = []
        prev = 0.0
        for p in seq:
            prev = sh._monotonic_progress(prev, p)
            guarded.append(prev)
        assert guarded == sorted(guarded), guarded
        # 回退样本被吸收：0.03/0.06 抬到 0.09
        assert guarded[3] == 0.09 and guarded[4] == 0.09


# ---------------------------------------------------------------------------
# 2. 超长表格行不得丢失
# ---------------------------------------------------------------------------

class TestSplitTableRowsOversizedGuard:

    def _make_table(self, long_cell_len: int) -> str:
        head = "| 设备名称 | 规格型号 | 数量 |"
        sep = "|---|---|---|"
        normal = "| 挖掘机 | PC220 | 3 |"
        long_row = f"| 塔式起重机 | {'QTZ80/' * (long_cell_len // 6)} | 2 |"
        return "\n".join([head, sep, normal, long_row, normal])

    def test_oversized_row_is_kept(self):
        """单行超过 chunk_size：必须独立成段，不得从输出中消失。"""
        sec = self._make_table(400)
        long_line = next(ln for ln in sec.split("\n") if len(ln) > 200)
        chunks: list[Chunk] = []
        _split_table_rows(sec, chunk_size=200, chunks=chunks,
                          heading="机械表", zone_type="machinery_stat",
                          priority_weight=1.5)
        joined = "\n".join(c.text for c in chunks)
        assert long_line.strip() in joined, "超长表格行被静默丢弃"
        # 每一段都应复用表头（列名不丢失）
        assert all("| 设备名称 |" in c.text for c in chunks)
        # 语义区元信息不得退化（历史 Bug：切分段被强制 general/0.8）
        assert all(c.zone_type == "machinery_stat" for c in chunks)

    def test_normal_rows_still_split_by_budget(self):
        """常规行仍按预算切分，行数守恒。"""
        rows = [f"| 设备{i} | 型号{i} | {i} |" for i in range(30)]
        sec = "\n".join(["| 设备名称 | 规格型号 | 数量 |", "|---|---|---|"] + rows)
        chunks: list[Chunk] = []
        _split_table_rows(sec, chunk_size=200, chunks=chunks, heading="t")
        joined = "\n".join(c.text for c in chunks)
        for r in rows:
            assert r in joined
        assert len(chunks) >= 2  # 确实发生了切分


# ---------------------------------------------------------------------------
# 3. 交叉校验器（零 LLM 确定性规则）最小契约守卫
# ---------------------------------------------------------------------------

def _conflicted(name, value, others):
    it = FactItem(name=name, value=value, key=name, source="doc")
    it.has_conflict = True
    it.conflict_values = [{"value": v, "source": "doc2", "confidence": 0.9}
                          for v in others]
    return it


class TestCrossValidatorContracts:

    def test_no_conflict_returns_empty_and_never_raises(self):
        items = [FactItem(name="总工期", value="300 日历天", key="d"),
                 # 序列值（list）不得使规则崩溃（历史 Bug：整组规则静默失效）
                 FactItem(name="施工流程", value=["土方开挖", "垫层浇筑"],
                          key="flow")]
        assert run_cross_validations(items) == []

    def test_material_grade_gap_reported(self):
        """C30 vs C40（同为混凝土序列、差 2 档）→ 高危材料冲突。"""
        items = [_conflicted("混凝土强度等级", "C30", ["C40"])]
        out = run_cross_validations(items)
        assert any(c["rule_id"] == "XV-MAT-DESIGN" and c["severity"] == "high"
                   for c in out)

    def test_machinery_entry_after_exit_is_high(self):
        items = [FactItem(name="挖掘机进场时间", value="2026-05-10", key="e"),
                 FactItem(name="挖掘机退场时间", value="2026-04-01", key="x")]
        out = run_cross_validations(items)
        assert any(c["rule_id"] == "XV-MACH-SCHED"
                   and c["conflict_type"] == "machinery_entry_after_exit"
                   and c["severity"] == "high" for c in out)
        # 跨条目冲突回写：两侧事实都被标记，供前端并排裁决
        assert all(it.has_conflict for it in items)

    def test_flow_adjacent_swap_is_high(self):
        """工序相邻换位（安全敏感）→ 无视相似度直接 high。"""
        items = [_conflicted("施工流程", "垫层浇筑→防水施工→钢筋绑扎",
                             ["防水施工→垫层浇筑→钢筋绑扎"])]
        out = run_cross_validations(items)
        assert any(c["rule_id"] == "XV-FLOW-SEQ"
                   and c["conflict_type"] == "flow_adjacent_swap"
                   and c["severity"] == "high" for c in out)

    def test_range_value_compatible_merges(self):
        """范围值兼容（C35 满足“不低于C30”）→ 不报冲突并撤销冲突标记。"""
        it = _conflicted("混凝土强度等级", "不低于C30", ["C35"])
        out = run_cross_validations([it])
        assert not any(c["conflict_type"] == "material_below_range_floor"
                       for c in out)
        assert it.has_conflict is False

    def test_range_revocation_is_identity_scoped_not_name_scoped(self):
        """撤销冲突必须按条目身份，不得按 name 全文匹配误伤同名条目。

        场景：合并池中存在两条同名「混凝土强度等级」（不同 key）——
        甲条目的矛盾属范围值兼容（应撤销）；乙条目 C40 vs C50 是真实
        矛盾（必须保留）。旧实现按 name 撤销会把乙条目的矛盾标记一并清掉。
        """
        a = _conflicted("混凝土强度等级", "不低于C30", ["C35"])
        b = _conflicted("混凝土强度等级", "C40", ["C50"])
        out = run_cross_validations([a, b])
        assert a.has_conflict is False, "范围值兼容条目应撤销矛盾标记"
        assert b.has_conflict is True, "同名条目的真实矛盾不得被误撤销"
        assert any(c["rule_id"] == "XV-MAT-DESIGN" and c["severity"] == "high"
                   for c in out), "C40 vs C50（差 2 档）应照常上报"
