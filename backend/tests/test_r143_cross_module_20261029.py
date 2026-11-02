"""跨模块深度收口 R143（2026-10-29）护栏。

锁定本轮修复的不变量（全部为「有代码依据或测试结果支撑」项）：

1. **D1（P1 数据丢失）**：批量自动修复的链式累积必须用全文（``after_full`` /
   ``_pending``），预览键 ``before/after`` 保持 ``[:4000]`` 展示契约；确认落库
   的 ``_merged_after_if_prefix`` 优先取 ``after_full``。
2. **D2（P1 假成功）**：``repair_agent`` 的正文/冲突状态写路径必须走
   ``review_db.exec_write``（R13 单一出口），禁止裸 ``db.execute`` + commit。
3. **D3（P2 静默陈旧）**：``review.py`` 三个 review_status 写端点必须在提交后
   调用 ``invalidate_overview_cache``（否则 DLV-09 结论最长 120s 陈旧）。
4. **BUG-3**：``export_audit._EXPORTER_VERSION`` 必须引用权威源
   （``export._EXPORTER_VERSION``），不得再持有陈旧字面量。
"""
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"


def _src(rel: str) -> str:
    return (APP / rel).read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# D1 · 批量自动修复全文透传
# ---------------------------------------------------------------------------
def test_merged_after_prefers_after_full():
    """``_merged_after_if_prefix`` 必须优先取 ``after_full``（全文），
    而非 ``after``（[:4000] 预览）。旧实现只读 ``after`` → 确认落库截断。"""
    from app.routers.review_autofix import _merged_after_if_prefix

    long_full = "很长的正文 " * 2000  # > 4000 字
    items = [
        {"rule_id": "R1", "chain_index": 0,
         "after": long_full[:4000], "after_full": long_full},
    ]
    merged = _merged_after_if_prefix(items, {"R1"})
    assert merged == long_full
    assert len(merged) > 4000


def test_chain_section_fixes_carries_after_full():
    """``_chain_section_fixes`` 产物必须带 ``after_full``/``before_full``，
    且链式累积用 ``sec_pending[0][2]``（全文）而非 ``rec["after"]``（截断）。"""
    src = _src("services/review_autofix.py")
    assert "sec_pending[0][2] if sec_pending else content" in src
    assert '"after_full": after' in src
    assert '"before_full": content' in src
    # 预览键仍保留展示契约（[:4000]），防「修好全文却把预览放大」的连带变化
    assert '"after": after[:4000]' in src
    assert '"before": content[:4000]' in src


# ---------------------------------------------------------------------------
# D2 · repair_agent R13 写路径收口
# ---------------------------------------------------------------------------
def test_repair_agent_uses_review_db_exec_write():
    """``repair_agent`` 的正文与冲突状态 UPDATE 必须走 ``review_db.exec_write``，
    不得裸 ``db.execute``（R13 假成功）。"""
    src = _src("services/repair_agent.py")
    # 导入经 ruff 合并为单行（consistency_edits, repair_record, review_db）
    assert "from app.services import consistency_edits, repair_record, review_db" in src
    # 两条关键写路径必须经 exec_write
    assert src.count("review_db.exec_write(") >= 3  # 正文 1 + repaired 1 + failed 1
    # 禁止裸 UPDATE sections / consistency_conflicts（防回退到假成功）
    assert "await db.execute(\n                        \"UPDATE sections" not in src
    assert "await db.execute(\n            \"UPDATE consistency_conflicts" not in src


# ---------------------------------------------------------------------------
# D3 · review_status 变更失效总检缓存
# ---------------------------------------------------------------------------
def test_review_status_write_invalidates_overview_cache():
    """``review.py`` 的批量 / 单章 / submit 三个状态写端点必须在 commit 后调用
    ``invalidate_overview_cache``（否则 DLV-09 结论最长 120s 陈旧）。"""
    src = _src("routers/review.py")
    assert src.count("invalidate_overview_cache(scheme_id)") >= 3
    assert "from app.routers.compliance import invalidate_overview_cache" in src


# ---------------------------------------------------------------------------
# BUG-3 · _EXPORTER_VERSION 单一事实源
# ---------------------------------------------------------------------------
def test_exporter_version_single_source_of_truth():
    """``export_audit._EXPORTER_VERSION`` 必须与 ``export._EXPORTER_VERSION``
    一致（经引用，不得持有陈旧字面量 "22"）。"""
    from app.routers import export as _ex
    from app.routers import export_audit as _ea

    assert _ea._EXPORTER_VERSION == _ex._EXPORTER_VERSION
    src = _src("routers/export_audit.py")
    assert "_EXPORTER_VERSION = _ex._EXPORTER_VERSION" in src
    assert "_EXPORTER_VERSION = \"22\"" not in src
